from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from activity import ActivityMonitor


class DesktopActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now=int(time.time())
        self.monitor=ActivityMonitor(self.temp.name)
        self.monitor.started=self.now-1
        self.path=Path(self.temp.name)/'Library/Logs/Claude/main.log'
        self.path.parent.mkdir(parents=True)
        self.transcript=Path(self.temp.name)/'.claude/projects/session.jsonl'
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text(json.dumps({'type':'user','timestamp':self.now,
                                               'message':{'content':'test'}})+'\n')

    def line(self,message,at):
        return datetime.fromtimestamp(at).strftime('%Y-%m-%d %H:%M:%S')+' [info] '+message+'\n'

    def append(self,message,at):
        with self.path.open('a') as f:f.write(self.line(message,at))

    def scan(self,at):
        with patch('activity.time.time',return_value=at):
            self.monitor.scan()
            return self.monitor.snapshot()

    def request(self,key='r1',tool='computer:request_access',session='local_session',at=None):
        self.append(f'Emitted tool permission request {key} for {tool} in session {session}',at or self.now+1)

    def test_permission_inside_computer_batch_uses_actual_dialog_signal(self):
        self.scan(self.now)
        with self.transcript.open('a') as f:
            f.write(json.dumps({'type':'assistant','timestamp':self.now+1,'message':{'content':[
                {'type':'tool_use','id':'batch','name':'mcp__computer-use__computer_batch'}]}})+'\n')
        self.assertEqual(self.scan(self.now+2)['providers']['claude']['state'],'working')
        self.request(at=self.now+3)
        data=self.scan(self.now+5)
        self.assertEqual((data['providers']['claude']['state'],data['providers']['claude']['reason']),('waiting','permission'))
        self.assertEqual(len(data['events']),1)
        self.append('Received permission response for r1: always (tool: computer:request_access)',self.now+6)
        self.assertEqual(self.scan(self.now+7)['providers']['claude']['state'],'working')

    def test_request_response_in_same_scan_does_not_emit_alert(self):
        self.scan(self.now)
        self.request()
        self.append('LocalSessions.respondToToolPermission: requestId=r1, decision=deny, hasUpdatedInput=false',self.now+1)
        data=self.scan(self.now+3)
        self.assertEqual(data['providers']['claude']['state'],'working')
        self.assertEqual(data['events'],[])

    def test_multiple_dialogs_wait_until_all_answered(self):
        self.scan(self.now)
        self.request('r1')
        self.request('r2',session='local_other')
        self.scan(self.now+3)
        self.append('Received permission response for r1: once (tool: computer:request_access)',self.now+4)
        self.assertEqual(self.scan(self.now+5)['providers']['claude']['state'],'waiting')
        self.append('LocalSessions.interrupt: sessionId=local_other',self.now+6)
        self.assertEqual(self.scan(self.now+7)['providers']['claude']['state'],'working')

    def test_restart_preserves_pending_state_without_replaying_old_event(self):
        self.request(at=self.now-20)
        data=self.scan(self.now)
        self.assertEqual(data['providers']['claude']['state'],'waiting')
        self.assertEqual(data['events'],[])

    def test_desktop_waiting_expires_without_reappearing_after_restart(self):
        for tool in ('AskUserQuestion','computer:request_access'):
            with self.subTest(tool=tool):
                self.path.write_text('')
                self.monitor = ActivityMonitor(self.temp.name)
                self.request(tool=tool,at=self.now)
                self.assertEqual(self.scan(self.now+1799)['providers']['claude']['state'],'waiting')
                data = self.scan(self.now+1800)
                self.assertEqual(data['providers']['claude'],{'state':'unknown','at':None,'reason':None})
                self.assertEqual(data['events'],[])
                self.monitor = ActivityMonitor(self.temp.name)
                self.assertEqual(self.scan(self.now+1801)['providers']['claude']['state'],'unknown')
                self.request(key='new',tool=tool,at=self.now+1802)
                self.assertEqual(self.scan(self.now+1803)['providers']['claude']['state'],'waiting')

    def test_partial_line_rotation_and_old_resolved_request(self):
        self.scan(self.now)
        line=self.line('Emitted tool permission request r1 for Bash in session local_session',self.now+1)
        self.path.write_text(line[:30])
        self.assertEqual(self.scan(self.now+2)['providers']['claude']['state'],'working')
        with self.path.open('a') as f:f.write(line[30:])
        self.assertEqual(self.scan(self.now+3)['providers']['claude']['state'],'waiting')
        self.path.rename(self.path.with_name('main1.log'))
        self.append('Received permission response for r1: deny (tool: Bash)',self.now+4)
        self.assertEqual(self.scan(self.now+5)['providers']['claude']['state'],'working')
        self.path.with_name('main1.log').write_text(line)
        self.assertEqual(self.scan(self.now+6)['providers']['claude']['state'],'working')

    def test_same_question_in_transcript_and_desktop_log_emits_once(self):
        self.scan(self.now)
        with self.transcript.open('a') as f:
            f.write(json.dumps({'type':'assistant','timestamp':self.now+1.2,'message':{'content':[
                {'type':'tool_use','id':'q','name':'AskUserQuestion'}]}})+'\n')
        self.request(tool='AskUserQuestion')
        data=self.scan(self.now+3)
        self.assertEqual(data['providers']['claude']['reason'],'input')
        self.assertEqual(len(data['events']),1)

    def test_completed_session_clears_dialog(self):
        self.scan(self.now)
        self.request()
        self.scan(self.now+3)
        self.append('[Stop hook] Query completed for session local_session',self.now+4)
        self.assertEqual(self.scan(self.now+5)['providers']['claude']['state'],'working')

    def wrap_up(self,at=None,session='cli_session',path=None):
        with (path or self.transcript).open('a') as f:
            f.write(json.dumps({'type':'assistant','sessionId':session,'timestamp':at or self.now+1,
                'message':{'content':[{'type':'tool_use','id':'card','name':'mcp__ccd_turn__wrap_up',
                                      'input':{'ask':'Choose'}}]}})+'\n')

    def test_desktop_reply_or_cancel_clears_lagging_transcript_after_restart(self):
        for action,state in (('LocalSessions.sendMessage: sessionId=local_session, messageLength=4','working'),
                             ('LocalSessions.interrupt: sessionId=local_session','idle')):
            with self.subTest(action=action):
                self.wrap_up()
                self.append('Mapping internal session local_session to CLI session cli_session',self.now)
                self.append(action,self.now+4)
                self.monitor=ActivityMonitor(self.temp.name)
                data=self.scan(self.now+5)
                self.assertEqual(data['providers']['claude']['state'],state)
                self.assertEqual(data['events'],[])
                self.assertEqual(self.scan(self.now+6)['providers']['claude']['state'],state)

    def test_reply_does_not_clear_new_question_or_another_session(self):
        self.wrap_up()
        other=self.transcript.with_name('other.jsonl')
        self.wrap_up(session='cli_other',path=other)
        self.append('Mapping internal session local_session to CLI session cli_session',self.now)
        self.append('LocalSessions.sendMessage: sessionId=local_session, messageLength=4',self.now+4)
        self.scan(self.now+5)
        self.assertEqual(self.monitor.files[other]['session'].state,'waiting')
        self.assertEqual(self.monitor.files[self.transcript]['session'].state,'working')
        self.wrap_up(at=self.now+6)
        self.scan(self.now+8)
        self.assertEqual(self.monitor.files[self.transcript]['session'].state,'waiting')

    def test_turn_end_keeps_wrap_up_waiting(self):
        self.wrap_up()
        self.append('Mapping internal session local_session to CLI session cli_session',self.now)
        self.append('[Stop hook] Query completed for session local_session',self.now+4)
        self.assertEqual(self.scan(self.now+5)['providers']['claude']['state'],'waiting')

    def test_persistent_user_frame_resolves_wait_when_logs_have_rotated(self):
        self.wrap_up()
        root=Path(self.temp.name)/'Library/Application Support/Claude/claude-code-sessions/account'
        root.mkdir(parents=True)
        path=root/'local_session.json'
        data={'sessionId':'local_session','cliSessionId':'cli_session',
              'latestUserFrameAt':(self.now-10)*1000}
        path.write_text(json.dumps(data))
        self.assertEqual(self.scan(self.now+2)['providers']['claude']['state'],'waiting')
        data['latestUserFrameAt']=(self.now+4)*1000
        path.write_text(json.dumps(data))
        self.assertEqual(self.scan(self.now+5)['providers']['claude']['state'],'working')

    def test_transcript_reply_clears_matching_desktop_dialog_only(self):
        self.request(tool='AskUserQuestion')
        self.request(key='other',tool='AskUserQuestion',session='local_other')
        self.append('Mapping internal session local_session to CLI session cli_session',self.now)
        self.scan(self.now+2)
        with self.transcript.open('a') as f:
            f.write(json.dumps({'type':'user','sessionId':'cli_session','timestamp':self.now+4,
                                'message':{'content':'Reply'}})+'\n')
        self.scan(self.now+5)
        self.assertEqual(set(self.monitor.desktop.pending),{'other'})

    def test_skip_resolves_both_copies_of_question_from_either_source(self):
        for source in ('desktop','transcript'):
            with self.subTest(source=source):
                self.monitor=ActivityMonitor(self.temp.name)
                self.path.write_text('')
                self.transcript.write_text(json.dumps({'type':'assistant','sessionId':'cli_session',
                    'timestamp':self.now+1.2,'message':{'content':[
                        {'type':'tool_use','id':'q','name':'AskUserQuestion'}]}})+'\n')
                self.append('Mapping internal session local_session to CLI session cli_session',self.now)
                self.request(tool='AskUserQuestion')
                self.assertEqual(self.scan(self.now+3)['providers']['claude']['state'],'waiting')
                if source == 'desktop':
                    self.append('LocalSessions.respondToToolPermission: requestId=r1, decision=deny, hasUpdatedInput=false',self.now+4)
                else:
                    with self.transcript.open('a') as f:
                        f.write(json.dumps({'type':'user','isMeta':True,'sessionId':'cli_session',
                            'timestamp':self.now+4,'message':{'content':[
                                {'type':'tool_result','tool_use_id':'q','is_error':True}]}})+'\n')
                self.assertEqual(self.scan(self.now+5)['providers']['claude']['state'],'working')
                self.assertEqual(self.monitor.desktop.pending,{})
                self.assertEqual(self.scan(self.now+6)['providers']['claude']['state'],'working')


if __name__=='__main__':unittest.main()
