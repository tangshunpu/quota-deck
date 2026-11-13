import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('./public/i18n.js',import.meta.url),'utf8');
function browser(stored,{blocked=false,path='/'}={}){
 const storage=new Map([['quota-deck.language.v1',stored]]),listeners={},events=[];
 const document={documentElement:{},addEventListener:(type,fn)=>listeners[type]=fn};
 const window={addEventListener:(type,fn)=>listeners[type]=fn,dispatchEvent:event=>events.push(event.type)};
 vm.runInNewContext(source,{window,document,location:{pathname:path},Event:class{constructor(type){this.type=type}},
  localStorage:{getItem:key=>{if(blocked)throw Error();return storage.get(key)},setItem:(key,value)=>{if(blocked)throw Error();storage.set(key,value)}}});
 return {i:window.quotaI18n,document,storage,events,listeners};
}
test('default Chinese, saved English, invalid choice, and blocked storage',()=>{
 assert.equal(browser().i.getLanguage(),'zh');
 assert.equal(browser('fr').i.getLanguage(),'zh');
 const b=browser('en');assert.equal(b.document.documentElement.lang,'en');assert.equal(b.i.t('等待输入'),'Awaiting input');
 b.i.setLanguage('invalid');assert.equal(b.i.getLanguage(),'en');assert.equal(b.events.length,0);
 b.i.setLanguage('zh');assert.equal(b.document.documentElement.lang,'zh-CN');assert.equal(b.storage.get('quota-deck.language.v1'),'zh');
 assert.deepEqual(b.events,['quota-language-change']);
 b.i.setLanguage('zh');assert.equal(b.events.length,1);
 const blocked=browser(null,{blocked:true});blocked.i.setLanguage('en');assert.equal(blocked.i.t('已完成'),'Completed');
});
test('parameterized quota labels and stale backend errors translate without losing values',()=>{
 const {i}=browser('en');
 assert.equal(i.dynamic('5 小时窗口'),'5-hour window');
 assert.equal(i.dynamic('1.5 天额度'),'1.5-day quota');
 assert.equal(i.dynamic('30 分钟窗口'),'30-minute window');
 assert.equal(i.dynamic('额度服务暂不可用（HTTP 503）；显示上次读数'),'Quota service unavailable (HTTP 503); Showing the last reading');
 assert.equal(i.t('{time}后重置',{time:'2h 15m'}),'Resets in 2h 15m');
 assert.equal(i.dynamic('MacBook Pro'),'MacBook Pro');
 assert.equal(i.dynamic('东北风5级'),'NE 5 Bft');
});
test('every literal interface string has an English translation',()=>{
 const {i}=browser('en');
 for(const file of ['./src/main.jsx','./src/stats.jsx','./src/reset-news.jsx','./src/reset-celebration.jsx','./public/control.js']){
  const text=readFileSync(new URL(file,import.meta.url),'utf8');
  const missing=[...text.matchAll(/\bt\('([^'\\]+)'/g)].map(match=>match[1]).filter(key=>i.t(key)===key);
  assert.deepEqual(missing,[],file);
 }
});
test('controller title and other tabs follow language changes',()=>{
 const b=browser('en',{path:'/control/'});assert.equal(b.document.title,'Quota Deck · Display control');
 b.listeners.storage({key:'quota-deck.language.v1',newValue:'zh'});
 assert.equal(b.i.getLanguage(),'zh');assert.equal(b.document.title,'Quota Deck · 屏幕遥控');
});
