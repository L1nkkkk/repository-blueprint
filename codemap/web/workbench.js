/* Requests, coverage and history are backed by the saved project, not UI timers. */
window.Workbench={mount(options){
  const make=(tag,attrs={},...children)=>{const node=document.createElement(tag);for(const [key,value] of Object.entries(attrs)){if(key==='text')node.textContent=value;else if(key==='onclick')node.onclick=value;else if(key==='hidden')node.hidden=value;else node.setAttribute(key,value);}node.append(...children);return node;};
  const call=(path,data)=>options.api(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Codemap-Token':options.token()},body:JSON.stringify(data)});
  const button=(text,onclick,attrs={})=>make('button',{text,onclick,...attrs});
  function dialog(title,id){
    const body=make('div',{class:'workbench-content'}),status=make('p',{class:'workbench-status',role:'status'});
    const modal=make('dialog',{class:'workbench-dialog',id,'aria-label':title},make('div',{class:'workbench-heading'},make('h2',{text:title}),button('关闭',()=>modal.close(),{'aria-label':'关闭'+title})),status,body);
    document.body.append(modal);return {modal,body,status};
  }
  const reader=dialog('Agent 分析任务','reader-dialog'),coverage=dialog('分析覆盖','coverage-dialog'),history=dialog('图谱历史','history-dialog');
  const metrics=dialog('审阅耗时','metrics-dialog');
  const requestList=make('div',{class:'request-list'}),requestDetails=make('div',{class:'request-details'});
  const requestQuestion=make('textarea',{'aria-label':'给 Agent 的问题',placeholder:'选择节点后提问，或描述想继续梳理的内容。',rows:3});
  const route=make('select',{'aria-label':'Agent 执行方式'},make('option',{value:'external',text:'外部 MCP Agent'}),make('option',{value:'codex',text:'本机 Codex 后台'}));
  const client=make('select',{'aria-label':'MCP Agent 类型'},...[['claude-code','Claude Code'],['gemini-cli','Gemini CLI'],['generic','其他 MCP Agent']].map(([value,text])=>make('option',{value,text})));
  const askButton=button('生成提问说明',()=>submitSelection(options.selection(),requestQuestion.value,'question'));
  const repositoryButton=button('生成全仓库接续说明',()=>submitSelection(null,'继续梳理这个仓库的全部剩余任务，逐批阅读、提交并核对；保留未确认关系的具体原因。','repository'));
  const requestActions=make('div',{class:'workbench-actions'},askButton,repositoryButton);
  const connectionText=make('textarea',{'aria-label':'MCP 接入配置',readonly:'',rows:7}),connectionNote=make('p',{class:'muted'});
  const handoffText=make('textarea',{'aria-label':'交给外部 Agent 的接续说明',readonly:'',rows:8});
  const handoffBox=make('section',{class:'agent-handoff',hidden:true},make('h3',{text:'接续说明已准备好'}),make('p',{class:'muted',text:'复制后发给已连接的 Agent。分析在对应宿主中开始；图谱提交后会同步到这份画布。'}),handoffText,button('复制接续说明',()=>copyText(handoffText)));
  const externalControls=make('section',{class:'agent-connection'},make('div',{class:'workbench-actions'},make('label',{},'接入 ',client)),make('details',{},make('summary',{text:'首次使用：查看 MCP 接入配置'}),connectionNote,connectionText,button('复制 MCP 配置',()=>copyText(connectionText))));
  const routeNote=make('p',{class:'muted'});
  const localHistory=make('details',{},make('summary',{text:'本机 Codex 请求记录'}),make('div',{class:'request-columns'},requestList,requestDetails));
  reader.body.append(make('div',{class:'workbench-actions'},make('label',{},'执行方式 ',route),button('审阅耗时',()=>openMetrics(),{'aria-label':'查看审阅耗时'})),routeNote,externalControls,requestQuestion,requestActions,handoffBox,localHistory);
  const externalMessage='支持本地 MCP 的 Agent 可接续同一份地图。模型、登录和停止操作由对应宿主管理；无需安装 Codex。';
  let connectionGeneration=0;
  async function copyText(node){
    try{await navigator.clipboard.writeText(node.value);reader.status.textContent='已复制。';}
    catch{node.focus();node.select();reader.status.textContent='内容已选中，请手动复制。';}
  }
  async function refreshConnection(){
    const generation=++connectionGeneration;
    try{
      const info=await call('/api/agent-connection?client='+encodeURIComponent(client.value));
      if(generation!==connectionGeneration)return;
      connectionText.value=JSON.stringify(info.config,null,2);
      connectionNote.textContent=info.destination+'。'+info.instructions;
    }catch(error){reader.status.textContent='接入配置读取失败：'+error.message;}
  }
  function updateRoute(){
    const external=route.value==='external';externalControls.hidden=!external;handoffBox.hidden=true;localHistory.open=!external;
    askButton.textContent=external?'生成提问说明':'提交给本机 Codex';
    repositoryButton.textContent=external?'生成全仓库接续说明':'继续全仓库梳理';
    routeNote.textContent=external?'准备说明后交给 Claude Code、Gemini CLI 或其他 MCP Agent；这里只复制上下文，不会自动启动分析。':'分析使用本机 Codex。可以暂停，已提交图谱和回答保存在工程里。';
    reader.status.textContent=external?externalMessage:(latestConnection?.message||'正在检查本机 Codex…');
    try{localStorage.setItem('blueprint-agent-route',route.value);localStorage.setItem('blueprint-mcp-client',client.value);}catch{}
    if(external)void refreshConnection();
  }
  route.onchange=updateRoute;client.onchange=()=>{handoffBox.hidden=true;updateRoute();};
  let selectedRequest=null,polling=false,submitting=false,latestRequests=[],latestConnection=null,lastDetail='',lastList='';
  const states={queued:'等待阅读进程',running:'执行中',paused:'已暂停',completed:'已完成',failed:'执行失败',needs_attention:'等待继续',cancelled:'已取消'};
  const stamp=time=>new Date(time*1000).toLocaleString('zh-CN',{hour12:false});
  const phaseNames={inventory:'建立清单',structure:'本地结构解析',claim_pack:'领取与组包',material:'读取与搜索',query:'查询与复用',prepare:'准备与校验',commit:'提交成果',status:'状态与版本检查',control:'任务控制',update:'更新核对',other:'其他工具'};
  const toolNames={init:'初始化',index:'结构索引',next:'领取与组包',pack:'阅读包',read:'读取源码',search:'搜索',context:'复用核对',query:'查询',prepare:'准备提交',commit:'提交',status:'状态检查',task:'任务控制',update:'更新核对',canvas:'打开画布',export:'导出'};
  const failureNames={revision:'版本变化',lease:'领取身份或有效期',source:'源码版本',evidence:'源码依据或分析深度',io:'文件读写',validation:'参数或结果校验',tool:'工具执行'};
  const duration=ms=>ms==null?'未结束':ms<1000?Math.round(ms)+' ms':ms<60000?(ms/1000).toFixed(2)+' 秒':(ms/60000).toFixed(1)+' 分钟';
  const byteSize=n=>n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KiB':(n/1048576).toFixed(2)+' MiB';
  let metricsData=null,metricsSession='',metricsBusy=false;
  const metricsSelect=make('select',{'aria-label':'选择统计连接'}),metricsResult=make('div',{class:'metrics-results'});
  const metricsDownload=button('导出本次报告',()=>{
    if(!metricsData)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify(metricsData,null,2)],{type:'application/json'}));
    const link=make('a',{href:url,download:'review-metrics-'+(metricsData.selected?.id||'empty')+'.json'});link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  },{disabled:''});
  const metricsCopy=button('复制报告',async()=>{
    if(!metricsData)return;
    try{await navigator.clipboard.writeText(JSON.stringify(metricsData,null,2));metrics.status.textContent='报告已复制，可保存为 JSON 用于对比。';}
    catch{metrics.status.textContent='浏览器未允许复制，请使用导出按钮或命令行保存报告。';}
  },{disabled:''});
  metricsSelect.onchange=()=>{metricsSession=metricsSelect.value;void refreshMetrics();};
  metrics.body.append(make('div',{class:'workbench-actions metrics-controls'},make('label',{},'统计连接 ',metricsSelect),button('刷新统计',()=>refreshMetrics()),metricsDownload,metricsCopy),metricsResult);
  function metricsTable(headers,rows){
    return make('div',{class:'metrics-table-scroll'},make('table',{class:'metrics-table'},make('thead',{},make('tr',{},...headers.map(text=>make('th',{text})))),make('tbody',{},...rows.map(row=>make('tr',{},...row.map(text=>make('td',{text})))))));
  }
  async function openMetrics(){
    if(!metrics.modal.open)metrics.modal.showModal();
    await refreshMetrics();
  }
  async function refreshMetrics(){
    if(metricsBusy||!metrics.modal.open||document.hidden)return;
    metricsBusy=true;const requested=metricsSession;
    try{
      const data=await call('/api/metrics'+(requested?'?session='+encodeURIComponent(requested):''));
      if(requested!==metricsSession)return;
      metricsData=data;metricsDownload.disabled=metricsCopy.disabled=data.state!=='recorded';
      const options=[make('option',{value:'',text:'最新连接'}),...data.sessions.map(s=>make('option',{value:s.id,text:s.client+' · '+stamp(s.started)}))];
      if(metricsSession&&data.selected&&!data.sessions.some(s=>s.id===metricsSession))options.push(make('option',{value:metricsSession,text:data.selected.client+' · '+stamp(data.selected.started)}));
      metricsSelect.replaceChildren(...options);metricsSelect.value=metricsSession;
      const expanded=new Set([...metricsResult.querySelectorAll('details[open]')].map(n=>n.dataset.section));
      metricsResult.replaceChildren();
      const p=data.progress;
      const progress=make('p',{class:'metrics-progress',text:`当前地图：文件已读 ${p.sources_read} / ${p.sources_total} · 已领取 ${p.task_counts.running} · 排队 ${p.task_counts.queued} · 有阻碍 ${p.task_counts.blocked}`});
      metricsResult.append(progress);
      if(p.claimed_tasks.length){
        metricsResult.append(make('h3',{text:'当前领取范围'}),...p.claimed_tasks.map(t=>make('p',{class:'metrics-claim',text:(t.paths.join('、')||t.id)+' · '+t.worker+' · '+(t.expires_at<data.generated_at?'领取已过期':'有效至 '+stamp(t.expires_at))})));
      }
      if(data.state!=='recorded'){
        metrics.status.textContent=data.state==='empty'?'还没有统计记录。当前批次提交后重连新版 MCP，下一次工具调用就会开始记录。此前的详细耗时无法补记。':data.message;
        metricsResult.append(make('p',{class:'metrics-note',text:data.scope_note}));return;
      }
      const s=data.summary,connection=data.selected;
      metrics.status.textContent=connection.client+' · 从 '+stamp(s.first_call_at)+' 开始记录 · '+(connection.closed?'连接已结束':'最近记录距今 '+duration(s.quiet_seconds*1000))+'。';
      if(!data.snapshot_matches_start)metricsResult.append(make('p',{class:'stale',text:'当前地图的源码清单与本连接开始时不同。比较性能时请使用相同源码、范围和模型设置。'}));
      if(connection.dropped||s.unfinished)metricsResult.append(make('p',{class:'stale',text:`统计存在缺口：${connection.dropped} 次记录异常，${s.unfinished} 条尚无结束记录。未结束记录不能证明进程仍在运行。`}));
      else if(!connection.closed&&s.quiet_seconds>=300)metricsResult.append(make('p',{class:'metrics-note',text:'已有一段时间没有新的工具记录。宿主可能在处理结果、等待网络或执行其他工作；请结合 WorkBuddy 等宿主的运行记录判断。'}));
      metricsResult.append(make('div',{class:'coverage-stats'},...[
        ['工具实际耗时',duration(s.tool_ms)],['调用间隔',duration(s.between_calls_ms)],['已观测成功批次',s.observed_batches],['重复返回源码行',s.repeated_lines+' / '+s.read_lines]
      ].map(([label,value])=>make('div',{},make('strong',{text:String(value)}),make('span',{text:label})))));
      metricsResult.append(make('p',{class:'metrics-note',text:`${s.calls} 次调用 · ${s.errors} 次失败 · ${s.retries} 次相同参数失败重试 · 参数 ${byteSize(s.input_bytes)} / 返回 ${byteSize(s.output_bytes)}。${s.successful_commit_calls>s.observed_batches?'重复提交的同一批次只计一次。':''}`}));
      metricsResult.append(make('h3',{text:'本地时间花在哪里'}),make('div',{class:'metrics-phases'},...data.phases.map(row=>{
        const bar=make('span',{class:'metrics-bar','aria-hidden':'true'});bar.style.width=(s.tool_ms?Math.max(0,Math.min(100,row.tool_ms/s.tool_ms*100)):0)+'%';
        return make('div',{class:'metrics-phase'},make('span',{text:phaseNames[row.phase]||row.phase}),make('div',{class:'metrics-track'},bar),make('span',{text:duration(row.tool_ms)+' · '+row.calls+' 次'}));
      })));
      if(data.tasks.length)metricsResult.append(make('details',{'data-section':'tasks'},make('summary',{text:'按任务查看耗时（最多 30 项）'}),metricsTable(['任务范围','工具耗时','已观测批次','当前队列状态'],data.tasks.map(t=>[t.paths.join('、')||t.task_id,duration(t.tool_ms),String(t.observed_batches),({queued:'待继续',running:'已领取',done:'已完成',blocked:'有阻碍'})[t.current_state]||'已不在当前地图']))));
      const taskPaths=new Map(data.tasks.map(t=>[t.task_id,t.paths.join('、')]));
      metricsResult.append(make('details',{'data-section':'events'},make('summary',{text:'最近 '+data.events.length+' 次工具调用'+(data.events_truncated?'（汇总包含全部已记录调用）':'')}),metricsTable(['时间 / 工具','范围','耗时 / 状态'],data.events.map(e=>[
        stamp(e.started)+' · '+(toolNames[e.tool]||e.tool),taskPaths.get(e.target)||e.target||taskPaths.get(e.task_id)||'地图',duration(e.elapsed_ms)+' · '+(e.status==='ok'?'成功':e.status==='unfinished'?'未收到结束记录':failureNames[e.error_kind]||'失败')+(e.retry?' · 相同请求重试':'')]))));
      metricsResult.append(make('p',{class:'metrics-note',text:'调用间隔可能包含模型处理、网络等待或其他工作，不能当作纯推理耗时。'}),make('details',{'data-section':'definitions'},make('summary',{text:'统计口径与限制'}),make('p',{class:'metrics-note',text:data.scope_note}),make('p',{class:'metrics-note',text:'重复行按同一连接、相同文件与内容指纹计算，包含阅读包与单独读取的重叠；重复核对也可能是合理操作。宿主名称由 MCP 客户端报告。'})));
      for(const node of metricsResult.querySelectorAll('details'))node.open=expanded.has(node.dataset.section);
    }catch(error){metrics.status.textContent='无法读取统计：'+error.message+'。若画布仍由旧版服务提供，请在当前批次提交后重新打开新版画布。';}
    finally{metricsBusy=false;if(requested!==metricsSession&&metrics.modal.open)void refreshMetrics();}
  }
  async function refreshRequests(){
    if(polling||document.hidden)return;polling=true;
    try{
      const data=await call('/api/requests');latestRequests=data.requests;latestConnection=data.connection;
      const active=latestRequests.filter(r=>['queued','running'].includes(r.state)).length;
      readerButton.textContent=active?'Agent · '+active:'Agent';
      if(route.value==='codex')reader.status.textContent=data.connection.message;
      if(!reader.modal.open)return;
      if(!selectedRequest&&latestRequests.length)selectedRequest=latestRequests[0].id;
      const listKey=JSON.stringify([latestRequests,selectedRequest]);
      if(listKey!==lastList){
        lastList=listKey;
        requestList.replaceChildren(...latestRequests.map(r=>button('',async()=>{selectedRequest=r.id;try{await refreshSelected();void refreshRequests();}catch(error){reader.status.textContent=error.message;}},{class:'request-item'+(r.id===selectedRequest?' active':'')})));
        [...requestList.children].forEach((node,i)=>{const r=latestRequests[i];node.append(make('strong',{text:r.question}),make('span',{text:states[r.state]+' · '+stamp(r.created_at)}));});
        if(!latestRequests.length)requestList.append(make('p',{class:'muted',text:'还没有从画布发起的请求。'}));
      }
      await refreshSelected();
    }catch(error){reader.status.textContent='请求状态读取失败：'+error.message;}
    finally{polling=false;}
  }
  async function refreshSelected(){
    if(!selectedRequest)return;
    const data=await call('/api/requests?id='+encodeURIComponent(selectedRequest));
    const detailKey=data.id+':'+data.updated_at+':'+latestConnection?.worker_active;if(detailKey===lastDetail)return;lastDetail=detailKey;
    const actions=make('div',{class:'workbench-actions'});
    async function control(action){
      try{await call('/api/requests',{action,id:data.id});await refreshSelected();void refreshRequests();}
      catch(error){reader.status.textContent=error.message;}
    }
    if(['running','queued'].includes(data.state))actions.append(button(data.stop_requested?'正在停止…':'暂停',()=>control('pause'),data.stop_requested?{disabled:''}:{}));
    if(['paused','failed','needs_attention'].includes(data.state))actions.append(button('继续',()=>control('resume')));
    if(data.state==='queued'&&!latestConnection?.worker_active)actions.append(button('启动阅读',()=>control('resume')));
    if(['running','queued','paused','failed','needs_attention'].includes(data.state))actions.append(button('取消请求',()=>control('cancel')));
    const output=make('pre',{class:'reader-answer',text:data.result||'回答会在实际阅读后显示。'}),events=make('ol',{class:'reader-events'});
    for(const item of (data.events||[]).slice(-12))events.append(make('li',{},make('time',{text:stamp(item.time)}),make('span',{text:item.text})));
    requestDetails.replaceChildren(make('h3',{text:data.question}),make('p',{class:'request-state',text:states[data.state]+(data.rounds?' · 已执行 '+data.rounds+' 批':'')}),actions);
    if(data.error)requestDetails.append(make('p',{class:'stale',text:data.error}));
    requestDetails.append(output,make('details',{},make('summary',{text:'实际执行记录'}),events));
  }
  async function submitSelection(selection,question,kind='question'){
    if(submitting)return;
    if(!question?.trim()){if(!reader.modal.open)reader.modal.showModal();requestQuestion.focus();reader.status.textContent='请输入想让 Agent 查清的问题。';return;}
    submitting=true;
    if(!reader.modal.open)reader.modal.showModal();
    const external=route.value==='external';
    reader.status.textContent=external?'正在准备接续说明…':'正在保存请求…';
    const graph=options.graph(),entity=selection?.entity?.id,context=selection?.context;
    const request={id:crypto.randomUUID(),kind,question:question.trim()};
    if(graph.entities.some(e=>e.id===entity))request.entity_id=entity;
    if(graph.contexts.some(c=>c.id===context))request.context_id=context;
    try{
      if(external){
        const {id,...context}=request;
        const result=await call('/api/handoff',{...context,client:client.value});
        handoffText.value=result.prompt;handoffBox.hidden=false;
        reader.status.textContent='说明已准备好，尚未启动 Agent。';
        return;
      }
      const result=await call('/api/requests',{action:'create',request});selectedRequest=result.id;requestQuestion.value='';
      await refreshRequests();if(result.connection_error)reader.status.textContent=result.connection_error;
    }catch(error){reader.status.textContent='请求未提交：'+error.message;}
    finally{submitting=false;}
  }
  const readerButton=button('Agent',()=>{reader.modal.showModal();void refreshRequests();},{'aria-label':'Agent 分析任务',title:'查看真实分析请求、进度和回答'});
  try{
    const savedRoute=localStorage.getItem('blueprint-agent-route'),savedClient=localStorage.getItem('blueprint-mcp-client');
    if(['external','codex'].includes(savedRoute))route.value=savedRoute;
    if(['claude-code','gemini-cli','generic'].includes(savedClient))client.value=savedClient;
  }catch{}
  updateRoute();
  const coverageButton=button('覆盖',()=>openCoverage(),{'aria-label':'查看分析覆盖',title:'文件阅读、已整理场景和待确认路径'});
  const historyButton=button('历史',()=>openHistory(),{'aria-label':'查看图谱历史',title:'对比历史图谱并恢复'});
  document.querySelector('#changes-open').before(coverageButton,readerButton,historyButton);

  let coverageData=null,coverageTab='scenarios';
  const coverageSearch=make('input',{type:'search',placeholder:'搜索文件、函数或场景','aria-label':'搜索覆盖记录'});
  const coverageRows=make('div',{class:'coverage-rows'}),coverageTabs=make('nav',{class:'workbench-tabs','aria-label':'覆盖分类'});
  for(const [value,label] of [['scenarios','已整理场景'],['files','文件阅读'],['outside','未纳入场景'],['pending','待确认关系']])coverageTabs.append(button(label,()=>{coverageTab=value;drawCoverage();},{'data-coverage-tab':value}));
  coverageSearch.oninput=drawCoverage;
  async function openCoverage(){
    if(!coverage.modal.open)coverage.modal.showModal();coverage.status.textContent='正在读取真实覆盖记录…';
    try{
      coverageData=await call('/api/coverage');const d=coverageData;
      const stats=make('div',{class:'coverage-stats'},...[
        ['源码阅读',d.reading.sources_read+' / '+d.reading.sources_total],['已整理场景',d.scenario_count],
        ['纳入场景的函数',d.callables_in_scenarios+' / '+d.callable_count],['待确认关系',d.pending_relations.length]
      ].map(([label,value])=>make('div',{},make('strong',{text:value}),make('span',{text:label}))));
      coverage.status.textContent=d.scope_note;
      coverage.body.replaceChildren(stats);
      if(d.snapshot.state!=='current')coverage.body.append(make('p',{class:'stale',text:'源码有变化，当前覆盖记录需要复核。新增 '+(d.snapshot.added?.length||0)+'、修改 '+(d.snapshot.changed?.length||0)+'、删除 '+(d.snapshot.deleted?.length||0)+'。'}));
      coverage.body.append(coverageTabs,coverageSearch,coverageRows);drawCoverage();
    }catch(error){coverage.status.textContent='覆盖记录读取失败：'+error.message;}
  }
  function drawCoverage(){
    if(!coverageData)return;const d=coverageData,q=coverageSearch.value.toLowerCase();
    coverageTabs.querySelectorAll('button').forEach(b=>{b.classList.toggle('active',b.dataset.coverageTab===coverageTab);b.setAttribute('aria-pressed',String(b.dataset.coverageTab===coverageTab));});
    let rows=coverageTab==='scenarios'?d.scenarios:coverageTab==='files'?d.files:coverageTab==='outside'?d.outside_scenarios:d.pending_relations;
    rows=rows.filter(row=>JSON.stringify(row).toLowerCase().includes(q));
    coverageRows.replaceChildren();
    if(coverageTab==='outside')coverageRows.append(make('p',{class:'muted',text:'这些函数可能已经读过，但尚未纳入已记录场景。这份列表不等于所有缺失路径。'}));
    for(const row of rows.slice(0,200)){
      const item=make('article',{class:'coverage-row'});
      if(coverageTab==='scenarios'){
        item.append(make('h3',{text:row.name}),make('p',{text:row.purpose||'用途尚未记录'}),make('small',{text:`${row.context_count} 个上下文 · ${row.data_count} 条数据关系 · ${row.pending_relations} 条待核对`}),button('查看场景',()=>{coverage.modal.close();options.openContext(row.id);}));
      }else if(coverageTab==='files'){
        item.append(make('strong',{text:row.path}),make('span',{text:!row.included?'未纳入阅读':row.read?'已读':'待阅读 / 复核'}));
      }else{
        const entity=coverageTab==='outside'?row.id:row.entity_id;
        const title=coverageTab==='outside'?row.name:row.from+' → '+row.to;
        item.append(make('h3',{text:title}),make('p',{text:coverageTab==='outside'?(row.reviewed?'函数阅读已核对，场景仍待整理':'函数及场景仍待核对'):row.reason}),
          button('让 Agent 继续分析',()=>{coverage.modal.close();return submitSelection({entity:{id:entity},context:row.context_id},'请核对 '+title+' 的输入、调用、数据去向及错误/分支边界，补充有源码依据的流程；不确定处说明原因。','analyze');}));
      }
      coverageRows.append(item);
    }
    coverageRows.append(make('p',{class:'muted',text:rows.length?`共 ${rows.length} 项${rows.length>200?'，显示前 200 项，可搜索缩小范围':''}`:'当前分类没有记录。'}));
  }

  let historyPreview=null;
  const historySelect=make('select',{'aria-label':'选择历史图谱版本'}),historyDetails=make('div',{class:'history-details'});
  const restoreButton=button('以此版本恢复图谱',()=>restore(),{disabled:''}),historyControls=make('div',{class:'workbench-actions'},make('label',{text:'历史版本'},historySelect),restoreButton);
  historySelect.onchange=()=>compareHistory();
  async function openHistory(){
    if(!history.modal.open)history.modal.showModal();history.status.textContent='正在读取已归档图谱…';
    try{
      const data=await call('/api/history');historySelect.replaceChildren(...data.snapshots.map(s=>make('option',{value:s.revision,text:'版本 '+s.revision})));
      history.body.replaceChildren(historyControls,historyDetails);
      if(!data.snapshots.length){history.status.textContent='还没有历史归档。源码更新或恢复前会保留当前图谱。';historyDetails.replaceChildren();restoreButton.disabled=true;return;}
      await compareHistory();
    }catch(error){history.status.textContent=error.message;}
  }
  async function compareHistory(){
    restoreButton.disabled=true;historyPreview=null;history.status.textContent='正在比较图谱记录和现有源码…';
    try{
      const selectedVersion=historySelect.value,p=await call('/api/history?revision='+encodeURIComponent(selectedVersion));
      if(selectedVersion!==historySelect.value)return;historyPreview=p;
      history.status.textContent=`历史版本 ${p.revision} → 当前版本 ${p.current_revision}。${p.scope_note}`;
      const labels={sources:'文件',entities:'节点',relations:'连线',flows:'数据身份'},rows=[];
      const fieldLabels={sha256:'源码版本',symbols_complete:'符号梳理完整性',included:'纳入范围',encoding:'文件编码',language:'语言',category:'文件分类',path:'文件路径',name:'名称',summary:'职责说明',analysis:'阅读状态',freshness:'依据时效',read_state:'文件阅读',evidence_ids:'源码依据',analysis_details:'函数分析',source_ids:'来源文件',roles:'角色',kind:'种类',context_id:'调用上下文',from_id:'起点',to_id:'终点',basis:'确认依据',reason:'待核对原因',line_count:'行数',bytes:'文件大小',producer_ids:'产生位置',derived_from:'输入来源',color_key:'数据标识',port_ids:'端口'};
      for(const [name,group] of Object.entries(p.changes)){
        const section=make('details',{class:'history-section'},make('summary',{text:`${labels[name]}：新增 ${group.added} · 删除 ${group.deleted} · 变化 ${group.modified}`}));
        for(const row of group.items){
          section.append(make('div',{class:'history-change '+row.kind},make('span',{text:row.before||'—'}),make('span',{text:row.after||'—'})));
          if(row.kind==='modified')section.append(make('p',{class:'history-fields',text:'变化项：'+row.fields.map(key=>fieldLabels[key]||key).join('、')}));
          if(row.before_summary!==row.after_summary&&(row.before_summary||row.after_summary))section.append(make('div',{class:'history-change descriptions'},make('p',{text:row.before_summary||'—'}),make('p',{text:row.after_summary||'—'})));
        }
        if(group.truncated)section.append(make('p',{class:'muted',text:'这里只展示前 160 项，完整图谱保存在归档中。'}));rows.push(section);
      }
      historyDetails.replaceChildren(make('div',{class:'history-columns'},make('strong',{text:'历史图谱'}),make('strong',{text:'当前图谱'})),...rows);
      for(const source of p.source_diffs){
        const section=make('details',{class:'history-section'},make('summary',{text:'源码对比 · '+source.path}));
        if(!source.available)section.append(make('p',{class:'muted',text:'这个早期版本没有保存完整源码文本，可以比较其图谱记录。'}));
        else{
          const code=make('pre',{class:'source-diff'});for(const line of source.diff.split('\n'))code.append(make('div',{class:line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-delete':'',text:line}));section.append(code);
          if(source.truncated)section.append(make('p',{class:'muted',text:'源码差异较长，显示开头部分。'}));
        }
        historyDetails.append(section);
      }
      historyDetails.append(make('p',{class:'restore-note',text:`恢复时会重新核对 ${p.source_update.affected_paths.length} 个受现有源码影响的文件。仓库源码文件不会被回退，阅读布局继续保留。`}));
      if(p.blocked_reason)historyDetails.append(make('p',{class:'stale',text:p.blocked_reason}));
      restoreButton.disabled=!p.can_restore;
    }catch(error){history.status.textContent='历史比较失败：'+error.message;}
  }
  async function restore(){
    if(!historyPreview)return;restoreButton.disabled=true;
    try{
      const result=await call('/api/history/restore',{revision:historyPreview.revision,expected_revision:historyPreview.current_revision,restore_id:historyPreview.restore_id});
      await options.changed();await openHistory();history.status.textContent=`已从版本 ${result.restored_from} 恢复为新版本 ${result.revision}。恢复前的版本 ${result.archived_revision} 已归档，仍可再恢复。`;
    }catch(error){history.status.textContent='恢复未执行：'+error.message+' 请重新比较后重试。';}
  }
  setInterval(()=>{if(options.graph())void refreshRequests();},2500);
  setInterval(()=>{if(options.graph()&&metrics.modal.open)void refreshMetrics();},5000);
  return {submitSelection,openCoverage,openHistory,refresh:refreshRequests};
}};
