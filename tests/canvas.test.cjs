const test = require('node:test');
const assert = require('node:assert/strict');
const B = require('../codemap/web/canvas-model.js');
const sample = require('../examples/reviewed-map-v1.json');
const clone = () => structuredClone(sample);
const data = r => ['data','read','write'].includes(r.kind);

test('main shows two distinct call interfaces and connects the first written value to the second call', () => {
  const before = JSON.stringify(sample), scene = B.scopedData(sample, 'ctx:main');
  assert.equal(JSON.stringify(sample), before);
  assert.equal(scene.nodes.length, 5); assert.equal(scene.edges.length, 7);
  assert.equal(new Set(scene.edges.flatMap(e => e.records.map(r => r.id))).size, 9);
  const calls = scene.nodes.filter(n => n.role === 'call');
  assert.deepEqual(calls.map(n => n.context), ['ctx:move-1','ctx:move-2']);
  for (const n of calls) {
    assert.deepEqual(n.ports.filter(p => p.direction === 'in').map(p => p.name), ['position','velocity','dt']);
    assert.deepEqual(n.ports.filter(p => p.direction === 'out').map(p => p.name), ['写回 state.position']);
  }
  const written = scene.edges.find(e => e.record.flow_id === 'flow:next-1');
  assert.equal(written.from, 'fn:move@ctx:move-1'); assert.equal(written.to, 'fn:move@ctx:move-2');
  assert.deepEqual(written.records.map(r => r.kind), ['write','read']);
  assert.equal(written.annotation, '写回');
  assert(!scene.nodes.some(n => n.entity.id === 'fn:integrate'));
});

test('expanding each call recovers all original data relations without mixing invocations', () => {
  const represented = new Set();
  for (const id of ['ctx:main','ctx:move-1','ctx:move-2','ctx:integrate-1','ctx:integrate-2']) {
    const scene = B.scopedData(sample, id);
    scene.edges.forEach(e => e.records.forEach(r => represented.add(r.id)));
    for (const e of scene.edges) {
      assert(e.records.every(r => sample.relations.includes(r)));
      assert(e.records.every(r => r.flow_id === e.record.flow_id));
    }
  }
  assert.deepEqual([...represented].sort(), sample.relations.filter(data).map(r=>r.id).sort());
  for (const number of [1,2]) {
    const scene = B.scopedData(sample, `ctx:move-${number}`);
    assert.equal(scene.nodes.length,3); assert.equal(scene.edges.length,4);
    const inner=scene.nodes.find(n=>n.role==='call');assert.equal(inner.context,`ctx:integrate-${number}`);
    assert.equal(scene.edges.find(e=>e.annotation==='写回').records.length,2);
    assert(!scene.edges.some(e=>e.record.flow_id===`flow:velocity-${3-number}`));
  }
});

test('a leaf retains its actual body and never creates an all-inputs-to-all-outputs relation', () => {
  const scene = B.scopedData(sample, 'ctx:integrate-1');
  assert.equal(scene.edges.length,4);assert(scene.edges.every(e=>e.records.length===1));
  const body=scene.nodes.find(n=>n.role==='body');assert.equal(body.ports.length,4);
  assert.equal(scene.edges.filter(e=>e.to===body.key).length,3);
  assert.equal(scene.edges.filter(e=>e.from===body.key).length,1);
  assert(scene.edges.find(e=>e.from===body.key).toPort.annotation==='返回');
});

test('ambiguous incoming identities and locally produced values are not contracted', () => {
  const g=clone(), written=g.relations.find(r=>r.kind==='write'&&r.flow_id==='flow:next-1');
  g.relations.push({...written,id:'ambiguous-second-write'});
  let scene=B.scopedData(g,'ctx:main');
  assert(!scene.edges.some(e=>e.from==='fn:move@ctx:move-1'&&e.to==='fn:move@ctx:move-2'));
  const local=clone(), f=local.flows.find(f=>f.id==='flow:next-1');
  f.producer_port_id=local.ports.find(p=>p.context_id==='ctx:main'&&p.direction==='out'&&p.name==='state.position@1').id;
  scene=B.scopedData(local,'ctx:main');
  assert(!scene.edges.some(e=>e.from==='fn:move@ctx:move-1'&&e.to==='fn:move@ctx:move-2'));
});

