/* Real graph reader: all displayed facts come from the saved project. */
'use strict';
const $ = s => document.querySelector(s);
const svgNS = 'http://www.w3.org/2000/svg';
const labels = {repository:'仓库',directory:'目录',module:'模块',file:'文件',namespace:'命名空间',package:'包',class:'类',struct:'结构体',interface:'接口',enum:'枚举',function:'函数',method:'方法',field:'字段',parameter:'参数',variable:'变量',value:'数据值',operation:'运算',external:'外部边界',unknown:'待识别'};
const roleLabels = {entry:'入口',event:'事件',test:'测试',tool:'工具',generated:'生成代码',vendor:'第三方',public_api:'公开接口'};
const stateLabels = {located:'待阅读',partial:'部分分析',reviewed:'已核对',queued:'等待阅读',running:'已领取',blocked:'待处理阻碍',done:'已完成'};
const icons = {container:'▱',file:'▤',scope:'{ }',type:'◇',callable:'ƒ',data:'●',operation:'±',external:'↗',unknown:'?'};
let payload, graph, catalog, entities, sources, ports, contexts, workflowTasks, topologyAnalysis, topologyScene, workflowScene, changePlan, checkingChanges=false;
let view = {}, selected = null, selectedLink = null, sceneNodes = [], sceneEdges = [], writeToken, saveTimer, saving = false, saveAgain = false, sourceGeneration = 0, loading = false;
let nodeElements = new Map(), portElements = new Map();
let routedEdges = new Map(), wireFrame;
let fileScopeCache=null;
let renderGeneration=0;
const camera=CanvasCamera.create({read:()=>layout().viewport,write:viewport=>{layout().viewport=viewport;applyViewport();},finish:saveSoon,reducedMotion:()=>window.matchMedia('(prefers-reduced-motion: reduce)').matches});
const clamp = (n,min,max) => Math.min(max,Math.max(min,n));
const elem = (tag, attrs={}, ...children) => {
  const e = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key === 'class') e.className = value;
    else if (key === 'text') e.textContent = value;
    else if (key.startsWith('on')) e.addEventListener(key.slice(2),value);
    else if (value !== undefined && value !== null) e.setAttribute(key,value);
  }
  e.append(...children.filter(x=>x!==null && x!==undefined)); return e;
};
const svg = (tag,attrs={}) => {const e=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e;};
const family = e => catalog.kinds[e.kind]?.family || 'unknown';
const state = e => e.freshness==='stale'?'待重新核对':e.structure&&e.analysis==='located'?'结构已解析':stateLabels[e.analysis];
const notice = text => {$('#notice').hidden=!text;$('#notice').textContent=text||'';};
async function api(url,options) {const r=await fetch(url,options);const data=await r.json();if(!r.ok)throw new Error(data.error||'读取失败');return data;}
function taskColor(id){const slot=view.taskColors[id];if(slot===undefined)return 'var(--muted)';const hue=(slot*137.508+46)%360;return `light-dark(hsl(${hue} 65% 35%),hsl(${hue} 70% 70%))`;}
function taskPaint(ids){return ids?.length===1?taskColor(ids[0]):'var(--muted)';}
function taskNames(ids){return (ids||[]).map(id=>workflowTasks.byId.get(id)?.label).filter(Boolean).join('、')||'未归属任务';}
function rootId(){return graph.entities.find(e=>e.kind==='repository')?.id;}
function scoped(){return view.mode==='relations'&&view.layer==='data'&&view.presentation==='scoped';}
function key(){return view.mode==='workflow'?`workflow:${view.scope}`:view.mode==='topology'?`topology:${view.topologyLayer}:${view.grouping}:${[...view.expandedGroups].sort().join('|')}`:scoped()?`relations:scoped:${view.scope}`:view.mode==='relations'?`relations:${view.layer}`:`structure:${view.parent}`;}
function flowView(){return view.mode!=='structure';}
function focusedFlows(){return view.mode==='workflow'?Workflow.trace(graph,view.focus):new Set(view.focus);}
function sceneFocus(){return Workflow.focus({nodes:sceneNodes,edges:sceneEdges},view.taskFocus,[...focusedFlows()]);}
function applyFlowFocus(){const focus=sceneFocus();for(const [k,card] of nodeElements)card.classList.toggle('dim',flowView()&&focus.active&&!focus.nodes.has(k));renderFlows();renderWorkflowNavigation();drawWires();saveSoon();}
function focusTask(id,toggle=false){camera.cancel();view.taskFocus=toggle?(view.taskFocus.includes(id)?view.taskFocus.filter(k=>k!==id):[...view.taskFocus,id]):[id];view.focus=[];applyFlowFocus();renderTaskDetails(workflowTasks.byId.get(id));if(view.taskFocus.length){const focus=sceneFocus();frameNodes(sceneNodes.filter(n=>focus.nodes.has(n.key)));}}
function traceData(flow,taskIds){view.taskFocus=taskIds;view.focus=[flow.id];applyFlowFocus();}
function expandGroup(id){view.expandedGroups=[...new Set([...view.expandedGroups,id])];selected=null;selectedLink=null;render();saveSoon();}
function layout(){view.layouts??={};return view.layouts[key()]??=( {positions:{},viewport:{x:40,y:42,z:1}} );}
function remember(){view.history.push({mode:view.mode,parent:view.parent,layer:view.layer,presentation:view.presentation,scope:view.scope,grouping:view.grouping,topologyLayer:view.topologyLayer,expandedGroups:[...view.expandedGroups],taskFocus:[...view.taskFocus],focus:[...view.focus],fileSelection:view.fileSelection||null});}
function enterContext(id){remember();if(workflowTasks.byContext.get(id)!==workflowTasks.byContext.get(view.scope)){view.taskFocus=[workflowTasks.byContext.get(id)].filter(Boolean);view.focus=[];}if(view.mode!=='workflow')view.mode='relations';view.layer='data';view.presentation='scoped';view.scope=id;selected=null;selectedLink=null;$('#search').value='';panelControls.closeDrawers();render();saveSoon();}
function saveSoon(){clearTimeout(saveTimer);$('#saved').textContent='正在保存阅读设置…';saveTimer=setTimeout(save,280);}
async function save(){
  if(!writeToken)return;
  if(saving){saveAgain=true;return;}saving=true;
  try{await api('/api/view',{method:'POST',headers:{'Content-Type':'application/json','X-Codemap-Token':writeToken},body:JSON.stringify(view)});$('#saved').textContent='阅读设置已保存';}
  catch(error){$('#saved').textContent='保存失败：'+error.message;}
  finally{saving=false;if(saveAgain){saveAgain=false;save();}}
}
function applyTheme(theme){view.theme=['light','dark','system'].includes(theme)?theme:'system';document.documentElement.dataset.theme=view.theme;document.querySelectorAll('.theme button').forEach(b=>{b.classList.toggle('active',b.dataset.theme===view.theme);b.setAttribute('aria-pressed',String(b.dataset.theme===view.theme));});}
function childrenOf(id){
  const direct=new Set(graph.memberships.filter(m=>m.parent_id===id).map(m=>m.child_id));
  const parent=entities.get(id);
  if(parent?.kind==='file'){
    const inFile=new Set(graph.entities.filter(e=>e.kind!=='file'&&e.source_ids.some(s=>parent.source_ids.includes(s))).map(e=>e.id));
    const nested=new Set(graph.memberships.filter(m=>inFile.has(m.parent_id)&&inFile.has(m.child_id)).map(m=>m.child_id));
    for(const child of inFile)if(!nested.has(child))direct.add(child);
  }
  return [...direct].map(id=>entities.get(id)).filter(Boolean);
}
function pathFor(e){return e.source_ids.map(s=>sources.get(s)?.path).filter(Boolean).join(', ');}
function goTo(id,push=true){
  if(push)remember();
  view.mode='structure';view.parent=id;selectedLink=null;selected={key:id,entity:entities.get(id),context:null};view.fileSelection=selected.entity.kind==='file'?selected.entity.source_ids[0]:null;panelControls.closeDrawers();renderDetails(selected);render();saveSoon();
}
function fileScope(sourceId){
  if(fileScopeCache?.graph===graph&&fileScopeCache.sourceId===sourceId)return fileScopeCache.ids;
  const ids=new Set(graph.entities.filter(e=>e.source_ids.includes(sourceId)).map(e=>e.id)),parents=new Map();
  for(const m of graph.memberships){if(!parents.has(m.child_id))parents.set(m.child_id,[]);parents.get(m.child_id).push(m.parent_id);}
  const pending=[...ids];for(let i=0;i<pending.length;i++)for(const parent of parents.get(pending[i])||[])if(!ids.has(parent)){ids.add(parent);pending.push(parent);}
  fileScopeCache={graph,sourceId,ids};return ids;
}
function fileNodes(sourceId){
  if(!sourceId)return [];
  const ids=fileScope(sourceId);return sceneNodes.filter(n=>ids.has(n.entity.id)||(n.members||[]).some(id=>ids.has(id)));
}
function syncFileSelection(){
  const active=new Set(view.fileSelection?[view.fileSelection]:selected?.entity?.source_ids||[]);
  for(const button of $('#file-list').querySelectorAll('button[data-source-id]')){const on=active.has(button.dataset.sourceId);button.classList.toggle('active',on);button.setAttribute('aria-pressed',String(on));}
  const matches=new Set(fileNodes(view.fileSelection).map(n=>n.key));
  for(const [k,card] of nodeElements){card.classList.toggle('file-match',matches.has(k));card.classList.toggle('selected',selected?.key===k);}
}
function frameFile(sourceId){
  if(view.fileSelection!==sourceId)return;
  frameNodes(fileNodes(sourceId));
}
function frameNodes(nodes,options){
  // On narrow windows, leave the selected node visible instead of covering it
  // with a drawer. Its details remain available from the Details toggle.
  panelControls.closeDrawers();
  const keys=new Set(nodes.map(n=>n.key));
  camera.focus(()=>{
    const boxes=[...keys].flatMap(k=>{const p=layout().positions[k],card=nodeElements.get(k);return p&&card?[{x:p.x,y:p.y,width:card.offsetWidth,height:card.offsetHeight}]:[];});
    const canvas=$('#canvas');return CanvasCamera.frame(layout().viewport,boxes,{width:canvas.clientWidth,height:canvas.clientHeight});
  },options);
}
function openFile(source){
  const file=graph.entities.find(e=>e.kind==='file'&&e.source_ids.includes(source.id));
  if(!file)return;
  if(!fileNodes(source.id).length){
    remember();view.mode='structure';view.parent=graph.memberships.find(m=>m.axis==='physical'&&m.child_id===file.id)?.parent_id||rootId();$('#search').value='';
  }
  view.fileSelection=source.id;selected={key:file.id,entity:file,context:null,sourceSelection:true};selectedLink=null;
  panelControls.closeDrawers();panelControls.open('details');render();renderDetails(selected);
  frameFile(source.id);saveSoon();
}
function updateStatus(data){
  const c=data.coverage,counts=data.task_counts;
  $('#progress').max=Math.max(c.sources_total,1);$('#progress').value=c.sources_read;
  $('#progress-number').textContent=`${c.sources_read} / ${c.sources_total}`;
  const changed=payload?.snapshot?.state!=='current';
  $('#coverage').textContent=`源码已读 ${c.sources_read} / ${c.sources_total} · 待办 ${counts.queued+counts.blocked+counts.running}。${changed?'源码已变化，当前成果需要重新核对。':c.status==='complete'?'已保存完整梳理成果。':c.status==='coverage_with_unresolved_relations'?`全仓库阅读已覆盖，仍有 ${c.uncertain_relations.length} 条关系待确认。`:'当前是部分成果。扫描清单不计为阅读完成。'}`;
  $('#execution').textContent=data.execution==='paused'?'任务队列已暂停':data.execution==='worker_lease_active'?'阅读者已领取任务（有效期内）':!changed&&c.status==='complete'?'本次梳理已完成 · 当前无阅读任务在运行':!changed&&c.status==='coverage_with_unresolved_relations'?'阅读记录已保存 · 未确定关系可在拓扑与详情中核对':'有待办待继续 · 可在 Agent 面板启动阅读';
  if(data.expired_tasks.length)$('#execution').textContent='阅读者领取已过期 · 等待恢复';
}
function renderSidebar(){
  const query=$('#search').value.trim().toLocaleLowerCase();
  const matching=graph.sources.filter(s=>s.path.toLocaleLowerCase().includes(query));
  const list=$('#file-list'),existing=new Map([...list.querySelectorAll('button[data-source-id]')].map(button=>[button.dataset.sourceId,button])),items=[];
  $('#file-count').textContent=graph.sources.length+' 个文件';
  $('#file-shown').textContent=matching.length>200?'前 200 项':'';
  for(const s of matching.slice(0,200)){
    const file=graph.entities.find(e=>e.kind==='file'&&e.source_ids.includes(s.id));
    const roles=(file?.roles||[]).map(r=>roleLabels[r]).join(' · ');
    const caption=[s.language||s.category,roles,!s.included?'仅登记':s.read_state==='read'?'已读':'待阅读'].filter(Boolean).join(' · ');
    const button=existing.get(s.id)||elem('button',{'data-source-id':s.id});
    button.setAttribute('aria-label',`选择文件 ${s.path}`);button.title='单击定位并高亮，双击展开文件内部';
    button.onclick=()=>openFile(s);button.ondblclick=()=>{if(file){$('#search').value='';goTo(file.id);}};
    const contentKey=JSON.stringify([s.path,caption]);if(button.dataset.content!==contentKey){button.dataset.content=contentKey;button.replaceChildren(elem('span',{class:'file-kind',text:'▤'}),s.path,elem('small',{text:caption}));}
    items.push(button);
  }
  if(!matching.length)items.push(elem('p',{class:'muted',text:'没有匹配的文件。下方画布会同时搜索代码节点。'}));
  if(list.children.length!==items.length||items.some((item,index)=>list.children[index]!==item)){const scroll=list.scrollTop;list.replaceChildren(...items);list.scrollTop=scroll;}
  const inv=graph.inventory||{},content=$('#scope-content');content.replaceChildren();
  $('#scope-summary').textContent=`范围与读取限制（${(inv.gaps||[]).length} 个清单缺口）`;
  for(const entry of inv.excluded_paths||[])content.append(elem('p',{text:`排除：${entry.path} · ${entry.reason}`}));
  for(const gap of inv.gaps||[])content.append(elem('p',{class:'stale',text:`缺口：${gap.path} · ${gap.reason}`}));
  for(const s of graph.sources.filter(s=>s.reason))content.append(elem('p',{text:`${s.path} · ${s.reason}`}));
  for(const p of inv.empty_directories||[])content.append(elem('p',{text:`空目录：${p}`}));
  if(!content.children.length)content.append(elem('p',{text:'所有已枚举文件均在清单内；没有记录到跳过或失败。'}));
  updateStatus(payload.status);
  if(!$('#index-build').disabled){const s=payload.structure,states=s?.states||{};$('#index-progress').textContent=s?`${(states.parsed||0)+(states.partial||0)} / ${s.files_total} 个文件 · ${s.symbols} 个声明。职责与数据流仍需审阅。`:'尚未解析';}
  $('#task-list').replaceChildren(...graph.tasks.filter(t=>t.state!=='done').map(t=>elem('div',{class:'task-item'},elem('strong',{text:stateLabels[t.state]}),elem('div',{text:t.source_ids.map(s=>sources.get(s)?.path).join(', ')||'仓库职责与跨文件关系核对'}),elem('div',{text:t.reason||''}))));
}
function buildScene(){
  let nodes=[],edges=[];
  const query=$('#search').value.trim().toLocaleLowerCase();
  if(view.mode==='structure'){
    const items=query?graph.entities.filter(e=>`${e.name} ${pathFor(e)}`.toLocaleLowerCase().includes(query)):childrenOf(view.parent);
    nodes=items.map(e=>({key:e.id,entity:e,context:null}));
  }else if(view.mode==='workflow'){
    workflowScene=Workflow.project(graph,view.scope);({nodes,edges}=workflowScene);
  }else if(view.mode==='topology'){
    topologyScene=Topology.project(graph,{grouping:view.grouping,layer:view.topologyLayer,expanded:view.expandedGroups});
    ({nodes,edges}=topologyScene);
  }else if(scoped()){
    ({nodes,edges}=Blueprint.scopedData(graph,view.scope));
  }else{
    const instances=new Map();
    const add=(id,context)=>{const e=entities.get(id);if(!e)return null;const k=`${id}@${context}`;if(!instances.has(k))instances.set(k,{key:k,entity:e,context,columnHint:contextDepth(context)});return k;};
    for(const r of graph.relations){
      const data=['data','read','write'].includes(r.kind);
      if(view.layer==='data'&&!data||view.layer==='call'&&r.kind!=='call')continue;
      let from,to;
      if(data){const a=ports.get(r.from_id),b=ports.get(r.to_id);from=add(a.entity_id,a.context_id);to=add(b.entity_id,b.context_id);}
      else{const c=contexts.get(r.context_id);from=add(r.from_id,r.kind==='call'?(c.parent_id||c.id):c.id);to=add(r.to_id,c.id);}
      const annotation=data?(r.kind==='write'?'写回':contexts.get(ports.get(r.from_id).context_id)?.parent_id===ports.get(r.to_id).context_id?'返回':''):'';
      if(from&&to)edges.push({id:r.id,record:r,records:[r],from,to,data,annotation,fromPort:data?ports.get(r.from_id):null,toPort:data?ports.get(r.to_id):null});
    }
    nodes=[...instances.values()];
  }
  if(view.mode!=='structure'&&query){const matching=new Set(nodes.filter(n=>`${n.entity.name} ${pathFor(n.entity)}`.toLocaleLowerCase().includes(query)).map(n=>n.key));const neighbors=new Set(matching);edges.forEach(e=>{if(matching.has(e.from)||matching.has(e.to)){neighbors.add(e.from);neighbors.add(e.to);}});nodes=nodes.filter(n=>neighbors.has(n.key));edges=edges.filter(e=>neighbors.has(e.from)&&neighbors.has(e.to));}
  const total=nodes.length;
  if(total>150&&view.fileSelection){const ids=fileScope(view.fileSelection),match=n=>ids.has(n.entity.id)||(n.members||[]).some(id=>ids.has(id));nodes=[...nodes.filter(match),...nodes.filter(n=>!match(n))];}
  nodes=nodes.slice(0,150);const keys=new Set(nodes.map(n=>n.key));edges=edges.filter(e=>keys.has(e.from)&&keys.has(e.to));
  const decorated=Workflow.decorate({nodes,edges,...(view.mode==='workflow'?{scopeId:view.scope,entry:workflowScene.entry,inputs:workflowScene.inputs}:{})},workflowTasks);
  sceneNodes=decorated.nodes;sceneEdges=decorated.edges;if(view.mode==='workflow')workflowScene={...workflowScene,...decorated};
  const underlying=new Set(edges.flatMap(e=>e.records.map(r=>r.id))).size;
  $('#canvas-info').textContent=`${scoped()?'本层 · ':''}${nodes.length} 个节点 · ${edges.length} 条${scoped()?'连线':'关系'}${scoped()&&underlying!==edges.length?`（${underlying} 条依据关系）`:''}${total>150?' · 前 150 个，请缩小范围':''}`;
  if(view.mode==='topology')$('#canvas-info').textContent+=` · ${nodes.reduce((sum,n)=>sum+n.internalRelations.length,0)} 条内部关系已折叠`;
  if(view.mode==='workflow')$('#canvas-info').textContent=`本层 · ${nodes.filter(n=>n.role==='workflow-input').length} 个输入 · ${edges.filter(e=>e.data).length} 条数据连线（${underlying} 条依据） · 灰线表示流程归属${total>150?' · 前 150 个节点，请缩小范围':''}`;
}
function renderBreadcrumbs(){
  const target=$('#breadcrumbs');target.replaceChildren();
  if(view.mode==='topology'){target.append(elem('span',{text:'全仓库 · 拓扑总览'}));return;}
  if(view.mode==='relations'||view.mode==='workflow'){
    if(view.mode!=='workflow'&&!scoped()){target.append(elem('span',{text:'关系核对 · 全部上下文'}));return;}
    const trail=[];let c=contexts.get(view.scope);while(c){trail.unshift(c);c=contexts.get(c.parent_id);}
    for(const c of trail){if(target.children.length)target.append(elem('span',{class:'muted',text:'›'}));const ev=graph.evidence.find(e=>e.id===c.callsite_evidence_id);target.append(elem('button',{text:(entities.get(Blueprint.contextOwner(graph,c.id))?.name||'数据流')+(ev?` :${ev.start_line}`:''),title:contextLabel(c.id),onclick:()=>{if(c.id!==view.scope)enterContext(c.id);}}));}return;
  }
  const trail=[],visited=new Set();let cursor=view.parent;
  while(cursor&&!visited.has(cursor)){visited.add(cursor);const e=entities.get(cursor);if(e)trail.unshift(e);cursor=graph.memberships.find(m=>m.child_id===cursor)?.parent_id;}
  if(trail[0]?.id!==rootId())trail.unshift(entities.get(rootId()));
  for(const e of trail.filter(Boolean)){if(target.children.length)target.append(elem('span',{class:'muted',text:'/'}));target.append(elem('button',{text:e.name,onclick:()=>goTo(e.id)}));}
}
function renderFlows(){
  const strip=$('#flows');strip.replaceChildren();strip.hidden=!flowView();
  if(strip.hidden)return;
  const displayed=new Set([...sceneEdges,...sceneNodes].flatMap(e=>e.taskIds));
  const records=new Map(sceneEdges.filter(e=>!e.association).flatMap(e=>e.records).map(r=>[r.id,r]));
  const pending=[...records.values()].filter(r=>r.basis!=='source'||r.freshness==='stale').length;
  const heading=elem('div',{class:'legend-heading'},elem('span',{class:'legend-caption',text:'任务颜色 · 可多选',title:'同色属于同一任务。悬停名称查看用途，点击查看任务详情。'}));
  if(pending)heading.append(elem('span',{class:'legend-review',text:`${pending} 条待核对`,title:'当前展示的关系中，有源码已变化或尚未确认的依据。点连线查看具体状态；线条和箭头只表示传递方向。'}));
  strip.append(heading);
  strip.append(elem('button',{text:'全部任务',class:view.taskFocus.length||view.focus.length?'':'active','aria-pressed':String(!view.taskFocus.length&&!view.focus.length),onclick:()=>{view.taskFocus=[];view.focus=[];render();saveSoon();}}));
  for(const task of workflowTasks.list.filter(t=>displayed.has(t.id))){
    const b=elem('button',{class:'task-chip'+(view.taskFocus.includes(task.id)?' active':''),'aria-pressed':String(view.taskFocus.includes(task.id)),title:`${task.label}\n${task.purpose}\n点击高亮这个任务的所有线路`,'aria-label':`高亮任务 ${task.label}`,'aria-description':task.purpose,onclick:()=>focusTask(task.id,true)},elem('span',{class:'flow-dot'}),elem('span',{class:'task-copy',text:task.label}));
    b.style.setProperty('--flow',taskColor(task.id));strip.append(b);
  }
  if(sceneEdges.some(e=>!e.association&&!e.taskIds.length))strip.append(elem('span',{class:'unassigned-task',text:'灰色 · 未归属任务',title:'尚未记录明确的流程归属，不能仅凭函数或数据相同自动合并任务。'}));
  if(view.focus.length)strip.append(elem('div',{class:'data-trace'},elem('span',{text:'正在追踪数据：'+graph.flows.filter(f=>view.focus.includes(f.id)).map(f=>f.name).join('、')}),elem('button',{text:'恢复任务全部线路',onclick:()=>{view.focus=[];applyFlowFocus();}})));
}
function contextLabel(id){const trail=[],seen=new Set();let c=contexts.get(id);while(c&&!seen.has(c.id)){seen.add(c.id);const ev=graph.evidence.find(e=>e.id===c.callsite_evidence_id);if(ev)trail.unshift(`${sources.get(ev.source_id)?.path}:${ev.start_line}`);c=contexts.get(c.parent_id);}return trail.join(' › ')||'独立分析上下文';}
function contextDepth(id){let depth=0,c=contexts.get(id);const seen=new Set();while(c?.parent_id&&!seen.has(c.id)){seen.add(c.id);depth++;c=contexts.get(c.parent_id);}return depth;}
function renderFlowNavigation(){
  $('#flow-navigation').hidden=view.mode!=='relations'||view.layer!=='data';$('#presentation').value=view.presentation;$('#scope-label').hidden=!scoped();
  const choices=Blueprint.scopeChoices(graph);$('#scope').replaceChildren(...choices.map(c=>elem('option',{value:c.id,text:c.entity.name})));
  let c=contexts.get(view.scope);while(c?.parent_id)c=contexts.get(c.parent_id);$('#scope').value=c?.id||'';
  $('#flow-guide').textContent=scoped()?(sceneEdges.some(e=>e.records.length>1)?'同一数据的中转已合并 · 点开调用查看内部':'本层数据与边界 · 沿面包屑返回调用方'):'返回与写回可能向左 · 箭头表示数据方向';
}
function renderWorkflowNavigation(){
  const active=view.mode==='workflow';$('#workflow-navigation').hidden=!active;if(!active)return;
  const choices=Workflow.choices(graph);$('#workflow-scope').replaceChildren(...choices.map(c=>elem('option',{value:c.id,text:c.label})));
  let root=contexts.get(view.scope);while(root?.parent_id)root=contexts.get(root.parent_id);$('#workflow-scope').value=root?.id||'';
  const names=graph.flows.filter(f=>view.focus.includes(f.id)).map(f=>f.name);
  $('#workflow-guide').textContent=names.length?'追踪 '+names.join('、')+' 及其派生结果':`已记录 ${workflowScene?.inputs.length||0} 个输入 · 点入口查看整体，点输入追踪数据`;
  $('#workflow-whole').disabled=!view.scope;
}
function renderTopology(){
  $('#topology-navigation').hidden=view.mode!=='topology';if(view.mode!=='topology')return;
  $('#topology-grouping').value=view.grouping;$('#topology-layer').value=view.topologyLayer;$('#topology-collapse').disabled=!view.expandedGroups.length;
  const s=Topology.shape(sceneNodes,sceneEdges),a=topologyAnalysis;
  let explanation=view.topologyLayer==='call'?`已记录调用中有 ${a.callCycles.length} 组可能递归。`:
    view.topologyLayer==='control'?(a.controlEdges?`已记录 ${a.controlEdges} 条控制关系，含 ${a.controlCycles.length} 个控制环；是否执行取决于运行条件。`:'尚未记录控制流；不能据此判断代码没有循环。'):
    view.topologyLayer==='data'?`已记录 ${a.counts.return||0} 条返回、${a.counts.write_back||0} 条写回；数据往返不代表控制循环。`:'引用、继承、接口实现与事件关系；归组不产生新的源码依赖。';
  $('#topology-summary').textContent=`当前视图：${s.forks.length} 处分流 · ${s.joins.length} 处汇入 · ${s.isolated.length} 个未连通节点。${explanation}${a.uncertain?` ${a.uncertain} 条候选或过期关系不计入循环判断。`:''}`;
}
function arrangeNodes(reset=false){
  const dimensions=Object.fromEntries([...nodeElements].map(([k,e])=>[k,{width:e.offsetWidth,height:e.offsetHeight}]));
  const positions=view.mode==='workflow'?Workflow.arrange({...workflowScene,nodes:sceneNodes,edges:sceneEdges},dimensions,layout().positions,reset):Blueprint.arrange(sceneNodes,sceneEdges,dimensions,layout().positions,reset);Object.assign(layout().positions,positions);
  for(const [k,card] of nodeElements){card.style.left=positions[k].x+'px';card.style.top=positions[k].y+'px';}
}
function syncCanvasControls(){
  $('#arrange').disabled=!sceneNodes.length;$('#undo-arrange').hidden=!layout().beforeArrange;
  const motion=$('#flow-motion');motion.hidden=!flowView()||!sceneEdges.some(e=>e.data);
  motion.textContent=view.flowMotion?'暂停流动':'开启流动';motion.setAttribute('aria-pressed',String(!!view.flowMotion));
  $('#world').classList.toggle('motion-off',!view.flowMotion);
  if(view.flowMotion&&!document.hidden)$('#wires').unpauseAnimations();else $('#wires').pauseAnimations();
}
function render(){
  if(!graph)return;
  camera.cancel();const generation=++renderGeneration;
  if(!selected&&!selectedLink&&view.fileSelection){const file=graph.entities.find(e=>e.kind==='file'&&e.source_ids.includes(view.fileSelection));if(file){selected={key:file.id,entity:file,context:null,sourceSelection:true};renderDetails(selected);}}
  if(!selected&&!selectedLink){sourceGeneration++;$('#details').replaceChildren(elem('div',{class:'placeholder'},'选择一个节点或连线',elem('br'),elem('small',{text:'查看职责、数据去向与源码依据'})));}
  document.querySelectorAll('.view-tabs button').forEach(b=>b.classList.toggle('active',b.dataset.mode===view.mode));
  $('#layer-label').hidden=view.mode!=='relations';$('#layer').value=view.layer;$('#back').disabled=!view.history.length;
  renderSidebar();buildScene();renderBreadcrumbs();renderFlows();renderFlowNavigation();renderWorkflowNavigation();renderTopology();syncCanvasControls();
  const container=$('#nodes');container.replaceChildren();nodeElements=new Map();portElements=new Map();
  const positions=layout().positions,firstLayout=!Object.keys(positions).length;
  const focus=sceneFocus();
  const rows=new Map();
  sceneNodes.forEach((n,index)=>{
    const column=view.mode==='relations'?contextDepth(n.context):index%3;
    const row=view.mode==='relations'?(rows.get(column)||0):Math.floor(index/3);rows.set(column,row+1);
    const e=n.entity,workflowRole=n.role==='workflow-entry'||n.role==='workflow-input',fam=workflowRole?'workflow':family(e),position=positions[n.key]||(view.mode==='structure'?(positions[n.key]={x:column*320,y:row*370,locked:false}):{x:0,y:0,locked:false});
    const card=elem('article',{class:'node','data-family':fam,'data-view-role':n.role,'data-node-key':n.key,'aria-label':`${n.role==='workflow-entry'?'流程入口':n.role==='workflow-input'?'流程输入':labels[e.kind]} ${n.title||e.name}${n.role==='call'?' '+contextLabel(n.context):''}`,tabindex:0});
    card.style.left=position.x+'px';card.style.top=position.y+'px';
    card.classList.toggle('selected',selected?.key===n.key);card.classList.toggle('dim',flowView()&&focus.active&&!focus.nodes.has(n.key));
    const lock=elem('button',{class:'node-lock','aria-label':`${position.locked?'解锁':'锁定'} ${n.title||e.name}`,text:position.locked?'▣':'▫',onclick:event=>{event.stopPropagation();positions[n.key].locked=!positions[n.key].locked;render();saveSoon();}});
    card.append(elem('div',{class:'node-head'},elem('span',{class:'node-icon',text:{input:'↦',output:'↤',source:'●','workflow-entry':'◉','workflow-input':'●'}[n.role]||icons[fam]}),elem('span',{class:'node-title',text:n.title||e.name,title:n.title||e.name}),lock));
    card.append(elem('div',{class:'node-subtitle'},elem('span',{text:n.role==='call'?(view.mode==='workflow'?'子流程':'调用接口'):{input:'输入边界',output:'结果边界',source:'本层数据',body:'函数内部','workflow-entry':'流程归组','workflow-input':n.summary}[n.role]||[labels[e.kind],...e.roles.map(r=>roleLabels[r])].join(' · ')}),elem('span',{text:state(e),class:e.freshness==='stale'?'stale':''})));
    card.append(elem('p',{class:'node-summary',text:n.summary||e.summary||e.structure?.signature||'已定位代码位置，等待阅读并整理职责。'}));
    if(e.structure)card.append(elem('div',{class:'node-context',text:`${pathFor(e)}:${e.structure.start_line}–${e.structure.end_line}`}));
    if(n.members)card.append(elem('div',{class:'node-topology',text:`${n.members.length} 个实体 · ${n.internalRelations.length} 条内部关系${n.ambiguous?' · 多重归属保留原节点':''}`}));
    if(n.context&&!['source','input','output','workflow-entry','workflow-input'].includes(n.role))card.append(elem('div',{class:'node-context',text:contextLabel(n.context),title:n.context}));
    card.style.setProperty('--flow',taskPaint(n.taskIds));
    if(n.taskIds.length>1){const badges=elem('div',{class:'node-tasks'});for(const id of n.taskIds){const b=elem('button',{title:`参与任务：${taskNames([id])}`,onclick:event=>{event.stopPropagation();focusTask(id);}},elem('span',{class:'flow-dot'}),taskNames([id]));b.style.setProperty('--flow',taskColor(id));badges.append(b);}card.append(badges);}
    if((n.context||n.ports?.length)&&(view.mode==='workflow'||(view.mode==='topology'?view.topologyLayer==='data':view.layer!=='call'))){
      const ownerPorts=n.ports||graph.ports.filter(p=>p.entity_id===e.id&&p.context_id===n.context&&p.channel==='data').map(p=>({...p,annotation:sceneEdges.find(edge=>edge.fromPort?.id===p.id&&edge.annotation)?.annotation||''}));
      const row=elem('div',{class:'ports'});
      if(!ownerPorts.some(p=>p.direction==='in')||!ownerPorts.some(p=>p.direction==='out'))row.classList.add('single-side');
      for(const direction of ['in','out']){const col=elem('div',{class:'port-col'});for(const p of ownerPorts.filter(p=>p.direction===direction)){
        const f=graph.flows.find(f=>f.id===p.flow_id||f.producer_port_id===p.id)||graph.flows.find(f=>graph.relations.some(r=>r.flow_id===f.id&&(r.from_id===p.id||r.to_id===p.id)));
        const portName=p.annotation&&p.name.startsWith(p.annotation+' ')?p.name.slice(p.annotation.length+1):p.name;
        const taskIds=p.taskIds||[workflowTasks.byContext.get(p.context_id)].filter(Boolean);
        const pin=elem('i',{class:'pin'});const line=elem('div',{class:`port ${direction}`,title:`任务：${taskNames(taskIds)}\n${p.name}: ${p.type}${f?' · '+f.value_version+' · 数据编号 '+f.color_key:''}`},pin,elem('span',{text:portName}));if(p.annotation)line.append(elem('small',{class:'port-annotation',text:p.annotation}));line.style.setProperty('--flow',taskPaint(taskIds));col.append(line);portElements.set(p.id,pin);
        if(f){line.setAttribute('role','button');line.tabIndex=0;line.setAttribute('aria-label',`追踪数据 ${f.name} · ${p.name}`);const follow=event=>{event.stopPropagation();if(n.role==='workflow-input')selectNode(n);else traceData(f,taskIds);};line.addEventListener('click',follow);line.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();follow(event);}});}
      }if(col.children.length||!row.classList.contains('single-side'))row.append(col);}if(ownerPorts.length)card.append(row);
    }
    if(n.expandContext)card.append(elem('button',{class:'node-enter',text:view.mode==='workflow'?'展开子流程 ↗':'展开调用内部 ↗','aria-label':`展开 ${e.name} ${contextLabel(n.context)} 的内部`,onclick:event=>{event.stopPropagation();enterContext(n.expandContext);}}));
    const kids=workflowRole?[]:childrenOf(e.id);
    if(n.canExpand)card.append(elem('button',{class:'node-enter',text:'展开组内拓扑 ↗',onclick:event=>{event.stopPropagation();expandGroup(e.id);}}));
    else if(kids.length)card.append(elem('button',{class:'node-enter',text:`展开 ${kids.length} 个节点 ↗`,onclick:event=>{event.stopPropagation();goTo(e.id);}}));
    card.addEventListener('click',event=>{if(card.dataset.moved==='true'){card.dataset.moved='false';return;}if(event.detail>1)return;selectNode(n,{delay:event.detail?240:0});});
    card.addEventListener('dblclick',()=>n.canExpand?expandGroup(e.id):n.expandContext?enterContext(n.expandContext):kids.length?goTo(e.id):selectNode(n));
    card.addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target===card)selectNode(n);});
    card.querySelector('.node-head').addEventListener('pointerdown',event=>startNodeDrag(event,n,card));
    container.append(card);nodeElements.set(n.key,card);
  });
  $('#empty').hidden=!!sceneNodes.length;
  $('#empty').textContent=$('#search').value?'没有匹配的代码节点。':view.mode==='workflow'?'尚未整理流程。阅读者记录入口与调用上下文后，会显示在这里；仓库文件仍可从左侧查看。':view.mode==='relations'?'还没有保存这类关系。阅读者提交带源码依据的调用或数据流后，会显示在这里。':'这一层尚未整理子节点。可从左侧打开文件核对源码，再让阅读者继续拆解。';
  if(view.mode!=='structure')arrangeNodes();
  syncFileSelection();applyViewport();requestAnimationFrame(()=>{if(generation!==renderGeneration)return;drawWires();if(firstLayout&&sceneNodes.length&&!camera.active)fit();});
}
function selectNode(n,options){selected=n;selectedLink=null;view.fileSelection=null;if(n.role==='workflow-entry'||n.role==='workflow-input'){view.taskFocus=n.taskIds;view.focus=n.flowId?[n.flowId]:[];applyFlowFocus();}syncFileSelection();renderDetails(n);panelControls.open('details');frameNodes([n],options);}
function renderTaskDetails(task){
  if(!task)return;
  selected={key:'task:'+task.id,taskId:task.id};selectedLink=null;view.fileSelection=null;syncFileSelection();sourceGeneration++;
  const generation=sourceGeneration,details=$('#details'),sourceBox=elem('div');details.replaceChildren();
  for(const card of nodeElements.values())card.classList.remove('selected');
  const heading=elem('div',{class:'detail-title task-heading'},elem('span',{class:'flow-dot'}),task.label);heading.style.setProperty('--flow',taskColor(task.id));details.append(heading);
  details.append(elem('p',{class:'detail-summary',text:task.purpose}),elem('p',{class:'source-caption',text:'这个颜色表示本任务及其子流程。不同输入、计算结果和返回值仍各有名称与数据身份。'}));
  if(task.entity.freshness==='stale')details.append(elem('p',{class:'stale',text:'入口源码已变化，用途说明需要重新核对。'}));
  details.append(elem('button',{class:'evidence-button',text:'打开这个任务的流程入口',onclick:()=>{view.mode='workflow';enterContext(task.id);view.taskFocus=[task.id];view.focus=[];if(workflowScene.entry)selectNode(workflowScene.entry);fit();saveSoon();}}));
  const proofs=elem('section',{class:'detail-section'},elem('h3',{text:'用途依据 · 入口职责'}));
  for(const id of task.entity.evidence_ids||[]){const ev=graph.evidence.find(e=>e.id===id),source=sources.get(ev.source_id);proofs.append(elem('button',{class:'evidence-button',text:`${source.path}:${ev.start_line}–${ev.end_line} · ${ev.note}`,onclick:()=>showSource(sourceBox,ev.source_id,Math.max(1,ev.start_line-2),ev,generation)}));}
  proofs.append(sourceBox);details.append(proofs);panelControls.open('details');
}
function renderWorkflowDetails(n,details,generation){
  details.append(elem('div',{class:'detail-title',text:n.title}));
  details.append(elem('p',{class:'source-caption',text:'所属任务：'+taskNames(n.taskIds)}));
  const flow=graph.flows.find(f=>f.id===n.flowId),sourceBox=elem('div');
  if(flow){
    const origin=n.origin==='boundary'?entities.get(contexts.get(n.context)?.caller_id)?.name||'调用方':n.entity.name;
    details.append(elem('p',{class:'detail-summary',text:`数据版本：${flow.value_version} · 编号 ${flow.color_key}\n来源：${n.summary}，${origin}。颜色表示任务，输入名称区分数据。`}));
    if(n.entity.freshness==='stale'||n.records.some(r=>r.freshness==='stale'))details.append(elem('p',{class:'stale',text:'源码已变化，这份数据的依据需要重新核对。'}));
    if(n.records.some(r=>r.basis!=='source'))details.append(elem('p',{class:'stale',text:'部分传递关系仍待核对，沿线查看具体依据。'}));
    const derived=Workflow.trace(graph,[flow.id]);derived.delete(flow.id);
    details.append(elem('section',{class:'detail-section'},elem('h3',{text:'参与产生的后续结果'}),elem('p',{class:'source-caption',text:graph.flows.filter(f=>derived.has(f.id)).map(f=>f.name).join('、')||'当前尚未记录派生结果。'})));
    const ids=new Set([...(flow.evidence_ids||[]),...n.records.flatMap(r=>r.evidence_ids||[])]);
    for(const id of ids){const ev=graph.evidence.find(e=>e.id===id),s=sources.get(ev.source_id);details.append(elem('button',{class:'evidence-button',text:`${s.path}:${ev.start_line}–${ev.end_line} · ${ev.note}`,onclick:()=>showSource(sourceBox,ev.source_id,Math.max(1,ev.start_line-2),ev,generation)}));}
  }else{
    details.append(elem('p',{class:'detail-summary',text:`${n.entity.summary||n.entity.name}\n\n这个入口把本层输入组织在一起。灰色虚线表示流程归属；实际传递由彩色连线表达。`}));
    details.append(elem('section',{class:'detail-section'},elem('h3',{text:`本层输入 · ${workflowScene.inputs.length}`} ),...workflowScene.inputs.map(input=>elem('button',{class:'evidence-button',text:input.title,onclick:()=>selectNode(input)}))));
    details.append(elem('p',{class:'source-caption',text:'展开子流程可继续追踪；不同调用位置各自保留输入与结果。'}));
  }
  details.append(sourceBox);
}
function renderDetails(n){
  sourceGeneration++;const generation=sourceGeneration,e=n.entity,details=$('#details');details.replaceChildren();
  if(n.role==='workflow-entry'||n.role==='workflow-input'){renderWorkflowDetails(n,details,generation);return;}
  details.append(elem('div',{class:'detail-title',text:e.name}));
  const tags=elem('div',{class:'detail-tags'},...([labels[e.kind],e.language,state(e),...e.roles.map(r=>roleLabels[r])].filter(Boolean).map(text=>elem('span',{class:'badge',text}))));details.append(tags);
  if(e.kind==='file')details.append(elem('button',{class:'evidence-button',text:'展开文件内部 ↗',onclick:()=>{$('#search').value='';goTo(e.id);}}));
  if(n.context)details.append(elem('p',{class:'source-caption',text:'当前调用：'+contextLabel(n.context)}));
  if(n.role&&n.role!=='group')details.append(elem('p',{class:'source-caption',text:`当前显示：${n.title||'调用接口'}。${['input','output','source'].includes(n.role)?'这是画布中的数据边界，下方为所属函数的源码说明。':'输入输出只显示当前层的连接。'}`}));
  if(n.members){details.append(elem('p',{class:'source-caption',text:`聚合 ${n.members.length} 个源码实体；${n.internalRelations.length} 条内部关系保留在组内。${n.ambiguous?'多重归属没有被任意归到一个组。':''}`}));
    if(n.canExpand)details.append(elem('button',{class:'evidence-button',text:'展开组内拓扑 ↗',onclick:()=>expandGroup(e.id)}));
    if(n.internalRelations.length)details.append(elem('section',{class:'detail-section'},elem('h3',{text:'折叠的内部关系'}),...n.internalRelations.slice(0,100).map(r=>elem('button',{class:'evidence-button',text:`${r.kind} · ${r.id}`,onclick:()=>renderWireDetails({id:r.id,record:r,records:[r],internal:true})}))));
  }
  if(n.expandContext)details.append(elem('button',{class:'evidence-button',text:'展开这次调用的内部数据流 ↗',onclick:()=>enterContext(n.expandContext)}));
  details.append(elem('p',{class:'detail-summary',text:e.summary||'已在仓库中定位到此节点。职责、调用和数据关系仍等待实际阅读。'}));
  if(e.structure){const s=e.structure;details.append(elem('section',{class:'detail-section'},elem('h3',{text:'声明与参数'}),elem('pre',{class:'syntax-signature',text:s.signature}),...s.parameters.map(p=>elem('p',{class:'source-caption',text:p.text||[p.name,p.type].filter(Boolean).join(': ')})),elem('p',{class:'source-caption',text:s.state==='partial'?'此文件有语法解析缺口，需要结合源码核对。':'由本地语法解析器提取；不表示已经审阅其行为。'})));}
  for(const [key,label] of Object.entries({inputs:'输入',outputs:'输出',calls:'调用',reads:'读取',writes:'写入',conditions:'条件与分支'}))if(e.details?.[key]){
    const items=e.details[key];details.append(elem('section',{class:'detail-section'},elem('h3',{text:label}),items.length?elem('ul',{},...items.map(x=>elem('li',{text:typeof x==='string'?x:JSON.stringify(x)}))):elem('p',{class:'source-caption',text:'未记录此项；以职责说明及源码为准。'})));
  }
  const proofSection=elem('section',{class:'detail-section'},elem('h3',{text:'源码依据'})),sourceBox=elem('div');
  const evidence=graph.evidence.filter(ev=>e.evidence_ids.includes(ev.id));
  for(const ev of evidence){const s=sources.get(ev.source_id);proofSection.append(elem('button',{class:'evidence-button',text:`${s.path}:${ev.start_line}–${ev.end_line}\n${ev.note}`,onclick:()=>showSource(sourceBox,ev.source_id,Math.max(1,ev.start_line-2),ev,generation)}));}
  if(!evidence.length)for(const id of e.source_ids){const s=sources.get(id),anchor=e.structure?.source_id===id?e.structure:null;proofSection.append(elem('button',{class:'evidence-button',text:`打开 ${s.path}${anchor?':'+anchor.start_line:''}`,onclick:()=>showSource(sourceBox,id,Math.max(1,(anchor?.start_line||1)-2),anchor,generation)}));}
  if(!evidence.length&&!e.source_ids.length)proofSection.append(elem('p',{class:'source-caption',text:'容器节点尚无职责依据。可展开到文件。'}));
  proofSection.append(sourceBox);details.append(proofSection);
  const question=elem('textarea',{class:'question',placeholder:'例如：这个数据最后被谁使用？','aria-label':'节点问题'});
  const output=elem('pre',{class:'copy-context',hidden:true});
  const button=elem('button',{class:'copy-button',text:'复制问题与节点上下文',onclick:async()=>{
    const text=`请继续梳理仓库 ${graph.project.name}。\n仓库位置：${graph.project.source_root}\n工程 ID：${graph.project.id}\n图谱版本：${graph.project.revision}\n节点：${e.id}（${e.name}）\n调用上下文：${n.context||'未指定'}\n源码：${pathFor(e)}\n问题：${question.value||'继续拆解此节点的职责、调用与数据去向。'}\n保留全仓库剩余队列，按 repository-blueprint Skill 提交有源码依据的批次。`;
    output.textContent=text;output.hidden=false;try{await navigator.clipboard.writeText(text);button.textContent='已复制，可交给当前 AI 会话';}catch{button.textContent='上下文已列出，可选择复制';}
  }});
  const agentActions=elem('div',{class:'agent-node-actions'},
    elem('button',{text:'让 Agent 回答',onclick:()=>workbench.submitSelection(n,question.value||'请结合源码说明这个节点的职责、输入和数据去向。','question')}),
    elem('button',{text:'继续拆解',onclick:()=>workbench.submitSelection(n,question.value||'继续拆解这个节点的调用、输入输出、成员读写和分支，补充有源码依据的流程。','analyze')}));
  details.append(elem('section',{class:'detail-section'},elem('h3',{text:'继续分析这个节点'}),question,agentActions,button,elem('p',{class:'source-caption',text:'Agent 请求会保存到工程，可查看进度、暂停和继续；复制上下文仍可用于其他会话。'}),output));
}
function highlightSource(data){
  return new Promise(resolve=>{
    let worker,timer;const finish=result=>{clearTimeout(timer);worker?.terminate();resolve(result);};
    try{
      worker=new Worker('/source-highlight.js');timer=setTimeout(()=>finish(null),4000);
      worker.onmessage=event=>finish(event.data);worker.onerror=event=>{event.preventDefault();finish(null);};worker.postMessage(data);
    }catch{finish(null);}
  });
}
async function showSource(target,id,start,proof,generation){
  const request=Symbol();target.sourceRequest=request;
  const current=()=>generation===sourceGeneration&&target.sourceRequest===request;
  target.replaceChildren(elem('p',{class:'source-caption',text:'正在核对源码版本…'}));
  try{const data=await api(`/api/source?id=${encodeURIComponent(id)}&start=${start}&limit=80&context=1`);if(!current())return;
    const box=elem('div',{class:'source',tabindex:0,'aria-label':'源码，可横向滚动'}),codes=[];
    for(const line of data.lines){
      const code=elem('code',{text:line.text});codes.push(code);
      box.append(elem('div',{class:'source-line'+(proof&&line.number>=proof.start_line&&line.number<=proof.end_line?' highlight':'')},elem('b',{text:line.number}),code));
    }
    const languageNames={cpp:'C / C++',csharp:'C#',python:'Python',typescript:'TypeScript',javascript:'JavaScript',xml:'HTML / XML',json:'JSON'};
    const colorState=elem('span',{text:'正在配色…'}),language=elem('span',{class:'badge',text:languageNames[data.language]||data.language||'文本'});
    target.replaceChildren(elem('div',{class:'source-language'},language,colorState),elem('p',{class:'source-caption',text:`${data.path} · ${data.start_line}–${data.end_line} / ${data.total_lines} 行 · 与保存版本一致`}),box);
    if(data.next_start)target.append(elem('button',{class:'evidence-button',text:'读取后续源码',onclick:()=>showSource(target,id,data.next_start,proof,generation)}));
    const highlighted=await highlightSource(data);if(!current())return;
    if(highlighted?.language&&highlighted.lines.length===codes.length){
      for(let i=0;i<codes.length;i++){
        // Only the bundled highlighter's escaped markup enters HTML, never raw source.
        codes[i].innerHTML=highlighted.lines[i];
        if(codes[i].textContent!==data.lines[i].text)codes[i].textContent=data.lines[i].text;
      }
      language.textContent=languageNames[highlighted.language]||highlighted.language;colorState.textContent='语法高亮';
    }else colorState.textContent='纯文本';
  }catch(error){if(current())target.replaceChildren(elem('p',{class:'stale',text:'源码暂不可作为当前依据：'+error.message}));}
}
function wireTipScale(){return 1/Math.max(layout().viewport.z,.5);}
function applyViewport(){const v=layout().viewport;$('#world').style.transform=`translate(${v.x}px,${v.y}px) scale(${v.z})`;$('#zoom-level').value=Math.round(v.z*100)+'%';$('#canvas').style.backgroundPosition=`${v.x}px ${v.y}px`;$('#canvas').style.backgroundSize=`${22*v.z}px ${22*v.z}px`;for(const tip of $('#wires').querySelectorAll('.wire-tip'))tip.setAttribute('transform',`scale(${wireTipScale()})`);drawMinimap();}
function portPoint(id){const p=portElements.get(id);if(!p)return null;const r=p.getBoundingClientRect(),c=$('#canvas').getBoundingClientRect(),v=layout().viewport;return{x:(r.x+r.width/2-c.x-v.x)/v.z,y:(r.y+r.height/2-c.y-v.y)/v.z};}
function cardPoint(key,out,edgeId){
  const e=nodeElements.get(key),p=layout().positions[key];
  const neighbors=sceneEdges.filter(edge=>!edge.data&&(out?edge.from:edge.to)===key),index=neighbors.findIndex(edge=>edge.id===edgeId);
  const span=e.dataset.viewRole==='workflow-entry'?0:Math.min(Math.max(0,e.offsetHeight-64),Math.max(0,neighbors.length-1)*18);
  const offset=neighbors.length>1&&index>=0?span*(index/(neighbors.length-1)-.5):0;
  return{x:p.x+(out?e.offsetWidth:0),y:p.y+e.offsetHeight/2+offset};
}
function relationLabel(kind){return {data:'数据传递',read:'读取',write:'写回',call:'调用',control:'控制流',reference:'引用',inherit:'继承',implements:'接口实现',event:'事件'}[kind]||kind;}
function requestWires(){cancelAnimationFrame(wireFrame);wireFrame=requestAnimationFrame(drawWires);}
function renderWireDetails(edge){
  selected=null;selectedLink=edge;view.fileSelection=null;syncFileSelection();sourceGeneration++;const generation=sourceGeneration,details=$('#details');details.replaceChildren();
  for(const card of nodeElements.values())card.classList.remove('selected');
  const taskIds=edge.taskIds||[...new Set(edge.records.map(r=>workflowTasks.byContext.get(r.context_id)).filter(Boolean))];
  for(const id of taskIds){const task=workflowTasks.byId.get(id),b=elem('button',{class:'evidence-button task-heading',title:task.purpose,onclick:()=>focusTask(id)},elem('span',{class:'flow-dot'}),'任务：'+task.label);b.style.setProperty('--flow',taskColor(id));details.append(b,elem('p',{class:'source-caption',text:task.purpose}));}
  const f=graph.flows.find(f=>f.id===edge.record.flow_id);
  details.append(elem('div',{class:'detail-title',text:f?f.name:({call:'调用关系',control:'控制关系',reference:'引用关系',inherit:'继承关系',implements:'接口实现',event:'事件关系'}[edge.record.kind]||'源码关系')}));
  if(f){details.append(elem('p',{class:'source-caption',text:`数据版本：${f.value_version} · 数据编号 ${f.color_key}`}));if(f.derived_from.length)details.append(elem('p',{class:'source-caption',text:'由这些值产生：'+f.derived_from.map(id=>graph.flows.find(f=>f.id===id).name).join('、')}));details.append(elem('button',{class:'evidence-button',text:'仅追踪这份数据',onclick:()=>traceData(f,taskIds)}));}
  details.append(elem('p',{class:'detail-summary',text:edge.aggregated?`当前归组聚合了 ${edge.records.length} 条同类型、同数据身份与调用上下文的关系。原始端点与依据保留如下。`:edge.records.length>1?`同一数据经 ${edge.viaOwner} 中转，合并成这条线。下面保留 ${edge.records.length} 条原始关系，可逐条核对。`:edge.internal?'这是折叠在组内的原始关系；展开后可查看具体端点。':'这条线对应已保存的源码关系，箭头表示关系方向。'}));
  const endpointName=id=>{const p=ports.get(id);return p?`${entities.get(p.entity_id).name} · ${p.name}（${contextLabel(p.context_id)}）`:entities.get(id)?.name||id;};
  const sourceBox=elem('div');
  for(const r of edge.records){const section=elem('section',{class:'detail-section'},elem('h3',{text:relationLabel(r.kind)}),elem('p',{class:'source-caption',text:`${endpointName(r.from_id)} → ${endpointName(r.to_id)}`}));
    if(r.kind==='call')section.append(elem('p',{class:'source-caption',text:'调用位置：'+contextLabel(r.context_id)}));
    if(r.basis!=='source'||r.freshness==='stale')section.append(elem('p',{class:'stale',text:r.freshness==='stale'?'源码已变化，这条依据需要重新核对。':r.reason||'关系尚待核对。'}));
    for(const id of r.evidence_ids||[]){const ev=graph.evidence.find(e=>e.id===id),s=sources.get(ev.source_id);section.append(elem('button',{class:'evidence-button',text:`${s.path}:${ev.start_line}–${ev.end_line} · ${ev.note}`,onclick:()=>showSource(sourceBox,ev.source_id,Math.max(1,ev.start_line-2),ev,generation)}));}details.append(section);
  }
  details.append(sourceBox);
  if(edge.internal)return;
  const hasVia=!!layout().routes?.[edge.id];details.append(elem('section',{class:'detail-section'},elem('h3',{text:'调整这条连线'}),elem('p',{class:'source-caption',text:'整理点只改变走线，可在画布上拖动；双击整理点可移除。'}),elem('button',{class:'evidence-button',text:hasVia?'移除整理点':'添加整理点',onclick:()=>{
    layout().routes??={};if(layout().routes[edge.id])delete layout().routes[edge.id];else{const points=routedEdges.get(edge.id)?.points;if(!points?.length)return;let distance=0;const segments=[];for(let i=1;i<points.length;i++){const length=Math.hypot(points[i].x-points[i-1].x,points[i].y-points[i-1].y);segments.push({a:points[i-1],b:points[i],length});distance+=length;}let half=distance/2;for(const s of segments){if(half<=s.length){const t=half/(s.length||1);layout().routes[edge.id]={x:s.a.x+(s.b.x-s.a.x)*t,y:s.a.y+(s.b.y-s.a.y)*t};break;}half-=s.length;}}
    drawWires();renderWireDetails(edge);saveSoon();
  }})));
  panelControls.open('details');
}
function wireFocus(edge){
  if(edge.association){selectNode(workflowScene.entry);return;}
  view.taskFocus=edge.taskIds;view.focus=[];applyFlowFocus();renderWireDetails(edge);
}
function dragReroute(event,edge,handle){
  if(event.button!==0)return;event.preventDefault();event.stopPropagation();handle.setPointerCapture(event.pointerId);
  const startX=event.clientX,startY=event.clientY;let moved=false;
  const move=e=>{if(Math.abs(e.clientX-startX)+Math.abs(e.clientY-startY)<2&&!moved)return;moved=true;const c=$('#canvas').getBoundingClientRect(),v=layout().viewport;layout().routes[edge.id]={x:(e.clientX-c.x-v.x)/v.z,y:(e.clientY-c.y-v.y)/v.z};requestWires();};
  // Keep the captured handle alive while the paths are redrawn.
  handle.dataset.dragging='true';
  const end=()=>{delete handle.dataset.dragging;handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);if(moved){cancelAnimationFrame(wireFrame);drawWires();saveSoon();}};
  handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);
}
function appendFlowMotion(surface,route,paint,lane,budget){
  const length=route.points.slice(1).reduce((sum,p,i)=>sum+Math.hypot(p.x-route.points[i].x,p.y-route.points[i].y),0);
  const count=Math.min(budget,Math.max(1,Math.min(3,Math.floor(length/330)))),duration=clamp(length/100,2.8,16);
  for(let i=0;i<count;i++){
    const phase=(lane*.173+i/count)%1,begin=`${-phase*duration}s`,head=svg('g',{class:'wire-motion','aria-hidden':'true'});
    const arrow=svg('path',{d:'M -10 -4 L 0 0 L -10 4 Z',class:'wire-tip traveling',transform:`scale(${wireTipScale()})`});
    arrow.style.setProperty('--flow',paint);
    head.append(arrow,svg('animateMotion',{path:route.d,dur:`${duration}s`,begin,repeatCount:'indefinite',rotate:'auto',calcMode:'paced'}),svg('animate',{attributeName:'opacity',values:'0;1;1;0',keyTimes:'0;0.08;0.92;1',dur:`${duration}s`,begin,repeatCount:'indefinite'}));
    surface.append(head);
  }
  return count;
}
function drawWires(){
  const surface=$('#wires'),dragging=surface.querySelector('[data-dragging]');
  for(const child of [...surface.children])if(child!==dragging)child.remove();routedEdges=new Map();
  const rectangles=[...nodeElements].map(([key,e])=>({key,...layout().positions[key],width:e.offsetWidth,height:e.offsetHeight}));
  const occupied=[],focus=sceneFocus();let blocked=0,motionBudget=96,motionPending=sceneEdges.filter(e=>e.data&&(!focus.active||focus.edges.has(e.id))).length;
  [...sceneEdges].sort((a,b)=>Number(!!b.association)-Number(!!a.association)).forEach((edge,i)=>{const r=edge.record,a=edge.data?portPoint(edge.fromPort.id):cardPoint(edge.from,true,edge.id),b=edge.data?portPoint(edge.toPort.id):cardPoint(edge.to,false,edge.id);if(!a||!b)return;
    const via=layout().routes?.[edge.id],route=Blueprint.route(a,b,rectangles,{from:edge.from,to:edge.to,lane:i,occupied:occupied.filter(s=>!r.flow_id||s.flow!==r.flow_id),via});routedEdges.set(edge.id,route);if(route.blocked)blocked++;
    const guides=route.guidePoints||route.points;
    for(let j=1;j<guides.length;j++){const segment=[guides[j-1],guides[j]];segment.flow=r.flow_id;occupied.push(segment);}
    const review=[];
    if(edge.records.some(r=>r.freshness==='stale'))review.push('源码已变化，待重新核对');
    if(edge.records.some(r=>r.basis!=='source'))review.push('包含尚未确认的关系');
    const emphasis=flowView()&&focus.active?(focus.edges.has(edge.id)?'focused':'dim'):'';
    const classes=`wire ${edge.association?'association':edge.data?'data':'call'} ${emphasis}`;
    const path=svg('path',{d:route.d,class:classes,'data-edge-id':edge.id,'data-review-pending':String(!!review.length),'data-route-blocked':String(!!route.blocked),tabindex:0,role:'button'});
    const f=graph.flows.find(f=>f.id===r.flow_id),paint=taskPaint(edge.taskIds);path.style.setProperty('--flow',paint);const title=svg('title');title.textContent=`任务：${taskNames(edge.taskIds)}\n${edge.taskIds.map(id=>workflowTasks.byId.get(id).purpose).join('\n')}\n${f?'数据：'+f.name:relationLabel(r.kind)}${r.kind==='call'?' · '+contextLabel(r.context_id):''}${edge.annotation?' · '+edge.annotation:''} · ${edge.records.length} 条依据${review.length?'\n核对状态：'+review.join('；'):''}${route.blocked?'\n整理点或节点重叠需要调整':''}`;path.append(title);path.setAttribute('aria-label','查看连线 '+title.textContent);
    if(edge.association){title.textContent='流程归属 · '+edge.memberName;path.setAttribute('aria-label','查看'+title.textContent);}
    path.addEventListener('click',()=>wireFocus(edge));path.addEventListener('keydown',event=>{if(event.key==='Enter')wireFocus(edge);});surface.append(path);
    if(!edge.association){
      // Route endpoints enter from the left. Keep the head outside the pin and
      // keep it readable on zoom, capped so adjacent pins stay distinct in an overview.
      const head=svg('g',{transform:`translate(${b.x-(edge.data?6:2)} ${b.y})`,'aria-hidden':'true'});
      const tip=svg('path',{d:'M -10 -4 L 0 0 L -10 4 Z',class:`wire-tip ${emphasis}`,transform:`scale(${wireTipScale()})`});
      tip.style.setProperty('--flow',paint);head.append(tip);surface.append(head);
    }
    if(edge.data&&emphasis!=='dim'){
      const allowance=Math.max(1,Math.floor(motionBudget/Math.max(1,motionPending)));motionPending--;
      if(!route.blocked&&motionBudget>0)motionBudget-=appendFlowMotion(surface,route,paint,i,allowance);
    }
    if(via){const handle=dragging?.dataset.edge===edge.id?dragging:svg('circle',{r:6,class:'reroute',tabindex:0,role:'button','aria-label':'拖动整理点；双击或按 Delete 移除','data-edge':edge.id});handle.setAttribute('cx',via.x);handle.setAttribute('cy',via.y);handle.style.setProperty('--flow',paint);
      if(handle!==dragging){handle.addEventListener('pointerdown',event=>dragReroute(event,edge,handle));const remove=()=>{delete layout().routes[edge.id];drawWires();renderWireDetails(edge);saveSoon();};handle.addEventListener('dblclick',event=>{event.stopPropagation();remove();});handle.addEventListener('keydown',event=>{if(event.key==='Delete'||event.key==='Backspace'){event.preventDefault();remove();}});surface.append(handle);}
    }
  });
  $('#route-status').hidden=!blocked;$('#route-status').textContent=blocked?`${blocked} 条连线过于拥挤，可点击上方“一键整理”。`:'';drawMinimap();
}
function drawMinimap(){
  const map=$('#minimap');map.replaceChildren();if(!sceneNodes.length)return;
  const pos=layout().positions,v=layout().viewport,c=$('#canvas');const minX=Math.min(0,-v.x/v.z,...sceneNodes.map(n=>pos[n.key].x)),minY=Math.min(0,-v.y/v.z,...sceneNodes.map(n=>pos[n.key].y));
  const maxX=Math.max((c.clientWidth-v.x)/v.z,...sceneNodes.map(n=>pos[n.key].x+(nodeElements.get(n.key)?.offsetWidth||242))),maxY=Math.max((c.clientHeight-v.y)/v.z,...sceneNodes.map(n=>pos[n.key].y+(nodeElements.get(n.key)?.offsetHeight||160)));
  const scale=Math.min(150/(maxX-minX||1),74/(maxY-minY||1));
  for(const n of sceneNodes)map.append(svg('rect',{x:9+(pos[n.key].x-minX)*scale,y:9+(pos[n.key].y-minY)*scale,width:(nodeElements.get(n.key)?.offsetWidth||242)*scale,height:(nodeElements.get(n.key)?.offsetHeight||160)*scale,rx:2,fill:'var(--line)'}));
  map.append(svg('rect',{x:9+(-v.x/v.z-minX)*scale,y:9+(-v.y/v.z-minY)*scale,width:c.clientWidth/v.z*scale,height:c.clientHeight/v.z*scale,fill:'none',stroke:'var(--accent)','stroke-width':1}));
}
function startNodeDrag(event,n,card){
  if(event.button!==0||event.target.closest('button'))return;const position=layout().positions[n.key];if(position.locked)return;
  event.preventDefault();event.stopPropagation();const x=event.clientX,y=event.clientY,px=position.x,py=position.y,z=layout().viewport.z;card.dataset.moved='false';
  const target=event.currentTarget;target.setPointerCapture(event.pointerId);
  const move=e=>{const dx=(e.clientX-x)/z,dy=(e.clientY-y)/z;if(Math.abs(dx)+Math.abs(dy)>4)card.dataset.moved='true';position.x=px+dx;position.y=py+dy;card.style.left=position.x+'px';card.style.top=position.y+'px';requestWires();};
  const end=()=>{target.removeEventListener('pointermove',move);target.removeEventListener('pointerup',end);target.removeEventListener('pointercancel',end);saveSoon();};
  target.addEventListener('pointermove',move);target.addEventListener('pointerup',end);target.addEventListener('pointercancel',end);
}
$('#canvas').addEventListener('pointerdown',event=>{
  if(!graph||event.button!==0||event.target.closest('.node,button,.wire,.reroute'))return;const canvas=$('#canvas'),v=layout().viewport,x=event.clientX,y=event.clientY,px=v.x,py=v.y;
  canvas.setPointerCapture(event.pointerId);canvas.classList.add('dragging');
  const move=e=>{v.x=px+e.clientX-x;v.y=py+e.clientY-y;applyViewport();};const end=()=>{canvas.classList.remove('dragging');canvas.removeEventListener('pointermove',move);canvas.removeEventListener('pointerup',end);canvas.removeEventListener('pointercancel',end);saveSoon();};canvas.addEventListener('pointermove',move);canvas.addEventListener('pointerup',end);canvas.addEventListener('pointercancel',end);
});
function zoom(factor,x,y){if(!graph)return;camera.cancel();const v=layout().viewport,z=clamp(v.z*factor,.22,2);v.x=x-(x-v.x)*z/v.z;v.y=y-(y-v.y)*z/v.z;v.z=z;applyViewport();saveSoon();}
$('#canvas').addEventListener('wheel',event=>{event.preventDefault();const rect=$('#canvas').getBoundingClientRect();zoom(Math.exp(-event.deltaY*.001),event.clientX-rect.x,event.clientY-rect.y);},{passive:false});
$('#zoom-in').onclick=()=>zoom(1.2,$('#canvas').clientWidth/2,$('#canvas').clientHeight/2);$('#zoom-out').onclick=()=>zoom(1/1.2,$('#canvas').clientWidth/2,$('#canvas').clientHeight/2);
function fit(focused=false){
  camera.cancel();
  if(!sceneNodes.length)return;
  const focus=sceneFocus(),onlyFocus=focused&&focus.active&&focus.nodes.size;
  const nodes=onlyFocus?sceneNodes.filter(n=>focus.nodes.has(n.key)):sceneNodes;
  const p=layout().positions,points=[...routedEdges].filter(([id])=>!onlyFocus||focus.edges.has(id)).flatMap(([,r])=>r.points);
  const minX=Math.min(...nodes.map(n=>p[n.key].x),...points.map(p=>p.x)),minY=Math.min(...nodes.map(n=>p[n.key].y),...points.map(p=>p.y)),maxX=Math.max(...nodes.map(n=>p[n.key].x+nodeElements.get(n.key).offsetWidth),...points.map(p=>p.x)),maxY=Math.max(...nodes.map(n=>p[n.key].y+nodeElements.get(n.key).offsetHeight),...points.map(p=>p.y));
  const c=$('#canvas'),z=clamp(Math.min((c.clientWidth-70)/(maxX-minX),(c.clientHeight-130)/(maxY-minY)),.22,1.15);layout().viewport={z,x:(c.clientWidth-(maxX-minX)*z)/2-minX*z,y:45-minY*z};applyViewport();saveSoon();
}
$('#fit').onclick=()=>fit();
$('#arrange').onclick=()=>{
  if(!sceneNodes.length)return;
  const current=layout();current.beforeArrange=structuredClone({positions:current.positions,routes:current.routes||{},viewport:current.viewport});
  for(const edge of sceneEdges)if(current.routes)delete current.routes[edge.id];
  arrangeNodes(true);drawWires();fit(true);syncCanvasControls();if(selectedLink)renderWireDetails(selectedLink);saveSoon();
};
$('#undo-arrange').onclick=()=>{
  const current=layout(),before=current.beforeArrange;if(!before)return;
  Object.assign(current,structuredClone(before));delete current.beforeArrange;render();if(selectedLink)renderWireDetails(selectedLink);saveSoon();
};
$('#flow-motion').onclick=()=>{view.flowMotion=!view.flowMotion;syncCanvasControls();saveSoon();};
document.addEventListener('visibilitychange',()=>{if(graph)syncCanvasControls();});
$('#home').onclick=()=>{$('#search').value='';goTo(rootId());};$('#back').onclick=()=>{const previous=view.history.pop();if(previous){Object.assign(view,previous);selected=null;selectedLink=null;render();saveSoon();}};
$('#search').oninput=()=>render();$('#layer').onchange=()=>{view.layer=$('#layer').value;selected=null;selectedLink=null;render();saveSoon();};
$('#presentation').onchange=()=>{view.presentation=$('#presentation').value;selected=null;selectedLink=null;render();saveSoon();};
$('#scope').onchange=()=>enterContext($('#scope').value);
$('#workflow-scope').onchange=()=>{view.focus=[];enterContext($('#workflow-scope').value);};
$('#workflow-whole').onclick=()=>{view.focus=[];$('#search').value='';let root=contexts.get(view.scope);while(root?.parent_id)root=contexts.get(root.parent_id);if(root&&root.id!==view.scope)enterContext(root.id);else render();if(workflowScene.entry)selectNode(workflowScene.entry);fit();saveSoon();};
$('#topology-grouping').onchange=()=>{view.grouping=$('#topology-grouping').value;view.expandedGroups=[];selected=null;selectedLink=null;render();saveSoon();};
$('#topology-layer').onchange=()=>{view.topologyLayer=$('#topology-layer').value;selected=null;selectedLink=null;render();saveSoon();};
$('#topology-collapse').onclick=()=>{view.expandedGroups=[];selected=null;selectedLink=null;render();saveSoon();};
document.querySelectorAll('.theme button').forEach(b=>b.onclick=()=>{applyTheme(b.dataset.theme);saveSoon();});
document.querySelectorAll('.view-tabs button').forEach(b=>b.onclick=()=>{remember();view.mode=b.dataset.mode;if((view.mode==='workflow'||scoped())&&view.taskFocus.length===1&&workflowTasks.byContext.get(view.scope)!==view.taskFocus[0])view.scope=view.taskFocus[0];selected=null;selectedLink=null;panelControls.closeDrawers();render();saveSoon();});
const panelControls=PanelLayout.mount({getState:()=>view,changed:saveSoon});
// Manual interaction takes over immediately, including node and sidebar resizing.
document.addEventListener('pointerdown',()=>camera.cancel(),true);
document.addEventListener('keydown',event=>{if(event.key==='Escape'||event.target.closest('.panel-resizer'))camera.cancel();},true);
window.addEventListener('resize',()=>camera.cancel());
new ResizeObserver(()=>{if(graph){drawMinimap();requestWires();}}).observe($('#canvas'));
async function load(initial=false){
  if(loading)return;loading=true;$('#refresh').disabled=true;
  try{payload=await api('/api/graph');graph=payload.graph;catalog=payload.catalog;writeToken=payload.write_token;entities=new Map(graph.entities.map(e=>[e.id,e]));sources=new Map(graph.sources.map(e=>[e.id,e]));ports=new Map(graph.ports.map(e=>[e.id,e]));contexts=new Map(graph.contexts.map(e=>[e.id,e]));
    topologyAnalysis=Topology.analyze(graph);workflowTasks=Workflow.tasks(graph);
    if(initial){view={mode:Blueprint.scopeChoices(graph).length?'workflow':'structure',parent:rootId(),layer:'data',theme:'system',layouts:{},history:[],focus:[],presentation:'scoped',scope:Blueprint.scopeChoices(graph)[0]?.id,grouping:'directory',topologyLayer:'call',expandedGroups:[],...payload.view};}
    panelControls.sync();
    if(typeof view.flowMotion!=='boolean')view.flowMotion=!window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(!Array.isArray(view.taskFocus)){const previous=new Set(view.focus||[]);view.taskFocus=[...new Set(graph.relations.filter(r=>previous.has(r.flow_id)).map(r=>workflowTasks.byContext.get(r.context_id)).filter(Boolean))];view.focus=[];}
    view.taskFocus=view.taskFocus.filter(id=>workflowTasks.byId.has(id));view.taskColors=Workflow.colors(workflowTasks.list,view.taskColors);
    if(!contexts.has(view.scope))view.scope=Blueprint.scopeChoices(graph)[0]?.id;selectedLink=null;
    if(!view.scope)view.presentation='raw';
    if(!entities.has(view.parent))view.parent=rootId();applyTheme(view.theme);
    if(!sources.has(view.fileSelection))view.fileSelection=null;
    $('#project-name').textContent=graph.project.name;$('#project-meta').textContent=`全仓库 · 图谱版本 ${graph.project.revision} · ${graph.sources.length} 个实际文件`;
    const snap=payload.snapshot;notice(snap.state==='current'?'':`源码与图谱需要重新核对：新增 ${(snap.added||[]).length}、修改 ${(snap.changed||[]).length}、删除 ${(snap.deleted||[]).length}。${snap.reason||'点击“检查代码变化”查看影响范围并更新原工程。'}`);
    view.focus=view.focus.filter(id=>graph.flows.some(f=>f.id===id));
    render();
    if(selected){const task=workflowTasks.byId.get(selected.taskId),fresh=selected.sourceSelection&&entities.has(selected.entity.id)?{...selected,entity:entities.get(selected.entity.id)}:sceneNodes.find(n=>n.key===selected.key);if(task)renderTaskDetails(task);else if(fresh){selected=fresh;renderDetails(selected);}else{selected=null;sourceGeneration++;$('#details').replaceChildren(elem('p',{class:'muted',text:'原节点已不在当前视图中，请选择节点查看最新依据。'}));}syncFileSelection();}
    if(initial)saveSoon();
  }catch(error){notice('工程读取失败：'+error.message);}finally{loading=false;$('#refresh').disabled=false;}
}
$('#refresh').onclick=()=>load();
$('#index-build').onclick=async()=>{
  const button=$('#index-build'),progress=$('#index-progress'),diagnostics=$('#index-diagnostics');button.disabled=true;diagnostics.replaceChildren();
  let offset=0,snapshot,parsed=0,hits=0,symbols=0,gaps=0;
  try{
    do{
      progress.textContent=`正在解析结构… 已检查 ${offset} 个文件`;
      const page=await api('/api/index',{method:'POST',headers:{'Content-Type':'application/json','X-Codemap-Token':writeToken},body:JSON.stringify({offset,limit:50,...(snapshot?{snapshot_id:snapshot}:{})})});
      parsed+=page.parsed_files;hits+=page.cache_hits;symbols+=page.files.reduce((sum,f)=>sum+f.symbol_count,0);
      for(const file of page.files)if(file.state!=='parsed'){gaps++;if(diagnostics.children.length<100)diagnostics.append(elem('p',{class:'source-caption',text:`${file.path}：${file.diagnostics.map(d=>d.message).join('；')}`}));}
      offset=page.next_offset;snapshot=page.snapshot_id;
    }while(offset!==null);
    await load();progress.textContent=`已生成 ${symbols} 个声明 · 本次解析 ${parsed} 个文件 · 复用 ${hits} 个${gaps?` · ${gaps} 个文件有缺口`:''}。`;
    if(!gaps)diagnostics.append(elem('p',{class:'source-caption',text:'本次未报告语法错误。宏展开、类型解析和动态调用仍需另行核对。'}));
  }catch(error){progress.textContent='解析未完成：'+error.message;}finally{button.disabled=false;}
};
const changesButton=elem('button',{id:'changes-open',text:'检查代码变化',onclick:()=>checkChanges()});$('#refresh').before(changesButton);
const workbench=Workbench.mount({api,token:()=>writeToken,graph:()=>graph,selection:()=>selected,changed:()=>load(),openContext:id=>{view.mode='workflow';enterContext(id);fit();}});
async function checkChanges(renames=[]){
  if(!Array.isArray(renames))renames=[];
  if(checkingChanges)return;checkingChanges=true;changePlan=null;
  const dialog=$('#changes-dialog'),content=$('#changes-content');if(!dialog.open)dialog.showModal();
  $('#changes-check').disabled=true;$('#changes-apply').disabled=true;content.replaceChildren(elem('p',{text:'正在核对文件内容和已有依赖…'}));
  try{
    changePlan=renames.length?await api('/api/changes/preview',{method:'POST',headers:{'Content-Type':'application/json','X-Codemap-Token':writeToken},body:JSON.stringify({renames})}):await api('/api/changes');const p=changePlan;
    content.replaceChildren(elem('p',{class:'changes-summary',text:p.state==='current'?'源码与当前图谱版本一致。':`修改 ${p.changed.length} · 新增 ${p.added.length} · 删除 ${p.deleted.length} · 移动 ${p.renamed?.length||0}`}));
    if(p.state!=='current'){
      for(const [title,paths] of [['修改的文件',p.changed],['新增的文件',p.added],['删除的文件',p.deleted],['需要重读或核对的范围',p.affected_paths]]){
        if(!paths.length)continue;const section=elem('section',{},elem('h3',{text:`${title}（${paths.length}）`}));
        section.append(elem('ul',{},...paths.slice(0,200).map(path=>elem('li',{text:path}))));if(paths.length>200)section.append(elem('p',{text:'面板显示前 200 项，完整范围保留在任务中。'}));content.append(section);
      }
      if(p.renamed?.length){
        const section=elem('section',{},elem('h3',{text:'保留身份的移动 / 重命名'}));
        for(const pair of p.renamed)section.append(elem('p',{text:`${pair.from} → ${pair.to} · ${pair.basis==='identical_content'?'内容相同且唯一匹配':'已指定对应关系'}`}));
        content.append(section);
      }
      if(p.change_details?.length){
        const kinds={cosmetic:'注释 / 格式',implementation:'函数实现',contract:'声明或模块状态',build:'构建配置',content:'内容变化',unknown:'保守复核'};
        content.append(elem('section',{},elem('h3',{text:'变化类型与复核原因'}),...p.change_details.map(d=>elem('p',{text:`${d.path} · ${kinds[d.kind]}：${d.reason}`}))));
      }
      if(p.added.length&&p.deleted.length){
        const before=elem('select',{'aria-label':'重命名前的文件'},...p.deleted.map(path=>elem('option',{value:path,text:path}))),after=elem('select',{'aria-label':'重命名后的文件'},...p.added.map(path=>elem('option',{value:path,text:path})));
        content.append(elem('section',{},elem('h3',{text:'移动时也修改了内容？'}),elem('p',{class:'muted',text:'选择对应的旧、新文件，保留原节点身份；内容变化仍会重新核对。'}),elem('div',{class:'rename-pair'},before,elem('span',{text:'→'}),after,elem('button',{text:'按重命名关联',onclick:()=>checkChanges([...(p.rename_approvals||[]),{from:before.value,to:after.value}])}))));
      }
      if(p.rename_approvals?.length)content.append(elem('button',{text:'撤销手动重命名关联',onclick:()=>checkChanges()}));
      if(p.inventory_changed&&!p.added.length&&!p.changed.length&&!p.deleted.length&&!p.renamed?.length)content.append(elem('p',{text:'目录或读取范围的记录发生变化，需要重新核对仓库概况。'}));
      content.append(elem('p',{class:'muted',text:'按语法变化及已记录的依赖确定复核范围；未知变化和构建配置仍保守处理。更新保留旧图谱、节点布局和颜色。未记录的间接依赖仍需 Agent 搜索确认。'}));
      if(p.gaps.length)content.append(elem('p',{class:'stale',text:'部分路径无法完整读取，本次不能更新：'+p.gaps.map(g=>`${g.path} · ${g.reason}`).join('；')}));
      $('#changes-apply').disabled=!p.can_apply;
    }else if(graph.last_update){content.append(elem('p',{text:payload.status.coverage.status==='complete'?'最近更新的内容已完成核对。':'新版本已登记，待核对内容和任务保存在原工程中。'}));}
    if(p.state!=='current')await load();
  }catch(error){content.replaceChildren(elem('p',{class:'stale',text:'检查失败：'+error.message}));}
  finally{checkingChanges=false;$('#changes-check').disabled=false;}
}
$('#changes-close').onclick=()=>$('#changes-dialog').close();
$('#changes-check').onclick=checkChanges;
$('#changes-apply').onclick=async()=>{
  if(!changePlan||checkingChanges)return;checkingChanges=true;$('#changes-apply').disabled=true;$('#changes-check').disabled=true;
  try{await api('/api/changes',{method:'POST',headers:{'Content-Type':'application/json','X-Codemap-Token':writeToken},body:JSON.stringify({expected_revision:changePlan.base_revision,plan_id:changePlan.plan_id,renames:changePlan.rename_approvals||[]})});changePlan=null;await load();$('#changes-content').replaceChildren(elem('p',{class:'changes-summary',text:'新版本已保存，受影响部分已加入重读队列。'}),elem('p',{text:'旧版图谱与阅读布局已保留。可以在 Agent 面板中继续全仓库梳理，完成核对后自动显示新成果。'}));}
  catch(error){$('#changes-content').append(elem('p',{class:'stale',text:'更新未完成：'+error.message+'。请重新检查后继续。'}));}
  finally{checkingChanges=false;$('#changes-check').disabled=false;}
};
window.addEventListener('resize',()=>{if(graph){applyViewport();drawWires();}});
setInterval(async()=>{if(!graph||document.hidden||loading)return;try{const s=await api('/api/version');updateStatus(s);if(s.project.revision!==graph.project.revision||s.structure_version!==payload.structure?.version)await load();}catch{$('#execution').textContent='本地连接中断 · 已显示的成果仍可浏览';}},4000);
load(true);
