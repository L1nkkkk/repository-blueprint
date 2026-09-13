const test=require('node:test');
const assert=require('node:assert/strict');
const W=require('../codemap/web/workflow-model.js');
const B=require('../codemap/web/canvas-model.js');
const sample=require('../examples/reviewed-map-v1.json');
const ids=scene=>scene.edges.flatMap(e=>e.records.map(r=>r.id)).sort();

test('one workflow entry organizes all five independent inputs without adding source facts',()=>{
  const before=JSON.stringify(sample),scene=W.project(sample,'ctx:main');
  assert.equal(scene.nodes.filter(n=>n.role==='workflow-entry').length,1);
  assert.equal(scene.inputs.length,5);
  assert.deepEqual(new Set(scene.inputs.map(n=>n.flowId)),new Set(['flow:initial','flow:velocity-1','flow:dt-1','flow:velocity-2','flow:dt-2']));
  const links=scene.edges.filter(e=>e.association);
  assert.equal(links.length,5);assert(links.every(e=>e.from===scene.entry.key&&e.records.length===0&&!e.data));
  assert.deepEqual(ids(scene),ids(B.scopedData(sample,'ctx:main')));
  assert.equal(JSON.stringify(sample),before);
  const second=scene.nodes.find(n=>n.context==='ctx:move-2'&&n.expandContext);
  const first=scene.nodes.find(n=>n.context==='ctx:move-1'&&n.expandContext);
  assert(scene.edges.some(e=>e.from===first.key&&e.to===second.key&&e.record.flow_id==='flow:next-1'));
  assert(!scene.inputs.some(n=>n.flowId==='flow:next-1')); // produced inside this workflow
});

test('nested workflows retain their input identities, evidence and call boundaries',()=>{
  for(const name of ['ctx:move-1','ctx:move-2','ctx:integrate-1','ctx:integrate-2']){
    const scene=W.project(sample,name);
    assert.equal(scene.inputs.length,3);
    assert(scene.inputs.every(n=>n.origin==='boundary'));
    assert.deepEqual(ids(scene),ids(B.scopedData(sample,name)));
    for(const edge of scene.edges.filter(e=>e.data)){
      assert(edge.records.every(r=>sample.relations.includes(r)));
      for(const [key,port] of [[edge.from,edge.fromPort],[edge.to,edge.toPort]])
        assert(scene.nodes.find(n=>n.key===key).ports.some(p=>p.id===port.id));
    }
  }
});

test('fan-out of one input uses one input node while equally named independent values remain distinct',()=>{
  const g=structuredClone(sample),edge=g.relations.find(r=>r.flow_id==='flow:velocity-1'&&r.context_id==='ctx:move-1'&&g.ports.find(p=>p.id===r.from_id).context_id==='ctx:main');
  g.relations.push({...edge,id:'second-use'});
  const scene=W.project(g,'ctx:main');
  assert.equal(scene.inputs.length,5);
  const input=scene.inputs.find(n=>n.flowId==='flow:velocity-1');
  assert.equal(scene.edges.filter(e=>e.data&&e.from===input.key).length,2);
  g.flows.find(f=>f.id==='flow:velocity-2').name=g.flows.find(f=>f.id==='flow:velocity-1').name;
  assert.equal(W.project(g,'ctx:main').inputs.length,5);
});

test('separate scenarios using the same functions keep separate workflow entry points',()=>{
  const g=structuredClone(sample),cid=id=>'other:'+id,pid=id=>'other:'+id,fid=id=>'other:'+id;
  g.contexts.push(...sample.contexts.map(c=>({...c,id:cid(c.id),parent_id:c.parent_id?cid(c.parent_id):null})));
  g.ports.push(...sample.ports.map(p=>({...p,id:pid(p.id),context_id:cid(p.context_id)})));
  g.flows.push(...sample.flows.map(f=>({...f,id:fid(f.id),producer_port_id:pid(f.producer_port_id),derived_from:f.derived_from.map(fid),color_key:'other:'+f.color_key})));
  g.relations.push(...sample.relations.map(r=>({...r,id:'other:'+r.id,context_id:cid(r.context_id),
    ...(['data','read','write'].includes(r.kind)?{from_id:pid(r.from_id),to_id:pid(r.to_id),flow_id:fid(r.flow_id)}:{})})));
  const first=W.project(g,'ctx:main'),other=W.project(g,'other:ctx:main');
  assert.notEqual(first.entry.key,other.entry.key);assert.equal(other.inputs.length,5);
  assert.notEqual(first.entry.title,other.entry.title);
  assert.equal(new Set(W.choices(g).map(c=>c.label)).size,2);
  assert(first.inputs.every(n=>!n.flowId.startsWith('other:')));
  assert(other.inputs.every(n=>n.flowId.startsWith('other:')));
  assert(ids(other).every(id=>id.startsWith('other:')));
});

