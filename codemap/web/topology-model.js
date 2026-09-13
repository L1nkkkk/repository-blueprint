/* Semantic topology and folding. No coordinates or writes to the source graph. */
(function(root) {
  'use strict';
  const dataKinds = new Set(['data','read','write']);
  const index = rows => new Map(rows.map(row => [row.id,row]));
  const distinct = items => [...new Set(items)];

  function components(keys, edges) {
    const next = new Map(keys.map(k=>[k,[]])), back = new Map(keys.map(k=>[k,[]]));
    for(const e of edges)if(next.has(e.from)&&next.has(e.to)){next.get(e.from).push(e.to);back.get(e.to).push(e.from);}
    const seen = new Set(), order = [];
    // Iterative traversal also works for long repository dependency chains.
    for(const key of keys)if(!seen.has(key)){
      seen.add(key);const stack=[[key,0]];
      while(stack.length){const frame=stack.at(-1), targets=next.get(frame[0]);
        if(frame[1]<targets.length){const to=targets[frame[1]++];if(!seen.has(to)){seen.add(to);stack.push([to,0]);}}
        else{order.push(frame[0]);stack.pop();}
      }
    }
    const groups=[], membership=new Map();
    for(const key of order.reverse())if(!membership.has(key)){
      const id=groups.length,group=[],stack=[key];membership.set(key,id);
      while(stack.length){const k=stack.pop();group.push(k);for(const p of back.get(k))if(!membership.has(p)){membership.set(p,id);stack.push(p);}}
      groups.push(group);
    }
    const self = new Set(edges.filter(e=>e.from===e.to).map(e=>e.from));
    return {groups,membership,cycles:groups.filter(g=>g.length>1||self.has(g[0]))};
  }

  function relationType(graph, relation, ports=index(graph.ports), contexts=index(graph.contexts)) {
    if(!dataKinds.has(relation.kind))return relation.kind;
    const a=ports.get(relation.from_id),b=ports.get(relation.to_id);
    if(!a||!b)return 'data';
    if(contexts.get(a.context_id)?.parent_id===b.context_id)return relation.kind==='write'?'write_back':'return';
    if(contexts.get(b.context_id)?.parent_id===a.context_id)return 'argument';
    return relation.kind==='write'?'state_write':'data';
  }

  function analyze(graph) {
    const current=graph.relations.filter(r=>r.basis==='source'&&r.freshness==='current');
    const calls=current.filter(r=>r.kind==='call').map(r=>({from:r.from_id,to:r.to_id,record:r}));
    // A static call cycle is possible recursion, not an observed runtime loop.
    const controls=current.filter(r=>r.kind==='control').map(r=>({from:JSON.stringify([r.from_id,r.context_id]),to:JSON.stringify([r.to_id,r.context_id]),record:r}));
    const cycles=edges=>components(distinct(edges.flatMap(e=>[e.from,e.to])),edges).cycles.map(nodes=>{
      const members=new Set(nodes);return {nodes,relationIds:edges.filter(e=>members.has(e.from)&&members.has(e.to)).map(e=>e.record.id)};
    });
    const ports=index(graph.ports),contexts=index(graph.contexts),count={};
    for(const r of current){const type=relationType(graph,r,ports,contexts);count[type]=(count[type]||0)+1;}
    return {callCycles:cycles(calls),controlCycles:cycles(controls),controlEdges:controls.length,
            counts:count,uncertain:graph.relations.length-current.length};
  }

  function shape(nodes, edges) {
    const incoming=new Map(nodes.map(n=>[n.key,new Set()])),outgoing=new Map(nodes.map(n=>[n.key,new Set()]));
    for(const e of edges)if(e.from!==e.to&&outgoing.has(e.from)&&incoming.has(e.to)){
      outgoing.get(e.from).add(e.to);incoming.get(e.to).add(e.from);
    }
    const selected=predicate=>nodes.filter(n=>predicate(incoming.get(n.key),outgoing.get(n.key))).map(n=>n.key);
    return {sources:selected((i,o)=>!i.size&&o.size),sinks:selected((i,o)=>i.size&&!o.size),
      forks:selected((i,o)=>o.size>1),joins:selected((i,o)=>i.size>1),isolated:selected((i,o)=>!i.size&&!o.size),
      cycles:components(nodes.map(n=>n.key),edges).cycles};
  }

  function project(graph, {grouping='directory',layer='call',expanded=[]}={}) {
    const entities=index(graph.entities),ports=index(graph.ports),contexts=index(graph.contexts),open=new Set(expanded);
    const axes=new Map(),children=new Map();
    for(const m of graph.memberships){
      if(!axes.has(m.axis))axes.set(m.axis,new Map());const parents=axes.get(m.axis);
      if(!parents.has(m.child_id))parents.set(m.child_id,[]);parents.get(m.child_id).push(m.parent_id);
      if(!children.has(m.parent_id))children.set(m.parent_id,new Set());children.get(m.parent_id).add(m.child_id);
    }
    const nearest=(id,axis,kinds)=>{
      let frontier=[id];const seen=new Set();
      while(frontier.length){const found=frontier.filter(k=>kinds.has(entities.get(k)?.kind));if(found.length)return distinct(found);
        const next=[];for(const k of frontier)if(!seen.has(k)){seen.add(k);next.push(...(axes.get(axis)?.get(k)||[]));}frontier=distinct(next).filter(k=>!seen.has(k));
      }return [];
    };
    const filesBySource=new Map();
    for(const e of graph.entities.filter(e=>e.kind==='file'))for(const s of e.source_ids){if(!filesBySource.has(s))filesBySource.set(s,[]);filesBySource.get(s).push(e.id);}
    const sourceFiles=e=>distinct([...nearest(e.id,'physical',new Set(['file'])),...e.source_ids.flatMap(s=>filesBySource.get(s)||[])]);
    const topDirectory=id=>{
      const dirs=nearest(id,'physical',new Set(['directory']));if(!dirs.length)return [id];
      const result=[],pending=[...dirs],seen=new Set();
      while(pending.length){const d=pending.pop();if(seen.has(d))continue;seen.add(d);
        const parents=(axes.get('physical')?.get(d)||[]).filter(k=>entities.get(k)?.kind==='directory');
        if(parents.length)pending.push(...parents);else result.push(d);
      }return distinct(result);
    };
    const mappings=new Map(),ambiguous=new Set();
    for(const e of graph.entities){
      let candidates=[];
      if(grouping==='file')candidates=sourceFiles(e);
      else if(grouping==='directory')candidates=e.kind==='directory'?topDirectory(e.id):distinct(sourceFiles(e).flatMap(topDirectory));
      else if(grouping==='semantic')candidates=nearest(e.id,'semantic',new Set(['namespace','package','class','struct','interface','enum']));
      else if(grouping==='architecture')candidates=nearest(e.id,'architecture',new Set(['module']));
      if(candidates.length>1)ambiguous.add(e.id);
      let representative=candidates.length===1?candidates[0]:e.id;
      if(open.has(representative)){
        const files=sourceFiles(e);
        representative=grouping==='directory'&&files.length===1?files[0]:e.id;
        if(open.has(representative))representative=e.id;
      }
      mappings.set(e.id,representative);
    }
    const nodes=new Map(),edges=new Map(),membersByGroup=new Map();
    for(const [id,group] of mappings){if(!membersByGroup.has(group))membersByGroup.set(group,[]);membersByGroup.get(group).push(id);}
    const add=id=>{
      const representative=mappings.get(id)||id,key=`topology:${representative}`;
      if(!nodes.has(key))nodes.set(key,{key,entity:entities.get(representative),context:null,ports:[],
        members:membersByGroup.get(representative)||[id],internalRelations:[],role:'group'});
      return nodes.get(key);
    };
    // Retain isolated/unread leaves too; entry reachability never defines scope.
    for(const e of graph.entities)if(e.kind!=='repository'&&!children.get(e.id)?.size)add(e.id);
    const include=r=>layer==='data'?dataKinds.has(r.kind):layer==='call'?r.kind==='call':layer==='control'?r.kind==='control':['inherit','implements','reference','event'].includes(r.kind);
    for(const r of graph.relations.filter(include)){
      const data=dataKinds.has(r.kind),a=data?ports.get(r.from_id):null,b=data?ports.get(r.to_id):null;
      const from=add(data?a.entity_id:r.from_id),to=add(data?b.entity_id:r.to_id);
      if(from.key===to.key&&from.members.length>1){from.internalRelations.push(r);continue;}
      // Do not merge distinct values, invocations, directions or relation kinds.
      const type=relationType(graph,r,ports,contexts);
      const key=JSON.stringify([from.key,to.key,r.kind,r.flow_id||'',r.context_id,type]);
      if(!edges.has(key)){
        const port=(node,p,direction)=>{
          const id=`${node.key}:${direction}:${r.flow_id}:${r.context_id}:${type}`;
          if(!node.ports.some(p=>p.id===id))node.ports.push({...p,id,direction,flow_id:r.flow_id,
            annotation:({return:'返回',write_back:'写回'})[type]||''});
          return node.ports.find(p=>p.id===id);
        };
        edges.set(key,{id:`fold:${key}`,from:from.key,to:to.key,record:r,records:[],data,aggregated:true,
          annotation:({return:'返回',write_back:'写回',argument:'传参',control:'控制'})[type]||'',
          fromPort:data?port(from,a,'out'):null,toPort:data?port(to,b,'in'):null});
      }
      edges.get(key).records.push(r);
    }
    const shown=[...nodes.values()];
    for(const n of shown){n.ambiguous=n.members.some(id=>ambiguous.has(id));n.canExpand=n.members.some(id=>id!==n.entity.id);}
    const links=[...edges.values()];
    return {nodes:shown,edges:links,shape:shape(shown,links),represented:distinct([...links.flatMap(e=>e.records.map(r=>r.id)),...shown.flatMap(n=>n.internalRelations.map(r=>r.id))]).length};
  }
  const api={components,relationType,analyze,shape,project};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.Topology=api;
})(typeof globalThis==='object'?globalThis:this);
