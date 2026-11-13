"""Today's token usage from other machines, read over SSH (e.g. via Tailscale).

Hosts are listed in ~/.config/quota-deck/hosts.json:
    [{"name": "MacBook Pro", "ssh": "user@your-host"}]
tokens.py is streamed to the remote python3 on stdin, so nothing is installed
there and only aggregated counts come back. Uses the user's normal SSH keys.
"""
import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent
TRAILER = '\nimport json as _j\n_c=TokenCounter()\n_c.scan()\nprint(_j.dumps(_c.snapshot()))\n'
INTERVAL = 120


def load_hosts(home):
    try:
        hosts = json.loads((Path(home)/'.config/quota-deck/hosts.json').read_text())
    except (OSError, ValueError):
        return []
    return [h for h in hosts if isinstance(h, dict) and isinstance(h.get('ssh'), str)
            and h['ssh'] and not h['ssh'].startswith('-')]


def fetch(target):
    script = (HERE/'tokens.py').read_text()+TRAILER
    result = subprocess.run(
        ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
         target, 'python3 -'],
        input=script, capture_output=True, text=True, timeout=45)
    if result.returncode:
        message = (result.stderr.strip().splitlines() or ['ssh failed'])[-1]
        raise RuntimeError(message[:160])
    return json.loads(result.stdout.strip().splitlines()[-1])


class RemoteTokens:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.lock = threading.Lock()
        # Last readings survive panel restarts, so a sleeping laptop still shows today's usage.
        self.cache = self.home/'.cache/quota-deck/remote-tokens.json'
        self.hosts = self.load_cache()

    def load_cache(self):
        today = datetime.now().strftime('%Y-%m-%d')
        try:
            saved = json.loads(self.cache.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(saved, dict):
            return {}
        return {name:{**entry, 'status':'offline', 'note':'上次读数（尚未重新连接）'}
                for name, entry in saved.items()
                if isinstance(entry, dict) and entry.get('day') == today and isinstance(entry.get('providers'), dict)}

    def save_cache(self):
        with self.lock:
            saved = {name:entry for name, entry in self.hosts.items() if entry.get('updatedAt')}
        try:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache.with_suffix('.tmp')
            temporary.write_text(json.dumps(saved, ensure_ascii=False))
            temporary.replace(self.cache)
        except OSError:
            pass

    def poll_once(self):
        today = datetime.now().strftime('%Y-%m-%d')
        for host in load_hosts(self.home):
            name = str(host.get('name') or host['ssh'])
            try:
                data = fetch(host['ssh'])
                entry = {'name':name, 'status':'ok', 'note':'', 'updatedAt':time.time(),
                         'day':data.get('day'), 'providers':data.get('providers') or {}}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                with self.lock:
                    previous = copy.deepcopy(self.hosts.get(name))
                # Keep today's last reading while a laptop sleeps or roams; drop it after midnight.
                keep = previous and previous.get('day') == today
                entry = {**(previous if keep else {'providers':{}, 'updatedAt':None, 'day':None}),
                         'name':name, 'status':'offline', 'note':str(error)}
            with self.lock:
                self.hosts[name] = entry
        self.save_cache()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.hosts.values()))

    def run(self, stop):
        while not stop.is_set():
            self.poll_once()
            stop.wait(INTERVAL)


def merge(local, hosts):
    """Sum local and remote per-provider usage; keep a per-host breakdown in `hosts`."""
    merged = copy.deepcopy(local)
    providers = merged.setdefault('providers', {})
    today = merged.get('day')
    breakdown = [{'name':'本机', 'status':'ok', 'providers':local.get('providers') or {}}]
    for host in hosts:
        usable = host.get('day') == today
        breakdown.append({'name':host['name'], 'status':host['status'], 'note':host.get('note', ''),
                          'updatedAt':host.get('updatedAt'),
                          'providers':host.get('providers') if usable else {}})
        if not usable:
            continue
        for key, usage in (host.get('providers') or {}).items():
            if not isinstance(usage, dict):
                continue
            total = providers.get(key)
            if total is None:
                providers[key] = copy.deepcopy(usage)
                continue
            for field in ('input', 'cached', 'output', 'total'):
                total[field] = total.get(field, 0)+usage.get(field, 0)
            if isinstance(total.get('hourly'), list) and isinstance(usage.get('hourly'), list):
                total['hourly'] = [a+b for a, b in zip(total['hourly'], usage['hourly'])]
    merged['hosts'] = breakdown
    return merged
