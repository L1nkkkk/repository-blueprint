/* Resizable reading panels. Preferences are independent of canvas coordinates. */
(function(root,factory){
  const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.PanelLayout=api;
})(globalThis,function(){
  'use strict';
  const specs={files:{width:234,min:180,max:420,breakpoint:580},details:{width:360,min:260,max:900,breakpoint:880}};
  const clamp=(value,min,max)=>Math.max(min,Math.min(max,value));
  function normalize(saved){
    return Object.fromEntries(Object.entries(specs).map(([id,spec])=>[id,{
      width:Number.isFinite(saved?.[id]?.width)?clamp(saved[id].width,spec.min,spec.max):spec.width,
      collapsed:saved?.[id]?.collapsed===true
    }]));
  }
  function measure(saved,width,drawer=''){
    const preferences=normalize(saved),result={};
    for(const [id,spec] of Object.entries(specs)){
      const overlay=width<=spec.breakpoint,open=overlay?drawer===id:!preferences[id].collapsed;
      result[id]={overlay,open,width:overlay?Math.min(preferences[id].width,width*.9):preferences[id].width};
    }
    const docked=Object.keys(specs).filter(id=>result[id].open&&!result[id].overlay);
    const budget=width-280-docked.length*6,total=docked.reduce((sum,id)=>sum+result[id].width,0);
    const slack=docked.reduce((sum,id)=>sum+result[id].width-specs[id].min,0);
    if(total>budget&&slack>0)for(const id of docked)result[id].width-=Math.min(1,(total-budget)/slack)*(result[id].width-specs[id].min);
    for(const id of docked){
      result[id].width=Math.round(result[id].width);
      result[id].max=Math.max(specs[id].min,Math.min(specs[id].max,budget-docked.filter(other=>other!==id).reduce((sum,other)=>sum+result[other].width,0)));
    }
    return result;
  }
  function mount({getState,changed}){
    const workspace=document.querySelector('.workspace'),backdrop=document.querySelector('#panel-backdrop');
    let drawer='',last;
    const panels=Object.fromEntries(Object.keys(specs).map(id=>[id,{
      aside:document.querySelector('#'+id+'-panel'),handle:document.querySelector('#resize-'+id),
      toggle:document.querySelector('#toggle-'+id),close:document.querySelector('#close-'+id)
    }]));
    function sync(){
      const state=getState();state.panels=normalize(state.panels);
      last=measure(state.panels,workspace.clientWidth,drawer);
      for(const [id,ui] of Object.entries(panels)){
        const item=last[id],docked=item.open&&!item.overlay;
        ui.aside.hidden=!item.open;ui.aside.dataset.panelMode=item.overlay?'overlay':'docked';
        ui.aside.style.setProperty('--panel-width',item.width+'px');
        workspace.style.setProperty('--'+id+'-width',docked?item.width+'px':'0px');
        workspace.style.setProperty('--'+id+'-handle',docked?'6px':'0px');
        ui.handle.hidden=!docked;ui.handle.setAttribute('aria-valuenow',Math.round(item.width));
        ui.handle.setAttribute('aria-valuemin',specs[id].min);ui.handle.setAttribute('aria-valuemax',Math.floor(item.max||specs[id].max));
        ui.handle.setAttribute('aria-valuetext',Math.round(item.width)+' 像素');
        ui.toggle.setAttribute('aria-expanded',String(item.open));ui.toggle.classList.toggle('active',item.open);
      }
      backdrop.hidden=!Object.values(last).some(item=>item.overlay&&item.open);
    }
    function setOpen(id,open){
      sync();if(last[id].overlay)drawer=open?id:'';else getState().panels[id].collapsed=!open;
      sync();changed();
    }
    function closeDrawers(){drawer='';sync();}
    function resize(id,width){
      sync();
      // An explicit resize starts from the widths currently on screen. Window resizing
      // alone keeps the original preferences so a larger window can restore them.
      for(const other of Object.keys(specs))if(last[other].open&&!last[other].overlay)getState().panels[other].width=last[other].width;
      getState().panels[id].width=clamp(width,specs[id].min,last[id].max||specs[id].max);sync();
    }
    for(const [id,ui] of Object.entries(panels)){
      ui.toggle.onclick=()=>{sync();setOpen(id,!last[id].open);};
      ui.close.onclick=()=>{setOpen(id,false);ui.toggle.focus();};
      ui.handle.ondblclick=()=>{getState().panels[id].width=specs[id].width;sync();changed();};
      ui.handle.onkeydown=event=>{
        if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
        event.preventDefault();sync();const delta=(event.shiftKey?64:24)*(id==='files'?1:-1);
        resize(id,event.key==='Home'?specs[id].min:event.key==='End'?last[id].max:last[id].width+(event.key==='ArrowRight'?delta:-delta));changed();
      };
      ui.handle.onpointerdown=event=>{
        if(event.button!==0)return;event.preventDefault();sync();
        const startX=event.clientX,startWidth=last[id].width,previous=normalize(getState().panels);
        ui.handle.focus();ui.handle.setPointerCapture(event.pointerId);document.body.classList.add('resizing-panels');
        const move=e=>resize(id,startWidth+(e.clientX-startX)*(id==='files'?1:-1));
        const end=e=>{
          ui.handle.removeEventListener('pointermove',move);ui.handle.removeEventListener('pointerup',end);ui.handle.removeEventListener('pointercancel',end);
          document.body.classList.remove('resizing-panels');
          if(e.type==='pointercancel'){getState().panels=previous;sync();}
          if(ui.handle.hasPointerCapture(event.pointerId))ui.handle.releasePointerCapture(event.pointerId);changed();
        };
        ui.handle.addEventListener('pointermove',move);ui.handle.addEventListener('pointerup',end);ui.handle.addEventListener('pointercancel',end);
      };
    }
    backdrop.onclick=closeDrawers;
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&drawer){const id=drawer;closeDrawers();panels[id].toggle.focus();}});
    window.addEventListener('resize',()=>{if(drawer&&workspace.clientWidth>specs[drawer].breakpoint)drawer='';sync();});
    sync();return {sync,open:id=>setOpen(id,true),closeDrawers};
  }
  return {normalize,measure,mount};
});
