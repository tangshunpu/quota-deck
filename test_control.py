import functools
import http.client
import json
from datetime import datetime
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from control import DisplayControl, next_boundary
from activity import ActivityMonitor
from remote_activity import RemoteActivity
from server import Handler
from token_history import TokenHistory
from reset_news import ResetNews


class Stub:
    def snapshot(self):
        return {}
    def refresh(self):
        return True


class RemoteHandler(Handler):
    def is_local(self):
        return not self.server.force_remote


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.control = DisplayControl(self.home.name)
        stub = Stub()
        handler = functools.partial(RemoteHandler, store=stub, monitor=stub, tokens=stub, remote=stub)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.control = self.control
        self.server.activity = ActivityMonitor(self.home.name)
        self.server.remote_activity = RemoteActivity(self.home.name)
        self.server.token_history = TokenHistory(self.home.name)
        self.server.reset_news = ResetNews()
        self.server.force_remote = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.home.cleanup()

    def request(self, path, data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        defaults = {'Content-Type':'application/json','X-Quota-Deck':'1','Cookie':self.cookie}
        defaults.update(headers or {})
        connection.request('GET' if data is None else 'POST', path,
                           body=None if data is None else json.dumps(data), headers=defaults)
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        connection.close()
        return result

    def login(self):
        status, headers, _ = self.request('/api/login', {'key':self.control.key})
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])

    def test_lan_requires_key_and_rejects_wrong_key(self):
        for path in ('/api/control','/api/status','/api/system','/api/weather','/api/activity',
                     '/api/codex/reset-news',
                     '/api/tokens/history','/api/tokens/heatmap','/api/tokens/monthly'):
            self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request('/api/control', {'action':'reload'})[0], 401)
        self.assertEqual(self.request('/api/login', {'key':'wrong'})[0], 401)
        self.login()
        status, _, body = self.request('/api/control')
        self.assertEqual(status, 200)
        self.assertNotIn(self.control.key.encode(), body)

    def test_reset_news_authenticated_response(self):
        self.login()
        status, _, body = self.request('/api/codex/reset-news')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['mode'], 'unavailable')

    def test_token_statistics_api_ranges_filters_heatmap_and_query_validation(self):
        self.login()
        history = self.server.token_history
        day = history.today().isoformat()
        history.ingest('local','本机',{'files':[{'id':'a'*64,'provider':'codex','mtimeNs':1,
            'size':1,'through':day,'days':{day:{'input':10,'cached':20,'output':5,'total':35}}}]})
        for path, days in (('/api/tokens/history?days=7',7),('/api/tokens/history?days=30',30),
                           ('/api/tokens/heatmap',365)):
            status, _, body = self.request(path)
            self.assertEqual(status,200)
            result = json.loads(body)
            self.assertEqual(result['days'],days)
            self.assertEqual(result['summary']['total'],35)
            cells = result.get('cells',result.get('daily'))
            self.assertEqual(len(cells),days)
            self.assertEqual(sum(c['total'] for c in cells),35)
        status,_,body = self.request('/api/tokens/history?days=7&provider=claude&host=local')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['summary']['total'],0)
        status,_,body = self.request('/api/tokens/monthly?months=12&provider=codex&host=local')
        self.assertEqual(status,200)
        monthly=json.loads(body)
        self.assertEqual(len(monthly['monthly']),12)
        self.assertEqual(monthly['summary']['total'],35)
        self.assertEqual(monthly['monthly'][-1]['month'],day[:7])
        self.assertTrue(monthly['monthly'][-1]['isPartial'])
        for query in ('months=0','months=121','months=no','months=1&months=2','days=7','end=2026-99'):
            self.assertEqual(self.request('/api/tokens/monthly?'+query)[0],400,query)
        for query in ('days=367','days=-1','days=no','days=7&days=30','provider=unknown',
                      'host=unknown','end=2026-99-99','unknown=value','&'.join('x'+str(i)+'=1' for i in range(9))):
            self.assertEqual(self.request('/api/tokens/history?'+query)[0],400,query)

    def test_tailscale_source_can_reach_lan_and_tailnet_hosts_but_needs_login(self):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(server_port=18768, control=self.control)
        for destination in ('192.168.1.20', '100.64.0.20'):
            handler.connection = SimpleNamespace(getsockname=lambda: (destination, 18768))
            for peer in ('100.64.0.1', '100.64.0.20', '100.127.255.254'):
                handler.client_address = (peer, 45678)
                handler.headers = {'Host':f'{destination}:18768'}
                self.assertTrue(handler.allowed_host())
                self.assertFalse(handler.is_local())
                self.assertFalse(handler.authenticated())
                handler.headers['Cookie'] = 'quota_access=' + self.control.key
                self.assertTrue(handler.authenticated())
                handler.headers['Host'] = 'evil.example:18768'
                self.assertFalse(handler.allowed_host())
            for peer in ('100.63.255.255', '100.128.0.0', '8.8.8.8'):
                handler.client_address = (peer, 45678)
                handler.headers = {'Host':f'{destination}:18768'}
                self.assertFalse(handler.allowed_host())

    def test_activity_preview_requires_login_and_does_not_change_task(self):
        command = {'provider':'codex','kind':'completed'}
        self.assertEqual(self.request('/api/activity/preview',command)[0],401)
        self.login()
        before = self.server.activity.snapshot()['providers']
        self.assertEqual(self.request('/api/activity/preview',command,{'X-Quota-Deck':''})[0],403)
        self.assertEqual(self.request('/api/activity/preview',command)[0],200)
        status, _, body = self.request('/api/activity')
        result = json.loads(body)
        self.assertEqual(status,200)
        self.assertEqual(result['providers'],before)
        self.assertEqual(result['events'][-1]['kind'],'completed')
        self.assertTrue(result['events'][-1]['preview'])
        for bad in ({'provider':'unknown','kind':'completed'}, {'provider':'codex','kind':'unknown'}):
            self.assertEqual(self.request('/api/activity/preview',bad)[0],400)

    def test_origin_host_and_custom_header_are_enforced(self):
        self.login()
        command = {'action':'reload'}
        self.assertEqual(self.request('/api/control', command, {'Origin':'http://evil.example'})[0],403)
        self.assertEqual(self.request('/api/control', command, {'X-Quota-Deck':''})[0],403)
        self.assertEqual(self.request('/api/control', headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/display/heartbeat', {'revision':0,'theme':'dark'})[0],403)

    def test_commands_persist_and_ack_requires_real_current_screen_revision(self):
        self.login()
        status, _, body = self.request('/api/control', {'action':'details','provider':'codex','open':True})
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(state['details']['codex'])
        self.assertFalse(state['screen']['online'])
        self.assertTrue(DisplayControl(self.home.name).snapshot()['details']['codex'])
        self.server.force_remote = False
        report = {'instance':state['instance'],'revision':state['revision'],'theme':'dark'}
        self.assertEqual(self.request('/api/display/heartbeat', report)[0],200)
        self.assertTrue(self.control.snapshot()['screen']['online'])
        self.control.command({'action':'overview'})
        self.assertNotEqual(self.control.snapshot()['revision'], self.control.snapshot()['screen']['appliedRevision'])
        self.control.heartbeat(report)
        self.assertNotEqual(self.control.snapshot()['revision'], self.control.snapshot()['screen']['appliedRevision'])

    def test_theme_override_auto_and_reload(self):
        self.login()
        state = self.control.command({'action':'theme','mode':'light'})
        self.assertEqual(state['theme']['mode'], 'light')
        self.assertGreater(state['theme']['override']['until'], 0)
        self.assertEqual(self.control.command({'action':'theme','mode':'auto'})['theme']['mode'], 'auto')
        self.assertEqual(self.control.command({'action':'reload'})['reloadId'],1)
        self.assertEqual(self.control.command({'action':'reload'})['reloadId'],2)

    def test_language_persists_and_screen_reports_applied_language(self):
        self.login()
        status, _, body = self.request('/api/control', {'action':'language','language':'en'})
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(state['language'], 'en')
        self.assertEqual(DisplayControl(self.home.name).snapshot()['language'], 'en')
        self.assertIsNone(state['screen']['language'])
        report = {'instance':state['instance'],'revision':state['revision'],
                  'theme':'dark','language':'en'}
        self.control.heartbeat(report)
        self.assertEqual(self.control.snapshot()['screen']['language'], 'en')
        for bad in ('fr', None, [], {'language':'zh'}):
            self.assertEqual(self.request('/api/control', {'action':'language','language':bad})[0], 400)
        self.assertEqual(self.control.snapshot()['revision'], state['revision'])
        self.control.command({'action':'language','language':'zh'})
        self.control.heartbeat(report)
        self.assertNotEqual(self.control.snapshot()['screen']['appliedRevision'], self.control.snapshot()['revision'])
        self.assertEqual(DisplayControl(self.home.name).snapshot()['language'], 'zh')

    def test_screen_reports_rendered_alert_without_accepting_stale_instance(self):
        state=self.control.snapshot()
        report={'instance':state['instance'],'revision':state['revision'],'theme':'dark',
                'activity':{'lastPollAt':123,'alerts':[{'id':'mac-event','provider':'claude',
                    'kind':'completed','host':'MacBook Pro','startedAt':122,'preview':False}]}}
        self.control.heartbeat(report)
        activity=self.control.snapshot()['screen']['activity']
        self.assertEqual(activity['lastPollAt'],123)
        self.assertEqual(activity['alerts'][0]['host'],'MacBook Pro')
        self.assertEqual(activity['alerts'][0]['duration'],15)
        self.control.heartbeat({**report,'instance':'old','activity':{'alerts':[]}})
        self.assertEqual(self.control.snapshot()['screen']['activity'],activity)
        report['activity']['alerts'][0]['kind']='waiting'
        self.control.heartbeat(report)
        self.assertIsNone(self.control.snapshot()['screen']['activity']['alerts'][0]['duration'])
        report['activity']['alerts'][0]['preview']=True
        self.control.heartbeat(report)
        self.assertEqual(self.control.snapshot()['screen']['activity']['alerts'][0]['duration'],15)

    def test_bad_payloads_do_not_mutate_state(self):
        self.login()
        before = self.control.snapshot()['revision']
        for bad in ([], None, {'action':'shell','command':'anything'},
                    {'action':'theme','mode':'invalid'}, {'action':'details','provider':'codex','open':'true'}):
            if bad is None:
                with self.assertRaises(ValueError):
                    self.control.command(bad)
            else:
                self.assertEqual(self.request('/api/control', bad)[0],400)
        self.assertEqual(self.control.snapshot()['revision'],before)

    def test_persistence_failure_does_not_ack_unsaved_command(self):
        with patch('pathlib.Path.write_text', side_effect=OSError('disk full')):
            self.server.force_remote = False
            self.assertEqual(self.request('/api/control',{'action':'reload'})[0],500)
        self.assertEqual(self.control.snapshot()['revision'],0)

    def test_boundary_is_screen_local_time(self):
        for hour, target_hour, next_day in [(0,7,False),(6,7,False),(7,18,False),(17,18,False),(18,7,True),(23,7,True)]:
            now = datetime(2026,10,22,hour,0,0)
            with patch('control.datetime') as clock:
                clock.now.return_value = now
                expected=datetime(2026,10,23 if next_day else 22,target_hour)
                self.assertEqual(next_boundary(),expected.timestamp()*1000)


if __name__ == '__main__':
    unittest.main()
