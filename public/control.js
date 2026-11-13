(() => {
 const {t,setLanguage,getLanguage}=window.quotaI18n;
 const $ = selector => document.querySelector(selector);
 let state, busy = false, pollTimer;
 let feedbackKey='';
 const feedback = message => { feedbackKey=message;$('#feedback').textContent=t(message); };
 function localize(){
  document.querySelectorAll('[data-i18n]').forEach(el=>el.textContent=t(el.dataset.i18n));
  document.querySelectorAll('[data-i18n-aria]').forEach(el=>el.setAttribute('aria-label',t(el.dataset.i18nAria)));
  $('#language-toggle').textContent=getLanguage()==='zh'?'EN':'中文';
  $('#language-toggle').setAttribute('aria-label',getLanguage()==='zh'?'Switch to English':'切换到中文');
  feedback(feedbackKey);
 }
 window.addEventListener('quota-language-change',localize);
 localize();
 async function request(path, body) {
  const response = await fetch(path, {method: body ? 'POST' : 'GET',
   headers: body ? {'Content-Type':'application/json','X-Quota-Deck':'1'} : {},
   body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(5000)});
  if (response.status === 401) { $('#login').hidden=false; $('#controls').hidden=true; throw Error(path==='/api/login'?'访问密钥不正确':'请先连接面板'); }
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '操作失败，请重试');
  return result;
 }
 function render(next) {
  state = next; setLanguage(state.language); $('#login').hidden=true; $('#controls').hidden=false;
  const online=state.screen.online, applied=online && state.screen.appliedRevision===state.revision && state.screen.language===state.language;
  $('#status-dot').classList.toggle('online',applied);
  $('#connection-text').textContent=t(applied?'实体屏已连接':online?'正在应用设置…':'等待实体屏连接');
  $('#connection-note').textContent=t(applied?'设置已应用到 HDMI 屏幕':online?'指令已发送，等待屏幕确认':'设置已保存，屏幕连接后自动应用。');
  $('#current-theme').textContent=`${t(applied?'当前':'目标')}: ${t(state.theme.effective==='dark'?'深色':'浅色')}`;
  document.querySelectorAll('[data-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.mode===state.theme.mode)));
  document.querySelectorAll('[data-provider]').forEach(button=>{
   const open=state.details[button.dataset.provider];button.setAttribute('aria-pressed',String(open));button.textContent=t(open?'返回仪表':'打开详情');
  });
  document.querySelectorAll('[data-language]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.language===state.language)));
  $('#schedule-note').textContent=t(state.theme.mode==='auto'?'每天 18:00–次日 07:00 深色，其余时间浅色。按屏幕本机时间。':'当前为临时手动选择；下一个 07:00 或 18:00 自动恢复定时切换。');
 }
 async function poll() {
  clearTimeout(pollTimer);
  try { if(!busy)render(await request('/api/control')); }
  catch(error) {
   if(!$('#controls').hidden){$('#connection-text').textContent=t('控制服务暂时离线');$('#connection-note').textContent=t('正在重新连接…');$('#status-dot').classList.remove('online');}
   else if(error.message!=='请先连接面板')feedback('无法连接控制服务，请检查网络。');
  } finally { if($('#login').hidden)pollTimer=setTimeout(poll,1500); }
 }
 async function command(body,path='/api/control') {
  if(busy)return;busy=true;document.querySelectorAll('#controls button').forEach(b=>b.disabled=true);
  try {
   const result=await request(path,body);
   if(path==='/api/control'){render(result);feedback('设置已发送。');}
   else if(path==='/api/activity/preview')feedback('预览已发送；在线屏幕约 2 秒内显示。');
   else feedback(result.accepted?'正在刷新账户额度。':'查询间隔不足 60 秒，保留最新读数。');
  }catch(error){feedback(error.message);}
  finally{busy=false;document.querySelectorAll('#controls button').forEach(b=>b.disabled=false);poll();}
 }
 $('#language-toggle').addEventListener('click',()=>{
  const language=getLanguage()==='zh'?'en':'zh';
  if(state&&!$('#controls').hidden)command({action:'language',language});else setLanguage(language);
 });
 document.querySelectorAll('[data-language]').forEach(button=>button.addEventListener('click',()=>command({action:'language',language:button.dataset.language})));
 document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>command({action:'theme',mode:button.dataset.mode})));
 document.querySelectorAll('[data-provider]').forEach(button=>button.addEventListener('click',()=>command({action:'details',provider:button.dataset.provider,open:!state.details[button.dataset.provider]})));
 $('#overview').addEventListener('click',()=>command({action:'overview'}));
 $('#refresh').addEventListener('click',()=>command({action:'refresh'},'/api/refresh'));
 $('#reload').addEventListener('click',()=>command({action:'reload'}));
 document.querySelectorAll('[data-preview]').forEach(button=>button.addEventListener('click',()=>command({provider:$('#alert-provider').value,kind:button.dataset.preview},'/api/activity/preview')));
 async function login(key) {
  try { await request('/api/login',{key});$('#key').value='';feedback('');await poll(); }
  catch(error){feedback(error.message);$('#login').hidden=false;}
 }
 $('#login-form').addEventListener('submit',event=>{event.preventDefault();login($('#key').value.trim());});
 const connect=()=>{
  const key=location.hash.slice(1);
  if(key){history.replaceState(null,'',location.pathname);login(key);}else poll();
 };
 window.addEventListener('hashchange',connect);
 connect();
})();
