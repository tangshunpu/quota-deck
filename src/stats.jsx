import React, {useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore} from 'react';
import './stats.css';

/* Token history for browser viewers. The HDMI kiosk never renders this module's output. */
const {t,dynamic:translate,getLanguage}=window.quotaI18n;
const subscribeLanguage=callback=>{window.addEventListener('quota-language-change',callback);return()=>window.removeEventListener('quota-language-change',callback)};
const useLanguage=()=>useSyncExternalStore(subscribeLanguage,getLanguage);
// Bottom-to-top stack and legend order. Codex sits between Claude's amber and Grok's grey
// so touching segments stay distinguishable in both themes.
const STACK=['claude','codex','grok'];
const FILTERS=['codex','grok','claude'];
// The grok category also holds Grok Bot and Cursor usage, so it is shown as xAI; the API id stays grok.
const NAMES={codex:'Codex',grok:'xAI',claude:'Claude'};
const SAVED='quota-deck.stats.v1';
const REFRESH=120000;
const MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const WEEKDAYS={zh:['周日','周一','周二','周三','周四','周五','周六'],en:['Sun','Mon','Tue','Wed','Thu','Fri','Sat']};

function compact(n){
 if(n===null||n===undefined)return '--';
 const value=n>=1e9?(n/1e9).toFixed(2)+'B':n>=1e6?(n/1e6).toFixed(n>=1e8?0:1)+'M':n>=1e3?(n/1e3).toFixed(n>=1e5?0:1)+'K':String(Math.round(n));
 return value.replace(/\.0(?=[KMB])/,'');
}
const share=(part,whole)=>{if(!whole)return '—';const p=part/whole*100;return p>0&&p<.1?'<0.1%':`${p<10?p.toFixed(1):Math.round(p)}%`};
const parts=iso=>iso.split('-').map(Number);
const weekday=iso=>{const [y,m,d]=parts(iso);return new Date(Date.UTC(y,m-1,d)).getUTCDay()};
const english=()=>getLanguage()==='en';
const shortDate=iso=>{const [,m,d]=parts(iso);return english()?`${MONTHS[m-1]} ${d}`:`${m}/${d}`};
const longDate=iso=>{const [y,m,d]=parts(iso),w=WEEKDAYS[getLanguage()][weekday(iso)];return english()?`${w}, ${MONTHS[m-1]} ${d}, ${y}`:`${y}年${m}月${d}日 ${w}`};
const monthName=month=>{const [,m]=parts(month);return english()?MONTHS[m-1]:`${m}月`};
const monthTitle=month=>{const [y,m]=parts(month);return english()?`${MONTHS[m-1]} ${y}`:`${y}年${m}月`};
const fullDate=iso=>{const [y,m,d]=parts(iso);return english()?`${MONTHS[m-1]} ${d}, ${y}`:`${y}/${m}/${d}`};
const range=(from,to)=>`${from} – ${to}`;
// Calendar days from 1 January through `iso`, inclusive.
const dayOfYear=iso=>{const [y,m,d]=parts(iso);return (Date.UTC(y,m-1,d)-Date.UTC(y,0,1))/864e5+1};
async function readTokens(signal,path,params){
 const response=await fetch(`/api/tokens/${path}?${new URLSearchParams(params)}`,{signal});
 if(response.status===401)throw Object.assign(new Error('auth'),{auth:true});
 const body=await response.json();
 if(!response.ok)throw Object.assign(new Error(body.error||''),{status:response.status});
 return body;
}
const totals=(entry,keys)=>Object.fromEntries(keys.map(k=>[k,entry.providers?.[k]?.total||0]));
function niceTicks(max){
 if(!(max>0))return [0];
 const raw=max/3,magnitude=10**Math.floor(Math.log10(raw));
 const step=[1,2,2.5,5,10].map(f=>f*magnitude).find(s=>s>=raw);
 return Array.from({length:Math.ceil(max/step-1e-9)+1},(_,i)=>i*step);
}
// Rounded data end, square at the baseline.
function barPath(x,y,w,h,r){
 r=Math.max(0,Math.min(r,h,w/2));
 return `M${x},${y+h}V${y+r}Q${x},${y} ${x+r},${y}H${x+w-r}Q${x+w},${y} ${x+w},${y+r}V${y+h}Z`;
}
// Width of a container plus its font size, so SVG geometry follows the page's type scale.
function useBox(){
 const ref=useRef(null),[box,setBox]=useState({width:0,em:16});
 useLayoutEffect(()=>{
  const element=ref.current;if(!element)return;
  const measure=()=>setBox({width:element.clientWidth,em:parseFloat(getComputedStyle(element).fontSize)||16});
  const observer=new ResizeObserver(measure);observer.observe(element);measure();
  return()=>observer.disconnect();
 },[]);
 return [ref,box];
}

