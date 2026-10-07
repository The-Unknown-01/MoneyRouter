(() => {
 const charts = new Map();
 const drafts = new Map();
 let pendingConfirm = null;
 let previousPath=location.pathname;
 let savedScroll=0;
 let savedFocus=null;
 const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
 function initialize(root=document) {
  const main=document.querySelector('#app-main');
  if(main){const heading=main.querySelector('h1');if(heading)document.title=heading.textContent.trim()+' · 稳序';const period=main.querySelector('[name=period]');if(period)main.dataset.period=period.value;}
  const dataElement=document.querySelector('#chart-data');
  const data=dataElement?JSON.parse(dataElement.dataset.json):{};
  root.querySelectorAll('[data-chart]').forEach(element => {
   if(charts.has(element)) return;
   const key=element.dataset.chart;
   const palette=['#246654','#69a38b','#a3c2a3','#cfb780','#7c9baf'];
   let option=null;
   const common={animation:!reduced(),animationDuration:260,color:palette,textStyle:{fontFamily:'Segoe UI, Microsoft YaHei, sans-serif',color:'#6f8779'},tooltip:{trigger:'item',valueFormatter:v=>'¥ '+Number(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}};
   if(key==='categories'&&data.categories?.length) option={...common,grid:{left:85,right:30,top:10,bottom:25},xAxis:{type:'value',splitLine:{lineStyle:{color:'#edf1ec'}}},yAxis:{type:'category',data:data.categories.map(v=>v.name)},series:[{type:'bar',barMaxWidth:18,itemStyle:{borderRadius:[0,5,5,0]},data:data.categories.map(v=>v.value/100)}]};
   if(key==='budget'&&data.budget?.income_cents>0){const fields=[['necessary_cents','必要支出'],['debt_cents','债务还款'],['wants_cents','可选支出'],['savings_cents','储蓄']];option={...common,legend:{bottom:0,itemWidth:10,itemHeight:10},grid:{left:8,right:8,top:15,bottom:55},xAxis:{type:'value',show:false},yAxis:{type:'category',show:false,data:['分配']},series:fields.map(([field,name])=>({type:'bar',stack:'budget',name,barWidth:30,data:[data.budget[field]/100]}))};}
   if(key==='allocation'&&data.allocation?.some(v=>v.amount_cents>0))option={...common,legend:{bottom:0},series:[{type:'pie',radius:['52%','72%'],center:['50%','43%'],label:{show:false},data:data.allocation.map(v=>({name:v.category,value:v.amount_cents/100}))}]};
   if(key==='reserve'&&data.reserve?.target_cents>0){option={...common,legend:{bottom:0},grid:{left:8,right:8,top:15,bottom:50},xAxis:{type:'value',show:false},yAxis:{type:'category',show:false,data:['预备金']},series:[{type:'bar',name:'已有预备金',stack:'reserve',barWidth:24,data:[Math.min(data.reserve.existing_cents,data.reserve.target_cents)/100]},{type:'bar',name:'待补足',stack:'reserve',barWidth:24,itemStyle:{color:'#e5eee3'},data:[data.reserve.gap_cents/100]}]};}
   if(key==='diff'&&data.diff?.length){const rows=data.diff.filter(v=>v.planned_cents!=null&&v.actual_cents!=null);const labels={income:'收入',necessary:'必要支出',debt:'还款',wants:'可选支出',savings:'储蓄',balance:'结余'};if(rows.length)option={...common,tooltip:{...common.tooltip,trigger:'axis'},legend:{bottom:0},grid:{left:60,right:20,top:20,bottom:55},xAxis:{type:'category',axisLabel:{interval:0,fontSize:10},data:rows.map(v=>v.label||labels[v.layer]||v.layer)},yAxis:{type:'value'},series:[{name:'计划',type:'bar',data:rows.map(v=>v.planned_cents/100)},{name:'实际',type:'bar',data:rows.map(v=>v.actual_cents/100)}]};}
   if(key==='history'&&data.history?.length){const rows=data.history;option={...common,tooltip:{...common.tooltip,trigger:'axis'},legend:{bottom:0},grid:{left:65,right:20,top:15,bottom:65},xAxis:{type:'category',data:rows.map(v=>v.period)},yAxis:{type:'value'},series:[{name:'收入',type:'line',connectNulls:false,data:rows.map(v=>v.snapshot.income?.amount_cents==null?null:v.snapshot.income.amount_cents/100)},{name:'支出',type:'line',data:rows.map(v=>v.snapshot.spend_total_cents/100)},{name:'结余',type:'line',data:rows.map(v=>v.snapshot.balance_cents==null?null:v.snapshot.balance_cents/100)}]};}
   if(!option||!window.echarts){element.innerHTML='<div class="chart-empty">暂无可展示的数据</div>';return;}
   const chart=echarts.init(element,null,{renderer:'svg'});chart.setOption(option);
   if(key==='categories')chart.on('click',event=>{htmx.ajax('GET','/ledger?period='+encodeURIComponent(main.dataset.period)+'&category='+encodeURIComponent(event.name),{target:'#app-main',swap:'outerHTML'});history.pushState({},'', '/ledger?period='+encodeURIComponent(main.dataset.period)+'&category='+encodeURIComponent(event.name));});
   const observer=new ResizeObserver(()=>chart.resize());observer.observe(element);charts.set(element,{chart,observer});
  });
  document.querySelectorAll('form').forEach(form=>{const saved=drafts.get(location.pathname+'|'+form.getAttribute('action'));if(saved)for(const [name,value] of Object.entries(saved)){const input=form.elements[name];if(input&&input.type!=='file'&&!['csrf','request_id','id'].includes(name))input.value=value;}});
 const convo=document.querySelector('#conversation');if(convo)convo.scrollTop=convo.scrollHeight;
 }
 document.addEventListener('input',event=>{const input=event.target;const form=input.closest('form');if(!form||!input.name||input.type==='file')return;const key=location.pathname+'|'+form.getAttribute('action');const saved=drafts.get(key)||{};saved[input.name]=input.value;drafts.set(key,saved);});
 document.addEventListener('operationCompleted',()=>{for(const key of drafts.keys())if(key.startsWith(location.pathname+'|'))drafts.delete(key);});
 document.addEventListener('DOMContentLoaded',()=>initialize());
 document.addEventListener('htmx:historyRestore',()=>{for(const [el,value] of charts){if(!el.isConnected){value.observer.disconnect();value.chart.dispose();charts.delete(el);}}initialize(document);previousPath=location.pathname;});
 document.addEventListener('htmx:beforeRequest',event=>{
  const form=event.detail.elt.closest('form');if(!form)return;
  form.setAttribute('aria-busy','true');
  if(form.matches('[data-agent-form]')){
   const text=form.querySelector('[name=message]')?.value?.trim();
   if(text){const row=document.createElement('div');row.className='chat chat-end optimistic-message';const bubble=document.createElement('div');bubble.className='chat-bubble';bubble.textContent=text;row.append(bubble);document.querySelector('#conversation')?.append(row);row.scrollIntoView({block:'nearest',behavior:reduced()?'auto':'smooth'});}
  }
 });
 document.addEventListener('operationFailed',()=>{document.querySelectorAll('input[name=request_id]').forEach(input=>input.value=crypto.randomUUID());});
 document.addEventListener('htmx:beforeSwap',event=>{
  if(event.detail.target?.id==='app-main'){
   savedScroll=window.scrollY;
   const active=document.activeElement;
   savedFocus=active?.name?{name:active.name,action:active.form?.getAttribute('action')}:null;
  }
  if(event.detail.xhr.status===422||event.detail.xhr.status===409){event.detail.shouldSwap=true;event.detail.isError=false;}
  const target=event.detail.target;
  for(const [el,value] of charts){if(target===el||target?.contains(el)){value.observer.disconnect();value.chart.dispose();charts.delete(el);}}
 });
 document.addEventListener('htmx:afterSwap',event=>{
  initialize(document);
  if(event.detail.target?.id==='app-main'){
   if(previousPath===location.pathname){
    window.scrollTo(0,savedScroll);
    if(savedFocus){
     const form=Array.from(document.forms).find(f=>f.getAttribute('action')===savedFocus.action);
     const field=form?.elements[savedFocus.name];
     if(typeof field?.focus==='function')field.focus({preventScroll:true});
    }
   }else window.scrollTo(0,0);
   previousPath=location.pathname;
  }
 });
 document.addEventListener('htmx:afterRequest',event=>{event.detail.elt.closest('form')?.removeAttribute('aria-busy');if(event.detail.failed){document.querySelectorAll('.optimistic-message').forEach(el=>el.remove());const target=document.querySelector('#operation');if(target&&event.detail.xhr.status!==422)target.textContent='连接中断，输入已保留，请重试。';}});
 document.addEventListener('htmx:configRequest',event=>{
  const form=event.detail.elt.closest('form');if(form?.dataset.confirm&&!form.dataset.confirmed){event.preventDefault();pendingConfirm=form;document.querySelector('#confirm-text').textContent=form.dataset.confirm;document.querySelector('#confirm-dialog').showModal();}
 });
 document.addEventListener('click',event=>{
  const button=event.target.closest('[data-edit-id]');if(button){const form=document.querySelector('#transaction-form');for(const key of ['id','date','direction','amount','category','description','note'])form.elements[key].value=key==='id'?button.dataset.editId:button.dataset[key];document.querySelector('#manual-entry').open=true;if(matchMedia('(max-width:600px)').matches){const dialog=document.querySelector('#entry-dialog');dialog.querySelector('.entry-dialog-body').append(form);dialog.showModal();}else form.scrollIntoView({behavior:reduced()?'auto':'smooth',block:'center'});form.elements.amount.focus();}
  if(event.target.closest('[data-entry-close]'))document.querySelector('#entry-dialog').close();
  if(event.target.closest('[data-dialog-cancel]')){document.querySelector('#confirm-dialog').close();pendingConfirm=null;}
  if(event.target.closest('[data-dialog-confirm]')){document.querySelector('#confirm-dialog').close();if(pendingConfirm){pendingConfirm.dataset.confirmed='true';pendingConfirm.requestSubmit();delete pendingConfirm.dataset.confirmed;pendingConfirm=null;}}
 });
 document.addEventListener('close',event=>{if(event.target.id==='entry-dialog'){const form=event.target.querySelector('#transaction-form');if(form)document.querySelector('#manual-entry').append(form);}},true);
 document.addEventListener('change',event=>{if(event.target.name==='direction'){const select=event.target.form.elements.category;select.value=event.target.value==='income'?'工资':event.target.value==='transfer'?'转账':'餐饮';}});
})();