test('merged edges retain uncertainty and stale evidence', () => {
  const g=clone(), r=g.relations.find(r=>r.kind==='write'&&r.flow_id==='flow:next-1');r.freshness='stale';r.basis='candidate';r.reason='needs review';
  const edge=B.scopedData(g,'ctx:main').edges.find(e=>e.records.some(record=>record.id===r.id&&record.freshness==='stale'));
  assert(edge.records.some(r=>r.freshness==='stale'&&r.basis==='candidate'&&r.reason==='needs review'));
});

test('dependency layout is deterministic, handles cycles and preserves saved/locked positions', () => {
  const nodes=['a','b','c','d'].map(key=>({key})),edges=[{from:'a',to:'b'},{from:'b',to:'a'},{from:'b',to:'c'}];
  const sizes=Object.fromEntries(nodes.map(n=>[n.key,{width:242,height:180}]));
  const saved={c:{x:660,y:50,locked:true},d:{x:900,y:500,locked:false}};
  const regular=B.arrange(nodes,edges,sizes,saved);assert.deepEqual(regular.c,saved.c);assert.deepEqual(regular.d,saved.d);
  const arranged=B.arrange(nodes,edges,sizes,saved,true);assert.deepEqual(arranged.c,saved.c);assert.notDeepEqual(arranged.d,saved.d);
  assert.deepEqual(arranged,B.arrange(nodes,edges,sizes,saved,true));
  for (const a of nodes) for (const b of nodes) if(a!==b) {
    const p=arranged[a.key],q=arranged[b.key];assert(!(p.x<q.x+242&&p.x+242>q.x&&p.y<q.y+180&&p.y+180>q.y));
  }
});

test('tidying interleaved workflows separates their bands and clears dragged overlaps', () => {
  const nodes=['a:input','b:input','a:body','b:body','a:call','b:call'].map(key=>({key,columnHint:key.endsWith('call')?1:0}));
  const edges=['a','b'].flatMap(id=>[{from:id+':input',to:id+':body'},{from:id+':body',to:id+':call'},{from:id+':call',to:id+':body'}]);
  const dimensions=Object.fromEntries(nodes.map(n=>[n.key,{width:n.key.endsWith('body')?310:210,height:n.key.endsWith('input')?100:250}]));
  const dragged=Object.fromEntries(nodes.map(n=>[n.key,{x:80,y:80,locked:false}])),before=JSON.stringify({nodes,edges,dragged});
  const positions=B.arrange(nodes,edges,dimensions,dragged,true);
  const bottom=Math.max(...nodes.filter(n=>n.key.startsWith('a:')).map(n=>positions[n.key].y+dimensions[n.key].height));
  const top=Math.min(...nodes.filter(n=>n.key.startsWith('b:')).map(n=>positions[n.key].y));
  assert(top>bottom+80,'independent workflows must not weave through the same band');
  for(const id of ['a','b'])assert(positions[id+':input'].x<positions[id+':body'].x&&positions[id+':body'].x<positions[id+':call'].x);
  for(const a of nodes)for(const b of nodes)if(a!==b){
    const p=positions[a.key],q=positions[b.key],s=dimensions[a.key],t=dimensions[b.key];
    assert(!(p.x<q.x+t.width&&p.x+s.width>q.x&&p.y<q.y+t.height&&p.y+s.height>q.y));
  }
  assert.equal(JSON.stringify({nodes,edges,dragged}),before,'tidying must not rewrite facts or the undo snapshot');
});

