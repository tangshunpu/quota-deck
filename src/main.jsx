import React, {memo, useEffect, useRef, useState, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
import './style.css';
import {TokenStats} from './stats.jsx';
import {ResetNews} from './reset-news.jsx';

const {t,dynamic:translate,getLanguage,setLanguage}=window.quotaI18n;
const subscribeLanguage=callback=>{window.addEventListener('quota-language-change',callback);return()=>window.removeEventListener('quota-language-change',callback)};
const useLanguage=()=>useSyncExternalStore(subscribeLanguage,getLanguage);
function LanguageToggle(){
 const language=useLanguage(),[saving,setSaving]=useState(false),[error,setError]=useState(false);
 const change=async()=>{
  const next=language==='zh'?'en':'zh';
  if(!NATIVE){setLanguage(next);return}
  setSaving(true);setError(false);
  try{
   const response=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-Quota-Deck':'1'},body:JSON.stringify({action:'language',language:next}),signal:AbortSignal.timeout(5000)});
   if(!response.ok)throw new Error();
   // The screen poll applies the saved language and acknowledges its revision.
  }catch{setError(true)}finally{setSaving(false)}
 };
 return <button className="language-toggle" disabled={saving} onClick={change} aria-label={language==='zh'?'Switch to English':'切换到中文'} title={error?t('无法保存语言设置，请重试'):language==='zh'?'Switch to English':'切换到中文'}>{error?'!':language==='zh'?'EN':'中文'}</button>;
}
// Theme colours live in CSS; Grok keeps its monochrome identity in both modes.
const META={
 codex:{name:'Codex',logo:'openai'},
 grok:{name:'Grok',logo:'xai'},
 claude:{name:'Claude',logo:'claude'}};
const INITIAL=Object.keys(META).map(id=>({id,status:'loading',windows:[],note:'正在读取本机账户'}));
const STATUS={ready:'已连接',loading:'连接中',missing:'待连接',expired:'登录已过期',unavailable:'额度未知',error:'读取失败',limited:'稍后重试',stale:'上次读数'};
const REDUCED=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
// Quota reset celebration: ring refill, then flashing and fireworks. REFILL_MS matches the CSS delays.
const RESET_RISE=10,REFILL_MS=1500,CELEBRATION_MS=5200;
const NATIVE=!!window.webkit?.messageHandlers?.quota;
// Browsers get a scaled dashboard with token statistics below; the HDMI kiosk keeps its full-screen layout.
if(!NATIVE)document.documentElement.classList.add('web');
const DISPLAY_ACTIVITY={lastPollAt:null,alerts:{}};
const WEEK=['周日','周一','周二','周三','周四','周五','周六'];
function Icon({name,...props}){
 const paths={
  chevron:<path d="m9 6 6 6-6 6"/>,close:<path d="m7 7 10 10M17 7 7 17"/>,
  sun:<><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/></>,
  moon:<path d="M20.5 13.1A8.6 8.6 0 0 1 10.9 3.5a8.6 8.6 0 1 0 9.6 9.6Z"/>,
  check:<><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></>,
  input:<><path d="M4 4h16v12H9l-5 4V4Z"/><path d="M8 8h8m-8 4h5"/></>,
  refresh:<><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.5-1.7L20 8M4 16l2.4 2.7A7 7 0 0 0 18 17"/></>,
  power:<><path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/></>,
  database:<><ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8v-13"/><path d="M4.5 12c0 1.6 3.4 2.8 7.5 2.8s7.5-1.2 7.5-2.8"/></>};
 return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>{paths[name]}</svg>;
}
function tokens(n){
 if(n===null||n===undefined)return '--';
 if(n>=1e9)return (n/1e9).toFixed(2)+'B';
 if(n>=1e6)return (n/1e6).toFixed(n>=1e8?0:1)+'M';
 if(n>=1e3)return (n/1e3).toFixed(n>=1e5?0:1)+'K';
 return String(Math.round(n));
}
const bytes=n=>n>=1024**3?`${(n/1024**3).toFixed(1)}G`:`${Math.round(n/1024**2)}M`;
const hhmm=t=>new Date(t*1000).toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit'});
// Eases a displayed number toward its target, so new readings count up instead of jumping.
function useTween(target,duration=900){
 const [value,setValue]=useState(target===null||target===undefined?null:REDUCED?target:0);
 const from=useRef(value);
 useEffect(()=>{
  if(target===null||target===undefined){setValue(null);from.current=null;return}
  const start=from.current??0;
  if(REDUCED||start===target){setValue(target);from.current=target;return}
  let frame;const begin=performance.now();
  const step=t=>{const k=Math.min(1,(t-begin)/duration),eased=1-Math.pow(1-k,3),v=start+(target-start)*eased;
   setValue(v);from.current=v;if(k<1)frame=requestAnimationFrame(step)};
  frame=requestAnimationFrame(step);return()=>cancelAnimationFrame(frame);
 },[target,duration]);
 return value;
}
// Briefly true after `value` changes; drives the bounce on updated readings.
function useFlash(value,ms=700){
 const [on,setOn]=useState(false),first=useRef(true);
 useEffect(()=>{if(first.current){first.current=false;return}if(value===null||value===undefined)return;setOn(true);const t=setTimeout(()=>setOn(false),ms);return()=>clearTimeout(t)},[value,ms]);
 return on;
}
function useClock(){const [now,setNow]=useState(Date.now());useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[]);return now}
function ThemeToggle(){
 const [theme,setTheme]=useState(()=>document.documentElement.dataset.theme||'dark');
 useEffect(()=>{
  const update=()=>setTheme(document.documentElement.dataset.theme);
  window.addEventListener('quota-theme-change',update);
  update();
  return()=>window.removeEventListener('quota-theme-change',update);
 },[]);
 const label=t(theme==='dark'?'切换到浅色模式':'切换到深色模式');
 return <button className="theme-toggle" onClick={()=>window.quotaTheme.toggle()} aria-label={label} title={t('自动：18:00–07:00 深色；07:00–18:00 浅色。{label}，下个时间点恢复自动。',{label})}><Icon name={theme==='dark'?'sun':'moon'}/></button>;
}
function useScreenControl(){
 const [control,setControl]=useState(null);
 const applied=useRef(null),reloadId=useRef(null);
 useEffect(()=>{
  if(!NATIVE)return;
  let active=true,timer,controller;
  const poll=async()=>{
   controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),3500);
   try{
    const response=await fetch('/api/control',{signal:controller.signal});
    if(!response.ok)throw new Error();
    const state=await response.json();if(!active)return;
    if(applied.current?.instance!==state.instance)reloadId.current=state.reloadId;
    else if(reloadId.current!==state.reloadId){location.reload();return}
    window.quotaTheme.setOverride(state.theme.override);
    setLanguage(state.language);
    if(applied.current?.revision!==state.revision||applied.current?.instance!==state.instance){
     applied.current=state;setControl(state);
    }
   }catch{/* Keep the last screen state while reconnecting. */}
   finally{clearTimeout(timeout);if(active)timer=setTimeout(poll,1000)}
  };
  poll();return()=>{active=false;clearTimeout(timer);controller?.abort()};
 },[]);
 // Acknowledge after React commits the new card state, not merely after receiving it.
 useEffect(()=>{
  if(!NATIVE||!control)return;
  const send=()=>fetch('/api/display/heartbeat',{method:'POST',headers:{'Content-Type':'application/json','X-Quota-Deck':'1'},
   body:JSON.stringify({instance:control.instance,revision:control.revision,theme:document.documentElement.dataset.theme,language:getLanguage(),
    activity:{lastPollAt:DISPLAY_ACTIVITY.lastPollAt,alerts:Object.values(DISPLAY_ACTIVITY.alerts)}}),signal:AbortSignal.timeout(3000)}).catch(()=>{});
  send();const timer=setInterval(send,2000);return()=>clearInterval(timer);
 },[control]);
 const details=(provider,open)=>{
  if(!NATIVE)return;
  fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-Quota-Deck':'1'},body:JSON.stringify({action:'details',provider,open})}).catch(()=>{});
 };
 return {control,details};
}
function useAgentActivity(){
 const [activity,setActivity]=useState({providers:{},notices:{},resets:{}});
 useEffect(()=>{
  let live=true,first=true,timer,controller;let seen=new Set();
  const poll=async()=>{
   controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),3500);
   try{
    const response=await fetch('/api/activity',{signal:controller.signal});if(!response.ok)throw new Error();
    const data=await response.json();if(!live)return;
    DISPLAY_ACTIVITY.lastPollAt=Date.now()/1000;
    // Reset previews only replay the quota celebration; they never become task alerts.
    const notices={},resets={};
    for(const event of data.events||[]){
     if(!first&&!seen.has(event.id)&&Date.now()/1000-event.at<30)(event.kind==='reset'?resets:notices)[event.provider]=event;
     seen.add(event.id);
    }
    first=false;seen=new Set([...seen].slice(-128));
    setActivity(previous=>({providers:data.providers||{},notices:{...previous.notices,...notices},resets:{...previous.resets,...resets}}));
   }catch{if(live)setActivity(previous=>({...previous,providers:{}}))}
   finally{clearTimeout(timeout);if(live)timer=setTimeout(poll,2000)}
  };
  poll();return()=>{live=false;clearTimeout(timer);controller?.abort()};
 },[]);
 return activity;
}
// Polls `url`; retries every 10 s until the backend has data, then every `interval` ms.
function usePoll(url,interval){
 const [data,setData]=useState(null);
 useEffect(()=>{let active=true,timer;
  const load=()=>fetch(url).then(r=>r.ok?r.json():null).catch(()=>null).then(d=>{if(!active)return;if(d)setData(d);timer=setTimeout(load,d?interval:10000)});
  load();return()=>{active=false;clearTimeout(timer)}},[url,interval]);
 return data;
}
// Load colour: green below 50 %, orange to 80 %, red above.
const pressure=v=>v===null||v===undefined?'':v>=80?'lvl-high':v>=50?'lvl-mid':'lvl-ok';

