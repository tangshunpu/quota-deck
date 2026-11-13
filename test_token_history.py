from datetime import date, datetime, timedelta, timezone
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from token_history import TokenHistory, host_id
from token_scan import scan_daily
from test_tokens import write


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)
        self.clock=patch('token_history.local_timezone',return_value='Asia/Singapore')
        self.clock.start()
        self.history=TokenHistory(self.home)
        self.today=self.history.today()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def usage(self,n):
        return {'input':n,'cached':0,'output':0,'total':n}

    def batch(self,n,day=None,id='a',provider='codex',extra=None):
        days={(day or self.today).isoformat():self.usage(n)}
        if extra:days.update(extra)
        return {'files':[{'id':id*64,'provider':provider,'mtimeNs':n+1,'size':n+1,
                         'through':self.today.isoformat(),'days':days}],'errors':0}

    def test_idempotent_updates_restart_and_deleted_logs_keep_history(self):
        h=self.history;batch=self.batch(100)
        h.ingest('local','本机',batch);h.ingest('local','本机',batch)
        self.assertEqual(h.history()['summary']['total'],100)
        h.ingest('local','本机',self.batch(120))
        h.ingest('local','本机',self.batch(20))
        h.ingest('local','本机',{'files':[]})
        self.assertEqual(TokenHistory(self.home).history()['summary']['total'],120)
        h.ingest('local','本机',self.batch(30,id='b'))
        self.assertEqual(h.history()['summary']['total'],150)

    def test_window_filters_hosts_and_missing_days(self):
        h=self.history
        h.ingest('local','本机',self.batch(10,extra={
            (self.today-timedelta(days=7)).isoformat():self.usage(20),
            (self.today-timedelta(days=29)).isoformat():self.usage(30)}))
        h.ingest('mac','Mac',self.batch(40,provider='claude'))
        h.failed('mac','Renamed Mac')
        seven=h.history(days=7);thirty=h.history(days=30)
        self.assertEqual(seven['summary']['total'],50)
        self.assertEqual(thirty['summary']['total'],100)
        self.assertEqual(len(seven['daily']),7)
        self.assertFalse(seven['daily'][0]['hasData'])
        self.assertTrue(seven['daily'][-1]['hasData'])
        self.assertEqual(h.history(provider='codex')['summary']['total'],60)
        self.assertEqual(h.history(host='mac')['summary']['total'],40)
        self.assertEqual(h.history(host='mac')['hosts'][0]['status'],'offline')
        self.assertEqual(sum(d['total'] for d in thirty['daily']),thirty['summary']['total'])
        self.assertEqual(thirty['summary']['activeDays'],3)
        self.assertEqual(thirty['summary']['daysWithData'],3)
        self.assertEqual(thirty['summary']['peak']['total'],50)

    def test_heatmap_sunday_alignment_levels_and_padding(self):
        h=self.history
        h.ingest('local','本机',self.batch(100,extra={
            (self.today-timedelta(days=1)).isoformat():self.usage(25)}))
        result=h.heatmap(days=30)
        self.assertEqual(len(result['cells']),30)
        self.assertEqual(result['cells'][-1]['level'],4)
        self.assertEqual(result['cells'][-2]['level'],2)
        self.assertEqual(result['cells'][0]['level'],0)
        grid=datetime.fromisoformat(result['gridStartDate'])
        self.assertEqual(grid.weekday(),6)
        actual=[]
        for week in result['weeks']:
            self.assertEqual(len(week),7)
            for cell in week:
                if cell:
                    self.assertEqual(result['weeks'][cell['week']][cell['weekday']],cell)
                    actual.append(cell['date'])
        self.assertEqual(actual,[d['date'] for d in result['cells']])
        self.assertEqual(h.heatmap(days=7)['summary']['total'],125)

    def test_heatmap_quartiles_keep_contrast_under_a_single_spike(self):
        h=self.history
        days={(self.today-timedelta(days=n)).isoformat():self.usage(10*n) for n in range(1,8)}
        days[(self.today-timedelta(days=8)).isoformat()]=self.usage(0)
        h.ingest('local','本机',self.batch(100000,extra=days))
        result=h.heatmap(days=30)
        levels={c['date']:c['level'] for c in result['cells']}
        # Usage days 10…70 and a spike rank into quartiles; a logged day with 0 tokens stays level 0.
        self.assertEqual([levels[(self.today-timedelta(days=n)).isoformat()] for n in range(1,8)],[1,1,2,2,3,3,4])
        self.assertEqual(levels[self.today.isoformat()],4)
        self.assertEqual(levels[(self.today-timedelta(days=8)).isoformat()],0)
        self.assertEqual(result['scale'],{'method':'quartile','daysWithUsage':8,'upper':[0,20,40,60,100000]})
        # Filters rank their own days; a lone usage day is the top level, equal days share it.
        self.assertEqual([c['level'] for c in h.heatmap(days=1)['cells']],[4])
        h.ingest('mac','Mac',self.batch(5,id='b',provider='claude',extra={(self.today-timedelta(days=1)).isoformat():self.usage(5)}))
        claude=h.heatmap(days=30,provider='claude')
        self.assertEqual([c['level'] for c in claude['cells'] if c['total']],[4,4])
        self.assertEqual(claude['scale']['upper'],[0,None,None,None,5])
        self.assertEqual(h.heatmap(days=30,provider='grok')['scale'],{'method':'quartile','daysWithUsage':0,'upper':[0,None,None,None,None]})

    def test_monthly_calendar_boundaries_leap_year_and_current_partial_month(self):
        h=self.history
        with patch.object(h,'today',return_value=date(2024,3,5)):
            days={'2023-12-31':self.usage(10),'2024-01-01':self.usage(20),
                  '2024-02-29':self.usage(30),'2024-03-05':self.usage(40)}
            h.ingest('local','本机',{'files':[{'id':'a'*64,'provider':'codex','mtimeNs':1,
                'size':1,'through':'2024-03-05','days':days}]})
            r=h.monthly(months=4)
            self.assertEqual([p['month'] for p in r['monthly']],['2023-12','2024-01','2024-02','2024-03'])
            self.assertEqual([p['total'] for p in r['monthly']],[10,20,30,40])
            self.assertEqual(r['summary']['total'],100)
            self.assertEqual(r['summary']['averagePerMonth'],25)
            self.assertEqual(r['monthly'][2]['days'],29)
            self.assertEqual(r['monthly'][2]['endDate'],'2024-02-29')
            self.assertFalse(r['monthly'][2]['isPartial'])
            self.assertTrue(r['monthly'][3]['isPartial'])
            self.assertEqual(r['monthly'][3]['days'],5)
            self.assertEqual(r['monthly'][3]['calendarDays'],31)
            old=h.monthly(months=2,end='2024-02')
            self.assertEqual(old['endDate'],'2024-02-29')
            self.assertFalse(old['includesCurrentMonth'])
            self.assertEqual(old['summary']['total'],50)
            self.assertEqual(h.monthly(months=1,end='2024-02-15')['summary']['total'],0)
            self.assertTrue(h.monthly(months=1,end='2024-02-15')['monthly'][0]['isPartial'])
            self.assertEqual(h.monthly(months=1,provider='claude')['summary']['total'],0)
            self.assertEqual(h.monthly(months=1,host='local')['summary']['total'],40)

    def test_monthly_empty_months_filters_and_invalid_parameters(self):
        h=self.history
        h.ingest('local','本机',self.batch(10))
        h.ingest('mac','Mac',self.batch(30,provider='claude'))
        r=h.monthly()
        self.assertEqual(len(r['monthly']),12)
        self.assertFalse(r['monthly'][0]['hasData'])
        self.assertEqual(r['monthly'][0]['total'],0)
        self.assertEqual(r['summary']['total'],40)
        self.assertEqual(sum(p['total'] for p in r['monthly']),r['summary']['total'])
        self.assertEqual(h.monthly(host='mac')['summary']['total'],30)
        self.assertEqual(h.monthly(provider='codex')['summary']['total'],10)
        for filters in ({'months':0},{'months':121},{'months':True},{'provider':'bad'},{'host':'bad'},
                        {'end':'2024-13'},{'end':'0001-01','months':2},
                        {'end':(self.today+timedelta(days=35)).strftime('%Y-%m')}):
            with self.subTest(filters=filters),self.assertRaises(ValueError):h.monthly(**filters)

    def test_invalid_packets_rollback_and_bad_queries(self):
        h=self.history
        invalid=self.batch(10)
        invalid['files'].append({**invalid['files'][0],'id':'b'*64,'days':{self.today.isoformat():self.usage(-1)}})
        with self.assertRaises(ValueError):h.ingest('local','本机',invalid)
        self.assertEqual(h.history()['summary']['total'],0)
        self.assertEqual(h.manifest('local'),{})
        for filters in ({'days':0},{'days':367},{'days':True},{'provider':'unknown'},
                        {'host':'not-known'},{'end':'2026-02-30'},
                        {'end':(self.today+timedelta(days=1)).isoformat()}):
            with self.subTest(filters=filters),self.assertRaises(ValueError):h.history(**filters)

    def test_sqlite_concurrent_read_and_two_host_writers(self):
        errors=[]
        def write_host(host):
            try:
                for n in range(1,10):self.history.ingest(host,host,self.batch(n))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=write_host,args=(host,)) for host in ('local','mac')]
        for t in threads:t.start()
        for _ in range(10):self.history.history(days=7)
        for t in threads:t.join()
        self.assertEqual(errors,[])
        self.assertEqual(self.history.history()['summary']['total'],18)

    def test_parser_crosses_midnight_using_display_timezone_and_reuses_manifest(self):
        day=self.today
        before=datetime(day.year,day.month,day.day,tzinfo=timezone.utc)-timedelta(hours=8,seconds=1)
        after=before+timedelta(seconds=2)
        def event(at,total,last):
            return {'timestamp':at.isoformat(),'payload':{'type':'token_count','info':{
                'total_token_usage':{'input_tokens':total,'cached_input_tokens':0,'output_tokens':0},
                'last_token_usage':{'input_tokens':last,'cached_input_tokens':0,'output_tokens':0}}}}
        path=self.home/'.codex/sessions/a.jsonl'
        write(path,[event(before,10,10),event(after,30,20)])
        self.history.collect_local()
        result=self.history.history(days=7)
        self.assertEqual([d['total'] for d in result['daily'][-2:]],[10,20])
        first,last=self.history.window()
        with patch('token_scan.ENTRIES',{'codex':lambda _:self.fail('unchanged file reparsed')}):
            batch=scan_daily(self.home,self.history.manifest('local'),first,last,self.history.timezone)
        self.assertEqual(batch['files'],[])
        path.unlink();self.history.collect_local()
        self.assertEqual(self.history.history()['summary']['total'],30)

    def test_shared_claude_and_grok_readers_and_malformed_rows(self):
        at=datetime.now(timezone.utc).isoformat()
        usage={'input_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':5,'output_tokens':7}
        row={'type':'assistant','timestamp':at,'requestId':'r','message':{'id':'m','usage':usage}}
        write(self.home/'.claude/projects/p/s.jsonl',[[],row,row])
        path=self.home/'.grok/sessions/p/s/usage.json';path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'turns':[None,{'endedAt':at,'inputTokens':100,'cachedReadTokens':30,
            'cacheCreationTokens':5,'outputTokens':10}]}))
        self.history.collect_local()
        result=self.history.history()
        self.assertEqual(result['providers']['claude']['total'],42)
        self.assertEqual(result['providers']['grok']['total'],115)
        self.assertEqual(result['summary']['cached'],50)

    def test_codex_archiving_and_duplicate_copy_do_not_double_count(self):
        at=datetime.now(timezone.utc).isoformat()
        row={'timestamp':at,'payload':{'type':'token_count','info':{
            'total_token_usage':{'input_tokens':10},'last_token_usage':{'input_tokens':10}}}}
        source=self.home/'.codex/sessions/2026/rollout-unique.jsonl'
        archive=self.home/'.codex/archived_sessions/rollout-unique.jsonl'
        write(source,[row]);self.history.collect_local()
        write(archive,[row]);self.history.collect_local()
        self.assertEqual(self.history.history()['summary']['total'],10)
        source.unlink();self.history.collect_local()
        self.assertEqual(self.history.history()['summary']['total'],10)

    @unittest.skipUnless(shutil.which('zstd'),'zstd CLI unavailable')
    def test_codex_compressed_rollouts_keep_identity_and_reject_truncation(self):
        at=datetime.now(timezone.utc).isoformat()
        row={'timestamp':at,'payload':{'type':'token_count','info':{
            'total_token_usage':{'input_tokens':10},'last_token_usage':{'input_tokens':10}}}}
        source=self.home/'.codex/sessions/2026/rollout-a.jsonl'
        write(source,[row]);self.history.collect_local()
        # Codex compresses older rollouts in place; the same rollout must not count twice.
        subprocess.run(['zstd','-q','--rm',str(source)],check=True)
        self.history.collect_local()
        self.assertEqual(self.history.history()['summary']['total'],10)
        archived=self.home/'.codex/archived_sessions/rollout-b.jsonl'
        write(archived,[row,{**row,'payload':{**row['payload'],'info':{
            'total_token_usage':{'input_tokens':25},'last_token_usage':{'input_tokens':15}}}}])
        subprocess.run(['zstd','-q','--rm',str(archived)],check=True)
        self.history.collect_local()
        self.assertEqual(self.history.history()['summary']['total'],35)
        packed=Path(str(archived)+'.zst').read_bytes()
        broken=self.home/'.codex/sessions/2026/rollout-c.jsonl.zst'
        broken.write_bytes(packed[:len(packed)//2])
        first,last=self.history.window()
        batch=scan_daily(self.home,self.history.manifest('local'),first,last,self.history.timezone)
        self.assertEqual((batch['files'],batch['errors']),([],1))

    def test_parser_upgrade_reparses_existing_logs_once(self):
        h=self.history;day=self.today.isoformat()
        at=datetime.now(timezone.utc).isoformat()
        usage={'input_tokens':10,'cache_read_input_tokens':20,'cache_creation_input_tokens':5,'output_tokens':7}
        write(self.home/'.claude/projects/p/s.jsonl',[{'type':'assistant','timestamp':at,'requestId':'r',
            'message':{'id':'m','usage':usage}}])
        live=hashlib.sha256('claude/p/s.jsonl'.encode()).hexdigest()
        def packet(file_id,provider,n):
            return {'files':[{'id':file_id,'provider':provider,'mtimeNs':1,'size':1,'through':day,
                              'days':{day:self.usage(n)}}],'errors':0}
        # Saved by an older parser: an over-count for a live log and a reading whose log is gone.
        h.ingest('local','本机',packet(live,'claude',84));h.ingest('local','本机',packet('d'*64,'codex',7))
        self.assertTrue(h.reparse_due('local'))
        h.collect_local()
        self.assertEqual(h.history()['summary']['total'],42+7)
        self.assertFalse(h.reparse_due('local'))
        self.assertTrue(h.reparse_due(host_id('mac')))
        h.ingest('local','本机',packet(live,'claude',30));h.collect_local()
        self.assertEqual(h.history()['summary']['total'],42+7)

    def test_new_calendar_day_rechecks_previously_future_records(self):
        day=self.today
        tomorrow=day+timedelta(days=1)
        path=self.home/'.grok/sessions/a/b/usage.json'
        path.parent.mkdir(parents=True)
        at=datetime(tomorrow.year,tomorrow.month,tomorrow.day,12,tzinfo=timezone.utc).isoformat()
        path.write_text(json.dumps({'turns':[{'endedAt':at,'inputTokens':50}]}))
        self.history.collect_local()
        self.assertEqual(self.history.history()['summary']['total'],0)
        with patch.object(self.history,'today',return_value=tomorrow):
            self.history.collect_local()
            self.assertEqual(self.history.history()['summary']['total'],50)

    def test_remote_packet_script_is_read_only_and_uses_fixed_display_timezone(self):
        h=self.history
        def remote(args,**kwargs):
            self.assertEqual(args[-2:],['test-target','python3 -'])
            script=kwargs['input']
            self.assertIn('Asia/Singapore',script)
            self.assertNotIn('sqlite3',script)
            # Execute the streamed source under an isolated HOME, with stdout as the SSH transport.
            namespace={'__name__':'__remote__'}
            output=io.StringIO()
            with patch('pathlib.Path.home',return_value=self.home),contextlib.redirect_stdout(output):
                exec(compile(script,'<remote token scan>','exec'),namespace)
            return type('Result',(),{'returncode':0,'stdout':output.getvalue()})()
        with patch('token_history.subprocess.run',side_effect=remote):
            h.collect_remote({'ssh':'test-target','name':'Mac'})
        self.assertEqual(h.history()['hosts'][0]['id'],host_id('test-target'))
        self.assertEqual(h.history()['hosts'][0]['status'],'ok')


if __name__=='__main__':unittest.main()