test('sibling ordering removes avoidable crossings without weighting duplicate data wires', () => {
  const nodes=['entry','left:a','left:b','right:b','right:a','sink'].map(key=>({key}));
  const edges=[{from:'entry',to:'left:a'},{from:'entry',to:'left:b'},{from:'left:a',to:'right:a'},{from:'left:b',to:'right:b'},{from:'right:a',to:'sink'},{from:'right:b',to:'sink'}];
  const sizes=Object.fromEntries(nodes.map(n=>[n.key,{width:242,height:180}]));
  const p=B.arrange(nodes,edges,sizes,{},true);
  assert((p['left:a'].y-p['left:b'].y)*(p['right:a'].y-p['right:b'].y)>0);
  assert.deepEqual(p,B.arrange(nodes,[...edges,...Array.from({length:12},()=>({...edges[2]}))],sizes,{},true));
});

function renderedCurve(d) {
  const parts=d.match(/[MC]|[-+]?(?:\d*\.)?\d+(?:e[-+]?\d+)?/gi);
  assert(!/[LQHVSAZ]/i.test(d),'wire must use smooth cubic segments');
  assert.equal(parts.shift(),'M');let previous={x:+parts.shift(),y:+parts.shift()};
  const points=[previous],curves=[];
  while(parts.length){
    assert.equal(parts.shift(),'C');const a=previous,b={x:+parts.shift(),y:+parts.shift()},c={x:+parts.shift(),y:+parts.shift()},end={x:+parts.shift(),y:+parts.shift()};
    curves.push([a,b,c,end]);
    for(let j=1;j<=128;j++){const t=j/128,s=1-t;points.push({x:s*s*s*a.x+3*s*s*t*b.x+3*s*t*t*c.x+t*t*t*end.x,y:s*s*s*a.y+3*s*s*t*b.y+3*s*t*t*c.y+t*t*t*end.y});}
    previous=end;
  }
  for(let i=1;i<curves.length;i++){
    const left=curves[i-1],right=curves[i],a={x:left[3].x-left[2].x,y:left[3].y-left[2].y},b={x:right[1].x-right[0].x,y:right[1].y-right[0].y};
    assert((a.x*b.x+a.y*b.y)/(Math.hypot(a.x,a.y)*Math.hypot(b.x,b.y))>.9999,'joined curves must share a tangent');
  }
  return points;
}

function clearRoute(route, rectangles) {
  assert.equal(route.blocked,false);
  // Test the path actually sent to SVG, not just the routing search's guides.
  const points=renderedCurve(route.d);
  for(let i=1;i<points.length;i++) {
    const a=points[i-1],b=points[i];
    for(let j=1;j<50;j++){const t=j/50,p={x:a.x+(b.x-a.x)*t,y:a.y+(b.y-a.y)*t};assert(!rectangles.some(r=>p.x>r.x+.01&&p.x<r.x+r.width-.01&&p.y>r.y+.01&&p.y<r.y+r.height-.01),`route crosses a node at ${JSON.stringify(p)}`);}
  }
}
test('forward routing bends around an intervening node', () => {
  const boxes=[{key:'a',x:0,y:0,width:242,height:220},{key:'block',x:320,y:60,width:100,height:320},{key:'b',x:530,y:200,width:242,height:220}];
  const route=B.route({x:242,y:100},{x:530,y:300},boxes,{from:'a',to:'b'});
  clearRoute(route,boxes);assert(route.points.some(p=>p.y<=60||p.y>=380));
});

test('pin centers on the inside half of a card border remain connected', () => {
  const boxes=[{key:'a',x:0,y:0,width:242,height:240},{key:'b',x:334,y:280,width:242,height:240}];
  const route=B.route({x:241.5,y:180},{x:334.5,y:360},boxes,{from:'a',to:'b'});
  // Border contact is allowed; neither curve may enter a card's interior.
  clearRoute(route,boxes.map(r=>({...r,x:r.x+1,y:r.y+1,width:r.width-2,height:r.height-2})));
});