function Tooltip({tip}){
 const ref=useRef(null),[position,setPosition]=useState(null);
 useLayoutEffect(()=>{
  if(!tip||!ref.current)return;
  const {width,height}=ref.current.getBoundingClientRect();
  let x=tip.x+14,y=tip.y+14;
  if(x+width>innerWidth-8)x=tip.x-14-width;
  if(y+height>innerHeight-8)y=tip.y-14-height;
  setPosition({x:Math.max(8,x),y:Math.max(8,y)});
 },[tip]);
 if(!tip)return null;
 return <div ref={ref} className="stats-tip" role="tooltip" style={position?{left:position.x,top:position.y}:{left:tip.x+14,top:tip.y+14,visibility:'hidden'}}>{tip.content}</div>;
}
function TipBody({title,note,entry,keys}){
 return <>
  <div className="tip-title">{title}{note?<span>{note}</span>:null}</div>
  {!entry.hasData?<div className="tip-empty">{t('无记录')}</div>:<>
   {keys.length>1?keys.map(k=><div className="tip-row" key={k}><i className={`key key-${k}`}/><b>{compact(entry.providers?.[k]?.total??0)}</b><span>{NAMES[k]}</span></div>):null}
   <div className="tip-row tip-total"><b>{compact(entry.total)}</b><span>{t('合计')}</span></div>
   <div className="tip-mix">{t('输入')} {compact(entry.input)} · {t('缓存读取')} {compact(entry.cached)} · {t('输出')} {compact(entry.output)}</div>
  </>}
 </>;
}

/* Stacked daily or monthly columns with a crisp y-axis; the hover target is the whole slot. */
function Columns({points,keys,height,sublabels,thin,label,tip,onTip,onLeave}){
 const [ref,{width,em}]=useBox();
 const max=Math.max(0,...points.map(p=>p.total)),ticks=niceTicks(max),top=ticks.at(-1)||1;
 const left=2.8*em,right=.2*em,head=1.3*em,foot=(sublabels?2.5:1.5)*em,h=height*em;
 const plot=h-head-foot,slot=Math.max(0,width-left-right)/points.length;
 const barWidth=Math.min(1.5*em,Math.max(2,slot*.62));
 const y=value=>head+plot-value/top*plot;
 const peak=max>0?points.findIndex(p=>p.total===max):-1,last=points.length-1;
 // On narrow charts keep every n-th label, counted back from the current point.
 const every=thin?Math.max(1,Math.ceil(2.6*em/slot)):1;
 return <div ref={ref} className="columns" style={{height:`${height}em`}}>
  {width?<svg width={width} height={h} role="img" aria-label={label}>
   <g className="axis">{ticks.map(v=><g key={v} className={v?'':'baseline'}>
    <line x1={left} x2={width-right} y1={Math.round(y(v))+.5} y2={Math.round(y(v))+.5}/>
    <text x={left-.45*em} y={y(v)} dy=".34em" textAnchor="end">{compact(v)}</text>
   </g>)}</g>
   {points.map((p,i)=>{
    const center=left+slot*(i+.5),x=center-barWidth/2,present=keys.filter(k=>p.values[k]>0);
    let base=y(0);
    const segments=present.map((k,j)=>{
     const size=Math.max(1,p.values[k]/top*plot);base-=size;
     // A 2px surface gap separates each segment from the one beneath it.
     const gap=j>0&&size>3?2:0;
     return <path key={k} className={`fill-${k}`} d={barPath(x,base,barWidth,size-gap,j===present.length-1?4:0)}/>;
    });
    const active=tip?.key===p.key;
    return <g key={p.key} className={`slot ${p.partial?'partial':''} ${p.current?'current':''}`}>
     <rect className={`wash ${active?'on':''}`} x={left+slot*i+1} y={head} width={Math.max(0,slot-2)} height={plot} rx="3"/>
     {segments}
     {!p.hasData?<circle className="no-data" cx={center} cy={y(0)-3.5} r="2.2"/>:null}
     {i===peak?<text className="peak" x={center} y={y(p.total)-.4*em} textAnchor="middle">{compact(p.total)}</text>:null}
     {p.label&&(last-i)%every===0?<text className="tick" x={center} y={head+plot+1.05*em} textAnchor="middle">{p.label}</text>:null}
     {p.sub?<text className="tick sub" x={center} y={head+plot+2.1*em} textAnchor="middle">{p.sub}</text>:null}
    </g>;
   })}
   {points.map((p,i)=><rect key={p.key} className="hit" x={left+slot*i} y={0} width={slot} height={h}
    onPointerMove={event=>onTip(event,p.key,p.tip)} onPointerDown={event=>onTip(event,p.key,p.tip)} onPointerLeave={onLeave}/>)}
  </svg>:null}
 </div>;
}

