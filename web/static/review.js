(() => {
 const valid = v => typeof v === 'number' && Number.isFinite(v);
 const yuan = v => valid(v) ? v / 100 : null;
 const axis = {type:'value', axisLabel:{fontSize:10},splitLine:{lineStyle:{color:'#edf1ec'}}};
 const labels = {income_cents:'实际收入',spend_total_cents:'实际支出',balance_cents:'净结余',investment_return_cents:'投资盈亏'};
 window.reviewChartOption = (key, data, common, mode='cashflow') => {
  const summary=data.review || {}, insights=summary.insights?.schema_version ? summary.insights : summary.diff?.insights || {}, diff=summary.diff || {};
  const base={...common, animationDuration:500, aria:{enabled:true}, tooltip:{...common.tooltip,trigger:'axis',confine:true}, legend:{bottom:0,itemWidth:10,itemHeight:10,textStyle:{fontSize:11,color:'#718275'}},grid:{left:65,right:22,top:20,bottom:55}};
  if(key==='review-comparison') {
   const rows=(insights.metrics || []).filter(m=>labels[m.id]);
   if(!rows.some(m=>valid(m.current_cents)||valid(m.previous_cents)))return null;
   return {...base,xAxis:{type:'category',data:rows.map(m=>labels[m.id]),axisLabel:{fontSize:10,interval:0}},yAxis:axis,series:[{name:'上月',type:'bar',barMaxWidth:28,itemStyle:{color:'#c7d5bf',borderRadius:[4,4,0,0]},data:rows.map(m=>yuan(m.previous_cents))},{name:insights.review_mode==='final'?'本月':'本月截至资料日',type:'bar',barMaxWidth:28,itemStyle:{color:'#3e735c',borderRadius:[4,4,0,0]},data:rows.map(m=>yuan(m.current_cents))}]};
  }
  if(key==='review-categories') {
   const rows=(diff.categories || []).filter(m=>valid(m.actual_cents)||valid(m.planned_cents)).sort((a,b)=>(b.actual_cents || 0)-(a.actual_cents || 0));
   if(!rows.length)return null;
   return {...base, dataZoom:rows.length>6?[{type:'inside',yAxisIndex:0,start:0,end:6/rows.length*100},{type:'slider',yAxisIndex:0,width:8,right:0,start:0,end:6/rows.length*100}]:[], grid:{left:72,right:30,top:10,bottom:45},xAxis:axis,yAxis:{type:'category',inverse:true,data:rows.map(m=>m.category),axisLabel:{fontSize:11,width:60,overflow:'truncate'}},series:[{name:'预算',type:'bar',barMaxWidth:12,itemStyle:{color:'#dce5d5',borderRadius:[0,4,4,0]},data:rows.map(m=>yuan(m.planned_cents))},{name:'实际支出',type:'bar',barMaxWidth:12,data:rows.map(m=>({value:yuan(m.actual_cents),itemStyle:{color:valid(m.planned_cents)&&m.actual_cents>m.planned_cents?'#b98261':'#5e896f',borderRadius:[0,4,4,0]}}))}]};
  }
  if(key==='review-history') {
   const records=new Map((data.history || []).filter(r=>r.period<=summary.period).map(r=>[r.period,r.snapshot]));
   if(!records.size)return null;
   const periods=[...records.keys()].sort(), first=periods[0], last=periods.at(-1), months=[];
   let [y,m]=first.split('-').map(Number);
   while(months.length<120){const p=`${y}-${String(m).padStart(2,'0')}`;if(p>last)break;months.push(p);if(++m>12){m=1;y++}}
   const fields=mode==='returns'?[['investment_return_cents','投资盈亏','#b09254']]:[['income_cents','收入','#9bbfa7'],['spend_total_cents','支出','#b9a47b'],['balance_cents','结余','#2d7159']];
   const get=(s,id)=>{if(!s)return null;const actual=s.income?.role==='actual';if(id==='income_cents')return actual?s.income.amount_cents:null;if(id==='balance_cents')return actual?s.balance_cents:null;if(id==='investment_return_cents')return s.investments?.month_return_cents;return s[id]};
   return {...base,xAxis:{type:'category',boundaryGap:false,data:months},yAxis:axis,series:fields.map(([id,name,color])=>({name,type:'line',smooth:false,connectNulls:false,symbolSize:7,lineStyle:{width:2,color},itemStyle:{color},areaStyle:id==='balance_cents'?{color:'#377b5910'}:undefined,data:months.map(p=>{const s=records.get(p);return {value:yuan(get(s,id)),symbol:s?.coverage_complete===true?'circle':'emptyCircle'}})}))};
  }
  return null;
 };
})();
