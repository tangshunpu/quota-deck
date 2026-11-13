"""Durable daily usage, aggregate APIs, and local/SSH background collection."""
from bisect import bisect_right
from datetime import date, datetime, timedelta
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from remote_tokens import load_hosts
from token_scan import scan_daily

PROVIDERS = ('codex','grok','claude')
FIELDS = ('input','cached','output','total')
BACKFILL_DAYS = 366
# Bump when a parser fix can lower saved counts (v2: double-counted forked sessions). Each host's
# existing logs are then reparsed once and replace their saved days; deleted logs keep theirs.
PARSER_VERSION = 2
HERE = Path(__file__).resolve().parent


def zero():
    return dict.fromkeys(FIELDS,0)


def add_usage(target, usage):
    for field in FIELDS:
        target[field] += usage[field]


def host_id(target):
    import hashlib
    return 'ssh-'+hashlib.sha256(target.encode()).hexdigest()[:20]


def local_timezone():
    candidates = [os.environ.get('TZ','').lstrip(':')]
    try:
        candidates.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass
    candidates.append('Asia/Singapore')
    for value in candidates:
        try:
            ZoneInfo(value)
            return value
        except (ZoneInfoNotFoundError, ValueError):
            continue


class TokenHistory:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        directory = self.home/'.local/share/quota-deck'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory/'tokens.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hosts(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,
                    last_attempt REAL,last_success REAL,errors INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS files(
                    host TEXT NOT NULL,id TEXT NOT NULL,provider TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,size INTEGER NOT NULL,through TEXT NOT NULL,PRIMARY KEY(host,id));
                CREATE TABLE IF NOT EXISTS daily(
                    host TEXT NOT NULL,file TEXT NOT NULL,provider TEXT NOT NULL,day TEXT NOT NULL,
                    input INTEGER NOT NULL,cached INTEGER NOT NULL,output INTEGER NOT NULL,total INTEGER NOT NULL,
                    PRIMARY KEY(host,file,day));
                CREATE INDEX IF NOT EXISTS daily_range ON daily(day,provider,host);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('timezone',local_timezone()))
            self.timezone = db.execute('SELECT value FROM metadata WHERE key=?',('timezone',)).fetchone()[0]
        self.path.chmod(0o600)
        self.zone = ZoneInfo(self.timezone)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def today(self):
        return datetime.now(self.zone).date()

    def manifest(self, host):
        with self.connect() as db:
            return {r['id']:[r['mtime_ns'],r['size'],r['through']] for r in db.execute('SELECT * FROM files WHERE host=?',(host,))}

    def window(self):
        end = self.today()
        return (end-timedelta(days=BACKFILL_DAYS-1)).isoformat(),end.isoformat()

    def reparse_due(self, host):
        with self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?',('parser:'+host,)).fetchone()
        return row is None or int(row['value']) < PARSER_VERSION

    def ingest(self, host, name, batch, replace=False):
        """Replace each file's absolute day totals, never add a repeated snapshot twice.

        Deleted/rotated logs and shorter partial rewrites cannot erase saved usage. Only a
        `replace` batch (a full reparse after a parser fix) may lower the days of logs it read.
        Validate the whole packet before committing any of it.
        """
        if not isinstance(batch,dict) or not isinstance(batch.get('files'),list):
            raise ValueError('Invalid token history packet')
        rows, files, seen = [], [], set()
        first,last = (date.fromisoformat(d) for d in self.window())
        for item in batch['files']:
            if (not isinstance(item,dict) or not isinstance(item.get('id'),str)
                    or not re.fullmatch('[a-f0-9]{64}',item['id']) or item['id'] in seen
                    or item.get('provider') not in PROVIDERS or not isinstance(item.get('days'),dict)):
                raise ValueError('Invalid token history file')
            seen.add(item['id'])
            for field in ('mtimeNs','size'):
                if type(item.get(field)) is not int or not 0 <= item[field] <= 2**63-1:
                    raise ValueError('Invalid token file fingerprint')
            if item.get('through') != last.isoformat():
                raise ValueError('Stale token history collection window')
            files.append((host,item['id'],item['provider'],item['mtimeNs'],item['size'],item['through']))
            for day,usage in item['days'].items():
                parsed = date.fromisoformat(day)
                if day != parsed.isoformat() or not first <= parsed <= last or not isinstance(usage,dict):
                    raise ValueError('Invalid token day')
                if any(type(usage.get(f)) is not int or not 0 <= usage[f] <= 2**63-1 for f in FIELDS):
                    raise ValueError('Invalid token count')
                if usage['input']+usage['cached']+usage['output'] != usage['total']:
                    raise ValueError('Inconsistent token count')
                rows.append((host,item['id'],item['provider'],day,*(usage[f] for f in FIELDS)))
        errors = batch.get('errors',0)
        if type(errors) is not int or errors < 0:
            raise ValueError('Invalid scan error count')
        now = time.time()
        with self.connect() as db:
            if replace:
                db.executemany('DELETE FROM daily WHERE host=? AND file=? AND day BETWEEN ? AND ?',
                    [(host,file_id,first.isoformat(),last.isoformat()) for file_id in seen])
                db.execute('INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    ('parser:'+host,str(PARSER_VERSION)))
            db.executemany('''INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(host,file,day) DO UPDATE SET input=excluded.input,cached=excluded.cached,
                output=excluded.output,total=excluded.total WHERE excluded.total>=daily.total''',rows)
            db.executemany('''INSERT INTO files VALUES(?,?,?,?,?,?) ON CONFLICT(host,id) DO UPDATE SET
                provider=excluded.provider,mtime_ns=excluded.mtime_ns,size=excluded.size,through=excluded.through''',files)
            db.execute('''INSERT INTO hosts VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,status=excluded.status,last_attempt=excluded.last_attempt,
                last_success=excluded.last_success,errors=excluded.errors''',
                (host,name,'partial' if errors else 'ok',now,now,errors))

    def failed(self, host, name):
        with self.connect() as db:
            db.execute('''INSERT INTO hosts(id,name,status,last_attempt,errors) VALUES(?,?,'offline',?,1)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,status='offline',
                last_attempt=excluded.last_attempt,errors=hosts.errors+1''',(host,name,time.time()))

    def report_failure(self, host, name):
        try:
            self.failed(host,name)
        except (OSError, sqlite3.Error):
            # A temporary database/disk failure must not stop future collection attempts.
            pass

    def history(self, days=30, provider=None, host=None, end=None):
        if type(days) is not int or not 1 <= days <= 366:
            raise ValueError('days must be between 1 and 366')
        if provider is not None and provider not in PROVIDERS:
            raise ValueError('unknown provider')
        last = date.fromisoformat(end) if end else self.today()
        if (end and end != last.isoformat()) or last > self.today():
            raise ValueError('end must be a local date not in the future')
        first = last-timedelta(days=days-1)
        filters, args = ['day BETWEEN ? AND ?'],[first.isoformat(),last.isoformat()]
        if provider:
            filters.append('provider=?');args.append(provider)
        if host:
            filters.append('host=?');args.append(host)
        with self.connect() as db:
            hosts = [dict(r) for r in db.execute('SELECT * FROM hosts ORDER BY id')]
            if host and host not in {h['id'] for h in hosts}:
                raise ValueError('unknown host')
            rows = [dict(r) for r in db.execute('SELECT day,provider,host,SUM(input) AS input,'
                'SUM(cached) AS cached,SUM(output) AS output,SUM(total) AS total FROM daily WHERE '+
                ' AND '.join(filters)+' GROUP BY day,provider,host ORDER BY day',args)]
        providers = (provider,) if provider else PROVIDERS
        daily = { (first+timedelta(days=i)).isoformat():{
            'date':(first+timedelta(days=i)).isoformat(),**zero(),'hasData':False,
            'providers':{p:{**zero(),'hasData':False} for p in providers}} for i in range(days)}
        totals = {p:zero() for p in providers}
        by_host = {h['id']:zero() for h in hosts if not host or h['id']==host}
        for row in rows:
            d = daily[row['day']]
            add_usage(d,row);d['hasData']=True
            add_usage(d['providers'][row['provider']],row);d['providers'][row['provider']]['hasData']=True
            add_usage(totals[row['provider']],row);add_usage(by_host[row['host']],row)
        summary = zero()
        for usage in totals.values():
            add_usage(summary,usage)
        values = list(daily.values())
        peak = max(values,key=lambda d:d['total'])
        summary.update(activeDays=sum(d['total']>0 for d in values),
            daysWithData=sum(d['hasData'] for d in values),averagePerDay=round(summary['total']/days,2),
            peak={'date':peak['date'],'total':peak['total']} if summary['total'] else None)
        return {'timezone':self.timezone,'startDate':first.isoformat(),'endDate':last.isoformat(),
            'days':days,'includesToday':last==self.today(),'filters':{'provider':provider,'host':host},
            'summary':summary,'providers':totals,'daily':values,
            'hosts':[{**h,'usage':by_host[h['id']]} for h in hosts if h['id'] in by_host]}

    def heatmap(self, **filters):
        result = self.history(**filters)
        first = date.fromisoformat(result['startDate'])
        grid_start = first-timedelta(days=(first.weekday()+1)%7)
        # Quartiles of the days with usage, so one spike cannot push every other day into level 1:
        # level = ceil(4 × share of usage days at or below this total).
        values = sorted(d['total'] for d in result['daily'] if d['total'])
        def level(total):
            return 0 if not total else -(-4*bisect_right(values,total)//len(values))
        upper = [0,None,None,None,None]
        for value in values:
            upper[level(value)] = value
        weeks = [[None]*7 for _ in range(((date.fromisoformat(result['endDate'])-grid_start).days//7)+1)]
        cells = []
        for day in result.pop('daily'):
            d = date.fromisoformat(day['date'])
            week,weekday = (d-grid_start).days//7,(d.weekday()+1)%7
            cell = {'date':day['date'],'total':day['total'],'hasData':day['hasData'],
                'level':level(day['total']),
                'week':week,'weekday':weekday}
            cells.append(cell);weeks[week][weekday]=cell
        result.update(cells=cells,weeks=weeks,weekStartsOn='sunday',
            gridStartDate=grid_start.isoformat(),scale={'method':'quartile','daysWithUsage':len(values),'upper':upper})
        return result

    def monthly(self, months=12, provider=None, host=None, end=None):
        """Calendar-month points for a line/bar chart; current month stays explicitly partial."""
        if type(months) is not int or not 1 <= months <= 120:
            raise ValueError('months must be between 1 and 120')
        today = self.today()
        if end and re.fullmatch(r'\d{4}-\d{2}',end):
            month_start = date.fromisoformat(end+'-01')
            if month_start > today:
                raise ValueError('end must not be a future month')
            following = date(month_start.year+month_start.month//12,month_start.month%12+1,1)
            last = min(following-timedelta(days=1),today)
        else:
            last = date.fromisoformat(end) if end else today
            if (end and end != last.isoformat()) or last > today:
                raise ValueError('end must be a local date not in the future')
        ordinal = last.year*12+last.month-1
        if ordinal-months+1 < 12:
            raise ValueError('month range precedes year 1')
        points, totals, by_host = [], {}, {}
        summary = zero()
        active_days = days_with_data = 0
        for value in range(ordinal-months+1,ordinal+1):
            year,month = divmod(value,12)
            first = date(year,month+1,1)
            next_year,next_month = divmod(value+1,12)
            following = date(next_year,next_month+1,1)
            calendar_end = following-timedelta(days=1)
            period_end = min(calendar_end,last)
            count = (period_end-first).days+1
            result = self.history(days=count,provider=provider,host=host,end=period_end.isoformat())
            points.append({'month':first.isoformat()[:7],'startDate':first.isoformat(),
                'endDate':period_end.isoformat(),'days':count,'calendarDays':calendar_end.day,
                'isPartial':period_end<calendar_end or period_end==today,
                'hasData':result['summary']['daysWithData']>0,**result['summary'],
                'providers':result['providers']})
            add_usage(summary,result['summary'])
            active_days += result['summary']['activeDays']
            days_with_data += result['summary']['daysWithData']
            for key,usage in result['providers'].items():
                add_usage(totals.setdefault(key,zero()),usage)
            for entry in result['hosts']:
                add_usage(by_host.setdefault(entry['id'],zero()),entry['usage'])
        summary.update(activeDays=active_days,daysWithData=days_with_data,
            activeMonths=sum(p['total']>0 for p in points),monthsWithData=sum(p['hasData'] for p in points),
            averagePerMonth=round(summary['total']/months,2))
        return {'timezone':self.timezone,'startDate':points[0]['startDate'],'endDate':last.isoformat(),
            'months':months,'includesCurrentMonth':(last.year,last.month)==(today.year,today.month),
            'filters':{'provider':provider,'host':host},'summary':summary,'providers':totals,
            'monthly':points,'hosts':[{**h,'usage':by_host[h['id']]} for h in result['hosts']]}

    def collect_local(self):
        first,last = self.window()
        reparse = self.reparse_due('local')
        known = {} if reparse else self.manifest('local')
        self.ingest('local','本机',scan_daily(self.home,known,first,last,self.timezone),replace=reparse)

    def collect_remote(self, config):
        target = config['ssh'];host = host_id(target);name = str(config.get('name') or target)
        first,last = self.window()
        # Stream parsers and a hash-only manifest; install/write nothing on the Mac.
        scanner = (HERE/'token_scan.py').read_text().replace('from tokens import ENTRIES, SOURCES\n','')
        reparse = self.reparse_due(host)
        parameters = json.dumps({'known':{} if reparse else self.manifest(host),'start':first,'end':last,
                                 'timezone':self.timezone})
        script = (HERE/'tokens.py').read_text()+'\n'+scanner+'\nimport json\n'
        script += '_args=json.loads('+repr(parameters)+')\nprint(json.dumps(scan_daily(Path.home(),**_args)))\n'
        response = subprocess.run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=8',
            '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',target,'python3 -'],
            input=script,capture_output=True,text=True,timeout=90)
        if response.returncode:
            raise RuntimeError('Remote token history scan failed')
        batch = json.loads(response.stdout.strip().splitlines()[-1])
        self.ingest(host,name,batch,replace=reparse)

    def run_local(self, stop):
        while not stop.is_set():
            try:
                self.collect_local()
            except (OSError, ValueError, sqlite3.Error):
                self.report_failure('local','本机')
            stop.wait(60)

    def run_remote(self, stop):
        while not stop.is_set():
            seen = set()
            for config in load_hosts(self.home):
                if stop.is_set():
                    return
                target = config['ssh']
                if target in seen:
                    continue
                seen.add(target)
                try:
                    self.collect_remote(config)
                except (OSError, ValueError, IndexError, RuntimeError, subprocess.SubprocessError, sqlite3.Error):
                    self.report_failure(host_id(target),str(config.get('name') or target))
            stop.wait(120)