function Legend({providers,keys}){
 const total=keys.reduce((sum,k)=>sum+(providers[k]?.total||0),0);
 return <div className="legend">
  <div className="share" aria-hidden="true">{keys.map(k=>providers[k]?.total?<i key={k} className={`swatch-${k}`} style={{flexGrow:providers[k].total}}/>:null)}</div>
  <ul>{keys.map(k=><li key={k}><i className={`swatch swatch-${k}`}/><span>{NAMES[k]}</span><b>{compact(providers[k]?.total||0)}</b><small>{share(providers[k]?.total||0,total)}</small></li>)}</ul>
 </div>;
}
function Mix({usage}){
 return <div className="mix">{[['输入','input'],['缓存读取','cached'],['输出','output']].map(([name,key])=>
  <span key={key}>{t(name)} <b>{compact(usage[key])}</b> <small>{share(usage[key],usage.total)}</small></span>)}</div>;
}
function Tile({label,value,detail}){
 return <div><dt>{label}</dt><dd>{value}{detail?<small>{detail}</small>:null}</dd></div>;
}
function DataTable({headers,rows}){
 return <details className="data-table">
  <summary>{t('查看明细表')}</summary>
  <div className="table-scroll"><table>
   <thead><tr>{headers.map((h,i)=><th key={i} scope="col">{h}</th>)}</tr></thead>
   <tbody>{rows.map(row=><tr key={row.key} className={row.empty?'empty':''}>{row.cells.map((cell,i)=>i?<td key={i}>{cell}</td>:<th key={i} scope="row">{cell}</th>)}</tr>)}</tbody>
  </table></div>
 </details>;
}
const usageCells=(entry,keys)=>entry.hasData
 ?[...(keys.length>1?keys.map(k=>compact(entry.providers?.[k]?.total||0)):[]),compact(entry.input),compact(entry.cached),compact(entry.output),compact(entry.total)]
 :[...(keys.length>1?keys.map(()=>'—'):[]),'—','—','—',t('无记录')];
const usageHeaders=(first,keys)=>[first,...(keys.length>1?keys.map(k=>NAMES[k]):[]),t('输入'),t('缓存读取'),t('输出'),t('合计')];

