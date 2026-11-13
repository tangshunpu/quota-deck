import test from 'node:test';
import assert from 'node:assert/strict';
import {createResetSignalTracker} from './src/reset-signals.mjs';

const now=1800000000;
const signal=(id,at=now,kind='announcement')=>({id,at,kind});
const data=(signals=[],extra={})=>({mode:'scheduled',signals,stale:false,...extra});
function storage(){const values=new Map();return {getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)}}

test('initial history is silent; only newly observed signals play once',()=>{
 const track=createResetSignalTracker(storage());
 assert.equal(track(data([signal('old')]),now),null);
 assert.equal(track(data([signal('old'),signal('new')]),now)?.id,'new');
 assert.equal(track(data([signal('new')]),now),null);
});
test('reload and a subsequent poll cannot replay a previously seen signal',()=>{
 const saved=storage(),first=createResetSignalTracker(saved);
 first(data(),now);assert.equal(first(data([signal('new')]),now)?.id,'new');
 const reloaded=createResetSignalTracker(saved);
 reloaded(data(),now);assert.equal(reloaded(data([signal('new')]),now),null);
});
test('forecasts, deadlines, old, future and invalid signals never trigger',()=>{
 const track=createResetSignalTracker(storage());track(data(),now);
 assert.equal(track(data([],{mode:'forecast',forecast:{probability24h:99}}),now),null);
 assert.equal(track(data([],{scheduled:{deadline:now-1}}),now),null);
 for(const s of [signal('old',now-1801),signal('future',now+1),signal('hint',now,'hint'),signal('invalid',NaN)])assert.equal(track(data([s]),now),null);
});
test('stale signal waits for fresh confirmation; confirmed-reset signals are supported',()=>{
 const track=createResetSignalTracker(storage());track(data(),now);
 const s=signal('confirmed',now,'reset');
 assert.equal(track(data([s],{stale:true}),now),null);
 assert.equal(track(data([s]),now)?.kind,'reset');
});
test('storage failure still baselines history and deduplicates during this page session',()=>{
 const unavailable={getItem(){throw Error()},setItem(){throw Error()}};
 const track=createResetSignalTracker(unavailable);
 assert.equal(track(data([signal('initial')]),now),null);
 assert.equal(track(data([signal('new')]),now)?.id,'new');
 assert.equal(track(data([signal('new')]),now),null);
});
test('multiple fresh signals produce one celebration and remember the entire batch',()=>{
 const track=createResetSignalTracker(storage());track(data(),now);
 assert.equal(track(data([signal('a',now-1),signal('b')]),now)?.id,'b');
 assert.equal(track(data([signal('a',now-1)]),now),null);
});