/* ---------- Header ---------- */
function WeatherIcon({kind,day}){
 const sun=<g className="wx-sun"><circle cx="16" cy="16" r="6.5"/>{Array.from({length:8},(_,i)=><line key={i} x1="16" y1="4.5" x2="16" y2="2" transform={`rotate(${i*45} 16 16)`}/>)}</g>;
 const moon=<path className="wx-moon" d="M19 6a10 10 0 1 0 8 14A8 8 0 0 1 19 6z"/>;
 const cloud=(x=0,y=0,cls='wx-cloud')=><g transform={`translate(${x} ${y})`}><path className={cls} d="M9 26h15a6 6 0 0 0 0-12 8 8 0 0 0-15-1A6.5 6.5 0 0 0 9 26z"/></g>;
 const drops=[8,14,20].map((x,i)=><line key={x} className="wx-drop" x1={x} y1="28" x2={x-1.5} y2="31.5" style={{animationDelay:`${i*.25}s`}}/>);
 return <svg viewBox="0 0 34 34" className={`wx wx-${kind}`} aria-hidden="true">
  {kind==='clear'?(day?sun:moon):null}
  {['partly','cloud'].includes(kind)?<>{day?<g transform="translate(6 -4) scale(.85)">{sun}</g>:<g transform="translate(9 -3) scale(.7)">{moon}</g>}{cloud(0,2)}</>:null}
  {kind==='fog'?[12,17,22,27].map((y,i)=><line key={y} className="wx-fog" x1="5" y1={y} x2="29" y2={y} style={{animationDelay:`${i*.4}s`}}/>):null}
  {['rain','drizzle'].includes(kind)?<>{cloud(0,-6)}{drops}</>:null}
  {kind==='snow'?<>{cloud(0,-6)}{[8,14,20].map((x,i)=><circle key={x} className="wx-flake" cx={x} cy="29" r="1.3" style={{animationDelay:`${i*.4}s`}}/>)}</>:null}
  {kind==='storm'?<>{cloud(0,-6)}<path className="wx-bolt" d="M17 20l-4 7h4l-2 6 6-9h-4l2-4z"/></>:null}
 </svg>;
}
// PM2.5 level label colours (dark enough to read as text on their own 14 % tint).
const AIR={'优':'#248A3D','良':'#A16A00','轻度污染':'#C77700','中度污染':'#D70015','重度污染':'#8944AB','严重污染':'#7F3B1D'};
function Weather(){
 const w=usePoll('/api/weather',300000);
 const round=v=>v===null||v===undefined?'--':Math.round(v);
 if(!w)return <div className="section weather"><WeatherIcon kind="partly" day/><div className="muted">{t('天气加载中…')}</div></div>;
 const tip=[`${t('体感')} ${round(w.feelsLike)}°`,typeof w.wind==='string'?translate(w.wind):`${t('风速')} ${round(w.wind)} km/h`,`PM10 ${round(w.pm10)}`,w.aqi!=null?`AQI ${w.aqi} ${translate(w.aqiCategory)||''}`:'',
  w.tomorrow?`${t('明天')} ${translate(w.tomorrow.label)||''} ${round(w.tomorrow.low)}~${round(w.tomorrow.high)}°${w.tomorrow.pop!=null?` ${t('降水')} ${w.tomorrow.pop}%`:''}`:'',`${t('来源')} ${w.source||''}`].filter(Boolean).join(' · ');
 return <div className="section weather" title={tip}>
  <WeatherIcon kind={w.kind} day={w.day}/>
  <div className="wx-now"><strong>{round(w.temperature)}<small>°C</small></strong><span>{translate(w.city)} · {translate(w.label)}</span></div>
  <i className="divider"/>
  <div className="wx-more"><span>{round(w.low)} - {round(w.high)}°</span>
   <span className="pm">PM2.5 <b>{round(w.pm25)}</b>{w.level?<em style={{'--air':AIR[w.level]||'#6b7280'}}>{t(w.level.replace('污染',''))}</em>:null}</span></div>
 </div>;
}
function Meter({label,value,detail,detailLevel,title,narrow}){
 return <div className={`meter ${narrow?'meter-narrow':''} ${pressure(value)}`} title={title}>
  <div className="meter-top"><span>{label}</span><strong>{value===null?'--':`${value.toFixed(0)}%`}</strong>{detail?<em className={detailLevel||''}>{detail}</em>:null}</div>
  <div className="meter-bar"><i style={{width:`${Math.max(value??0,value?4:0)}%`}}/></div>
 </div>;
}
function Hardware({now}){
 const [system,setSystem]=useState(null),[failed,setFailed]=useState(false);
 useEffect(()=>{
  let active=true,inflight=false,controller;
  const update=async()=>{if(inflight)return;inflight=true;controller=new AbortController();const timer=setTimeout(()=>controller.abort(),3500);
   try{const response=await fetch('/api/system',{signal:controller.signal});if(!response.ok)throw new Error();const data=await response.json();if(active){setSystem(data);setFailed(false)}}
   catch{if(active)setFailed(true)}finally{inflight=false;clearTimeout(timer)}};
  update();const timer=setInterval(update,2000);return()=>{active=false;clearInterval(timer);controller?.abort()};
 },[]);
 const stale=failed||!system?.updatedAt||now/1000-system.updatedAt>10;
 const memory=system?.memory;
 const gpus=system?.gpus?.length?system.gpus:[{index:'0',percent:null,name:t('GPU 读数不可用')}];
 return <>
  <Meter label="CPU" narrow value={stale?null:system.cpu} title={t('所有 CPU 核心的总利用率')}/>
  <i className="divider"/>
  <Meter label="MEM" value={stale?null:memory?.percent??null} detail={!stale&&memory?`${bytes(memory.usedBytes)}/${bytes(memory.totalBytes)}`:''} detailLevel={stale?'':pressure(memory?.percent)} title={t('内存：总量减去可用内存')}/>
  {gpus.map(gpu=>{const vram=!stale&&gpu.usedBytes!=null&&gpu.totalBytes;return <React.Fragment key={gpu.index}><i className="divider"/>
   <Meter label={gpus.length>1?`GPU${gpu.index}`:'GPU'} value={stale?null:gpu.percent} detail={vram?`${bytes(gpu.usedBytes)}/${bytes(gpu.totalBytes)}`:''} detailLevel={vram?pressure(gpu.usedBytes/gpu.totalBytes*100):''} title={`${gpu.name||'GPU'} · ${t('计算利用率；右侧为显存占用')}`}/></React.Fragment>})}
 </>;
}