function Period({title,data,keys,tip,onTip,onLeave,prefix}){
 const s=data.summary,last=data.daily.length-1,week=data.days<=7;
 const points=data.daily.map((d,i)=>{
  const today=data.includesToday&&i===last;
  return {key:`${prefix}${d.date}`,values:totals(d,keys),total:d.total,hasData:d.hasData,current:today,
   label:week||(last-i)%7===0?shortDate(d.date):'',sub:week?(today?t('今天'):WEEKDAYS[getLanguage()][weekday(d.date)]):'',
   tip:<TipBody title={longDate(d.date)} note={today?t('今天尚未结束'):''} entry={d} keys={keys}/>};
 });
 return <article className="stats-card">
  <header className="stats-card-head"><h3>{title}</h3><span>{range(shortDate(data.startDate),shortDate(data.endDate))}</span></header>
  <div className="hero"><strong>{compact(s.total)}</strong><span>{t('Token 总量')}{data.includesToday?` · ${t('含今天（未结束）')}`:''}</span></div>
  <dl className="tiles">
   <Tile label={t('日均')} value={compact(s.averagePerDay)}/>
   <Tile label={t('峰值日')} value={s.peak?compact(s.peak.total):'—'} detail={s.peak?shortDate(s.peak.date):''}/>
   <Tile label={t('活跃天数')} value={s.activeDays} detail={`/ ${data.days}`}/>
  </dl>
  <Mix usage={s}/>
  {keys.length>1?<Legend providers={data.providers} keys={keys}/>:null}
  <Columns points={points} keys={keys} height={week?11.5:10.5} sublabels={week} label={`${title} · ${t('合计')} ${compact(s.total)}`} tip={tip} onTip={onTip} onLeave={onLeave}/>
  <DataTable headers={usageHeaders(t('日期'),keys)} rows={[...data.daily].reverse().map(d=>({key:d.date,empty:!d.hasData,cells:[shortDate(d.date)+(data.includesToday&&d.date===data.endDate?` · ${t('今天')}`:''),...usageCells(d,keys)]}))}/>
 </article>;
}

function Monthly({data,keys,tip,onTip,onLeave}){
 const s=data.summary,months=data.monthly;
 const best=months.reduce((a,b)=>b.total>a.total?b:a,months[0]);
 const current=data.includesCurrentMonth?months.at(-1):null;
 const points=months.map(m=>({key:`mo:${m.month}`,values:totals(m,keys),total:m.total,hasData:m.hasData,partial:m.isPartial,current:m===current,
  label:monthName(m.month),sub:m.isPartial?t('进行中'):'',
  tip:<TipBody title={monthTitle(m.month)} note={m.isPartial?t('截至 {date}，未满月',{date:shortDate(m.endDate)}):''} entry={m} keys={keys}/>}));
 return <article className="stats-card wide">
  <header className="stats-card-head"><h3>{t('月度趋势')}</h3><span>{range(monthTitle(months[0].month),monthTitle(months.at(-1).month))}</span></header>
  <div className="split">
   <div className="split-side">
    <div className="hero"><strong>{compact(s.total)}</strong><span>{t('近 {n} 个月合计',{n:data.months})}</span></div>
    <dl className="tiles">
     <Tile label={t('月均')} value={compact(s.averagePerMonth)}/>
     <Tile label={t('最高月份')} value={best.total?compact(best.total):'—'} detail={best.total?monthName(best.month):''}/>
     <Tile label={t('本月至今')} value={current?compact(current.total):'—'}/>
    </dl>
    {keys.length>1?<Legend providers={data.providers} keys={keys}/>:null}
   </div>
   <Columns points={points} keys={keys} height={13} sublabels thin label={`${t('月度趋势')} · ${t('合计')} ${compact(s.total)}`} tip={tip} onTip={onTip} onLeave={onLeave}/>
  </div>
  <p className="footnote">{t('半透明柱为未满月份，只统计到今天。')}</p>
  <DataTable headers={[...usageHeaders(t('月份'),keys).slice(0,-1),t('活跃天数'),t('合计')]}
   rows={[...months].reverse().map(m=>{const cells=usageCells(m,keys);return {key:m.month,empty:!m.hasData,
    cells:[monthTitle(m.month)+(m.isPartial?` · ${t('进行中')}`:''),...cells.slice(0,-1),m.hasData?m.activeDays:'—',cells.at(-1)]}})}/>
 </article>;
}

