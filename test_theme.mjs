import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('./public/theme.js',import.meta.url),'utf8');
const at=value=>new Date(`2026-10-22T${value}`).getTime();
function browser(time,{stored=null,native=null,blocked=false}={}){
 let now=time,timer,nextId=0,meta;
 const listeners={},storage=new Map(stored?[['quota-deck.theme.v2',JSON.stringify(stored)]]:[]);
 const root={dataset:{theme:'dark'},style:{}};
 class Clock extends Date{constructor(...args){super(...(args.length?args:[now]))}static now(){return now}}
 const listen=(name,fn)=>(listeners[name]??=[]).push(fn);
 const dispatch=name=>(listeners[name]||[]).forEach(fn=>fn());
 const window={quotaNativeTheme:native,addEventListener:listen,dispatchEvent:event=>dispatch(event.type)};
 const document={documentElement:root,addEventListener:listen,querySelector:()=>({setAttribute:(_,value)=>meta=value})};
 vm.runInNewContext(source,{Date:Clock,window,document,Event:class{constructor(type){this.type=type}},
  localStorage:{getItem:key=>{if(blocked)throw Error();return storage.get(key)??null},setItem:(key,value)=>{if(blocked)throw Error();storage.set(key,value)}},
  setTimeout:(fn,delay)=>{timer={fn,due:now+delay,id:++nextId};return timer.id},clearTimeout:()=>{timer=null}});
 return {theme:()=>root.dataset.theme,meta:()=>meta,toggle:()=>window.quotaTheme.toggle(),
  stored:()=>JSON.parse(storage.get('quota-deck.theme.v2')||'null'),
  advance:time=>{now=time;if(timer?.due<=now)timer.fn()},wake:time=>{now=time;dispatch('visibilitychange')}};
}
test('startup respects both local-time boundaries',()=>{
 for(const [time,expected] of [['00:00:00','dark'],['06:59:59','dark'],['07:00:00','light'],['17:59:59','light'],['18:00:00','dark'],['23:59:59','dark']])
  assert.equal(browser(at(time)).theme(),expected,time);
});
test('running page switches at 18:00 and 07:00 the next day',()=>{
 const b=browser(at('17:59:59'));b.advance(at('18:00:00'));assert.equal(b.theme(),'dark');
 b.advance(new Date('2026-10-23T07:00:00').getTime());assert.equal(b.theme(),'light');assert.equal(b.meta(),'#F5F6F8');
});
test('manual choice survives reload and expires without disabling the schedule',()=>{
 const b=browser(at('12:00:00'));b.toggle();assert.equal(b.theme(),'dark');assert.equal(b.stored().until,at('18:00:00'));
 const restored=browser(at('13:00:00'),{stored:b.stored()});assert.equal(restored.theme(),'dark');
 restored.advance(at('18:00:00'));assert.equal(restored.theme(),'dark');
 restored.advance(new Date('2026-10-23T07:00:00').getTime());assert.equal(restored.theme(),'light');
});
test('night override expires at next morning, including month rollover',()=>{
 const b=browser(new Date('2026-10-31T23:59:00').getTime());b.toggle();assert.equal(b.theme(),'light');
 assert.equal(b.stored().until,new Date('2026-11-01T07:00:00').getTime());
 b.advance(new Date('2026-11-01T18:00:00').getTime());assert.equal(b.theme(),'dark');
});
test('sleep resume and clock jumps resync without reloading',()=>{
 const b=browser(at('06:59:59'));b.wake(at('09:00:00'));assert.equal(b.theme(),'light');
 b.wake(at('20:00:00'));assert.equal(b.theme(),'dark');
});
test('legacy and expired choices cannot disable automatic switching',()=>{
 for(const stored of ['dark',{theme:'dark',until:at('07:00:00')},{theme:'invalid',until:at('18:00:00')}])
  assert.equal(browser(at('12:00:00'),{stored,native:'dark'}).theme(),'light');
});
test('native override works with blocked browser storage',()=>{
 const b=browser(at('12:00:00'),{native:{theme:'dark',until:at('18:00:00')},blocked:true});
 assert.equal(b.theme(),'dark');b.toggle();assert.equal(b.theme(),'light');
 b.advance(at('18:00:00'));assert.equal(b.theme(),'dark');
});
