const test=require('node:test');
const assert=require('node:assert/strict');
const Camera=require('../codemap/web/camera.js');
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-7,`${a} != ${b}`);

test('an offscreen node is centered at the existing scale without changing its position',()=>{
  const viewport=Object.freeze({x:300,y:200,z:.8});
  const node=Object.freeze({x:7000,y:-1200,width:242,height:300});
  const target=Camera.frame(viewport,[node],{width:1000,height:700});
  assert.equal(target.z,.8);
  close(target.x+(node.x+node.width/2)*target.z,500);
  close(target.y+(node.y+node.height/2)*target.z,315);
  assert.deepEqual(viewport,{x:300,y:200,z:.8});
});

test('a group shrinks to fit the visible canvas and leaves space for bottom controls',()=>{
  const boxes=[{x:-500,y:-600,width:320,height:240},{x:800,y:300,width:320,height:400}];
  const target=Camera.frame({x:0,y:0,z:1.4},boxes,{width:1000,height:700});
  assert.ok(target.z<1.4);
  for(const box of boxes){
    assert.ok(target.x+box.x*target.z>=40-1e-7);
    assert.ok(target.x+(box.x+box.width)*target.z<=960+1e-7);
    assert.ok(target.y+box.y*target.z>=40-1e-7);
    assert.ok(target.y+(box.y+box.height)*target.z<=590+1e-7);
  }
});

test('small nodes keep an overview scale and framing uses the resized canvas width',()=>{
  const node={x:2000,y:0,width:242,height:200},viewport={x:0,y:0,z:.3};
  const wide=Camera.frame(viewport,[node],{width:1000,height:700});
  const narrow=Camera.frame(viewport,[node],{width:600,height:700});
  assert.equal(narrow.z,.3);close(wide.x-narrow.x,200);
});

test('missing nodes and a hidden canvas do not produce invalid camera coordinates',()=>{
  assert.equal(Camera.frame({x:0,y:0,z:1},[],{width:100,height:100}),null);
  assert.equal(Camera.frame({x:0,y:0,z:1},[{x:NaN,y:0,width:2,height:3}],{width:100,height:100}),null);
  assert.equal(Camera.frame({x:0,y:0,z:1},[{x:0,y:0,width:2,height:3}],{width:0,height:100}),null);
});

function harness(reduced=false){
  let time=0,id=0,viewport={x:0,y:0,z:1},finished=0;
  const frames=new Map(),timers=new Map(),writes=[];
  const camera=Camera.create({read:()=>viewport,write:v=>{viewport=v;writes.push({...v});},finish:()=>finished++,reducedMotion:()=>reduced,
    now:()=>time,requestFrame:cb=>{const key=id++;frames.set(key,cb);return key;},cancelFrame:key=>frames.delete(key),
    later:(cb,delay)=>{const key=id++;timers.set(key,{cb,at:time+delay});return key;},clearLater:key=>timers.delete(key)});
  return {camera,writes,frames,get viewport(){return viewport;},get finished(){return finished;},advance(ms){
    time+=ms;
    for(const [key,task] of [...timers])if(task.at<=time){timers.delete(key);task.cb();}
    const pending=[...frames.values()];frames.clear();for(const cb of pending)cb(time);
  }};
}

test('selection moves through intermediate frames and saves once at its exact destination',()=>{
  const h=harness(),target={x:-1000,y:300,z:.7};h.camera.focus(()=>target);
  assert.equal(h.camera.active,true);h.advance(0);assert.equal(h.writes.length,0);
  h.advance(140);assert.ok(h.viewport.x<0&&h.viewport.x>target.x);assert.equal(h.finished,0);
  h.advance(140);assert.deepEqual(h.viewport,target);assert.equal(h.finished,1);assert.equal(h.camera.active,false);
});

test('a newer selection starts from the current position and ignores late old callbacks',()=>{
  const h=harness();h.camera.focus(()=>({x:-1000,y:0,z:1}));h.advance(0);h.advance(100);
  const oldCallback=[...h.frames.values()][0],current={...h.viewport},target={x:500,y:200,z:.8};
  h.camera.focus(()=>target);oldCallback(280);assert.deepEqual(h.viewport,current);
  h.advance(0);h.advance(280);assert.deepEqual(h.viewport,target);assert.equal(h.finished,1);
});

test('manual control or scene navigation cancels both queued and moving selections',()=>{
  const h=harness();h.camera.focus(()=>({x:100,y:0,z:1}),{delay:240});
  h.advance(100);h.camera.cancel();h.advance(1000);assert.equal(h.writes.length,0);
  h.camera.focus(()=>({x:100,y:0,z:1}));h.advance(0);h.advance(80);
  const stopped={...h.viewport};h.camera.cancel();h.advance(1000);
  assert.deepEqual(h.viewport,stopped);assert.equal(h.finished,0);assert.equal(h.camera.active,false);
});

test('pointer delay preserves double-click time and measures geometry after panel layout',()=>{
  const h=harness();let target={x:100,y:0,z:1};h.camera.focus(()=>target,{delay:240});
  h.advance(200);assert.equal(h.writes.length,0);target={x:400,y:10,z:.9};
  h.advance(40);h.advance(280);assert.deepEqual(h.viewport,target);
});

test('reduced motion jumps directly to the target and missing targets never animate',()=>{
  const h=harness(true),target={x:100,y:20,z:.6};h.camera.focus(()=>target);h.advance(0);
  assert.deepEqual(h.writes,[target]);assert.equal(h.finished,1);assert.equal(h.camera.active,false);
  h.camera.focus(()=>null);h.advance(0);assert.equal(h.writes.length,1);assert.equal(h.camera.active,false);
});
