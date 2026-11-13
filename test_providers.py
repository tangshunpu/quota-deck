import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from providers import window, codex_result, claude_result, grok_result, apply_configured_plan, ProviderError
from server import QuotaStore
import os
import providers


def write_claude(home, token, expires):
    path=Path(home)/'.claude/.credentials.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({'claudeAiOauth':{'accessToken':token,'expiresAt':expires*1000,
                                                 'subscriptionType':'max','rateLimitTier':'default_claude_max_5x'}}))


class QuotaTests(unittest.TestCase):
    def test_missing_and_malformed_percent_never_become_full(self):
        for value in [None,True,False,'0',float('nan'),float('inf'),-1]:
            self.assertIsNone(window('test',value))
        self.assertEqual(window('test',0)['remaining'],100)

    def test_exhausted_and_overused_quota(self):
        self.assertEqual(window('test',100)['remaining'],0)
        self.assertEqual(window('test',125)['remaining'],0)

    def test_codex_primary_may_be_weekly(self):
        result=codex_result({'rateLimits':{'primary':{'usedPercent':2,'windowDurationMins':10080}}})
        self.assertEqual(result['windows'][0]['label'],'每周额度')
        self.assertEqual(result['windows'][0]['remaining'],98)
        self.assertEqual(len(result['windows']),1)

    def test_codex_reads_correct_bucket(self):
        result=codex_result({'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':10}}},
                             'rateLimits':{'primary':{'usedPercent':99}}})
        self.assertEqual(result['windows'][0]['remaining'],90)

    def test_codex_reset_credits_count_only_available_unexpired(self):
        now=time.time()
        credits=[{'status':'available','expiresAt':now+3600},{'status':'available','expiresAt':now+7200},
                 {'status':'used','expiresAt':now+3600},{'status':'available','expiresAt':now-10}]
        result=codex_result({'rateLimitResetCredits':{'credits':credits}})
        self.assertEqual(result['resetCredits']['available'],2)
        self.assertAlmostEqual(result['resetCredits']['nextExpiry'],now+3600)
        self.assertIsNone(codex_result({})['resetCredits'])

    def test_configured_plan_fills_only_missing_fields(self):
        with tempfile.TemporaryDirectory() as home:
            path=Path(home)/'.config/quota-deck/plans.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'codex':{'plan':'Ignored','tier':'10x'},'grok':'Premium'}))
            self.assertEqual(apply_configured_plan({'plan':'Pro','tier':None},home,'codex'),{'plan':'Pro','tier':'10x'})
            self.assertEqual(apply_configured_plan({'plan':'','tier':None},home,'grok'),{'plan':'Premium','tier':None})
            self.assertEqual(apply_configured_plan({'plan':'Max','tier':'5x'},home,'claude'),{'plan':'Max','tier':'5x'})

    def test_claude_partial_payload(self):
        result=claude_result({'five_hour':None,'seven_day':{'utilization':21}}, {})
        self.assertEqual(len(result['windows']),1)
        self.assertEqual(result['windows'][0]['remaining'],79)

    def test_grok_period_without_percent_is_unknown(self):
        result=grok_result({'config':{'currentPeriod':{'start':time.time()-100,'end':time.time()+100}}})
        self.assertEqual(result['windows'],[])
        self.assertEqual(result['status'],'unavailable')

    def test_grok_weekly_period_and_real_zero(self):
        start=time.time()-100
        result=grok_result({'config':{'creditUsagePercent':0,'currentPeriod':{'start':start,'end':start+604800}}})
        self.assertEqual(result['windows'][0]['label'],'每周额度')
        self.assertEqual(result['windows'][0]['remaining'],100)

    def test_network_failure_preserves_timestamp_as_stale(self):
        store=QuotaStore()
        store.providers['codex']={'id':'codex','status':'ready','updatedAt':123,'windows':[window('week',5)]}
        def fail(_):raise ProviderError('error','网络失败')
        store._one('codex',fail)
        result=store.providers['codex']
        self.assertEqual(result['status'],'stale')
        self.assertEqual(result['updatedAt'],123)

    def test_auth_failure_clears_old_account_reading(self):
        store=QuotaStore()
        store.providers['grok']['windows']=[window('week',5)]
        def fail(_):raise ProviderError('expired','登录过期')
        store._one('grok',fail)
        self.assertEqual(store.providers['grok']['windows'],[])

    def test_next_read_follows_window_reset(self):
        store=QuotaStore()
        now=time.time()
        self.assertEqual(store.next_wait(295),295)
        store.providers['codex']['windows']=[window('5h',40,now+200),window('week',10,now+86400)]
        self.assertAlmostEqual(store.next_wait(295),215,delta=2)
        store.providers['codex']['windows']=[window('5h',40,now+10)]
        self.assertEqual(store.next_wait(295),60)
        store.providers['codex']['windows']=[window('5h',40,now-30)]
        self.assertEqual(store.next_wait(65),65)

    def test_refresh_is_throttled(self):
        store=QuotaStore()
        store.last_started=time.time()
        self.assertFalse(store.refresh())

    def test_rate_limit_backoff(self):
        store=QuotaStore()
        def fail(_):raise ProviderError('limited','稍后重试')
        store._one('claude',fail)
        with patch('builtins.print') as unused:
            store._one('claude',lambda _:self.fail('must not retry within cooldown'))
        self.assertGreater(store.cooldowns['claude'],time.time())


