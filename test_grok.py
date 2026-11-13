"""Grok login renewal, ACP transport lifecycle, and REST fallback regressions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from providers import fetch_grok, grok_rpc, grok_result, ProviderError


class GrokTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.path = self.home / '.grok/auth.json'
        self.path.parent.mkdir()
        self.environment = patch.dict(os.environ, {'GROK_HOME':str(self.path.parent)})
        self.environment.start()
        self.auth()
        start = time.time()-3600
        self.billing = {'config':{'creditUsagePercent':25,
            'currentPeriod':{'start':start,'end':start+604800}},'subscription_tier':'SuperGrok'}

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def auth(self, expiry=None, token='test-token', email='test@example.test'):
        self.path.write_text(json.dumps({'https://auth.x.ai::test':{
            'key':token,'expires_at':expiry if expiry is not None else time.time()+3600,
            'email':email,'user_id':email,'team_id':'personal'}}))

    def test_expired_token_is_renewed_by_cli_before_expiry_check(self):
        self.auth(expiry=time.time()-60, token='expired-test-token')
        def cli(home):
            self.assertEqual(home, self.home)
            self.auth(token='renewed-test-token')
            return self.billing
        with patch('providers.grok_rpc',side_effect=cli), patch('providers.get_json') as http:
            result=fetch_grok(self.home)
        http.assert_not_called()
        self.assertEqual(result['windows'][0]['remaining'],75)
        self.assertEqual(result['source'],'Grok CLI ACP')
        self.assertEqual(result['plan'],'SuperGrok')
        self.assertEqual(result['account'],'test@example.test')

    def test_missing_rpc_method_and_timeout_fall_back_to_fresh_token(self):
        for status in ('unavailable','error'):
            with self.subTest(status=status):
                def cli(_):
                    self.auth(token='new-token')
                    raise ProviderError(status,'RPC failed')
                with patch('providers.grok_rpc',side_effect=cli), patch('providers.get_json',return_value=self.billing) as http:
                    result=fetch_grok(self.home)
                self.assertEqual(result['status'],'ready')
                self.assertEqual(http.call_args.args[1]['Authorization'],'Bearer new-token')
                self.assertEqual(http.call_args.args[0],'https://cli-chat-proxy.grok.com/v1/billing?format=credits')

    def test_no_cli_installation_still_uses_valid_auth_file(self):
        with patch('providers.shutil.which',return_value=None), patch('providers.get_json',return_value=self.billing):
            self.assertEqual(fetch_grok(self.home)['status'],'ready')

    def test_expired_token_is_never_sent_to_http_after_failed_renewal(self):
        self.auth(expiry=time.time()-10)
        for status,expected in [('error','error'),('expired','expired')]:
            with patch('providers.grok_rpc',side_effect=ProviderError(status,'CLI failed')), patch('providers.get_json') as http:
                with self.assertRaises(ProviderError) as caught:fetch_grok(self.home)
            self.assertEqual(caught.exception.status,expected)
            http.assert_not_called()

    def test_account_switch_discards_rpc_and_queries_new_account(self):
        def cli(_):
            self.auth(token='other-token',email='other@example.test')
            return self.billing
        other={'config':{'creditUsagePercent':80}}
        with patch('providers.grok_rpc',side_effect=cli), patch('providers.get_json',return_value=other) as http:
            result=fetch_grok(self.home)
        self.assertEqual(result['account'],'other@example.test')
        self.assertEqual(result['windows'][0]['remaining'],20)
        self.assertEqual(http.call_args.args[1]['Authorization'],'Bearer other-token')

    def test_unknown_usage_does_not_become_full(self):
        with patch('providers.grok_rpc',return_value={'config':{}}), patch('providers.get_json',return_value={'config':{}}):
            result=fetch_grok(self.home)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['windows'],[])
        for value in ([],None,{'config':[]},{'config':{'onDemandUsed':10,'onDemandCap':5}}):
            self.assertEqual(grok_result(value)['windows'],[])

    def test_legacy_cli_shape_and_matching_period_bounds(self):
        start=time.time()-100
        result=grok_result({'monthlyLimit':{'val':200},'usage':{'totalUsed':{'val':50}},
            'billingCycle':{'billingPeriodStart':start,'billingPeriodEnd':start+30*86400}})
        self.assertEqual(result['windows'][0]['remaining'],75)
        self.assertEqual(result['windows'][0]['label'],'每月额度')
        result=grok_result({'config':{'creditUsagePercent':10,'currentPeriod':{'start':start-86400},
            'billingPeriodStart':start,'billingPeriodEnd':start+604800}})
        self.assertEqual(result['windows'][0]['minutes'],10080)

    def fake_cli(self, body):
        path=self.home/'fake-grok'
        path.write_text('#!/usr/bin/python3\n'+body)
        path.chmod(0o700)
        return path

    def test_real_pipes_skip_notifications_and_only_issue_read_only_rpc(self):
        path=self.fake_cli('''import json,sys,os
from pathlib import Path
assert sys.argv[1:]==['agent','stdio']
requests=[]
for line in sys.stdin:
 r=json.loads(line); requests.append(r)
 Path(os.environ['HOME'],'requests.json').write_text(json.dumps(requests))
 print(json.dumps({'method':'notification','params':{}}),flush=True)
 print(json.dumps({'id':999,'result':{}}),flush=True)
 result={} if r['method']=='initialize' else {'config':{'creditUsagePercent':25}}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
''')
        with patch('providers.shutil.which',return_value=str(path)), patch('providers.subprocess.Popen',wraps=subprocess.Popen) as spawn:
            result=grok_rpc(self.home)
        self.assertEqual(result['config']['creditUsagePercent'],25)
        requests=json.loads((self.home/'requests.json').read_text())
        self.assertEqual([r['method'] for r in requests],['initialize','_x.ai/billing'])
        self.assertEqual(requests[0]['params']['clientCapabilities'],{'fs':{'readTextFile':False,'writeTextFile':False},'terminal':False})
        self.assertEqual(requests[1]['params'],{})
        self.assertFalse(Path(spawn.call_args.kwargs['cwd']).exists())

    def test_rpc_errors_and_timeout_cleanup_child(self):
        for reply, status in [("{'error':{'code':-32601,'message':'different wording'}}",'unavailable'),
                              ("{'error':{'code':1,'message':'Authentication required; grok login'}}",'expired'),
                              ("{'result':[]}",'error')]:
            path=self.fake_cli(f'''import json,sys
for line in sys.stdin:
 r=json.loads(line)
 print(json.dumps({{'id':r['id'],**{reply}}}),flush=True)
''')
            with patch('providers.shutil.which',return_value=str(path)):
                with self.assertRaises(ProviderError) as caught:grok_rpc(self.home)
            self.assertEqual(caught.exception.status,status)
        path=self.fake_cli('import time,signal\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(60)\n')
        real_spawn=subprocess.Popen
        processes=[]
        def spawn(*args,**kwargs):
            p=real_spawn(*args,**kwargs);processes.append(p);return p
        start=time.monotonic()
        with patch('providers.shutil.which',return_value=str(path)),patch('providers.subprocess.Popen',side_effect=spawn):
            with self.assertRaises(ProviderError):grok_rpc(self.home,initialize_timeout=.15,billing_timeout=.15)
        self.assertLess(time.monotonic()-start,2)
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdin.closed)
        self.assertTrue(processes[0].stdout.closed)


if __name__=='__main__':unittest.main()
