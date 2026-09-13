/* A workflow is a reading scope containing many independent data identities. */
(function(root) {
  'use strict';
  const B=typeof module==='object'&&module.exports?require('./canvas-model.js'):root.Blueprint;
  const index=rows=>new Map(rows.map(row=>[row.id,row]));

  function choices(graph){
    const roots=B.scopeChoices(graph),counts=new Map(),seen=new Map();
    for(const c of roots)counts.set(c.entity.name,(counts.get(c.entity.name)||0)+1);
    return roots.map(c=>{const name=c.entity.name,number=(seen.get(name)||0)+1;seen.set(name,number);
      return {...c,label:counts.get(name)>1?`${name} · 场景 ${number}`:name};
    });
  }

  // A task owns a root context and all its calls. Data identity and derivation
  // stay in the evidence graph; neither a shared value nor a shared function
  // makes two independent tasks share their presentation color.
  function tasks(graph){
    const list=choices(graph).map(c=>({...c,purpose:c.entity.summary?.trim()||'用途待核对'}));
    const byId=index(list),contexts=index(graph.contexts),byContext=new Map();
    for(const context of graph.contexts){
      const path=[],seen=new Set();let current=context,id;
      while(current&&!seen.has(current.id)){
        if(byContext.has(current.id)){id=byContext.get(current.id);break;}
        seen.add(current.id);path.push(current.id);
        if(!current.parent_id){id=byId.has(current.id)?current.id:undefined;break;}
        current=contexts.get(current.parent_id);
      }
      for(const key of path)byContext.set(key,id);
    }
    return {list,byId,byContext};
  }

  function colors(taskList,saved={}){
    const result=new Map(Object.entries(saved||{}).filter(([,slot])=>Number.isSafeInteger(slot)&&slot>=0));
    const used=new Set(result.values());let slot=0;
    for(const task of taskList)if(!result.has(task.id)){
      while(used.has(slot))slot++;
      result.set(task.id,slot);used.add(slot);
    }
    return Object.fromEntries(result);
  }

  function decorate(scene,taskIndex){
    const distinct=items=>[...new Set(items.filter(Boolean))];
    const forRecords=records=>distinct(records.map(r=>taskIndex.byContext.get(r.context_id)));
    const edges=scene.edges.map(e=>({...e,taskIds:e.association?
      distinct([taskIndex.byContext.get(scene.scopeId)]):forRecords(e.records)}));
    const nodeTasks=new Map(),portTasks=new Map();
    for(const edge of edges)for(const side of ['from','to']){
      const key=edge[side],port=edge[side+'Port'];
      nodeTasks.set(key,distinct([...(nodeTasks.get(key)||[]),...edge.taskIds]));
      if(port)portTasks.set(port.id,distinct([...(portTasks.get(port.id)||[]),...edge.taskIds]));
    }
    const nodes=scene.nodes.map(n=>({...n,internalTaskFlows:(n.internalRelations||[]).map(r=>({taskId:taskIndex.byContext.get(r.context_id),flowId:r.flow_id})),taskIds:distinct([
      taskIndex.byContext.get(n.context),...(nodeTasks.get(n.key)||[]),...forRecords(n.internalRelations||[])
    ]),...(n.ports?{ports:n.ports.map(p=>({...p,taskIds:portTasks.get(p.id)||distinct([taskIndex.byContext.get(p.context_id)])}))}:{})}));
    return {...scene,nodes,edges,inputs:scene.inputs?.map(n=>nodes.find(p=>p.key===n.key)).filter(Boolean),entry:nodes.find(n=>n.key===scene.entry?.key)};
  }

  function focus(scene,taskIds=[],flowIds=[]){
    const tasks=new Set(taskIds),flows=new Set(flowIds),nodes=new Set(),edges=new Set();
    const includes=ids=>!tasks.size||ids.some(id=>tasks.has(id));
    for(const edge of scene.edges)if(!edge.association&&includes(edge.taskIds)&&(!flows.size||flows.has(edge.record.flow_id))){
      edges.add(edge.id);nodes.add(edge.from);nodes.add(edge.to);
    }
    for(const node of scene.nodes)if(includes(node.taskIds)&&(!flows.size||node.role==='workflow-entry'||
      node.internalTaskFlows?.some(r=>(!tasks.size||tasks.has(r.taskId))&&flows.has(r.flowId))))nodes.add(node.key);
    for(const edge of scene.edges)if(edge.association&&includes(edge.taskIds)&&nodes.has(edge.to)){
      edges.add(edge.id);nodes.add(edge.from);
    }
    return {nodes,edges,active:!!(tasks.size||flows.size)};
  }

  function trace(graph, seeds) {
    const flows=index(graph.flows), children=new Map();
    for(const f of graph.flows)for(const parent of f.derived_from){
      if(!children.has(parent))children.set(parent,[]);children.get(parent).push(f.id);
    }
    const found=new Set(seeds.filter(id=>flows.has(id))),queue=[...found];
    for(let i=0;i<queue.length;i++)for(const id of children.get(queue[i])||[]){
      if(!found.has(id)){found.add(id);queue.push(id);}
    }
    return found;
  }

  function project(graph, scopeId) {
    const base=B.scopedData(graph,scopeId),contexts=index(graph.contexts),entities=index(graph.entities),flows=index(graph.flows);
    const owner=entities.get(B.contextOwner(graph,scopeId)),scope=contexts.get(scopeId);
    if(!owner||!scope)return {nodes:[],edges:[],inputs:[],scopeId,entry:null};
    const originals=new Map(base.nodes.map(n=>[n.key,n])),inputs=new Map();
    const supplies=new Set(base.nodes.filter(n=>['source','input'].includes(n.role)).map(n=>n.key));
    for(const n of base.nodes.filter(n=>n.role==='entity'&&['external','field','parameter','variable','value'].includes(n.entity.kind))){
      const outgoing=base.edges.filter(e=>e.from===n.key);
      if(outgoing.length&&!base.edges.some(e=>e.to===n.key)&&outgoing.every(e=>!flows.get(e.record.flow_id).derived_from.length))supplies.add(n.key);
    }
    const edges=base.edges.map(e=>({...e}));
    for(const edge of edges){
      if(!supplies.has(edge.from))continue;
      const original=originals.get(edge.from),p=edge.fromPort,f=flows.get(edge.record.flow_id);
      // Repeated uses of the same port/value share one input. Distinct ports or
      // values never collapse just because they have the same displayed name.
      const sourceId=p.source_port_id||p.id,key=`workflow-input:${JSON.stringify([scopeId,sourceId,f.id])}`;
      if(!inputs.has(key)){
        const port={...p,id:key+':out',source_port_id:sourceId};
        inputs.set(key,{key,entity:original.entity,context:scopeId,role:'workflow-input',
          title:f.name,summary:original.role==='input'?'调用方传入':original.role==='entity'?'已记录的数据来源':'本层提供',flowId:f.id,ports:[port],
          inputPortId:sourceId,origin:original.role==='input'?'boundary':original.role==='entity'?'source':'local',records:[]});
      }
      const input=inputs.get(key);input.records.push(...edge.records);
      edge.from=key;edge.fromPort=input.ports[0];
    }
    const nodes=base.nodes.filter(n=>!supplies.has(n.key));
    const entry={key:`workflow-entry:${scopeId}`,entity:owner,context:scopeId,role:'workflow-entry',
      title:scope.parent_id?owner.name+' · 子流程':(choices(graph).find(c=>c.id===scopeId)?.label||owner.name)+' · 流程入口',
      summary:`${inputs.size} 个输入 · 点击查看整条流程`,ports:[],inputKeys:[...inputs.keys()]};
    const associations=[...inputs.values()];
    // Calls with no recorded data still belong to the scope, without implying
    // their execution order or fabricating arguments.
    if(!associations.length)associations.push(...nodes.filter(n=>n.expandContext));
    for(const target of associations)edges.push({id:`workflow-membership:${scopeId}:${target.key}`,
      from:entry.key,to:target.key,association:true,data:false,record:{kind:'association'},records:[],
      memberName:target.title||target.entity.name});
    return {nodes:[entry,...inputs.values(),...nodes],edges,inputs:[...inputs.values()],scopeId,entry};
  }

  function arrange(scene, dimensions, existing={}, reset=false) {
    const positions=B.arrange(scene.nodes,scene.edges,dimensions,existing,reset),entry=scene.entry;
    if(entry&&positions[entry.key]&&(!existing[entry.key]||reset&&!existing[entry.key].locked)){
      const inputs=scene.inputs.filter(n=>positions[n.key]);
      if(inputs.length){
        const top=Math.min(...inputs.map(n=>positions[n.key].y));
        const bottom=Math.max(...inputs.map(n=>positions[n.key].y+(dimensions[n.key]?.height||100)));
        positions[entry.key].y=(top+bottom-(dimensions[entry.key]?.height||100))/2;
      }
    }
    return positions;
  }
  const api={project,trace,arrange,choices,tasks,colors,decorate,focus};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.Workflow=api;
})(typeof globalThis==='object'?globalThis:this);
