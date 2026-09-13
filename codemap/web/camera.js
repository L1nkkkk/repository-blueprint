/* Selection framing changes only the viewport, never the graph or node layout. */
(function(root,factory){
  const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.CanvasCamera=api;
})(globalThis,function(){
  'use strict';
  const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));

  function frame(viewport,boxes,{width,height}){
    const valid=boxes.filter(b=>b&&['x','y','width','height'].every(k=>Number.isFinite(b[k]))&&b.width>0&&b.height>0);
    if(!valid.length||!(width>0&&height>0))return null;
    const left=Math.min(40,width*.2),right=left,top=Math.min(40,height*.2),bottom=Math.min(110,height*.2);
    const minX=Math.min(...valid.map(b=>b.x)),minY=Math.min(...valid.map(b=>b.y));
    const maxX=Math.max(...valid.map(b=>b.x+b.width)),maxY=Math.max(...valid.map(b=>b.y+b.height));
    // Keep the reader's scale unless the selected bounds need more room.
    const z=clamp(Math.min(viewport.z,(width-left-right)/(maxX-minX),(height-top-bottom)/(maxY-minY)),.22,2);
    return {z,x:(left+width-right)/2-(minX+maxX)/2*z,y:(top+height-bottom)/2-(minY+maxY)/2*z};
  }

  function create({read,write,finish=()=>{},reducedMotion=()=>false,duration=280,
    requestFrame=callback=>requestAnimationFrame(callback),cancelFrame=id=>cancelAnimationFrame(id),
    now=()=>performance.now(),later=(callback,delay)=>setTimeout(callback,delay),clearLater=id=>clearTimeout(id)}){
    let generation=0,animation=null,timer=null;
    function cancel(){
      generation++;if(animation!==null)cancelFrame(animation);if(timer!==null)clearLater(timer);
      animation=null;timer=null;
    }
    function focus(resolve,{delay=0}={}){
      cancel();const request=generation;
      function begin(){
        if(request!==generation)return;
        animation=null;timer=null;
        const target=resolve();if(!target)return;
        const from={...read()},start=now();
        if(reducedMotion()||duration<=0||(Math.abs(from.x-target.x)<.5&&Math.abs(from.y-target.y)<.5&&Math.abs(from.z-target.z)<.001)){
          write(target);finish();return;
        }
        function step(time){
          if(request!==generation)return;
          animation=null;const progress=clamp((time-start)/duration,0,1),ease=1-(1-progress)**3;
          write(progress===1?target:Object.fromEntries(['x','y','z'].map(k=>[k,from[k]+(target[k]-from[k])*ease])));
          if(progress<1)animation=requestFrame(step);else finish();
        }
        animation=requestFrame(step);
      }
      // Measure after the details panel and newly rendered cards have their sizes.
      if(delay>0)timer=later(()=>{if(request===generation){timer=null;animation=requestFrame(begin);}},delay);
      else animation=requestFrame(begin);
    }
    return {focus,cancel,get active(){return animation!==null||timer!==null;}};
  }
  return {frame,create};
});