test('reverse and self edges avoid nodes and reserve separate return lanes', () => {
  const boxes=[{key:'a',x:0,y:0,width:242,height:240},{key:'b',x:600,y:0,width:242,height:240}];
  const a={x:842,y:100},b={x:0,y:100};
  const first=B.route(a,b,boxes,{from:'b',to:'a',lane:0});clearRoute(first,boxes);
  const occupied=first.guidePoints.slice(1).map((p,i)=>[first.guidePoints[i],p]);
  const next=B.route(a,b,boxes,{from:'b',to:'a',lane:1,occupied});clearRoute(next,boxes);assert.notEqual(first.d,next.d);
  clearRoute(B.route({x:242,y:80},{x:0,y:160},[boxes[0]],{from:'a',to:'a'}),[boxes[0]]);
});

test('a narrow but passable corridor reduces decorative clearance before giving up', () => {
  const boxes=[{key:'other',x:0,y:0,width:242,height:350},{key:'target',x:320,y:0,width:242,height:240},{key:'source',x:640,y:0,width:242,height:240}];
  clearRoute(B.route({x:882,y:100},{x:320,y:180},boxes,{from:'source',to:'target',lane:6}),boxes);
});

test('manual reroute points are honored and impossible overlaps are reported', () => {
  const boxes=[{key:'a',x:0,y:0,width:242,height:220},{key:'b',x:700,y:0,width:242,height:220}],via={x:450,y:-90};
  const route=B.route({x:242,y:100},{x:700,y:100},boxes,{from:'a',to:'b',via});clearRoute(route,boxes);
  assert(renderedCurve(route.d).some(p=>Math.hypot(p.x-via.x,p.y-via.y)<.001),'curve must pass through the saved handle');
  assert.equal(B.route({x:242,y:100},{x:700,y:100},boxes,{from:'a',to:'b',via:{x:100,y:100}}).blocked,true);
});

test('a long detour uses broad smooth bends and keeps horizontal pin directions', () => {
  const boxes=[{key:'a',x:0,y:0,width:242,height:240},{key:'block',x:350,y:40,width:180,height:600},{key:'b',x:600,y:700,width:242,height:240}];
  const start={x:242,y:100},end={x:600,y:780},route=B.route(start,end,boxes,{from:'a',to:'b'});
  clearRoute(route,boxes);
  const points=renderedCurve(route.d);
  assert.deepEqual(points[0],start);assert.deepEqual(points.at(-1),end);
  const first=points[1],last=points.at(-2);
  assert(first.x>start.x&&Math.abs(first.y-start.y)<Math.abs(first.x-start.x));
  assert(last.x<end.x&&Math.abs(last.y-end.y)<Math.abs(last.x-end.x));
  const middle=points.filter(p=>p.y>250&&p.y<450);
  assert(Math.max(...middle.map(p=>p.x))-Math.min(...middle.map(p=>p.x))>4,'detour must not retain a long straight vertical elbow');
});

test('a reroute point below both pins does not retrace the same segment', () => {
  const boxes=[{key:'a',x:0,y:0,width:242,height:240},{key:'b',x:334,y:0,width:242,height:240}];
  const route=B.route({x:242,y:100},{x:334,y:100},boxes,{from:'a',to:'b',via:{x:288,y:160},lane:3});
  clearRoute(route,boxes);
  const segments=route.points.slice(1).map((p,i)=>[route.points[i],p]);
  for(let i=0;i<segments.length;i++)for(let j=i+1;j<segments.length;j++) {
    const [a,b]=segments[i],[c,d]=segments[j];
    if(a.x===b.x&&c.x===d.x&&a.x===c.x)assert(Math.min(Math.max(a.y,b.y),Math.max(c.y,d.y))-Math.max(Math.min(a.y,b.y),Math.min(c.y,d.y))<=.01);
    if(a.y===b.y&&c.y===d.y&&a.y===c.y)assert(Math.min(Math.max(a.x,b.x),Math.max(c.x,d.x))-Math.max(Math.min(a.x,b.x),Math.min(c.x,d.x))<=.01);
  }
});