/* ---------- Pixel pets: one mascot per provider, reacting to live state ---------- */
const SPRITES={
 claude:{w:14,body:[
  '...OOOOOOOO...',
  '..OOOOOOOOOO..',
  '..OOOOOOOOOO..',
  '..OOOOOOOOOO..',
  '..OOOOOOOOOO..',
  '..OOOOOOOOOO..',
  '..OOOOOOOOOO..',
  '..O.O....O.O..',
  '..O.O....O.O..'],
  eyes:[[4,2],[4,3],[9,2],[9,3]],arms:{l:[[0,3],[1,3],[0,4],[1,4]],r:[[12,3],[13,3],[12,4],[13,4]]},
  palette:{O:'#D98943'}},
 codex:{w:14,body:[
  '..TTTTTTTTTT..',
  '..TSSSSSSSST..',
  '..TSSSSSSSST..',
  '..TSSSSSSSST..',
  '..TSSSSSSSST..',
  '..TSSSSSSSST..',
  '..TTTTTTTTTT..',
  '...TT....TT...',
  '...TT....TT...'],
  eyes:[[4,2],[5,3],[4,4]],cursor:[[7,4],[8,4]],antenna:[[6,-2],[6,-1]],arms:{l:[[0,3],[1,3]],r:[[12,3],[13,3]]},
  palette:{T:'#1685E8',S:'#EAF5FF'}},
};
const LINES={working:'处理中…',low:'额度告急',sleep:'zzz',idle:''};
// Grok's mascot: a glossy black ball whose two slanted highlights double as eyes.
function GrokBall({mood,line,width}){
 // Proportions measured from the reference image (ball r = 4.7).
 const eye=(cx,cy,key,w)=>mood==='sleep'
  ?<rect key={key} x={cx-.75} y={cy+.2} width="1.5" height=".5" rx=".25" transform={`rotate(-12 ${cx} ${cy})`}/>
  :<rect key={key} x={cx-w/2} y={cy-.97} width={w} height="1.94" rx={w/2} transform={`rotate(-26 ${cx} ${cy})`}/>;
 return <g className={`pet pet-${mood} grok-ball`}>
  <ellipse cx="7" cy="9.7" rx="4.2" ry=".6" className="pet-shadow"/>
  <g className="pet-body"><g className="ball-squash">
   <circle cx="7" cy="5" r="4.7" className="ball"/>
   <g className="pet-eyes ball-eyes">{eye(8.03,2.88,'l',.85)}{eye(10.1,2.5,'r',.62)}</g>
   {mood==='low'?<rect x="2.6" y="-.4" width=".8" height="1.4" rx=".4" className="pet-sweat"/>:null}
  </g></g>
  {mood==='working'?<g className="pet-sparks"><rect x="-.6" y="1" width=".8" height=".8"/><rect x="13.4" y="0" width=".8" height=".8"/><rect x="13" y="2.6" width=".6" height=".6"/></g>:null}
  {line?<g transform="translate(14.6 -1.2)"><g className="bubble"><rect x="0" y="0" width={width} height="4.4" rx="2.2"/><path d="M.4 3.2l-1.6 1.2 2-.2z" className="bubble-tail"/><text x={width/2} y="3.15" textAnchor="middle">{line}</text></g></g>:null}
 </g>;
}
function Pet({id,mood}){
 if(id==='grok'){const line=t(LINES[mood]);return <GrokBall mood={mood} line={line} width={line?line.length*(getLanguage()==='en'?1.25:2.3)+2.6:0}/>}
 const s=SPRITES[id],px=(x,y,fill,key)=><rect key={key} x={x} y={y} width="1.02" height="1.02" fill={fill}/>;
 const ink='#1f2937',accent=Object.values(s.palette)[0];
 const body=[],outline=[];
 s.body.forEach((row,y)=>[...row].forEach((c,x)=>{if(s.palette[c]){body.push(px(x,y,s.palette[c],`${x}-${y}`));outline.push(<rect key={`${x}-${y}`} x={x-.4} y={y-.4} width="1.8" height="1.8"/>)}}));
 [...(s.antenna||[]),...s.arms.l,...s.arms.r].forEach(([x,y],i)=>outline.push(<rect key={'a'+i} x={x-.4} y={y-.4} width="1.8" height="1.8"/>));
 const line=t(LINES[mood]),width=line?line.length*(getLanguage()==='en'?1.25:2.3)+2.6:0;
 return <g className={`pet pet-${mood}`}>
  <ellipse cx={s.w/2} cy={9.7} rx={5} ry={.6} className="pet-shadow"/>
  <g className="pet-body">
   <g className="pet-outline">{outline}</g>
   {s.antenna?<g className="pet-antenna">{s.antenna.map(([x,y],i)=>px(x,y,i?accent:'#38bdf8',i))}</g>:null}
   {body}
   <g className="pet-arm pet-arm-l">{s.arms.l.map(([x,y],i)=>px(x,y,accent,i))}</g>
   <g className="pet-arm pet-arm-r">{s.arms.r.map(([x,y],i)=>px(x,y,accent,i))}</g>
   <g className="pet-eyes">{mood==='sleep'?s.eyes.filter((_,i)=>i%2===0).map(([x,y],i)=><rect key={i} x={x-.2} y={y+.5} width="1.4" height=".4" fill={ink}/>):s.eyes.map(([x,y],i)=>px(x,y,ink,i))}</g>
   {s.cursor?<g className="pet-cursor">{s.cursor.map(([x,y],i)=>px(x,y,ink,i))}</g>:null}
   {mood==='low'?<rect x="12.4" y="-.6" width=".8" height="1.4" className="pet-sweat"/>:null}
  </g>
  {mood==='working'?<g className="pet-sparks"><rect x="-1.5" y="1" width=".8" height=".8"/><rect x="15" y="0" width=".8" height=".8"/><rect x="14.6" y="2.6" width=".6" height=".6"/></g>:null}
  {line?<g transform="translate(15.6 -1.2)"><g className="bubble"><rect x="0" y="0" width={width} height="4.4" rx="2.2"/><path d="M.4 3.2l-1.6 1.2 2-.2z" className="bubble-tail"/><text x={width/2} y="3.15" textAnchor="middle">{line}</text></g></g>:null}
 </g>;
}
// True for 3 minutes after this provider's token total last grew.
function useActive(total){
 const [active,setActive]=useState(false),previous=useRef(total);
 useEffect(()=>{const before=previous.current;previous.current=total;if(before!=null&&total>before){setActive(true);const t=setTimeout(()=>setActive(false),180000);return()=>clearTimeout(t)}},[total]);
 return active;
}

