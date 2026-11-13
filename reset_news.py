"""Read-only community global-reset announcements and forecasts; never account quota."""
import copy
from datetime import datetime
import json
import math
import re
import threading
import time
from urllib.request import Request, urlopen

ANNOUNCEMENTS = 'https://codex-resets.com/api/resets?locale=zh-CN'
FORECAST = 'https://codex-reset.com/api/forecast?tz=Asia%2FSingapore'
MAX_AGE = 1800


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def percent(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 else None


def tweet_url(value):
    return value if isinstance(value, str) and re.fullmatch(r'https://x\.com/thsottiaux/status/\d+', value) else None


def parse_announcements(data):
    if not isinstance(data, dict) or not isinstance(data.get('events'), list) or 'scheduled' not in data:
        raise ValueError('Invalid announcement response')
    item = data['scheduled']
    scheduled = None
    if item is not None:
        if not isinstance(item, dict):
            raise ValueError('Invalid scheduled reset')
        deadline, announced = timestamp(item.get('scheduled_for')), timestamp(item.get('announced_at'))
        if not deadline or not announced or deadline < announced:
            raise ValueError('Invalid reset dates')
        # A banked grant is not an automatic global reset.
        if item.get('reset_type') == 'regular':
            scheduled = {'deadline':deadline, 'announcedAt':announced, 'url':tweet_url(item.get('tweet_url')),
                         'source':'https://codex-resets.com/zh-CN'}
    stats = data.get('stats') or {}
    signals = []
    for event in data['events'][:8]:
        if not isinstance(event, dict) or event.get('reset_type') != 'regular':
            continue
        at, url = timestamp(event.get('announced_at')), tweet_url(event.get('tweet_url'))
        if at and url:
            signals.append({'id':'reset:'+url.rsplit('/', 1)[-1], 'kind':'reset', 'at':at})
    return {'scheduled':scheduled, 'lastResetAt':timestamp(stats.get('last_reset_at')), 'signals':signals}


def parse_forecast(data):
    if not isinstance(data, dict) or not isinstance(data.get('probabilities'), dict):
        raise ValueError('Invalid forecast response')
    probabilities = data['probabilities']
    updated = timestamp(data.get('updated_at'))
    p24, p48 = percent(probabilities.get('rounded_24h')), percent(probabilities.get('rounded_48h'))
    if updated is None or p24 is None or p48 is None or p48 < p24:
        raise ValueError('Invalid forecast values')
    scheduled = None
    signal = data.get('official_signal') or {}
    window = signal.get('window') or {}
    deadline = timestamp(window.get('target_at') or window.get('end_at'))
    announced = timestamp(signal.get('at'))
    if (data.get('mode') == 'announced' and signal.get('signal_type') == 'dated_commitment'
            and deadline and announced and deadline >= announced):
        scheduled = {'deadline':deadline, 'announcedAt':announced, 'url':tweet_url(signal.get('url')),
                     'source':'https://codex-reset.com/'}
    return {'scheduled':scheduled, 'lastResetAt':timestamp(data.get('last_reset_at')),
            'updatedAt':updated, 'probability24h':p24, 'probability48h':p48}


def get_json(url):
    request = Request(url, headers={'Accept':'application/json', 'User-Agent':'QuotaDeck/1.0'})
    with urlopen(request, timeout=12) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError('Response too large')
    return json.loads(body)


class ResetNews:
    def __init__(self, fetch=get_json, clock=time.time):
        self.fetch, self.clock = fetch, clock
        self.lock = threading.Lock()
        self.sources = {}

    def refresh(self):
        for key, url, parse in (('announcements', ANNOUNCEMENTS, parse_announcements),
                                ('forecast', FORECAST, parse_forecast)):
            try:
                parsed = parse(self.fetch(url))
                with self.lock:
                    self.sources[key] = {'data':parsed, 'fetchedAt':self.clock(), 'failed':False}
            except Exception:
                with self.lock:
                    previous = self.sources.get(key, {'data':{}, 'fetchedAt':None})
                    self.sources[key] = {**previous, 'failed':True}

    def snapshot(self):
        now = self.clock()
        with self.lock:
            sources = copy.deepcopy(self.sources)
        def fresh(key):
            entry = sources.get(key, {})
            fetched = entry.get('fetchedAt')
            if fetched is None or not 0 <= now - fetched <= MAX_AGE:
                return None
            if key == 'forecast' and not -60 <= now - entry['data']['updatedAt'] <= MAX_AGE:
                return None
            return entry
        announcements, forecast = fresh('announcements'), fresh('forecast')
        entries = [entry for entry in (announcements, forecast) if entry]
        last_reset = max((entry['data'].get('lastResetAt') or 0 for entry in entries), default=0)
        base = {'mode':'unavailable', 'timezone':'Asia/Singapore', 'scheduled':None,
                'forecast':None, 'stale':False, 'updatedAt':None, 'signals':[]}
        # Only fresh source records emit news signals. No prediction or elapsed timer does.
        signals = {}
        for entry in entries:
            if entry['failed']:
                continue
            for signal in entry['data'].get('signals', []):
                if 0 <= now - signal['at'] <= MAX_AGE:
                    signals[signal['id']] = signal
        base['signals'] = list(signals.values())
        # Use the newest valid announcement; elapsed deadlines never imply successful resets.
        candidates = [(entry['data']['scheduled'], entry) for entry in entries
                      if entry['data'].get('scheduled') and
                      entry['data']['scheduled']['deadline'] > now and
                      last_reset < entry['data']['scheduled']['announcedAt'] <= now]
        if candidates:
            scheduled, entry = max(candidates, key=lambda pair:pair[0]['announcedAt'])
            if not entry['failed'] and scheduled['url'] and 0 <= now - scheduled['announcedAt'] <= MAX_AGE:
                base['signals'].append({'id':'announcement:'+scheduled['url'].rsplit('/', 1)[-1],
                                        'kind':'announcement', 'at':scheduled['announcedAt']})
            return {**base, 'mode':'scheduled', 'scheduled':scheduled,
                    'stale':entry['failed'], 'updatedAt':entry['fetchedAt']}
        if forecast:
            data = forecast['data']
            return {**base, 'mode':'forecast', 'stale':forecast['failed'] or not announcements or announcements['failed'],
                    'updatedAt':data['updatedAt'], 'forecast':{
                        'probability24h':data['probability24h'], 'probability48h':data['probability48h'],
                        'source':'https://codex-reset.com/'}}
        return base

    def run(self, stop):
        while not stop.is_set():
            self.refresh()
            with self.lock:
                failed = any(entry['failed'] for entry in self.sources.values())
            stop.wait(60 if failed else 300)
