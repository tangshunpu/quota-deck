#!/usr/bin/python3
"""Local display API and authenticated LAN controller; no model calls."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
import ipaddress
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
import threading
import time
from urllib.parse import urlparse, parse_qs

from providers import FETCHERS, ProviderError
from telemetry import SystemMonitor
from tokens import TokenCounter
from remote_tokens import RemoteTokens, merge
from weather import WeatherStore
from control import DisplayControl
from activity import ActivityMonitor
from remote_activity import RemoteActivity, merge_activity
from token_history import TokenHistory
from reset_news import ResetNews

ROOT = Path(__file__).resolve().parent


class QuotaStore:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.lock = threading.Lock()
        self.refreshing = False
        self.last_started = 0
        self.cooldowns = {}
        self.providers = {key:{'id':key,'status':'loading','windows':[],
                              'plan':'','source':'','note':'正在读取本机账户','updatedAt':None}
                          for key in FETCHERS}

    def snapshot(self):
        with self.lock:
            return {'providers':copy.deepcopy(list(self.providers.values())),
                    'refreshing':self.refreshing, 'interval':300,
                    'nextRefresh':self.last_started+300 if self.last_started else None,
                    'serverTime':time.time()}

    def failing(self):
        with self.lock:
            return any(p['status'] in ('error','stale') for p in self.providers.values())

    def next_wait(self, base):
        """Seconds until the next read: normally `base`, but shortly after a window's reset time so
        the screen can confirm the refilled quota. Keeps at least the 60 s query spacing."""
        now=time.time()
        with self.lock:
            due=[w['resetsAt'] for p in self.providers.values() for w in p.get('windows') or []
                 if isinstance(w.get('resetsAt'),(int,float)) and w['resetsAt']>now]
        return min(base,max(60,min(due)-now+15)) if due else base

    def refresh(self):
        with self.lock:
            if self.refreshing or time.time()-self.last_started<60:
                return False
            self.refreshing=True
            self.last_started=time.time()
        threading.Thread(target=self._refresh,daemon=True).start()
        return True

    def _one(self,key,fetch):
        with self.lock:
            if self.cooldowns.get(key,0)>time.time():
                return
        try:
            result=fetch(self.home)
            result.update(id=key,updatedAt=time.time())
        except Exception as error:
            status=error.status if isinstance(error,ProviderError) else 'error'
            note=str(error) if isinstance(error,ProviderError) else '额度读取失败，稍后自动重试'
            with self.lock:
                previous=copy.deepcopy(self.providers[key])
                if status=='limited':
                    self.cooldowns[key]=time.time()+600
            if status in ('error','limited') and previous.get('windows'):
                result={**previous,'status':'stale','note':note+'；显示上次读数'}
            else:
                # Plan and account don't change when one query fails; keep them on the card.
                result={'id':key,'windows':[],'status':status,'note':note,'source':'','updatedAt':None,
                        **{field:previous.get(field) for field in ('plan','tier','account') if previous.get(field)}}
                result.setdefault('plan','')
        with self.lock:
            self.providers[key]=result

    def _refresh(self):
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures=[pool.submit(self._one,key,fetch) for key,fetch in FETCHERS.items()]
                for future in futures:
                    future.result()
        finally:
            with self.lock:
                self.refreshing=False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,store,monitor,tokens,remote,**kwargs):
        self.remote=remote
        self.store=store
        self.tokens=tokens
        self.monitor=monitor
        super().__init__(*args,directory=str(ROOT/'dist'),**kwargs)

    def log_message(self,*args):
        pass

    def log_request(self, code='-', size='-'):
        # Log page entry and routing failures without URL keys, cookies, or request bodies.
        path = urlparse(self.path).path
        pages = ('/', '/control', '/control/', '/control.html')
        if str(code) not in ('403', '404') and path not in pages:
            return
        route = path if path in pages else ('/api/<redacted>' if path.startswith('/api/')
                                             else '/control/<redacted>' if path.startswith('/control/')
                                             else '<unrecognized path>')
        print(f'Quota Deck HTTP {code} {self.command} {route} peer={self.client_address[0]}', flush=True)

    def allowed_host(self):
        port=self.server.server_port
        address=ipaddress.ip_address(self.client_address[0])
        # Tailscale peers retain their 100.64/10 source address on routed LAN requests.
        # This permits reaching the login page; remote API access still requires the key.
        private=any(address in ipaddress.ip_network(net) for net in ('127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10'))
        local_address=self.connection.getsockname()[0]
        hosts={f'{local_address}:{port}'}
        if self.is_local():
            hosts.update((f'127.0.0.1:{port}',f'localhost:{port}'))
        return private and self.headers.get('Host') in hosts

    def is_local(self):
        return ipaddress.ip_address(self.client_address[0]).is_loopback

    def authenticated(self):
        if self.is_local():
            return True
        cookie=SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie',''))
            value=cookie.get('quota_access')
            return bool(value and hmac.compare_digest(value.value,self.server.control.key))
        except (ValueError, TypeError, CookieError):
            return False

    def read_json(self):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=4096 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
            raise ValueError('请求格式无效')
        return json.loads(self.rfile.read(size))

    def end_headers(self):
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'")
        super().end_headers()

    def json_response(self,data,status=200,headers=None):
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            self.send_error(403)
            return
        path=urlparse(self.path).path
        if path.startswith('/api/') and not self.authenticated():
            self.json_response({'error':'需要访问密钥'},401)
        elif path=='/api/health':
            self.json_response({'application':'quota-deck','version':2})
        elif path=='/api/control':
            self.json_response(self.server.control.snapshot())
        elif path=='/api/activity':
            self.json_response(merge_activity(self.server.activity.snapshot(),self.server.remote_activity.snapshot()))
        elif path=='/api/status':
            self.json_response({**self.store.snapshot(),'tokens':merge(self.tokens.snapshot(),self.remote.snapshot())})
        elif path in ('/api/tokens/history','/api/tokens/heatmap','/api/tokens/monthly'):
            try:
                query = parse_qs(urlparse(self.path).query,keep_blank_values=True,max_num_fields=8)
                period = 'months' if path.endswith('monthly') else 'days'
                if any(k not in (period,'provider','host','end') or len(v)!=1 for k,v in query.items()):
                    raise ValueError(f'supported parameters: {period}, provider, host, end')
                options = {k:v[0] for k,v in query.items()}
                options[period] = int(options.get(period,12 if period=='months' else 365 if path.endswith('heatmap') else 30))
                read = (self.server.token_history.monthly if period=='months' else
                        self.server.token_history.heatmap if path.endswith('heatmap') else self.server.token_history.history)
                self.json_response(read(**options))
            except (ValueError, TypeError, OverflowError) as error:
                self.json_response({'error':str(error)},400)
        elif path=='/api/system':
            self.json_response(self.monitor.snapshot())
        elif path=='/api/weather':
            self.json_response(self.server.weather.snapshot())
        elif path=='/api/codex/reset-news':
            self.json_response(self.server.reset_news.snapshot())
        elif path=='/api/refresh':
            self.send_error(405)
        elif path in ('/control','/control/'):
            self.path='/control.html'
            super().do_GET()
        elif path.startswith('/api/'):
            self.send_error(404)
        else:
            super().do_GET()

    def do_POST(self):
        origin=self.headers.get('Origin')
        if (not self.allowed_host() or origin not in (None,'http://'+self.headers.get('Host',''))
                or self.headers.get('X-Quota-Deck')!='1'):
            self.send_error(403)
            return
        path=urlparse(self.path).path
        if path=='/api/login':
            try:
                data=self.read_json()
                key=data.get('key') if isinstance(data,dict) else None
                if not isinstance(key,str) or not hmac.compare_digest(key,self.server.control.key):
                    self.json_response({'error':'访问密钥不正确'},401)
                    return
                self.json_response({'ok':True},headers={'Set-Cookie':f'quota_access={self.server.control.key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000'})
            except (ValueError, TypeError):
                self.json_response({'error':'请求格式无效'},400)
            return
        if not self.authenticated():
            self.json_response({'error':'需要访问密钥'},401)
            return
        if path=='/api/refresh':
            accepted=self.store.refresh()
            self.json_response({'accepted':accepted,**self.store.snapshot()})
        elif path in ('/api/control','/api/display/heartbeat','/api/activity/preview'):
            if path.endswith('heartbeat') and not self.is_local():
                self.send_error(403)
                return
            try:
                data=self.read_json()
                if path=='/api/control':
                    self.json_response(self.server.control.command(data))
                elif path=='/api/activity/preview':
                    if not isinstance(data,dict):
                        raise ValueError()
                    self.json_response(self.server.activity.preview(data.get('provider'),data.get('kind')))
                else:
                    self.server.control.heartbeat(data)
                    self.json_response({'ok':True})
            except (ValueError, TypeError):
                self.json_response({'error':'无效的屏幕操作'},400)
            except OSError:
                self.json_response({'error':'无法保存屏幕设置'},500)
        else:
            self.send_error(404)


def start_server(port=0,home=None,host='127.0.0.1'):
    if not (ROOT/'dist/index.html').exists():
        raise RuntimeError('UI not built; run npm run build in quota-deck first.')
    store=QuotaStore(home)
    monitor=SystemMonitor()
    tokens=TokenCounter(home)
    remote=RemoteTokens(home)
    server=ThreadingHTTPServer((host,port),partial(Handler,store=store,monitor=monitor,tokens=tokens,remote=remote))
    server.control=DisplayControl(home)
    server.activity=ActivityMonitor(home)
    server.remote_activity=RemoteActivity(home)
    server.token_history=TokenHistory(home)
    server.daemon_threads=True
    server.weather=WeatherStore()
    server.reset_news=ResetNews()
    stop=threading.Event()
    threading.Thread(target=server.activity.run,args=(stop,),daemon=True).start()
    threading.Thread(target=server.remote_activity.run,args=(stop,),daemon=True).start()
    threading.Thread(target=monitor.run,args=(stop,),daemon=True).start()
    threading.Thread(target=tokens.run,args=(stop,),daemon=True).start()
    threading.Thread(target=remote.run,args=(stop,),daemon=True).start()
    threading.Thread(target=server.token_history.run_local,args=(stop,),daemon=True).start()
    threading.Thread(target=server.token_history.run_remote,args=(stop,),daemon=True).start()
    threading.Thread(target=server.weather.run,args=(stop,),daemon=True).start()
    threading.Thread(target=server.reset_news.run,args=(stop,),daemon=True).start()
    def poll():
        # A transient network/proxy failure is retried after 1 minute, not 5.
        while not stop.is_set():
            store.refresh()
            stop.wait(5)
            while store.snapshot()['refreshing'] and not stop.is_set():
                stop.wait(1)
            stop.wait(store.next_wait(65 if store.failing() else 295))
    threading.Thread(target=poll,daemon=True).start()
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server,store,stop


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18768)
    parser.add_argument('--host',default='127.0.0.1')
    args=parser.parse_args()
    server,store,stop=start_server(args.port,host=args.host)
    print(f'Quota Deck: http://127.0.0.1:{server.server_port}',flush=True)
    try:
        while True:time.sleep(60)
    except KeyboardInterrupt:
        stop.set()
        server.shutdown()
        server.server_close()
