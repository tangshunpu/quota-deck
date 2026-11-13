"""Shared screen settings and LAN access credentials; no provider secrets."""
import copy
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import secrets
import threading
import time

PROVIDERS = ('codex', 'grok', 'claude')


def next_boundary():
    now = datetime.now()
    if now.hour < 7:
        next_time = now.replace(hour=7, minute=0, second=0, microsecond=0)
    elif now.hour < 18:
        next_time = now.replace(hour=18, minute=0, second=0, microsecond=0)
    else:
        next_time = (now + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    return next_time.timestamp() * 1000


def valid_override(value):
    return (isinstance(value, dict) and value.get('theme') in ('light', 'dark')
            and isinstance(value.get('until'), (int, float))
            and math.isfinite(value['until']) and value['until'] > time.time() * 1000)


class DisplayControl:
    def __init__(self, home=None):
        self.directory = Path(home or Path.home()) / '.config/quota-deck'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'display.json'
        self.lock = threading.RLock()
        self.state = {'revision': 0, 'override': None, 'language': 'zh',
                      'details': dict.fromkeys(PROVIDERS, False), 'reloadId': 0}
        try:
            saved = json.loads(self.path.read_text())
            if isinstance(saved, dict):
                if saved.get('language') in ('zh', 'en'):
                    self.state['language'] = saved['language']
                if valid_override(saved.get('override')):
                    self.state['override'] = saved['override']
                details = saved.get('details')
                if isinstance(details, dict):
                    self.state['details'] = {key: details.get(key) is True for key in PROVIDERS}
        except (OSError, ValueError):
            # Migrate the pre-remote-control temporary theme choice once.
            try:
                old = json.loads((self.directory / 'theme.json').read_text())
                if valid_override(old):
                    self.state['override'] = old
            except (OSError, ValueError):
                pass
        self.last_seen = 0
        self.applied_revision = None
        self.actual_theme = None
        self.actual_language = None
        self.activity = {'lastPollAt':None,'alerts':[]}
        self.instance = secrets.token_hex(8)
        key_path = self.directory / 'remote-key'
        try:
            self.key = key_path.read_text().strip()
            if len(self.key) < 32:
                raise ValueError('Invalid remote key')
        except (OSError, ValueError):
            self.key = secrets.token_urlsafe(32)
            key_path.write_text(self.key + '\n')
        key_path.chmod(0o600)

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            override = state.pop('override')
            if not valid_override(override):
                override = None
            hour = datetime.now().hour
            effective = override['theme'] if override else ('dark' if hour >= 18 or hour < 7 else 'light')
            state.update(instance=self.instance,
                         theme={'mode': override['theme'] if override else 'auto',
                                'override': override, 'effective': effective},
                         screen={'online': time.monotonic() - self.last_seen < 6 if self.last_seen else False,
                                 'appliedRevision': self.applied_revision, 'theme': self.actual_theme,
                                 'language': self.actual_language,
                                 'activity':copy.deepcopy(self.activity)})
            return state

    def command(self, command):
        if not isinstance(command, dict):
            raise ValueError('请求必须是对象')
        with self.lock:
            updated = copy.deepcopy(self.state)
            action = command.get('action')
            if action == 'theme' and command.get('mode') in ('auto', 'light', 'dark'):
                mode = command['mode']
                updated['override'] = None if mode == 'auto' else {'theme': mode, 'until': next_boundary()}
            elif action == 'language' and command.get('language') in ('zh', 'en'):
                updated['language'] = command['language']
            elif action == 'details' and command.get('provider') in PROVIDERS and type(command.get('open')) is bool:
                updated['details'][command['provider']] = command['open']
            elif action == 'overview':
                updated['details'] = dict.fromkeys(PROVIDERS, False)
            elif action == 'reload':
                updated['reloadId'] += 1
            else:
                raise ValueError('不支持的屏幕操作')
            updated['revision'] += 1
            # Commit only after the durable write succeeds.
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(updated))
            temporary.replace(self.path)
            self.state = updated
            return self.snapshot()

    def heartbeat(self, report):
        if (not isinstance(report, dict) or type(report.get('revision')) is not int
                or report.get('theme') not in ('light', 'dark')
                or ('language' in report and report['language'] not in ('zh', 'en'))):
            raise ValueError('无效屏幕状态')
        with self.lock:
            if report.get('instance') != self.instance or report['revision'] != self.state['revision']:
                return
            self.last_seen = time.monotonic()
            self.applied_revision = report['revision']
            self.actual_theme = report['theme']
            self.actual_language = report.get('language')
            activity = report.get('activity')
            if isinstance(activity,dict):
                last_poll = activity.get('lastPollAt')
                if isinstance(last_poll,(int,float)) and math.isfinite(last_poll):
                    self.activity['lastPollAt'] = last_poll
                alerts = activity.get('alerts')
                if isinstance(alerts,list):
                    valid = []
                    for alert in alerts[:3]:
                        if (not isinstance(alert,dict) or alert.get('provider') not in PROVIDERS
                                or alert.get('kind') not in ('completed','waiting')
                                or not isinstance(alert.get('id'),str) or len(alert['id'])>160
                                or not isinstance(alert.get('startedAt'),(int,float))
                                or not math.isfinite(alert['startedAt'])):
                            continue
                        valid.append({'id':alert['id'],'provider':alert['provider'],'kind':alert['kind'],
                            'host':str(alert.get('host',''))[:80],'preview':alert.get('preview') is True,
                            'startedAt':alert['startedAt'],
                            'duration':None if alert['kind']=='waiting' and alert.get('preview') is not True else 15})
                    self.activity['alerts'] = valid