/* ---------- Provider cards ---------- */
function Reset({at,now}){
 if(!at)return null;
 const left=Math.max(0,Math.floor(at-now/1000));
 if(!left)return <span className="warning">{t('等待刷新')}</span>;
 const days=Math.floor(left/86400),hours=Math.floor(left%86400/3600),minutes=Math.floor(left%3600/60);
 const en=getLanguage()==='en';
 const time=days?`${days}${en?'d ':'天'}${hours}${en?'h':'时'}`:hours?`${hours}${en?'h ':'时'}${minutes}${en?'m':'分'}`:`${minutes}${en?'m':'分'}`;
 return <span title={t('{days} 天 {hours} 小时 {minutes} 分钟后重置',{days,hours,minutes})}>{t('{time}后重置',{time})}</span>;
}
// Sweeps the ring from the pre-reset reading up to the current one, then hands over to the party phase.
function useRefill(celebrate,target){
 const [state,setState]=useState({value:null,phase:null}),goal=useRef(target);
 goal.current=target;
 useEffect(()=>{
  if(!celebrate){setState({value:null,phase:null});return}
  if(REDUCED){setState({value:null,phase:'party'});return}
  let frame;const begin=performance.now(),from=celebrate.from??0;
  const step=now=>{
   const k=Math.min(1,(now-begin)/REFILL_MS),eased=k<.5?4*k*k*k:1-Math.pow(-2*k+2,3)/2;
   if(k<1){setState({value:from+((goal.current??0)-from)*eased,phase:'refill'});frame=requestAnimationFrame(step)}
   else setState({value:null,phase:'party'});
  };
  frame=requestAnimationFrame(step);return()=>cancelAnimationFrame(frame);
 },[celebrate?.id]);
 return state;
}
const Gauge=memo(function Gauge({id,remaining,label,stale,mood,celebrate}){
 useLanguage();
 const tween=useTween(remaining,1600),pop=useFlash(remaining===null?null:Math.round(remaining));
 const refill=useRefill(celebrate,remaining),shown=refill.value??tween;
 const cx=150,cy=140,radius=112,length=2*Math.PI*radius*.75,full=2*Math.PI*radius;
 const point=(angle,r)=>{const radians=angle*Math.PI/180;return [cx+Math.cos(radians)*r,cy+Math.sin(radians)*r]};
 const arc=(cls,dash)=><circle cx={cx} cy={cy} r={radius} className={cls} strokeDasharray={dash} transform={`rotate(135 ${cx} ${cy})`}/>;
 const [kx,ky]=point(135+270*(shown??0)/100,radius);
 return <div className={`gauge ${remaining===null?'unknown':''} ${stale?'old':''} ${refill.phase?`celebrate-${refill.phase}`:''}`} role="img" aria-label={remaining===null?t('剩余额度未知'):t('{label}剩余 {value}%',{label:translate(label),value:remaining})}>
  <svg viewBox="0 6 300 236" aria-hidden="true">
   {arc('arc-track',`${length} ${full}`)}
   {refill.phase&&shown>0?arc('arc-glow',`${length*shown/100} ${full}`):null}
   {shown!==null&&shown>0?arc('arc-fill',`${length*shown/100} ${full}`):null}
   {shown!==null?<g transform={`translate(${kx} ${ky})`} className="knob"><circle r="15" className="knob-halo"/><circle r="10.5" className="knob-dot"/></g>:null}
   <g transform="translate(124.8 203) scale(3.6)"><Pet id={id} mood={mood}/></g>
  </svg>
  <div className="gauge-value"><div className={`reading ${pop?'pop':''}`}>{shown===null?'--':Math.round(shown)}{shown!==null?<span>%</span>:null}</div><div className="reading-label">{celebrate?`${celebrate.preview?t('预览')+' · ':''}${t('额度已重置')}`:t('剩余额度')}</div></div>
 </div>;
});
function Row({name,value,suffix,bar,reset,warn}){
 const shown=value===null||value===undefined?'—':`${value}${suffix}`;
 return <div className="row">
  <div className="row-head"><span className="row-name">{translate(name)}</span>{reset?<span className={`row-reset ${warn?'warning':''}`}>{reset}</span>:null}<b>{shown}</b></div>
  {bar}
 </div>;
}
const Bar=({value,pips})=>pips!=null
 ?<div className="pips">{Array.from({length:Math.max(pips,3)},(_,i)=><i key={i} className={i<pips?'on':''}/>)}</div>
 :<div className="bar"><i style={{width:`${value??0}%`}}/></div>;