test('input focus follows derived results without including independent contributing inputs',()=>{
  assert.deepEqual([...W.trace(sample,['flow:velocity-1'])].sort(),['flow:next-1','flow:next-2','flow:velocity-1']);
  assert.deepEqual([...W.trace(sample,['flow:velocity-2'])].sort(),['flow:next-2','flow:velocity-2']);
  assert.equal(W.trace(sample,['missing']).size,0);
});

test('an explicitly recorded external source joins the input group without absorbing the result consumer',()=>{
  const g=structuredClone(sample),flow=g.flows.find(f=>f.id==='flow:velocity-1');
  const external={...g.entities.find(e=>e.kind==='external'),id:'config:source',name:'Configuration'};
  g.entities.push(external);
  const previous=g.ports.find(p=>p.id===flow.producer_port_id),port={...previous,id:'config:port',entity_id:external.id};
  g.ports.push(port);flow.producer_port_id=port.id;
  for(const r of g.relations.filter(r=>r.from_id===previous.id))r.from_id=port.id;
  const scene=W.project(g,'ctx:main');
  assert.equal(scene.inputs.length,5);
  assert.equal(scene.inputs.find(n=>n.flowId===flow.id).entity.id,external.id);
  assert(scene.nodes.some(n=>n.entity.name==='assert / <cassert>'&&n.role==='entity'));
  assert.deepEqual(ids(scene),ids(B.scopedData(g,'ctx:main')));
});

test('entry placement centers on inputs, respects saved locks, and supports a filtered scene',()=>{
  const scene=W.project(sample,'ctx:main'),sizes=Object.fromEntries(scene.nodes.map(n=>[n.key,{width:n.role==='workflow-entry'?150:242,height:150}]));
  const placed=W.arrange(scene,sizes);
  assert(scene.inputs.every(n=>placed[n.key].x>placed[scene.entry.key].x));
  assert(scene.inputs.every(n=>placed[n.key].x<placed[scene.nodes.find(n=>n.expandContext).key].x));
  const saved={...placed,[scene.entry.key]:{x:-50,y:800,locked:true}};
  assert.deepEqual(W.arrange(scene,sizes,saved,true)[scene.entry.key],saved[scene.entry.key]);
  assert.deepEqual(W.arrange(scene,sizes,placed),placed);
  assert.doesNotThrow(()=>W.arrange({...scene,nodes:scene.inputs,edges:[]},sizes));
  for(const [id,a] of Object.entries(placed))for(const [other,b] of Object.entries(placed))if(id!==other)
    assert(!(a.x<b.x+sizes[other].width&&a.x+sizes[id].width>b.x&&a.y<b.y+sizes[other].height&&a.y+sizes[id].height>b.y));
});

test('no recorded data does not become fabricated input or control edges',()=>{
  const g=structuredClone(sample);g.ports=[];g.flows=[];g.relations=g.relations.filter(r=>r.kind==='call');
  const scene=W.project(g,'ctx:main');
  assert.equal(scene.inputs.length,0);assert(scene.nodes.filter(n=>n.expandContext).length===2);
  assert(scene.edges.every(e=>e.association&&e.records.length===0));
  assert.equal(W.project(g,'missing').nodes.length,0);
});

function twoTasks(){
  const g=structuredClone(sample),other=id=>id?'other:'+id:null;
  g.contexts.push(...sample.contexts.map(c=>({...c,id:other(c.id),parent_id:other(c.parent_id)})));
  g.ports.push(...sample.ports.map(p=>({...p,id:other(p.id),context_id:other(p.context_id)})));
  g.flows.push(...sample.flows.map(f=>({...f,id:other(f.id),producer_port_id:other(f.producer_port_id),derived_from:f.derived_from.map(other),color_key:other(f.color_key)})));
  g.relations.push(...sample.relations.map(r=>({...r,id:other(r.id),context_id:other(r.context_id),
    ...(['data','read','write'].includes(r.kind)?{from_id:other(r.from_id),to_id:other(r.to_id),flow_id:other(r.flow_id)}:{})})));
  return g;
}

