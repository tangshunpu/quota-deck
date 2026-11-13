import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tokens import TokenCounter

NOW = datetime.now(timezone.utc)
TODAY = NOW.isoformat().replace('+00:00', 'Z')
OLD = (NOW-timedelta(days=3)).isoformat().replace('+00:00', 'Z')


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))


def codex_event(at, input_tokens, cached, output):
    return {'timestamp':at, 'type':'event_msg', 'payload':{'type':'token_count', 'info':{'total_token_usage':{
        'input_tokens':input_tokens, 'cached_input_tokens':cached, 'output_tokens':output}}}}


class TokenTests(unittest.TestCase):
    def scan(self, build):
        with tempfile.TemporaryDirectory() as home:
            build(Path(home))
            counter = TokenCounter(home)
            counter.scan()
            return counter.snapshot()['providers']

    def test_claude_deduplicates_streamed_lines(self):
        usage = {'input_tokens':10, 'cache_creation_input_tokens':5, 'cache_read_input_tokens':100, 'output_tokens':7}
        row = {'type':'assistant', 'timestamp':TODAY, 'requestId':'r1', 'message':{'id':'m1', 'usage':usage}}
        old = {**row, 'timestamp':OLD, 'requestId':'r0', 'message':{'id':'m0', 'usage':usage}}
        result = self.scan(lambda home: write(home/'.claude/projects/p/s.jsonl', [old, row, row]))
        self.assertEqual({k:v for k,v in result['claude'].items() if k!='hourly'}, {'input':15, 'cached':100, 'output':7, 'total':122})
        self.assertEqual(sum(result['claude']['hourly']), 122)

    def test_codex_counts_only_todays_increase(self):
        rows = [codex_event(OLD, 1000, 800, 50), codex_event(TODAY, 1500, 1100, 80),
                codex_event(TODAY, 1500, 1100, 80)]
        result = self.scan(lambda home: write(home/'.codex/sessions/2026/01/01/r.jsonl', rows))
        self.assertEqual({k:v for k,v in result['codex'].items() if k!='hourly'}, {'input':200, 'cached':300, 'output':30, 'total':530})
        self.assertEqual(sum(result['codex']['hourly']), 530)

    def test_codex_continuation_file_does_not_recount_parent_total(self):
        def event(total, last):
            row = codex_event(TODAY, total, 0, 0)
            row['payload']['info']['last_token_usage'] = {'input_tokens':last, 'cached_input_tokens':0, 'output_tokens':0}
            return row
        def build(home):
            write(home/'.codex/sessions/a.jsonl', [event(100, 100), event(1000, 900)])
            write(home/'.codex/sessions/a_b.jsonl', [event(1050, 50), event(1200, 150)])
        self.assertEqual(self.scan(build)['codex']['total'], 1200)

    def test_grok_sums_turns_ended_today(self):
        turn = {'endedAt':TODAY, 'inputTokens':1000, 'cachedReadTokens':600, 'cacheCreationTokens':0, 'outputTokens':40}
        def build(home):
            path = home/'.grok/sessions/x/s1/usage.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'turns':[turn, {**turn, 'endedAt':OLD}]}))
        result = self.scan(build)
        self.assertEqual({k:v for k,v in result['grok'].items() if k!='hourly'}, {'input':400, 'cached':600, 'output':40, 'total':1040})
        self.assertEqual(sum(result['grok']['hourly']), 1040)

    def test_claude_fork_does_not_recount_copied_parent_messages(self):
        def row(id, n, **extra):
            return {'type':'assistant', 'timestamp':TODAY, 'requestId':'r'+id,
                    'message':{'id':id, 'usage':{'input_tokens':n}}, **extra}
        def build(home):
            write(home/'.claude/projects/p/parent.jsonl', [row('m1', 10)])
            write(home/'.claude/projects/p/fork.jsonl', [row('m1', 10, forkedFrom={'sessionId':'parent'}), row('m2', 5)])
        self.assertEqual(self.scan(build)['claude']['total'], 15)

    def test_codex_fork_skips_replayed_parent_history(self):
        def at(seconds):
            return (NOW+timedelta(seconds=seconds)).isoformat().replace('+00:00', 'Z')
        def meta(id, parent=None):
            return {'timestamp':at(0), 'type':'session_meta', 'payload':{'id':id, 'forked_from_id':parent}}
        def record(thread):
            return {'timestamp':at(0), 'type':'token_usage_record', 'payload':{'thread_id':thread}}
        def call(seconds, total, last):
            return {'timestamp':at(seconds), 'type':'event_msg', 'payload':{'type':'token_count', 'info':{
                'total_token_usage':{'input_tokens':total}, 'last_token_usage':{'input_tokens':last}}}}
        def build(home):
            folder = home/'.codex/sessions/2026/01/01'
            write(folder/'parent.jsonl', [meta('P'), record('P'), call(0, 100, 100), record('P'), call(1, 300, 200)])
            # Newer rollouts: replayed calls carry the parent's thread id, the fork's own call its own.
            write(folder/'fork.jsonl', [meta('F', 'P'), record('P'), call(0, 100, 100), record('P'),
                                        call(0, 300, 200), record('F'), call(5, 350, 50)])
            # Older rollouts without usage records: the replay is written within the fork instant.
            write(folder/'old-fork.jsonl', [meta('G', 'P'), call(0, 100, 100), call(0.01, 300, 200), call(6, 330, 30)])
        self.assertEqual(self.scan(build)['codex']['total'], 300+50+30)

    def test_codex_interleaved_side_thread_counter_is_not_recounted(self):
        def call(total, last):
            return {'timestamp':TODAY, 'type':'event_msg', 'payload':{'type':'token_count', 'info':{
                'total_token_usage':{'input_tokens':total}, 'last_token_usage':{'input_tokens':last}}}}
        rows = [call(1000, 1000), call(1100, 100), call(50, 50), call(80, 30), call(1150, 50), call(1150, 50)]
        result = self.scan(lambda home: write(home/'.codex/sessions/r.jsonl', rows))
        self.assertEqual(result['codex']['total'], 1000+100+50+30+50)

    def test_missing_cli_is_unknown_not_zero(self):
        self.assertIsNone(self.scan(lambda home: None)['codex'])


if __name__ == '__main__':
    unittest.main()