// Levels and each level's upper bound come from the API: quartiles of the days with usage.
function Heatmap({data,year,years,onYear,pending,tip,onTip,onLeave}){
 const [ref,{width,em}]=useBox(),scroller=useRef(null);
 const weeks=data.weeks,gap=3,labelWidth=2.2*em,head=1.4*em;
 const cell=Math.floor(Math.max(10,Math.min(1.1*em,(width-labelWidth-4)/weeks.length-gap)));
 const svgWidth=weeks.length*(cell+gap)-gap+1,svgHeight=head+7*(cell+gap);
 // Narrow screens scroll the year sideways; start at the most recent weeks.
 useLayoutEffect(()=>{const el=scroller.current;if(el)el.scrollLeft=el.scrollWidth},[width,data.startDate]);
 // Label the week that contains each month's first day, skipping labels that would collide.
 const months=[];
 weeks.forEach((week,i)=>{const first=week.find(c=>c&&c.date.endsWith('-01'));if(first&&(!months.length||i-months.at(-1).i>=3))months.push({i,month:first.date.slice(0,7)})});
 const s=data.summary,heat=`heat-${data.filters.provider||'all'}`;
 return <article className={`stats-card wide ${pending?'is-pending':''}`} aria-busy={pending}>
  <header className="stats-card-head heat-head">
   <div><h3>{t('热力图')}</h3><span>{range(fullDate(data.startDate),fullDate(data.endDate))} · {t('每格一天')}</span></div>
   <div className="segmented year-picker" role="group" aria-label={t('选择时间范围')}>
    {['',...years].map(y=><button key={y||'recent'} type="button" aria-pressed={year===y} onClick={()=>onYear(y)}>{y||t('近一年')}</button>)}
   </div>
  </header>
  <dl className="tiles inline">
   <Tile label={year?t('{year} 年合计',{year}):t('近一年合计')} value={compact(s.total)}/>
   <Tile label={t('有记录天数')} value={s.daysWithData} detail={`/ ${data.days}`}/>
   <Tile label={t('峰值日')} value={s.peak?compact(s.peak.total):'—'} detail={s.peak?shortDate(s.peak.date):''}/>
   <Tile label={t('日均')} value={compact(s.averagePerDay)}/>
  </dl>
  <div ref={ref} className="heat-frame">
   {width?<svg className="heatmap heat-days" width={labelWidth} height={svgHeight} aria-hidden="true">
    {[1,3,5].map(d=><text key={d} className="tick" x={labelWidth-.4*em} y={head+d*(cell+gap)+cell/2} dy=".34em" textAnchor="end">{WEEKDAYS[getLanguage()][d]}</text>)}
   </svg>:null}
   <div ref={scroller} className="heat-scroll">
    {width?<svg className={`heatmap ${heat}`} width={svgWidth} height={svgHeight} role="img"
     aria-label={`${t('热力图')} · ${t('有记录天数')} ${s.daysWithData} / ${data.days}`}>
     {months.map(m=><text key={m.month} className="tick" x={m.i*(cell+gap)} y={head*.62}>{monthName(m.month)}</text>)}
     {weeks.map((week,i)=>week.map((c,d)=>{
      if(!c)return null;
      const key=`h:${c.date}`,x=i*(cell+gap),y=head+d*(cell+gap),today=data.includesToday&&c.date===data.endDate;
      const body=<><div className="tip-title">{longDate(c.date)}{today?<span>{t('今天尚未结束')}</span>:null}</div>
       {c.hasData?<div className="tip-row tip-total"><b>{compact(c.total)}</b><span>Token</span></div>:<div className="tip-empty">{t('无记录')}</div>}</>;
      return <rect key={c.date} className={`cell ${c.hasData?`l${c.level}`:'empty'} ${today?'is-today':''} ${tip?.key===key?'on':''}`}
       x={x+.5} y={y+.5} width={cell-1} height={cell-1} rx="2.5"
       onPointerMove={event=>onTip(event,key,body)} onPointerDown={event=>onTip(event,key,body)} onPointerLeave={onLeave}/>;
     }))}
    </svg>:null}
   </div>
  </div>
  <div className="heat-legend">
   {data.scale.upper.map((top,l)=><span key={l} className="heat-key"><i className={`${heat} l${l}`}/>{l?top===null?'—':`≤ ${compact(top)}`:'0'}</span>)}
   <span className="heat-key"><i className="empty"/>{t('无记录')}</span>
   <small>{t('颜色按有记录日的四分位分为 4 档')}</small>
  </div>
 </article>;
}