function Windows({provider,short,weekly,now,stale}){
 const live=w=>w&&!(w.resetsAt&&w.resetsAt<=now/1000)?w.remaining:null;
 const credits=!short?provider.resetCredits:null;
 const left=credits?.nextExpiry?credits.nextExpiry-now/1000:null;
 const expiry=left===null?'':left>=86400?t('{n}天后过期',{n:Math.floor(left/86400)}):left>=3600?t('{n}小时后过期',{n:Math.floor(left/3600)}):t('{n}分后过期',{n:Math.max(1,Math.floor(left/60))});
 return <div className={`panel windows ${stale?'old':''}`}>
  {credits?<Row name="额度重置" value={credits.available} suffix={t(' 次')} reset={credits.available?expiry:''} warn={left!==null&&left<86400} bar={<Bar pips={credits.available}/>}/>
   :<Row name={short?.label||'当前窗口'} value={live(short)} suffix="%" reset={short?<Reset at={short.resetsAt} now={now}/>:t('服务未提供')} bar={<Bar value={live(short)}/>}/>}
  <Row name={weekly?.label||'每周额度'} value={live(weekly)} suffix="%" reset={weekly?<Reset at={weekly.resetsAt} now={now}/>:t('服务未提供')} bar={<Bar value={live(weekly)}/>}/>
 </div>;
}
function HourBars({hourly}){
 // The last eight hours, current hour last.
 const hour=new Date().getHours(),values=Array.from({length:8},(_,i)=>hourly?.[(hour-7+i+24)%24]||0),max=Math.max(1,...values);
 return <div className="hours" aria-hidden="true">{values.map((v,i)=><i key={i} className={i===7?'now':''} style={{height:`${v?Math.max(14,v/max*100):10}%`}}/>)}</div>;
}
function TokenRow({usage}){
 const total=useTween(usage?.total??null),pop=useFlash(usage?.total??null);
 const [delta,setDelta]=useState(null),previous=useRef(usage?.total);
 useEffect(()=>{
  const before=previous.current;previous.current=usage?.total;
  if(before!=null&&usage?.total>before){const key=Date.now();setDelta({key,value:usage.total-before});const t=setTimeout(()=>setDelta(d=>d?.key===key?null:d),2600);return()=>clearTimeout(t)}
 },[usage?.total]);
 return <div className="token-row">
  <Icon name="database" className="token-icon"/>
  <span className="token-label">{t('今日 Token')}</span>
  <HourBars hourly={usage?.hourly}/>
  <strong className={pop?'pop':''}>{tokens(total)}{delta?<em key={delta.key} className="delta">+{tokens(delta.value)}</em>:null}</strong>
 </div>;
}
function Details({provider,usage,hosts,id,now,onClose,onRefresh,refreshing}){
 const line=(k,v)=><div className="detail-line"><span>{translate(k)}</span><b>{v}</b></div>;
 return <div className="details">
  <div className="panel">
   {line('今日输入',tokens(usage?.input))}{line('缓存读取',tokens(usage?.cached))}{line('输出',tokens(usage?.output))}
   {(hosts||[]).length>1?hosts.map(h=><React.Fragment key={h.name}>{line(translate(h.name)+(h.status==='ok'?'':` · ${t('离线')}`),tokens(h.providers?.[id]?.total??null))}</React.Fragment>):null}
  </div>
  <div className="panel">
   {line('状态',t(STATUS[provider.status]||'未知'))}{line('最近同步',provider.updatedAt?hhmm(provider.updatedAt):'--')}
   {provider.note?<div className="detail-note">{translate(provider.note)}</div>:null}
  </div>
  <div className="detail-actions">
   <button className="pill-button" onClick={onRefresh} disabled={refreshing}><Icon name="refresh"/>{t(refreshing?'刷新中':'刷新额度')}</button>
   {NATIVE?<button className="pill-button danger" onClick={()=>window.webkit.messageHandlers.quota.postMessage('exit')}><Icon name="power"/>{t('退出面板')}</button>:null}
  </div>
 </div>;
}
// A fresh reading 10+ points above the previous fresh one means that window reset. Missing, unknown
// or stale readings never count, and nothing is assumed at the reset time itself.
function useQuotaReset(windows,fresh,preview){
 const last=useRef(null),[celebration,setCelebration]=useState(null);
 const readings=Object.fromEntries(windows.filter(w=>typeof w.remaining==='number').map(w=>[w.minutes??w.label,w.remaining]));
 const signature=JSON.stringify(readings);
 useEffect(()=>{
  if(!fresh)return;
  const before=last.current;last.current=readings;
  if(before&&Object.entries(readings).some(([key,value])=>before[key]!=null&&value-before[key]>=RESET_RISE))
   setCelebration({id:`reset-${Date.now()}`,from:Math.min(...Object.values(before))});
 },[signature,fresh]);
 useEffect(()=>{if(preview)setCelebration({id:preview.id,from:0,preview:true})},[preview?.id]);
 useEffect(()=>{if(!celebration)return;const timer=setTimeout(()=>setCelebration(null),CELEBRATION_MS);return()=>clearTimeout(timer)},[celebration?.id]);
 return celebration;
}
// Firework bursts around the ring, then confetti drifting down the card.
function Fireworks({delay}){
 const ref=useRef(null);
 useEffect(()=>{
  const canvas=ref.current,box=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1,ctx=canvas.getContext('2d');
  if(!ctx||!box.width)return;
  canvas.width=Math.round(box.width*ratio);canvas.height=Math.round(box.height*ratio);ctx.scale(ratio,ratio);
  const accent=getComputedStyle(canvas).getPropertyValue('--accent').trim()||'#3D8BFF';
  const colors=[accent,'#3D8BFF','#9A5CFF','#FF5FA8','#FFB534','#2ED3A6'],pick=()=>colors[Math.floor(Math.random()*colors.length)];
  const ring=canvas.parentElement.querySelector('.gauge')?.getBoundingClientRect();
  const cx=ring?ring.left-box.left+ring.width/2:box.width/2,cy=ring?ring.top-box.top+ring.height/2:box.height*.4,rh=ring?.height||box.height*.4;
  const u=box.width/330,parts=[];
  const burst=(x,y,count)=>{
   const color=pick();parts.push({flash:true,x,y,vx:0,vy:0,age:0,life:.35,size:30*u,color});
   for(let i=0;i<count;i++){const a=Math.PI*2*i/count+Math.random()*.25,v=(90+Math.random()*160)*u;
    parts.push({spark:true,x,y,vx:Math.cos(a)*v,vy:Math.sin(a)*v,age:0,life:1.1+Math.random()*.7,size:(2+Math.random()*1.6)*u,color:Math.random()<.5?color:pick(),twinkle:Math.random()<.4})}};
  const confetti=count=>{for(let i=0;i<count;i++)parts.push({x:Math.random()*box.width,y:-Math.random()*box.height*.35-8,vx:(Math.random()-.5)*30*u,vy:(50+Math.random()*50)*u,
   age:0,life:3.2,size:(6+Math.random()*4)*u,color:colors[i%colors.length],spin:Math.random()*6.3,turn:(Math.random()-.5)*14,sway:Math.random()*6.3})};
  // Bursts sit above the reading so the new number stays legible.
  const plan=[[0,()=>burst(cx,cy-rh*.34,56)],[.15,()=>confetti(40)],[.35,()=>burst(cx-box.width*.27,cy-rh*.22,40)],[.7,()=>burst(cx+box.width*.27,cy-rh*.2,40)],[1.1,()=>burst(cx,cy-rh*.42,34)]];
  let frame,start=null,last=0;
  const tick=now=>{
   if(start===null)start=last=now;
   const t=(now-start-delay)/1000,dt=Math.min(.05,(now-last)/1000);last=now;
   while(plan.length&&t>=plan[0][0])plan.shift()[1]();
   ctx.clearRect(0,0,box.width,box.height);
   for(const p of parts){
    p.age+=dt;
    if(p.spark){const drag=Math.pow(.25,dt);p.vx*=drag;p.vy=p.vy*drag+150*u*dt}
    else if(!p.flash){p.vy=Math.min(p.vy+40*u*dt,90*u);p.vx+=Math.sin(p.age*4+p.sway)*30*u*dt;p.spin+=p.turn*dt}
    p.x+=p.vx*dt;p.y+=p.vy*dt;
    const fade=Math.max(0,1-Math.pow(p.age/p.life,2.2));if(!fade)continue;
    ctx.globalAlpha=p.twinkle?fade*(.55+.45*Math.sin(p.age*30)):fade;ctx.fillStyle=ctx.strokeStyle=p.color;
    if(p.flash){const r=p.size*(.5+p.age/p.life),glow=ctx.createRadialGradient(p.x,p.y,0,p.x,p.y,r);
     glow.addColorStop(0,'#fff');glow.addColorStop(.35,p.color);glow.addColorStop(1,'transparent');ctx.fillStyle=glow;ctx.beginPath();ctx.arc(p.x,p.y,r,0,Math.PI*2);ctx.fill()}
    else if(p.spark){ctx.lineWidth=p.size;ctx.lineCap='round';ctx.beginPath();ctx.moveTo(p.x-p.vx*.06,p.y-p.vy*.06);ctx.lineTo(p.x,p.y);ctx.stroke()}
    else{const flip=Math.abs(Math.cos(p.spin*1.7))*p.size/2+1;ctx.save();ctx.translate(p.x,p.y);ctx.rotate(p.spin);ctx.fillRect(-p.size/2,-flip/2,p.size,flip);ctx.restore()}
   }
   ctx.globalAlpha=1;
   for(let i=parts.length-1;i>=0;i--)if(parts[i].age>=parts[i].life)parts.splice(i,1);
   if(plan.length||parts.length)frame=requestAnimationFrame(tick);
  };
  frame=requestAnimationFrame(tick);return()=>cancelAnimationFrame(frame);
 },[]);
 return <canvas ref={ref} className="fireworks" aria-hidden="true"/>;
}
function Provider({provider,now,offline,usage,hosts,index,onRefresh,refreshing,screenOpen,onDetails,agent,notice,resetPreview}){
 const meta=META[provider.id],windows=provider.windows||[];
 const [localOpen,setOpen]=useState(false);
 const open=screenOpen??localOpen;
 const valid=windows.filter(w=>!w.resetsAt||w.resetsAt>now/1000);
 const main=valid.length?valid.reduce((a,b)=>a.remaining<=b.remaining?a:b):null;
 const old=offline||provider.status==='stale'||(provider.updatedAt&&now/1000-provider.updatedAt>900);
 const status=old&&windows.length?'stale':provider.status;
 const weekly=windows.find(w=>w.minutes===10080),short=windows.find(w=>w!==weekly);
 const celebration=useQuotaReset(windows,provider.status==='ready'&&!old,resetPreview);
 const tokenActive=useActive(usage?.total);
 const active=agent&&agent.state!=='unknown'?agent.state==='working':tokenActive;
 const card=useRef(null),[alert,setAlert]=useState(false);
 const waitingSignal=agent?.state==='waiting'?{
  kind:'waiting',reason:agent.reason,host:agent.host,preview:false,
  id:notice?.kind==='waiting'&&!notice.preview&&notice.host===agent.host?notice.id:`waiting:${provider.id}:${agent.host||'local'}`
 }:null;
 // Real waiting follows the live state, so it survives the event's expiry and page reloads.
 // Previews and completion notifications remain time limited.
 const shownAlert=waitingSignal||(alert&&notice&&(notice.preview||notice.kind==='completed')?notice:null);
 useEffect(()=>{
  if(!notice)return;
  setAlert(true);
  const reduce=window.matchMedia('(prefers-reduced-motion: reduce)');
  const motion=!reduce.matches?card.current?.animate([
   {transform:'translateX(0)'},{transform:'translateX(-2px)'},{transform:'translateX(2px)'},
   {transform:'translateX(-1px)'},{transform:'translateX(1px)'},{transform:'translateX(0)'}
  ],{duration:420,easing:'ease-out'}):null;
  const stopMotion=()=>{if(reduce.matches)motion?.cancel()};reduce.addEventListener('change',stopMotion);
  const timer=setTimeout(()=>setAlert(false),15000);
  return()=>{clearTimeout(timer);motion?.cancel();reduce.removeEventListener('change',stopMotion)};
 },[notice?.id]);
 useEffect(()=>{
  if(!NATIVE||!shownAlert)return;
  const frame=requestAnimationFrame(()=>{
   if(DISPLAY_ACTIVITY.alerts[provider.id]?.id!==shownAlert.id)DISPLAY_ACTIVITY.alerts[provider.id]={
    id:shownAlert.id,provider:provider.id,kind:shownAlert.kind,host:shownAlert.host||'本机',preview:!!shownAlert.preview,
    startedAt:Date.now()/1000,duration:waitingSignal?null:15};
  });
  return()=>cancelAnimationFrame(frame);
 },[shownAlert?.id,provider.id]);
 const recentNotice=notice&&(notice.preview||notice.kind==='completed')&&now/1000-notice.at<12?notice:null;
 const signal=shownAlert||recentNotice||(agent?.state==='completed'&&now/1000-agent.at<15?{kind:agent.state,reason:agent.reason}:null);
 const signalText=signal?`${signal.preview?t('预览')+' · ':''}${t(signal.kind==='completed'?'已完成':signal.reason==='permission'?'等待确认':'等待输入')}`:null;
 const mood=status!=='ready'&&status!=='loading'?'sleep':main&&main.remaining<20?'low':active?'working':'idle';
 const plan=provider.plan?`${provider.plan}${provider.tier?' '+provider.tier:''}`:provider.status==='loading'?'':t('订阅');
 return <article ref={card} className={`card provider provider-${provider.id} ${active?'is-active':''} ${signal?`agent-${signal.kind}`:''} ${celebration?'celebrating':''}`} style={{'--delay':`${index*.1}s`}}>
  {active&&!shownAlert?<span className="activity-border" aria-hidden="true"/>:null}
  {shownAlert?<span key={shownAlert.id} className={`agent-alert alert-${shownAlert.kind}`} aria-hidden="true"><span className="alert-marquee"/></span>:null}
  {celebration?<span key={`${celebration.id}-glow`} className="celebration-glow" aria-hidden="true"/>:null}
  {celebration&&!REDUCED?<Fireworks key={celebration.id} delay={REFILL_MS}/>:null}
  <header className="provider-head">
   <span className="logo" style={{'--logo':`url('/logos/${meta.logo}.svg')`}} role="img" aria-label={`${meta.name} logo`}/>
   <div className="provider-text">
    <h2>{meta.name}{plan?<span className="plan">{plan}</span>:null}</h2>
    {signal?<p className="agent-status" role="status" title={provider.account||''}><Icon name={signal.kind==='completed'?'check':'input'}/>{signalText}</p>:<p className={status==='ready'||status==='loading'?'':'warning'}>{status==='ready'||status==='loading'?(provider.account||' '):`${t(STATUS[status])} · ${provider.account||''}`}</p>}
   </div>
   <button className={`chevron ${open?'open':''}`} onClick={()=>{setOpen(!open);onDetails(provider.id,!open)}} aria-label={t(open?'返回':'查看详情')}><Icon name={open?'close':'chevron'}/></button>
  </header>
  <div className="provider-news">{provider.id==='codex'?<ResetNews now={now} native={NATIVE}/>:null}</div>
  {open?<Details provider={provider} usage={usage} hosts={hosts} id={provider.id} now={now} onClose={()=>setOpen(false)} onRefresh={onRefresh} refreshing={refreshing}/>:<>
   <Gauge id={provider.id} remaining={main?.remaining??null} label={main?.label} stale={old} mood={mood} celebrate={celebration}/>
   <Windows provider={provider} short={short} weekly={weekly} now={now} stale={old}/>
  </>}
  <TokenRow usage={usage}/>
 </article>;
}

