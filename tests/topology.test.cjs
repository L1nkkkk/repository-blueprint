const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../codemap/web/topology-model.js');
const sample = require('../examples/reviewed-map-v1.json');
const clone = () => structuredClone(sample);
const data = r => ['data','read','write'].includes(r.kind);

test('returns and writes in the real sample never become control loops or call recursion', () => {
  const topology=T.analyze(sample);
  assert.equal(topology.callCycles.length,0);
  assert.equal(topology.controlCycles.length,0);
  assert.equal(topology.controlEdges,0);
  assert.equal(topology.counts.return,2);
  assert.equal(topology.counts.write_back,2);
});

test('static recursive calls are independent from context-scoped control loops and uncertain facts', () => {
  const g=clone(), base=g.relations.find(r=>r.kind==='call');
  g.relations.push({...base,id:'recurse',from_id:base.to_id,to_id:base.from_id});
  assert.equal(T.analyze(g).callCycles.length,1);
  assert.equal(T.analyze(g).controlCycles.length,0);
  g.relations.at(-1).basis='candidate';
  assert.equal(T.analyze(g).callCycles.length,0);
  g.relations.push({...base,id:'control-a',kind:'control',from_id:'a',to_id:'b',context_id:'one'},
                   {...base,id:'control-b',kind:'control',from_id:'b',to_id:'a',context_id:'two'});
  assert.equal(T.analyze(g).controlCycles.length,0);
  g.relations.at(-1).context_id='one';
  assert.equal(T.analyze(g).controlCycles.length,1);
  g.relations.at(-1).freshness='stale';
  assert.equal(T.analyze(g).controlCycles.length,0);
});

test('folding and expansion conserve every fact, evidence, identity and call context', () => {
  const before=JSON.stringify(sample), expected=sample.relations.filter(data).map(r=>r.id).sort();
  for(const grouping of ['directory','file','semantic','architecture','entity']){
    let scene=T.project(sample,{grouping,layer:'data'});
    for(let round=0;round<2;round++){
      const represented=[...scene.edges.flatMap(e=>e.records),...scene.nodes.flatMap(n=>n.internalRelations)];
      assert.deepEqual(represented.map(r=>r.id).sort(),expected);
      for(const r of represented)assert(sample.relations.includes(r));
      for(const e of scene.edges){
        assert(e.records.every(r=>r.kind===e.record.kind&&r.flow_id===e.record.flow_id&&r.context_id===e.record.context_id));
        if(e.data){assert(scene.nodes.find(n=>n.key===e.from).ports.some(p=>p.id===e.fromPort.id));assert(scene.nodes.find(n=>n.key===e.to).ports.some(p=>p.id===e.toPort.id));}
      }
      scene=T.project(sample,{grouping,layer:'data',expanded:scene.nodes.filter(n=>n.canExpand).map(n=>n.entity.id)});
    }
  }
  assert.equal(JSON.stringify(sample),before);
});

test('whole-repository topology retains disconnected tools and independent modules', () => {
  const scene=T.project(sample,{grouping:'directory',layer:'call'});
  const ids=new Set(scene.nodes.flatMap(n=>n.members));
  for(const name of ['Measure','DisplayUnits'])assert(ids.has(sample.entities.find(e=>e.name===name).id));
  assert(scene.nodes.some(n=>n.entity.name==='tools'));
  assert(scene.nodes.reduce((sum,n)=>sum+n.internalRelations.length,0)>0);
});

test('ambiguous architecture membership stays explicit rather than choosing the first parent', () => {
  const g=clone(), child=g.entities.find(e=>e.kind==='function');
  g.entities.push({...child,id:'module:a',kind:'module',source_ids:[]},{...child,id:'module:b',kind:'module',source_ids:[]});
  g.memberships.push({id:'ma',axis:'architecture',parent_id:'module:a',child_id:child.id},
                    {id:'mb',axis:'architecture',parent_id:'module:b',child_id:child.id});
  const scene=T.project(g,{grouping:'architecture',layer:'call'}), node=scene.nodes.find(n=>n.entity.id===child.id);
  assert(node);assert(node.ambiguous);
  assert.equal(node.members.filter(id=>id===child.id).length,1);
});

test('shape counts unique neighbors rather than parallel colored wires', () => {
  const nodes=['a','b','c','d','e'].map(key=>({key}));
  const edges=[{from:'a',to:'b'},{from:'a',to:'b'},{from:'a',to:'c'},{from:'b',to:'d'},{from:'c',to:'d'}];
  const shape=T.shape(nodes,edges);
  assert.deepEqual(shape.forks,['a']);assert.deepEqual(shape.joins,['d']);assert.deepEqual(shape.isolated,['e']);
  assert.equal(shape.cycles.length,0);
});

test('topology traversal handles long chains without recursive stack growth', () => {
  const keys=Array.from({length:12000},(_,i)=>String(i));
  const edges=keys.slice(1).map((to,i)=>({from:keys[i],to}));
  assert.equal(T.components(keys,edges).cycles.length,0);
  edges.push({from:keys.at(-1),to:keys[0]});
  assert.equal(T.components(keys,edges).cycles[0].length,keys.length);
});