function loadFilters(){
 try{
  const saved=JSON.parse(localStorage.getItem(SAVED));
  return {provider:FILTERS.includes(saved?.provider)?saved.provider:'',host:typeof saved?.host==='string'?saved.host:'',
   year:/^\d{4}$/.test(saved?.year)?saved.year:''};
 }catch{return {provider:'',host:'',year:''}}
}
export function TokenStats(){
 useLanguage();
 const [filters,setFilters]=useState(loadFilters);
 const [data,setData]=useState(null),[hosts,setHosts]=useState([]),[years,setYears]=useState([]),[today,setToday]=useState('');
 const [heat,setHeat]=useState(null),[heatPending,setHeatPending]=useState(true);
 const [status,setStatus]=useState('loading'),[pending,setPending]=useState(true),[failed,setFailed]=useState(false);
 const [tip,setTip]=useState(null);
 useEffect(()=>{try{localStorage.setItem(SAVED,JSON.stringify(filters))}catch{}},[filters]);
 useEffect(()=>{
  let active=true,timer,controller;
  const load=async()=>{
   clearTimeout(timer);controller?.abort();
   const current=controller=new AbortController(),read=(path,params)=>readTokens(current.signal,path,params);
   setPending(true);
   const scope=Object.fromEntries(Object.entries({provider:filters.provider,host:filters.host}).filter(([,v])=>v));
   try{
    // Unfiltered reads supply every host (selector and sync warnings) and every year with records.
    const [week,month,monthly,all,decade]=await Promise.all([read('history',{...scope,days:7}),read('history',{...scope,days:30}),
     read('monthly',{...scope,months:12}),read('history',{days:1}),read('monthly',{months:120})]);
    if(!active)return;
    const thisYear=all.endDate.slice(0,4);
    setData({week,month,monthly});setHosts(all.hosts);setToday(all.endDate);setStatus('ready');setFailed(false);
    setYears([...new Set([thisYear,...decade.monthly.filter(m=>m.hasData).map(m=>m.month.slice(0,4))])].sort().reverse());
   }catch(error){
    if(!active||error.name==='AbortError')return;
    // A saved host that no longer exists falls back to every host.
    if(error.status===400&&filters.host){setFilters(f=>({...f,host:''}));return}
    if(error.auth)setStatus('auth');else{setFailed(true);setStatus(s=>s==='ready'?s:'error')}
   }finally{if(active&&controller===current){setPending(false);timer=setTimeout(load,REFRESH)}}
  };
  load();
  const visible=()=>{if(document.visibilityState==='visible')load()};
  document.addEventListener('visibilitychange',visible);
  return()=>{active=false;clearTimeout(timer);controller?.abort();document.removeEventListener('visibilitychange',visible)};
 },[filters.provider,filters.host]);
 // The heatmap reloads on its own, so picking a year leaves the other charts untouched.
 useEffect(()=>{
  if(filters.year&&!today)return;
  let active=true,timer,controller;
  const load=async()=>{
   clearTimeout(timer);controller?.abort();
   const current=controller=new AbortController();
   const params=Object.fromEntries(Object.entries({provider:filters.provider,host:filters.host}).filter(([,v])=>v));
   if(filters.year){
    // A calendar year runs 1 January to 31 December; the current year stops at today.
    const end=filters.year===today.slice(0,4)?today:`${filters.year}-12-31`;
    Object.assign(params,{end,days:dayOfYear(end)});
   }
   setHeatPending(true);
   try{const result=await readTokens(current.signal,'heatmap',params);if(active)setHeat(result)}
   catch(error){
    if(!active||error.name==='AbortError')return;
    // A saved year outside the data, or in the future, falls back to the past year.
    if(error.status===400&&filters.year)setFilters(f=>({...f,year:''}));
   }finally{if(active&&controller===current){setHeatPending(false);timer=setTimeout(load,REFRESH)}}
  };
  load();
  const visible=()=>{if(document.visibilityState==='visible')load()};
  document.addEventListener('visibilitychange',visible);
  return()=>{active=false;clearTimeout(timer);controller?.abort();document.removeEventListener('visibilitychange',visible)};
 },[filters.provider,filters.host,filters.year,today]);
 useEffect(()=>{
  if(!tip)return;
  const outside=event=>{if(!event.target.closest?.('.hit,.cell'))setTip(null)};
  const clear=()=>setTip(null);
  document.addEventListener('pointerdown',outside);document.addEventListener('scroll',clear,{capture:true,passive:true});window.addEventListener('blur',clear);
  return()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('scroll',clear,{capture:true});window.removeEventListener('blur',clear)};
 },[tip!==null]);
 const onTip=(event,key,content)=>setTip({x:event.clientX,y:event.clientY,key,content});
 const onLeave=event=>{if(event.pointerType!=='touch')setTip(null)};
 // Keys follow the data on screen, not a filter whose request is still in flight.
 const provider=data?.week.filters.provider||'';
 const keys=provider?[provider]:STACK;
 const unsynced=hosts.filter(h=>h.status!=='ok');
 const shared={keys,tip,onTip,onLeave};
 return <section className="stats" aria-labelledby="stats-title">
  <header className="stats-head">
   <h2 id="stats-title">{t('Token 统计')}</h2>
   {data?<p>{t('所有主机按 {timezone} 归日；总量 = 新输入 + 缓存读取 + 输出。空心标记表示无记录，不代表确认没有使用。',{timezone:data.week.timezone})}</p>:null}
  </header>
  <div className="stats-filters">
   <div className="segmented" role="group" aria-label={t('按服务筛选')}>
    {['',...FILTERS].map(p=><button key={p||'all'} type="button" aria-pressed={filters.provider===p} onClick={()=>setFilters(f=>({...f,provider:p}))}>
     {p?<i className={`swatch swatch-${p}`}/>:null}{p?NAMES[p]:t('全部服务')}</button>)}
   </div>
   <label className="host-filter"><span>{t('主机')}</span>
    <select value={filters.host} onChange={event=>setFilters(f=>({...f,host:event.target.value}))}>
     <option value="">{t('全部主机')}</option>
     {hosts.map(h=><option key={h.id} value={h.id}>{translate(h.name)}</option>)}
     {filters.host&&!hosts.some(h=>h.id===filters.host)?<option value={filters.host}>{filters.host}</option>:null}
    </select>
   </label>
   {pending&&data?<span className="stats-sync">{t('更新中…')}</span>:null}
  </div>
  {unsynced.length||failed?<p className="stats-warning" role="status">
   {unsynced.map(h=>t(h.status==='offline'?'{name} 离线，统计保留已保存的数据':'{name} 部分日志读取失败',{name:translate(h.name)})).join('；')}
   {failed&&data?`${unsynced.length?'；':''}${t('统计暂时无法刷新，显示上次结果')}`:''}
  </p>:null}
  {status==='auth'?<div className="stats-card stats-message"><p>{t('查看统计需要先连接面板。')}</p><a href="/control/">{t('前往遥控页登录')}</a></div>
   :!data?<div className="stats-card stats-message"><p>{t(status==='error'?'暂时无法读取统计，稍后自动重试。':'正在读取统计…')}</p></div>
   :<div className={`stats-body ${pending?'is-pending':''}`} aria-busy={pending}>
    <div className="stats-pair">
     <Period title={t('近 7 天')} data={data.week} prefix="w:" {...shared}/>
     <Period title={t('近 30 天')} data={data.month} prefix="m:" {...shared}/>
    </div>
    <Monthly data={data.monthly} {...shared}/>
    {heat?<Heatmap data={heat} year={filters.year} years={years} onYear={year=>setFilters(f=>({...f,year}))} pending={heatPending} tip={tip} onTip={onTip} onLeave={onLeave}/>
     :<div className="stats-card stats-message"><p>{t('正在读取统计…')}</p></div>}
   </div>}
  <Tooltip tip={tip}/>
 </section>;
}