function App(){
 const language=useLanguage();
 const screen=useScreenControl();
 const activity=useAgentActivity();
 const now=useClock(),[data,setData]=useState({providers:INITIAL,refreshing:true}),[offline,setOffline]=useState(false),[toast,setToast]=useState('');
 const mounted=useRef(true),busy=useRef(false);
 useEffect(()=>{
  mounted.current=true;let controller;
  const load=async()=>{if(busy.current)return;busy.current=true;controller=new AbortController();const timer=setTimeout(()=>controller.abort(),7000);try{const response=await fetch('/api/status',{signal:controller.signal});if(!response.ok)throw new Error();const result=await response.json();if(mounted.current){setData(result);setOffline(false)}}catch{if(mounted.current)setOffline(true)}finally{busy.current=false;clearTimeout(timer)}};
  load();const timer=setInterval(load,4000);
  return()=>{mounted.current=false;clearInterval(timer);controller?.abort()};
 },[]);
 useEffect(()=>{if(!toast)return;const t=setTimeout(()=>setToast(''),4000);return()=>clearTimeout(t)},[toast]);
 useEffect(()=>{const key=e=>{if(e.key==='Escape'&&NATIVE)window.webkit.messageHandlers.quota.postMessage('exit')};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key)},[]);
 const refresh=async()=>{if(data.refreshing)return;try{const response=await fetch('/api/refresh',{method:'POST',headers:{'X-Quota-Deck':'1'}});if(!response.ok)throw new Error();const result=await response.json();setData(d=>({...result,tokens:d.tokens}));setOffline(false);setToast(result.accepted?'正在更新账户额度':'距上次查询不足 1 分钟，保留最新读数')}catch{setOffline(true);setToast('暂时无法连接额度服务')}};
 const providers=data.providers||INITIAL;
 const usage=data.tokens?.providers||{};
 const todayValue=Object.values(usage).some(Boolean)?Object.values(usage).reduce((sum,u)=>sum+(u?.total||0),0):null;
 const today=useTween(todayValue),todayPop=useFlash(todayValue);
 const hosts=data.tokens?.hosts||[];
 const clock=new Date(now);
 return <><main className="deck">
  <header className="topbar">
   <div className="card status-bar">
    <Weather/>
    <i className="divider"/>
    <div className="section today" title={hosts.length>1?hosts.map(h=>`${translate(h.name)}: ${t(h.status==='ok'?'在线':'离线（显示上次读数）')}`).join(' · '):t('本机 CLI 会话记录中今天消耗的 token 合计')}>
     <Icon name="database" className="today-icon"/><div><span>{t('今日合计')}</span><strong className={todayPop?'pop':''}>{tokens(today)}</strong></div>
    </div>
    <i className="divider"/>
    <Hardware now={now}/>
   </div>
   <div className="card clock-card">
   <button className="clock" onClick={refresh} title={t('点击刷新额度')} aria-label={t('刷新额度')}>
    <time>{clock.toLocaleTimeString('en-GB',{hour12:false,hour:'2-digit',minute:'2-digit'})}</time>
    <span>{language==='en'?clock.toLocaleDateString('en-GB',{day:'numeric',month:'short',weekday:'short'}):`${clock.getMonth()+1}月${clock.getDate()}日 ${WEEK[clock.getDay()]}`}{offline?<em className="warning"> · {t('离线')}</em>:data.refreshing?<em> · {t('同步中')}</em>:null}</span>
   </button>
   <div className="clock-actions"><ThemeToggle/><LanguageToggle/></div>
   </div>
  </header>
  <section className="instruments" aria-label={t('AI 额度仪表')}>{providers.map((provider,index)=><Provider provider={provider} key={provider.id} index={index} now={now} offline={offline} usage={usage[provider.id]} hosts={hosts} onRefresh={refresh} refreshing={data.refreshing} screenOpen={screen.control?.details[provider.id]} onDetails={screen.details} agent={activity.providers[provider.id]} notice={activity.notices[provider.id]} resetPreview={activity.resets[provider.id]}/>)}</section>
  {toast?<div className="toast" role="status">{t(toast)}</div>:null}
 </main>{NATIVE?null:<TokenStats/>}</>;
}
createRoot(document.getElementById('root')).render(<App/>);
