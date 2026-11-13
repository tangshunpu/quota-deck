from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from activity import ActivityMonitor, Session


def stamp(at):
    return datetime.fromtimestamp(at,timezone.utc).isoformat()


def codex(kind, at, **extra):
    return {'timestamp':stamp(at),'type':'event_msg','payload':{'type':kind,**extra}}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.home=tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.now=time.time()
        self.monitor=ActivityMonitor(self.home.name)
        self.monitor.started=self.now-1
        self.path=Path(self.home.name)/'.codex/sessions/current.jsonl'
        self.path.parent.mkdir(parents=True)

    def append(self,*rows):
        with self.path.open('a') as f:
            for row in rows:f.write(json.dumps(row)+'\n')

    def scan(self,at=None):
        with patch('activity.time.time',return_value=at or self.now):
            self.monitor.scan()
            return self.monitor.snapshot()

    def test_history_does_not_flash_and_new_completion_fires_once(self):
        self.append(codex('task_complete',self.now-2))
        self.assertEqual(self.scan()['events'],[])
        self.append(codex('task_started',self.now),codex('task_complete',self.now+.1))
        data=self.scan(self.now+1)
        self.assertEqual(len(data['events']),1)
        self.assertEqual(data['events'][0]['kind'],'completed')
        self.assertEqual(data['providers']['codex']['state'],'completed')
        self.assertEqual(len(self.scan(self.now+2)['events']),1)

    def test_missing_growth_and_abort_are_not_completion(self):
        self.append(codex('task_started',self.now))
        self.scan()
        self.assertEqual(self.scan(self.now+180)['events'],[])
        self.append(codex('turn_aborted',self.now+181))
        data=self.scan(self.now+182)
        self.assertEqual(data['events'],[])
        self.assertEqual(data['providers']['codex']['state'],'idle')

    def test_partial_json_line_is_retried_without_losing_event(self):
        self.append(codex('task_started',self.now));self.scan()
        line=json.dumps(codex('task_complete',self.now+1))
        with self.path.open('a') as f:f.write(line[:20])
        self.assertEqual(self.scan(self.now+1)['events'],[])
        with self.path.open('a') as f:f.write(line[20:]+'\n')
        self.assertEqual(len(self.scan(self.now+2)['events']),1)

    def test_codex_async_question_is_not_resolved_by_immediate_tool_ack(self):
        s=Session('codex')
        s.feed(codex('function_call',self.now,name='request_user_input_async',call_id='q'),'1')
        s.feed(codex('function_call_output',self.now+.1,call_id='q'),'2')
        s.feed(codex('task_complete',self.now+.2),'3')
        self.assertEqual(s.state,'waiting')
        s.feed(codex('message',self.now+1,role='user'),'4')
        self.assertEqual(s.state,'working')

    def test_sync_question_resumes_on_answer(self):
        s=Session('codex')
        s.feed(codex('function_call',self.now,name='request_user_input',call_id='q'),'1')
        self.assertEqual(s.state,'waiting')
        s.feed(codex('function_call_output',self.now+2,call_id='q'),'2')
        self.assertEqual(s.state,'working')

    def test_claude_completion_requires_final_response_and_turn_end(self):
        s=Session('claude')
        s.feed({'type':'assistant','timestamp':stamp(self.now),'message':{'stop_reason':'tool_use','content':[]}},'1')
        s.feed({'type':'system','subtype':'turn_duration','timestamp':stamp(self.now+1)},'2')
        self.assertNotEqual(s.state,'completed')
        s.feed({'type':'assistant','timestamp':stamp(self.now+2),'message':{'stop_reason':'end_turn','content':[]}},'3')
        self.assertNotEqual(s.state,'completed')
        s.feed({'type':'system','subtype':'turn_duration','timestamp':stamp(self.now+3)},'4')
        self.assertEqual(s.state,'completed')

    def test_claude_question_resumes_on_matching_tool_result(self):
        s=Session('claude')
        s.feed({'type':'assistant','timestamp':stamp(self.now),'message':{'content':[{'type':'tool_use','name':'AskUserQuestion','id':'q'}]}},'1')
        self.assertEqual(s.state,'waiting')
        s.feed({'type':'user','timestamp':stamp(self.now+1),'message':{'content':[{'type':'tool_result','tool_use_id':'q'}]}},'2')
        self.assertEqual(s.state,'working')

    def test_claude_skip_meta_result_and_direct_reply_clear_question(self):
        for content,meta in (([{'type':'tool_result','tool_use_id':'q','is_error':True}],True),
                ([{'type':'tool_result','tool_use_id':'other'}, {'type':'text','text':'My answer'}],False)):
            with self.subTest(content=content):
                s=Session('claude')
                s.feed({'type':'assistant','timestamp':self.now,'message':{'content':[
                    {'type':'tool_use','name':'AskUserQuestion','id':'q'}]}},'1')
                s.feed({'type':'user','isMeta':meta,'timestamp':self.now+1,
                        'message':{'content':content}},'2')
                self.assertEqual((s.state,s.reason,s.waiting),('working',None,{}))

    def test_claude_interruption_is_idle_and_does_not_complete(self):
        for marker in ('[Request interrupted by user]','[Request interrupted by user for tool use]'):
            s=Session('claude')
            s.feed({'type':'assistant','timestamp':self.now,'message':{'content':[
                {'type':'tool_use','name':'AskUserQuestion','id':'q'}]}},'1')
            s.feed({'type':'user','isMeta':True,'timestamp':self.now+1,
                    'message':{'content':[{'type':'text','text':marker}]}},'2')
            s.feed({'type':'system','subtype':'turn_duration','timestamp':self.now+2},'3')
            self.assertEqual((s.state,s.waiting),('idle',{}))

    def test_grok_auto_approval_does_not_alert_and_cancellation_is_not_success(self):
        self.path=Path(self.home.name)/'.grok/sessions/current/events.jsonl'
        self.path.parent.mkdir(parents=True)
        self.append({'type':'turn_started','ts':stamp(self.now),'session_relationship':'primary'})
        self.scan()
        self.append({'type':'permission_requested','ts':stamp(self.now+.1)},
                    {'type':'permission_resolved','ts':stamp(self.now+.2)})
        self.assertEqual(self.scan(self.now+1)['events'],[])
        self.append({'type':'turn_ended','ts':stamp(self.now+2),'outcome':'cancelled'})
        self.assertEqual(self.scan(self.now+3)['events'],[])

    def test_waiting_notice_is_delayed_and_input_has_priority_over_other_running_sessions(self):
        self.append(codex('task_started',self.now));self.scan()
        self.append(codex('function_call',self.now+.1,name='request_user_input',call_id='q'))
        self.assertEqual(self.scan(self.now+.2)['events'],[])
        result=self.scan(self.now+2)
        self.assertEqual(result['events'][0]['kind'],'waiting')
        other=self.path.with_name('other.jsonl')
        other.write_text(json.dumps(codex('task_started',self.now+2))+'\n')
        self.monitor.discovered=0
        self.assertEqual(self.scan(self.now+3)['providers']['codex']['state'],'waiting')

    def test_waiting_expires_after_thirty_minutes_despite_recent_log_activity(self):
        at = int(self.now)
        cases = [
            ('codex', '.codex/sessions/current.jsonl',
             codex('function_call',at,name=name,call_id='q'),
             codex('message',at+1802,role='user'))
            for name in ('request_user_input','request_user_input_async')]
        cases += [
            ('claude', '.claude/projects/current.jsonl',
             {'type':'assistant','timestamp':at,'message':{'content':[
                 {'type':'tool_use','name':name,'id':'q','input':{'ask':'Choose'}}]}},
             {'type':'user','timestamp':at+1802,'message':{'content':'Reply'}})
            for name in ('AskUserQuestion','mcp__computer-use__request_access','mcp__ccd_turn__wrap_up')]
        cases += [
            ('grok', '.grok/sessions/current/events.jsonl',
             {'type':kind,'timestamp':at,'tool_name':'ask_user_question'},
             {'type':'turn_started','timestamp':at+1802})
            for kind in ('tool_started','permission_requested')]
        for provider, filename, request, reply in cases:
            with self.subTest(provider=provider,request=request):
                self.path = Path(self.home.name)/filename
                self.path.parent.mkdir(parents=True,exist_ok=True)
                self.path.write_text('')
                self.monitor = ActivityMonitor(self.home.name)
                self.append(request)
                self.assertEqual(self.scan(at+1)['providers'][provider]['state'],'waiting')
                self.assertEqual(self.scan(at+1799)['providers'][provider]['state'],'waiting')
                self.append({'type':'progress','timestamp':at+1799.5})
                data = self.scan(at+1800)
                self.assertEqual(data['providers'][provider],{'state':'unknown','at':None,'reason':None})
                self.assertEqual(data['events'],[])
                self.monitor = ActivityMonitor(self.home.name)
                self.assertEqual(self.scan(at+1801)['providers'][provider]['state'],'unknown')
                self.append(reply)
                self.assertEqual(self.scan(at+1802)['providers'][provider]['state'],'working')
                self.append({**request,'timestamp':at+1803})
                self.assertEqual(self.scan(at+1804)['providers'][provider]['state'],'waiting')

    def test_expired_waiting_no_longer_masks_another_working_session(self):
        at = int(self.now)
        self.append(codex('function_call',at,name='request_user_input',call_id='q'))
        self.scan(at+1)
        other = self.path.with_name('other.jsonl')
        other.write_text(json.dumps(codex('task_started',at+1798))+'\n')
        self.monitor.discovered = 0
        self.assertEqual(self.scan(at+1799)['providers']['codex']['state'],'waiting')
        self.assertEqual(self.scan(at+1800)['providers']['codex']['state'],'working')

    def test_preview_never_changes_actual_state_and_rejects_unknown_provider(self):
        initial=self.monitor.snapshot()['providers']
        event=self.monitor.preview('codex','waiting')
        self.assertTrue(event['preview'])
        self.assertEqual(self.monitor.snapshot()['providers'],initial)
        with self.assertRaises(ValueError):self.monitor.preview('unknown','completed')
        with self.assertRaises(ValueError):self.monitor.preview('codex','shell')
        reset=self.monitor.preview('grok','reset')
        self.assertEqual((reset['kind'],reset['preview']),('reset',True))
        self.assertEqual(self.monitor.snapshot()['providers'],initial)

    def test_claude_wrap_up_question_survives_tool_ack_and_turn_completion(self):
        for arguments in ({'ask':'Choose an option','blocked':True},{'ask':'Choose an option'}, {'blocked':True}):
            s=Session('claude')
            s.feed({'type':'assistant','timestamp':self.now,'message':{'content':[
                {'type':'tool_use','id':'card','name':'mcp__ccd_turn__wrap_up','input':arguments}]}},'1')
            self.assertEqual((s.state,s.reason),('waiting','input'))
            s.feed({'type':'user','timestamp':self.now+1,'message':{'content':[
                {'type':'tool_result','tool_use_id':'card'}]}},'2')
            s.feed({'type':'assistant','timestamp':self.now+2,'message':{'stop_reason':'end_turn','content':[]}},'3')
            s.feed({'type':'system','timestamp':self.now+3,'subtype':'stop_hook_summary',
                    'preventedContinuation':False,'hasOutput':False},'4')
            self.assertEqual((s.state,s.reason),('waiting','input'))
            s.feed({'type':'user','isMeta':True,'timestamp':self.now+4,'message':{'content':'context'}},'5')
            self.assertEqual(s.state,'waiting')
            s.feed({'type':'user','timestamp':self.now+5,'message':{'content':'My answer'}},'6')
            self.assertEqual((s.state,s.reason),('working',None))

    def test_claude_wrap_up_without_ask_or_failed_publication_is_not_waiting(self):
        s=Session('claude')
        def card(arguments,token):
            s.feed({'type':'assistant','timestamp':self.now,'message':{'content':[
                {'type':'tool_use','id':'card','name':'mcp__ccd_turn__wrap_up','input':arguments}]}},token)
        for arguments in ({'recap':'done'},{'ask':'  ','actions':[{'label':'Optional follow-up'}],'blocked':False}):
            card(arguments,'1')
            self.assertNotEqual(s.state,'waiting')
        card({'ask':'Choose'},'2')
        s.feed({'type':'user','timestamp':self.now+1,'message':{'content':[
            {'type':'tool_result','tool_use_id':'card','is_error':True}]}},'3')
        self.assertEqual(s.state,'working')
        card({'ask':'Choose'},'4')
        card({'ask':'','blocked':False},'5')
        self.assertEqual(s.state,'working')

    def test_claude_pending_wrap_up_restores_after_restart(self):
        path=Path(self.home.name)/'.claude/projects/current.jsonl'
        path.parent.mkdir(parents=True)
        rows=[{'type':'assistant','timestamp':self.now-12,'message':{'content':[
            {'type':'tool_use','id':'card','name':'mcp__ccd_turn__wrap_up','input':{'ask':'Choose','blocked':True}}]}},
            {'type':'user','timestamp':self.now-11,'message':{'content':[{'type':'tool_result','tool_use_id':'card'}]}},
            {'type':'assistant','timestamp':self.now-10,'message':{'stop_reason':'end_turn','content':[]}},
            {'type':'system','timestamp':self.now-9,'subtype':'turn_duration'}]
        path.write_text('\n'.join(map(json.dumps,rows))+'\n')
        data=self.scan()
        self.assertEqual((data['providers']['claude']['state'],data['providers']['claude']['reason']),('waiting','input'))
        self.assertEqual(data['events'],[])

    def test_claude_computer_access_waits_until_matching_approval_or_rejection(self):
        for denied in (False,True):
            s=Session('claude')
            s.feed({'type':'assistant','timestamp':stamp(self.now),'message':{'stop_reason':'tool_use',
                'content':[{'type':'tool_use','id':'access','name':'mcp__computer-use__request_access'}]}},'1')
            self.assertEqual((s.state,s.reason),('waiting','permission'))
            s.feed({'type':'user','timestamp':stamp(self.now+1),'message':{
                'content':[{'type':'tool_result','tool_use_id':'other'}]}},'2')
            self.assertEqual((s.state,s.reason),('waiting','permission'))
            s.feed({'type':'user','timestamp':stamp(self.now+2),'message':{
                'content':[{'type':'tool_result','tool_use_id':'access','is_error':denied}]}},'3')
            self.assertEqual((s.state,s.reason),('working',None))

    def test_claude_question_and_permission_keep_waiting_until_both_resolved(self):
        s=Session('claude')
        s.feed({'type':'assistant','timestamp':stamp(self.now),'message':{'content':[
            {'type':'tool_use','id':'access','name':'mcp__computer-use__request_access'},
            {'type':'tool_use','id':'question','name':'AskUserQuestion'}]}},'1')
        self.assertEqual((s.state,s.reason),('waiting','input'))
        s.feed({'type':'user','timestamp':stamp(self.now+1),'message':{
            'content':[{'type':'tool_result','tool_use_id':'question'}]}},'2')
        self.assertEqual((s.state,s.reason),('waiting','permission'))

    def test_claude_pending_access_survives_monitor_restart_and_quick_results_do_not_alert(self):
        path=Path(self.home.name)/'.claude/projects/current.jsonl'
        path.parent.mkdir(parents=True)
        request={'type':'assistant','timestamp':stamp(self.now-10),'message':{'content':[
            {'type':'tool_use','id':'access','name':'mcp__computer-use__request_access'}]}}
        result={'type':'user','timestamp':stamp(self.now+1),'message':{'content':[
            {'type':'tool_result','tool_use_id':'access','is_error':True}]}}
        path.write_text(json.dumps(request)+'\n')
        data=self.scan()
        self.assertEqual(data['providers']['claude']['state'],'waiting')
        self.assertEqual(data['providers']['claude']['reason'],'permission')
        self.assertEqual(data['events'],[])
        request['timestamp']=stamp(self.now+1.1)
        result2={**result,'timestamp':stamp(self.now+1.2)}
        with path.open('a') as f:
            f.write('\n'.join(map(json.dumps,[result,request,result2]))+'\n')
        data=self.scan(self.now+2)
        self.assertEqual(data['providers']['claude']['state'],'working')
        self.assertEqual(data['events'],[])

    def test_child_agents_do_not_emit_top_level_completion(self):
        self.append({'type':'session_meta','timestamp':stamp(self.now),'payload':{'source':{'subagent':{}}}})
        self.scan()
        self.append(codex('task_started',self.now+1),codex('task_complete',self.now+2))
        self.assertEqual(self.scan(self.now+3)['events'],[])


if __name__=='__main__':unittest.main()