test('one task color covers independent inputs, derived results and every nested call without rewriting facts',()=>{
  const g=twoTasks(),before=JSON.stringify(g),tasks=W.tasks(g),palette=W.colors(tasks.list);
  assert.equal(tasks.list.length,2);
  assert.notEqual(palette['ctx:main'],palette['other:ctx:main']);
  for(const context of sample.contexts){
    if(['ctx:measure','ctx:display'].includes(context.id)){assert.equal(tasks.byContext.get(context.id),undefined);continue;}
    assert.equal(tasks.byContext.get(context.id),'ctx:main');
    assert.equal(tasks.byContext.get('other:'+context.id),'other:ctx:main');
    const base=W.project(g,context.id),scene=W.decorate(base,tasks);
    assert.deepEqual(ids(scene),ids(base));
    assert(scene.edges.every(e=>e.taskIds.length===1&&e.taskIds[0]==='ctx:main'));
    assert(scene.nodes.every(n=>n.taskIds.length===1&&n.taskIds[0]==='ctx:main'));
    assert(scene.inputs.every(n=>n.ports.every(p=>p.taskIds[0]==='ctx:main')));
    const focus=W.focus(scene,['ctx:main']);
    assert.equal(focus.edges.size,scene.edges.length);
    assert.equal(focus.nodes.size,scene.nodes.length);
  }
  assert.equal(JSON.stringify(g),before);
  assert(tasks.list.every(t=>t.purpose===t.entity.summary));
});

test('saved task colors survive reordered roots, new tasks, missing tasks and unrelated D keys',()=>{
  const g=twoTasks(),tasks=W.tasks(g),palette=W.colors(tasks.list);
  const reordered=[{id:'new:scenario'},...tasks.list.toReversed()];
  const updated=W.colors(reordered,palette);
  for(const task of tasks.list)assert.equal(updated[task.id],palette[task.id]);
  assert.equal(new Set(Object.values(updated)).size,3);
  const removed=W.colors([{id:'new:scenario'}],updated);
  assert.deepEqual(W.colors(tasks.list,removed),updated);
  for(const f of g.flows)f.color_key='changed:'+f.id;
  assert.deepEqual(W.colors(W.tasks(g).list,palette),palette);
});

test('task focus follows relation contexts even when topology folds shared functions into common nodes',()=>{
  const T=require('../codemap/web/topology-model.js'),g=twoTasks(),tasks=W.tasks(g);
  for(const layer of ['data','call']){
    const base=T.project(g,{grouping:'entity',layer}),scene=W.decorate(base,tasks),focus=W.focus(scene,['ctx:main']);
    assert.deepEqual(ids(scene),ids(base));
    assert(scene.nodes.some(n=>n.taskIds.length===2));
    const expected=scene.edges.filter(e=>e.records.every(r=>!r.id.startsWith('other:')));
    assert.deepEqual([...focus.edges].sort(),expected.map(e=>e.id).sort());
    assert(!scene.edges.filter(e=>e.records.some(r=>r.id.startsWith('other:'))).some(e=>focus.edges.has(e.id)));
    assert.equal(W.focus(scene,['ctx:main','other:ctx:main']).edges.size,scene.edges.length);
  }
  const folded=W.decorate(T.project(g,{grouping:'directory',layer:'data'}),tasks);
  assert(folded.nodes.some(n=>n.internalRelations.length));
  assert(folded.nodes.filter(n=>n.internalRelations.length).every(n=>W.focus(folded,['ctx:main']).nodes.has(n.key)));
});

test('data tracing narrows a selected task without recoloring values or including its other inputs',()=>{
  const g=twoTasks(),tasks=W.tasks(g),scene=W.decorate(W.project(g,'ctx:main'),tasks);
  const whole=W.focus(scene,['ctx:main']),trace=W.focus(scene,['ctx:main'],[...W.trace(g,['flow:velocity-1'])]);
  assert(trace.edges.size<whole.edges.size);
  assert(trace.nodes.has(scene.entry.key));
  assert(trace.nodes.has(scene.inputs.find(n=>n.flowId==='flow:velocity-1').key));
  assert(!trace.nodes.has(scene.inputs.find(n=>n.flowId==='flow:dt-1').key));
  assert(scene.edges.filter(e=>trace.edges.has(e.id)).every(e=>e.taskIds[0]==='ctx:main'));
  assert.equal(W.focus(scene,[],[]).edges.size,scene.edges.length);
});

test('unassigned contexts remain neutral and filtering away inputs does not create phantom nodes',()=>{
  const g=twoTasks();g.contexts.push({id:'catalog',parent_id:null});
  const tasks=W.tasks(g);
  assert.equal(tasks.byContext.get('catalog'),undefined);
  const unassigned={id:'dependency',record:{id:'dependency',context_id:'catalog'},records:[{id:'dependency',context_id:'catalog'}],from:'a',to:'b'};
  const scene=W.decorate({nodes:[{key:'a'},{key:'b'}],edges:[unassigned]},tasks);
  assert.deepEqual(scene.edges[0].taskIds,[]);
  assert.equal(W.focus(scene,['ctx:main']).edges.size,0);
  const full=W.project(g,'ctx:main'),filtered=W.decorate({...full,nodes:[full.entry],edges:[]},tasks);
  assert.equal(filtered.inputs.length,0);assert.equal(filtered.entry.key,full.entry.key);
});
