"""Stream aggregate CLI states over existing SSH connections; install nothing remotely."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time

from activity import SOURCES, WAITING_TIMEOUT
from remote_tokens import load_hosts

HERE = Path(__file__).resolve().parent
PRIORITY = {'unknown':0, 'idle':1, 'completed':2, 'working':3, 'waiting':4}


def stream_script():
    # Share the exact parsers with local monitoring, including Python 3.9 timestamp support.
    return ((HERE/'tokens.py').read_text() + '\n' +
            (HERE/'activity.py').read_text().replace('from tokens import parse_time\n', '') + '''
_monitor = ActivityMonitor()
try:
    while True:
        _monitor.scan()
        _data = _monitor.snapshot()
        _data['sentAt'] = time.time()
        print(json.dumps(_data), flush=True)
        time.sleep(2)
except (BrokenPipeError, KeyboardInterrupt):
    pass
''')


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class RemoteActivity:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.lock = threading.Lock()
        self.hosts = {}

    def accept(self, host, data):
        """Allowlisted aggregate fields only; normalize the Mac clock to this display."""
        if not isinstance(data, dict) or not finite(data.get('sentAt')):
            raise ValueError('Invalid activity packet')
        now = time.time()
        offset = now - data['sentAt']
        name = str(host.get('name') or host['ssh'])
        prefix = hashlib.sha256(host['ssh'].encode()).hexdigest()[:16]
        providers, events = {}, []
        raw_providers = data.get('providers')
        if not isinstance(raw_providers, dict) or not isinstance(data.get('events'), list):
            raise ValueError('Invalid activity packet')
        for provider in SOURCES:
            state = raw_providers.get(provider)
            if not isinstance(state, dict) or state.get('state') not in PRIORITY:
                continue
            providers[provider] = {'state':state['state'],
                'at':state['at'] + offset if finite(state.get('at')) else None,
                'reason':state.get('reason') if state.get('reason') in ('input','permission') else None,
                'host':name}
        for event in data['events'][-24:]:
            if (not isinstance(event, dict) or event.get('provider') not in SOURCES
                    or event.get('kind') not in ('completed','waiting')
                    or not isinstance(event.get('id'), str) or len(event['id']) > 128
                    or not finite(event.get('at')) or not 0 <= data['sentAt']-event['at'] < 30):
                continue
            events.append({'id':prefix+':'+event['id'], 'provider':event['provider'],
                           'kind':event['kind'], 'at':event['at']+offset, 'preview':False,
                           'reason':event.get('reason') if event.get('reason') in ('input','permission') else None,
                           'host':name})
        with self.lock:
            self.hosts[host['ssh']] = {'name':name, 'updatedAt':now, 'connected':True,
                                      'providers':providers, 'events':events}

    def disconnected(self, target):
        with self.lock:
            if target in self.hosts:
                self.hosts[target]['connected'] = False

    def snapshot(self):
        now = time.time()
        with self.lock:
            entries = copy.deepcopy(list(self.hosts.values()))
        hosts, providers, events = [], {}, []
        for entry in entries:
            online = entry['connected'] and now-entry['updatedAt'] < 10
            hosts.append({'name':entry['name'], 'status':'ok' if online else 'offline',
                          'updatedAt':entry['updatedAt']})
            if online:
                for key, state in entry['providers'].items():
                    if state['state'] == 'waiting' and (state['at'] is None
                            or not 0 <= now-state['at'] < WAITING_TIMEOUT):
                        continue
                    current = providers.get(key, {'state':'unknown', 'at':None})
                    if (PRIORITY[state['state']],state['at'] or 0) >= (PRIORITY[current['state']],current['at'] or 0):
                        providers[key] = state
            events.extend(e for e in entry['events'] if 0 <= now-e['at'] < 30)
        return {'hosts':hosts, 'providers':providers, 'events':events}

    def stream(self, host, stop):
        process = None
        try:
            process = subprocess.Popen(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=8',
                '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',host['ssh'],'python3 -u -'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            process.stdin.write(stream_script().encode())
            process.stdin.close()
            buffer = b''
            last_packet = time.monotonic()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while not stop.is_set() and time.monotonic()-last_packet < 15:
                    if not selector.select(timeout=1):
                        continue
                    chunk = os.read(process.stdout.fileno(),65536)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > 262144:
                        raise ValueError('Activity packet too large')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n',1)
                        self.accept(host,json.loads(line))
                        last_packet = time.monotonic()
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stdin.close()
            self.disconnected(host['ssh'])

    def worker(self, host, stop):
        while not stop.is_set():
            try:
                self.stream(host,stop)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                pass
            stop.wait(5)

    def run(self, stop):
        workers = {}
        try:
            while not stop.is_set():
                configured = {host['ssh']:host for host in load_hosts(self.home)}
                for target in set(workers)-set(configured):
                    worker_stop, thread = workers.pop(target)
                    worker_stop.set()
                    thread.join(timeout=5)
                    with self.lock:
                        self.hosts.pop(target,None)
                for target, host in configured.items():
                    if target not in workers:
                        with self.lock:
                            self.hosts[target] = {'name':str(host.get('name') or target),'connected':False,
                                'updatedAt':None,'providers':{},'events':[]}
                        worker_stop = threading.Event()
                        thread = threading.Thread(target=self.worker,args=(host,worker_stop),daemon=True)
                        workers[target] = (worker_stop,thread)
                        thread.start()
                stop.wait(10)
        finally:
            for worker_stop, _ in workers.values():
                worker_stop.set()
            for _, thread in workers.values():
                thread.join(timeout=5)


def merge_activity(local, remote):
    result = copy.deepcopy(local)
    for provider, state in remote.get('providers',{}).items():
        current = result['providers'].get(provider,{'state':'unknown','at':None})
        if (PRIORITY[state['state']],state['at'] or 0) > (PRIORITY[current['state']],current['at'] or 0):
            result['providers'][provider] = state
    result['events'] = sorted(result['events']+remote.get('events',[]),key=lambda e:e['at'])[-48:]
    result['hosts'] = remote.get('hosts',[])
    return result