class ClaudeRenewalTests(unittest.TestCase):
    def setUp(self):
        providers.claude_refresh_state.update(at=0,error=None)
        self.home=tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.seen=[]
        patcher=patch('providers.get_json',side_effect=lambda url,headers:(
            self.seen.append(headers['Authorization']) or {'seven_day':{'utilization':30}}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_token_skips_cli(self):
        write_claude(self.home.name,'fresh',time.time()+3600)
        with patch('providers.claude_cli_touch',side_effect=AssertionError('no touch')):
            result=providers.fetch_claude(Path(self.home.name))
        self.assertEqual(result['status'],'ready')
        self.assertEqual(self.seen,['Bearer fresh'])

    def test_expired_token_is_renewed_by_cli(self):
        write_claude(self.home.name,'old',time.time()-10)
        def touch(home):
            write_claude(home,'renewed',time.time()+28800)
            return 'changed'
        with patch('providers.claude_cli_touch',side_effect=touch):
            result=providers.fetch_claude(Path(self.home.name))
        self.assertEqual((result['status'],result['tier']),('ready','5x'))
        self.assertEqual(self.seen,['Bearer renewed'])

    def test_near_expiry_keeps_valid_token_when_cli_fails(self):
        write_claude(self.home.name,'soon',time.time()+120)
        with patch('providers.claude_cli_touch',return_value='exited'):
            self.assertEqual(providers.fetch_claude(Path(self.home.name))['status'],'ready')
        self.assertEqual(self.seen,['Bearer soon'])

    def test_timeout_is_transient_and_cooled_down(self):
        write_claude(self.home.name,'old',time.time()-10)
        with patch('providers.claude_cli_touch',return_value='timeout') as touch:
            for _ in range(2):
                with self.assertRaises(ProviderError) as caught:
                    providers.fetch_claude(Path(self.home.name))
                self.assertEqual(caught.exception.status,'error')
        self.assertEqual(touch.call_count,1)
        self.assertEqual(self.seen,[])

    def test_failed_renewal_reports_expired(self):
        write_claude(self.home.name,'old',time.time()-10)
        with patch('providers.claude_cli_touch',return_value='exited'):
            with self.assertRaises(ProviderError) as caught:
                providers.fetch_claude(Path(self.home.name))
        self.assertEqual(caught.exception.status,'expired')

    def test_stale_reading_kept_when_renewal_times_out(self):
        store=QuotaStore()
        store.providers['claude']={'id':'claude','windows':[window('week',30)],'status':'ready','updatedAt':5}
        store._one('claude',lambda _:(_ for _ in ()).throw(ProviderError('error','Claude 登录续期未完成')))
        self.assertEqual((store.providers['claude']['status'],store.providers['claude']['updatedAt']),('stale',5))

    def fake_cli(self, body):
        bindir=Path(self.home.name)/'bin'
        bindir.mkdir(exist_ok=True)
        script=bindir/'claude'
        script.write_text('#!/usr/bin/env python3\n'+body)
        script.chmod(0o755)
        return patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH']})

    def test_cli_touch_runs_in_pty_and_detects_rewrite(self):
        write_claude(self.home.name,'old',time.time()-10)
        body=('import json,os,sys,time\nassert sys.stdin.isatty()\nprint("Do you trust this folder?",flush=True)\n'
              'import tty\ntty.setraw(0)\nassert os.read(0,3)==b"\\x1b[B"\nassert os.read(0,1)==b"\\r"\nos.system("stty sane")\n'
              'print("Context 0%",flush=True)\nline=sys.stdin.readline()\n'
              'assert line.startswith("/status"), line\n'
              'p=os.path.expanduser("~/.claude/.credentials.json")\n'
              'json.dump({"claudeAiOauth":{"accessToken":"cli","expiresAt":(time.time()+3600)*1000}},open(p,"w"))\n'
              'time.sleep(30)\n')
        with self.fake_cli(body):
            started=time.monotonic()
            self.assertEqual(providers.claude_cli_touch(Path(self.home.name),timeout=8),'changed')
        self.assertLess(time.monotonic()-started,6)
        self.assertEqual(providers.claude_auth(self.home.name)['accessToken'],'cli')

    def test_cli_touch_times_out_and_reaps(self):
        write_claude(self.home.name,'old',time.time()-10)
        spawned=[]
        real=providers.subprocess.Popen
        def popen(*args,**kwargs):
            spawned.append(real(*args,**kwargs))
            return spawned[-1]
        with self.fake_cli('import time\ntime.sleep(60)\n'), patch('providers.subprocess.Popen',side_effect=popen):
            self.assertEqual(providers.claude_cli_touch(Path(self.home.name),timeout=3),'timeout')
        self.assertIsNotNone(spawned[0].returncode)


if __name__=='__main__':unittest.main()
