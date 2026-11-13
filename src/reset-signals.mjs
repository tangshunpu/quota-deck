// The initial snapshot is a baseline, including when storage is unavailable on the kiosk.
// Persist IDs before playing so reloads cannot replay a celebration already started.
export function createResetSignalTracker(storage){
 const key='quota-deck.reset-signals.v1';
 let initialized=false,seen=new Set();
 try{const saved=JSON.parse(storage?.getItem(key));if(Array.isArray(saved))seen=new Set(saved.filter(id=>typeof id==='string').slice(-128))}catch{}
 return (data,now)=>{
  if(!data||(data.mode==='unavailable'&&!data.signals?.length))return null;
  const signals=(Array.isArray(data.signals)?data.signals:[]).filter(signal=>
   signal&&typeof signal.id==='string'&&['announcement','reset'].includes(signal.kind)&&
   Number.isFinite(signal.at)&&now-signal.at>=0&&now-signal.at<=1800);
  const next=initialized&&!data.stale?signals.filter(signal=>!seen.has(signal.id)).sort((a,b)=>b.at-a.at)[0]:null;
  // A stale response is not evidence that a new event was seen; retry it after recovery.
  if(!data.stale){
   signals.forEach(signal=>seen.add(signal.id));
   seen=new Set([...seen].slice(-128));
   try{storage?.setItem(key,JSON.stringify([...seen]))}catch{}
   initialized=true;
  }
  return next||null;
 };
}
