import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from activity import ActivityMonitor, Session
from remote_activity import RemoteActivity, merge_activity, stream_script


class RemoteActivityTests(unittest.TestCase):
    def setUp(self):
        self.remote = RemoteActivity()
        self.host = {'ssh':'user@mac','name':'MacBook Pro'}
        self.now = time.time()

    def packet(self, state='waiting', at=None):
        at = self.now if at is None else at
        return {'sentAt':at, 'providers':{'codex':{'state':state,'at':at-2,'reason':'input'}},
                'events':[{'id':'same-id','provider':'codex','kind':'waiting','at':at-2,'reason':'input'}]}

    def test_clock_skew_normalized_and_only_safe_fields_forwarded(self):
        packet = self.packet(at=self.now+3600)
        packet['events'][0]['prompt'] = 'private'
        packet['providers']['codex']['path'] = '/private/session'
        with patch('remote_activity.time.time',return_value=self.now):
            self.remote.accept(self.host,packet)
            data = self.remote.snapshot()
        self.assertAlmostEqual(data['events'][0]['at'],self.now-2)
        self.assertEqual(data['hosts'][0]['status'],'ok')
        self.assertNotIn('private',json.dumps(data))
        self.assertEqual(data['events'][0]['host'],'MacBook Pro')

    def test_sleep_drops_active_state_but_keeps_recent_real_notification(self):
        with patch('remote_activity.time.time',return_value=self.now):
            self.remote.accept(self.host,self.packet())
        with patch('remote_activity.time.time',return_value=self.now+11):
            data = self.remote.snapshot()
            self.assertEqual(data['hosts'][0]['status'],'offline')
            self.assertEqual(data['providers'],{})
            self.assertEqual(len(data['events']),1)
        with patch('remote_activity.time.time',return_value=self.now+31):
            self.assertEqual(self.remote.snapshot()['events'],[])

    def test_disconnect_and_reconnect_do_not_replay(self):
        self.remote.accept(self.host,self.packet())
        self.remote.disconnected(self.host['ssh'])
        self.assertEqual(self.remote.snapshot()['providers'],{})
        packet = self.packet('working')
        packet['events'] = []  # Fresh remote monitor baselines its history.
        self.remote.accept(self.host,packet)
        self.assertEqual(self.remote.snapshot()['events'],[])
        self.assertEqual(self.remote.snapshot()['providers']['codex']['state'],'working')

    def test_host_ids_are_distinct_and_waiting_has_priority(self):
        self.remote.accept(self.host,self.packet())
        second = {'ssh':'user@mac2','name':'Other Mac'}
        self.remote.accept(second,self.packet('working'))
        local = ActivityMonitor().snapshot()
        local['providers']['codex'] = {'state':'working','at':self.now,'reason':None}
        result = merge_activity(local,self.remote.snapshot())
        self.assertEqual(result['providers']['codex']['state'],'waiting')
        self.assertEqual(len({e['id'] for e in result['events']}),2)
        self.assertEqual(local['providers']['codex']['state'],'working')

    def test_waiting_expires_with_clock_skew_and_reveals_other_host(self):
        packet = self.packet(at=self.now+3600)
        packet['providers']['codex']['at'] = packet['sentAt']-1799
        packet['events'] = []
        local = ActivityMonitor().snapshot()
        local['providers']['codex'] = {'state':'working','at':self.now-1,'reason':None}
        with patch('remote_activity.time.time',return_value=self.now):
            self.remote.accept(self.host,packet)
            self.assertEqual(merge_activity(local,self.remote.snapshot())['providers']['codex']['state'],'waiting')
        with patch('remote_activity.time.time',return_value=self.now+1):
            data = self.remote.snapshot()
            self.assertEqual(data['hosts'][0]['status'],'ok')
            self.assertEqual(data['providers'],{})
            self.assertEqual(data['events'],[])
            self.assertEqual(merge_activity(local,data)['providers']['codex']['state'],'working')
            self.remote.accept({'ssh':'user@mac2','name':'Other Mac'},self.packet('working',at=self.now+1))
            self.assertEqual(self.remote.snapshot()['providers']['codex']['host'],'Other Mac')

    def test_invalid_and_expired_events_ignored(self):
        packet = self.packet()
        packet['events'].extend([{'provider':'unknown'},None,{'id':'expired','provider':'codex',
                                'kind':'completed','at':self.now-60}])
        self.remote.accept(self.host,packet)
        self.assertEqual(len(self.remote.snapshot()['events']),1)
        with self.assertRaises(ValueError):
            self.remote.accept(self.host,{'sentAt':float('nan')})

    def test_mac_claude_stop_hook_summary_completes_final_reply(self):
        session = Session('claude')
        session.feed({'type':'assistant','timestamp':self.now,'message':{'stop_reason':'end_turn'}},'1')
        session.feed({'type':'system','timestamp':self.now+1,'subtype':'stop_hook_summary',
                      'preventedContinuation':False,'hookErrors':[], 'hookAdditionalContext':[],
                      'hasOutput':False, 'stopReason':''},'2')
        self.assertEqual(session.state,'completed')

    def test_mac_claude_blocked_stop_is_not_complete(self):
        for field,value in [('preventedContinuation',True),('hookErrors',['failed']),
                            ('hookAdditionalContext',['continue']),('hasOutput',True)]:
            session = Session('claude')
            session.feed({'type':'assistant','timestamp':self.now,'message':{'stop_reason':'end_turn'}},'1')
            row = {'type':'system','timestamp':self.now+1,'subtype':'stop_hook_summary',
                   'preventedContinuation':False,field:value}
            session.feed(row,'2')
            self.assertNotEqual(session.state,'completed')

    def test_stream_reads_fragmented_packets_and_stops_subprocess(self):
        packet = json.dumps(self.packet()).encode()+b'\n'
        script = 'import os,time\nos.write(1,'+repr(packet[:19])+')\ntime.sleep(.1)\nos.write(1,'+repr(packet[19:])+')\ntime.sleep(60)'
        process = subprocess.Popen([sys.executable,'-u','-c',script],stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        stop = threading.Event()
        with patch('remote_activity.subprocess.Popen',return_value=process), patch('remote_activity.stream_script',return_value=''):
            thread = threading.Thread(target=self.remote.stream,args=(self.host,stop))
            thread.start()
            try:
                deadline = time.monotonic()+3
                while not self.remote.snapshot()['events'] and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertEqual(len(self.remote.snapshot()['events']),1)
            finally:
                stop.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(process.poll())

    def test_streamed_parser_is_standalone_and_suppresses_history(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home)/'.codex/sessions/history.jsonl'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'timestamp':self.now-10,'payload':{'type':'task_complete'}})+'\n')
            claude = Path(home)/'.claude/projects/waiting.jsonl'
            claude.parent.mkdir(parents=True)
            claude.write_text(json.dumps({'type':'assistant','timestamp':self.now-1801,
                'message':{'content':[{'type':'tool_use','id':'q','name':'AskUserQuestion'}]}})+'\n')
            script = stream_script().replace('while True:', 'for _ in range(1):').replace('time.sleep(2)','pass')
            result = subprocess.run([sys.executable,'-'],input=script,text=True,capture_output=True,
                                    env={**os.environ,'HOME':home},cwd=home,timeout=5,check=True)
            data = json.loads(result.stdout)
            self.assertEqual(data['providers']['codex']['state'],'completed')
            self.assertEqual(data['providers']['claude']['state'],'unknown')
            self.assertEqual(data['events'],[])


if __name__ == '__main__':
    unittest.main()
