import React, {useCallback, useEffect, useState} from 'react';
import {createResetSignalTracker} from './reset-signals.mjs';
import {ResetCelebration} from './reset-celebration.jsx';

const {t}=window.quotaI18n;
const dateFormat=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Singapore',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
function resetTime(deadline){
 const parts=Object.fromEntries(dateFormat.formatToParts(new Date(deadline*1000)).map(part=>[part.type,part.value]));
 const time=`${parts.hour}${parts.minute==='00'?'':`:${parts.minute}`}${parts.dayPeriod.toLowerCase()}`;
 return t('{day}日{time}',{day:parts.day,time});
}

export function ResetNews({now,native}){
 const [data,setData]=useState(null),[failed,setFailed]=useState(false);
 const [celebration,setCelebration]=useState(null);
 const finishCelebration=useCallback(()=>setCelebration(null),[]);
 const [trackSignal]=useState(()=>{let storage;try{storage=window.localStorage}catch{}return createResetSignalTracker(storage)});
 useEffect(()=>{
  let live=true,timer,controller;
  const load=async()=>{
   let delay=10000;
   controller=new AbortController();
   const timeout=setTimeout(()=>controller.abort(),10000);
   try{
    const response=await fetch('/api/codex/reset-news',{signal:controller.signal});
    if(!response.ok)throw new Error('Reset news unavailable');
    const result=await response.json();
    if(result.mode!=='unavailable')delay=60000;
    if(live){setData(result);setFailed(false);const signal=trackSignal(result,Date.now()/1000);if(signal)setCelebration(signal)}
   }catch{if(live)setFailed(true)}
   finally{clearTimeout(timeout);if(live)timer=setTimeout(load,delay)}
  };
  load();return()=>{live=false;clearTimeout(timer);controller?.abort()};
 },[trackSignal]);
 const stale=failed||data?.stale;
 const fresh=data?.updatedAt&&now/1000-data.updatedAt<=1800;
 const scheduled=fresh&&data.mode==='scheduled'&&data.scheduled?.deadline>now/1000?data.scheduled:null;
 const forecast=fresh&&data.mode==='forecast'?data.forecast:null;
 const date=scheduled?resetTime(scheduled.deadline):'';
 const title=scheduled?t('下次全局重置'):t('全局重置预测');
 const value=scheduled?t('最晚{time}',{time:date}):forecast
  ?`24h ${forecast.probability24h}% · 48h ${forecast.probability48h}%`
  :t(!data&&!failed?'正在读取重置消息…':fresh&&data?.mode==='scheduled'?'公告时间已过，待确认':'暂无重置消息');
 const note=stale?t('更新失败 · 上次消息'):scheduled?t('Tibo 公告'):forecast?t('暂无有效公告 · 第三方预测'):t('等待更新');
 const source=scheduled?.source||forecast?.source||'https://codex-resets.com/zh-CN';
 const Tag=native?'div':'a';
 return <><Tag className={`reset-news ${scheduled?'has-announcement':forecast?'is-forecast':''}`} {...(native?{}:{href:scheduled?.url||source,target:'_blank',rel:'noopener noreferrer'})}
  title={`${title} · ${value}\n${note}\n${t('来源')}：${source}`}>
  <img src="/favicon.svg" alt="Tibo" width="30" height="30"/>
  <span className="reset-news-label">Reset{scheduled?<span className="reset-news-sender"> · Tibo</span>:null}</span>
  <strong>{value}</strong>
  <small className={stale?'warning':''}>{t(stale?'旧消息':scheduled?'已公告':forecast?'第三方预测':'待更新')}</small>
 </Tag>{celebration?<ResetCelebration signal={celebration} onFinish={finishCelebration}/>:null}</>;
}
