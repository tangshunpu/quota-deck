import React, {useEffect, useRef} from 'react';
import {createPortal} from 'react-dom';

export function ResetCelebration({signal,onFinish}){
 const canvasRef=useRef(null);
 useEffect(()=>{
  const timer=setTimeout(onFinish,8000);
  const canvas=canvasRef.current,ctx=canvas?.getContext('2d'),motion=matchMedia('(prefers-reduced-motion: reduce)');
  let frame,width=innerWidth,height=innerHeight,particles=[],start=null,last=null,index=0;
  const colors=document.documentElement.dataset.theme==='dark'
   ?['#59ADFF','#B39AFF','#FF93BF','#FFD27A','#77E3C3']
   :['#1685E8','#8756D9','#E63E91','#D49516','#0B9E7C'];
  const resize=()=>{width=innerWidth;height=innerHeight;const dpr=Math.min(devicePixelRatio||1,1.5);canvas.width=width*dpr;canvas.height=height*dpr;ctx.setTransform(dpr,0,0,dpr,0,0)};
  const burst=(x,y,color)=>{
   const scale=Math.min(width/1024,height/600,1.5);
   for(let i=0;i<80;i++){
    const angle=i/80*Math.PI*2,speed=(65+Math.random()*155)*scale;
    particles.push({x:x*width,y:y*height,vx:Math.cos(angle)*speed,vy:Math.sin(angle)*speed,
     life:1.8+Math.random()*.9,age:0,color,size:(1.8+Math.random()*1.5)*Math.max(.8,scale)});
   }
  };
  const plan=[[0,.16,.28],[.35,.81,.3],[.85,.49,.45],[1.6,.27,.65],[2,.76,.62],[2.7,.5,.22],[3.4,.12,.48],[3.8,.89,.44],[4.5,.35,.32],[4.9,.68,.28],[5.5,.5,.55]];
  const tick=now=>{
   if(start===null)start=last=now;
   const elapsed=(now-start)/1000,dt=Math.min(.04,(now-last)/1000);last=now;
   ctx.clearRect(0,0,width,height);
   while(index<plan.length&&elapsed>=plan[index][0]){const [,x,y]=plan[index];burst(x,y,colors[index%colors.length]);index++}
   for(const p of particles){
    p.age+=dt;const drag=Math.pow(.5,dt);p.vx*=drag;p.vy=p.vy*drag+45*dt;
    const oldX=p.x,oldY=p.y;p.x+=p.vx*dt;p.y+=p.vy*dt;
    ctx.globalAlpha=Math.max(0,1-(p.age/p.life)**2);ctx.strokeStyle=p.color;ctx.lineWidth=p.size;ctx.lineCap='round';
    ctx.beginPath();ctx.moveTo(oldX-p.vx*.06,oldY-p.vy*.06);ctx.lineTo(p.x,p.y);ctx.stroke();
   }
   particles=particles.filter(p=>p.age<p.life);ctx.globalAlpha=1;
   if(elapsed<8)frame=requestAnimationFrame(tick);
  };
  const stopMotion=()=>{if(motion.matches){cancelAnimationFrame(frame);ctx?.clearRect(0,0,width,height)}};
  if(ctx){resize();window.addEventListener('resize',resize);motion.addEventListener('change',stopMotion);if(!motion.matches)frame=requestAnimationFrame(tick)}
  return()=>{clearTimeout(timer);cancelAnimationFrame(frame);window.removeEventListener('resize',resize);motion.removeEventListener('change',stopMotion)};
 },[signal.id,onFinish]);
 const {t}=window.quotaI18n;
 return createPortal(<div className="reset-celebration" data-signal-id={signal.id}>
  <canvas ref={canvasRef} aria-hidden="true"/>
  <div className="reset-celebration-toast" role="status">
   <img src="/favicon.svg" alt="Tibo"/>
   <div><strong>{t('Reset 新消息')}</strong><span>{t(signal.kind==='reset'?'检测到全局重置消息':'Tibo 发布了新的重置公告')}</span></div>
  </div>
 </div>,document.body);
}
