"""Read explicit CLI lifecycle events, without exporting prompts or tool arguments."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

from tokens import parse_time

SOURCES = {'codex':('.codex/sessions','*.jsonl'),
           'claude':('.claude/projects','*.jsonl'),
           'grok':('.grok/sessions','events.jsonl')}
CLAUDE_WAIT_TOOLS = {'AskUserQuestion':'input', 'mcp__computer-use__request_access':'permission'}
WAITING_TIMEOUT = 30 * 60


def event_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    return parse_time(value)


class Session:
    def __init__(self, provider):
        self.provider = provider
        self.state = 'unknown'
        self.reason = None
        self.at = 0
        self.last_seen = 0
        self.token = None
        self.notified = None
        self.waiting = {}  # tool id -> sync / async (Codex), input / permission / async_input (Claude)
        self.end_seen = False
        self.ignored = False
        self.session_id = None
        self.last_input_at = 0
        self.last_interrupt_at = 0
        self.last_wait_at = 0
        self.wait_requests = {}
        self.tool_results = {}

    def change(self, state, at, token, reason=None):
        if state != self.state or reason != self.reason:
            self.state, self.reason, self.at, self.token = state, reason, at, token

    def feed(self, row, token):
        if not isinstance(row, dict):
            return
        at = event_time(row.get('timestamp', row.get('ts')))
        if at is None:
            return
        self.last_seen = max(self.last_seen, at)
        if self.provider == 'codex':
            p = row.get('payload')
            if not isinstance(p, dict):
                return
            kind = p.get('type')
            if row.get('type') == 'session_meta' and isinstance(p.get('source'), dict) and 'subagent' in p['source']:
                self.ignored = True
            if kind == 'task_started':
                self.waiting.clear()
                self.change('working', at, token)
            elif kind == 'task_complete' and not self.waiting:
                self.change('completed', at, token)
            elif kind in ('turn_aborted', 'task_aborted'):
                self.waiting.clear()
                self.change('idle', at, token)
            elif kind == 'message' and p.get('role') == 'user':
                self.waiting.clear()
                self.change('working', at, token)
            elif kind == 'function_call' and p.get('name', '').split('.')[-1] in ('request_user_input', 'request_user_input_async'):
                self.waiting[p.get('call_id')] = 'async' if p['name'].endswith('_async') else 'sync'
                self.change('waiting', at, token, 'input')
            elif kind == 'function_call_output' and self.waiting.get(p.get('call_id')) == 'sync':
                self.waiting.pop(p.get('call_id'), None)
                if not self.waiting:
                    self.change('working', at, token)
        elif self.provider == 'claude':
            if row.get('isSidechain'):
                self.ignored = True
                return
            if isinstance(row.get('sessionId'), str):
                self.session_id = row['sessionId']
            kind, message = row.get('type'), row.get('message') or {}
            if not isinstance(message, dict):
                return
            content = message.get('content')
            blocks = content if isinstance(content, list) else []
            if kind == 'user':
                results = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_result']
                texts = [b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text']
                if isinstance(content, str):
                    texts.append(content)
                interrupted = any(t.strip() in ('[Request interrupted by user]',
                    '[Request interrupted by user for tool use]') for t in texts if isinstance(t,str))
                if interrupted:
                    self.waiting.clear()
                    self.end_seen = False
                    self.last_interrupt_at = at
                    self.change('idle', at, token)
                elif not row.get('isMeta') and (not results or any(t.strip() for t in texts if isinstance(t,str))):
                    self.waiting.clear()
                    self.end_seen = False
                    self.last_input_at = at
                    self.change('working', at, token)
                elif results:
                    for block in results:
                        key = block.get('tool_use_id')
                        if key in self.wait_requests:
                            self.tool_results[key] = at
                        # The wrap-up tool acknowledges publishing the question card;
                        # its successful tool result is not the person's answer.
                        if self.waiting.get(key) != 'async_input' or block.get('is_error') is True:
                            self.waiting.pop(key, None)
                    if self.state == 'waiting':
                        reason = 'input' if any(v in ('input','async_input') for v in self.waiting.values()) else 'permission'
                        self.change('waiting' if self.waiting else 'working', at, token,
                                    reason if self.waiting else None)
            if kind == 'assistant':
                if message.get('stop_reason') in ('end_turn', 'stop_sequence'):
                    self.end_seen = True
                for block in blocks:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        self.end_seen = False
                        if block.get('name') == 'mcp__ccd_turn__wrap_up':
                            arguments = block.get('input')
                            if isinstance(arguments,dict):
                                # Replacing the wrap-up card replaces its pending ask.
                                self.waiting = {k:v for k,v in self.waiting.items() if v != 'async_input'}
                                ask = arguments.get('ask')
                                if isinstance(ask,str) and ask.strip() or arguments.get('blocked') is True:
                                    self.waiting[block.get('id')] = 'async_input'
                                    self.last_wait_at = at
                                reason = 'input' if any(v in ('input','async_input') for v in self.waiting.values()) else 'permission'
                                self.change('waiting' if self.waiting else 'working',at,token,
                                            reason if self.waiting else None)
                        elif block.get('name') in CLAUDE_WAIT_TOOLS:
                            self.waiting[block.get('id')] = CLAUDE_WAIT_TOOLS[block['name']]
                            self.last_wait_at = at
                            self.wait_requests[block.get('id')] = (block['name'],at)
                            reason = 'input' if any(v in ('input','async_input') for v in self.waiting.values()) else 'permission'
                            self.change('waiting', at, token, reason)
            ended = row.get('subtype') == 'turn_duration' or (
                row.get('subtype') == 'stop_hook_summary' and row.get('preventedContinuation') is False
                and not any(row.get(key) for key in ('stopReason','hookErrors','hookAdditionalContext','hasOutput')))
            if kind == 'system' and ended and self.end_seen and not self.waiting:
                self.change('completed', at, token)
        else:
            kind = row.get('type')
            if kind == 'turn_started':
                if row.get('session_relationship', 'primary') != 'primary':
                    self.ignored = True
                self.waiting.clear()
                self.change('working', at, token)
            elif kind == 'tool_started' and row.get('tool_name') == 'ask_user_question':
                self.waiting['question'] = 'sync'
                self.change('waiting', at, token, 'input')
            elif kind == 'permission_requested' or (kind == 'phase_changed' and row.get('phase') == 'permission_prompt'):
                self.change('waiting', at, token, 'input' if self.waiting else 'permission')
            elif kind == 'permission_resolved':
                self.change('waiting' if self.waiting else 'working', at, token, 'input' if self.waiting else None)
            elif kind == 'tool_completed' and row.get('tool_name') == 'ask_user_question':
                self.waiting.clear()
                self.change('working', at, token)
            elif kind == 'turn_ended':
                self.waiting.clear()
                self.change('completed' if row.get('outcome') == 'completed' else 'idle', at, token)


class ClaudeDesktopPermissions:
    """Actual desktop dialogs, including permission gates hidden inside other tools."""
    request = re.compile(r'^Emitted tool permission request ([\w-]+) for (\S+) in session ([\w-]+)$')
    response = re.compile(r'^(?:Received permission response for ([\w-]+):|LocalSessions\.respondToToolPermission: requestId=([\w-]+),)')
    closed = re.compile(r'^(?:LocalSessions\.interrupt: sessionId=|Interrupting session |\[Stop hook\] Query completed for session )([\w-]+)')
    mapping = re.compile(r'^Mapping internal session ([\w-]+) to CLI session ([\w-]+)$')
    sent = re.compile(r'^LocalSessions\.sendMessage: sessionId=([\w-]+)(?:,|$)')

    def __init__(self, home):
        self.root = Path(home)/'Library/Logs/Claude'
        self.cursors = {}
        self.pending = {}
        self.resolved = {}
        self.closed_sessions = {}
        self.cli_sessions = {}
        self.actions = {}
        self.metadata = {}
        self.answered = {}

    def act(self, session_id, at, state):
        if at >= self.actions.get(session_id, (0,None))[0]:
            self.actions[session_id] = (at,state)
        for key,(owner,session) in list(self.pending.items()):
            if owner == session_id and session.at <= at:
                self.pending.pop(key)
                self.resolved[key] = at

    def reconcile(self, sessions):
        """Use explicit user actions even while the desktop's transcript copy lags."""
        by_id = {s.session_id:s for s in sessions if s.session_id and not s.ignored}
        for owner,cli_id in self.cli_sessions.items():
            session = by_id.get(cli_id)
            if session is None:
                continue
            if session.last_input_at:
                self.act(owner,session.last_input_at,'working')
            if session.last_interrupt_at:
                self.act(owner,session.last_interrupt_at,'idle')
            at,state = self.actions.get(owner,(0,None))
            if session.state == 'waiting' and at >= session.last_wait_at:
                session.waiting.clear()
                session.end_seen = False
                session.last_seen = max(session.last_seen,at)
                session.change(state,at,'desktop-action:'+owner+':'+str(at))
        # The same synchronous question can be present in both sources. Resolve
        # its other copy only with the same session, tool and request time.
        for key,(owner,dialog) in list(self.pending.items()) + list(self.answered.items()):
            session = by_id.get(self.cli_sessions.get(owner))
            if session is None:
                continue
            for tool_id,(name,requested_at) in session.wait_requests.items():
                name = {'mcp__computer-use__request_access':'computer:request_access'}.get(name,name)
                if name != dialog.tool or abs(requested_at-dialog.at) >= 3:
                    continue
                if key in self.pending and tool_id in session.tool_results:
                    self.pending.pop(key,None)
                    self.resolved[key] = session.tool_results[tool_id]
                elif (key in self.answered and tool_id in session.waiting
                      and requested_at <= self.resolved[key]):
                    session.waiting.pop(tool_id)
                    reason = 'input' if any(v in ('input','async_input') for v in session.waiting.values()) else 'permission'
                    session.change('waiting' if session.waiting else 'working',self.resolved[key],
                                   'desktop-response:'+key,reason if session.waiting else None)

    def read_metadata(self):
        # Persistent IDs and the latest submitted user frame survive log rotation.
        root = self.root.parent.parent/'Application Support/Claude/claude-code-sessions'
        for path in root.rglob('local_*.json'):
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns,stat.st_size)
                if self.metadata.get(path) == signature or stat.st_size > 2*1024*1024:
                    continue
                data = json.loads(path.read_text())
                owner,cli_id = data.get('sessionId'),data.get('cliSessionId')
                if not isinstance(owner,str) or not isinstance(cli_id,str):
                    continue
                self.cli_sessions[owner] = cli_id
                at = data.get('latestUserFrameAt')
                if isinstance(at,(int,float)) and not isinstance(at,bool) and at > 0:
                    self.act(owner,at/1000,'working')
                self.metadata[path] = signature
            except (OSError,ValueError,AttributeError):
                continue

    def feed(self, line, now):
        match = re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[\w+\] (.*)$',line.rstrip())
        if not match:
            return
        try:
            at = datetime.strptime(match[1],'%Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:
            return
        if not 0 <= now-at < 86400:
            return
        message = match[2]
        mapping = self.mapping.match(message)
        if mapping:
            self.cli_sessions[mapping[1]] = mapping[2]
            return
        sent = self.sent.match(message)
        if sent:
            self.act(sent[1],at,'working')
            return
        request = self.request.match(message)
        if request:
            key, tool, session_id = request.groups()
            if (key in self.pending or key in self.resolved or at <= self.closed_sessions.get(session_id,0)
                    or at < self.actions.get(session_id,(0,None))[0]):
                return
            session = Session('claude')
            session.tool = tool
            session.last_seen = at
            session.change('waiting',at,hashlib.sha256(('claude-desktop:'+key).encode()).hexdigest()[:24],
                           'input' if tool == 'AskUserQuestion' else 'permission')
            self.pending[key] = (session_id,session)
            return
        response = self.response.match(message)
        if response:
            key = response[1] or response[2]
            dialog = self.pending.pop(key,None)
            if dialog is not None:
                self.answered[key] = dialog
            self.resolved[key] = at
            return
        closed = self.closed.match(message)
        if closed:
            session_id = closed[1]
            if not message.startswith('[Stop hook]'):
                self.act(session_id,at,'idle')
            self.closed_sessions[session_id] = at
            for key,(owner,session) in list(self.pending.items()):
                if owner == session_id and session.at <= at:
                    self.pending.pop(key)
                    self.resolved[key] = at

    def scan(self, now):
        self.read_metadata()
        paths = []
        for name in ('main1.log','main.log'):
            path = self.root/name
            try:
                paths.append((path.stat().st_mtime,path))
            except OSError:
                pass
        for _,path in sorted(paths):
            try:
                stat = path.stat()
                cursor = self.cursors.get(path)
                if cursor is None or cursor['inode'] != stat.st_ino or stat.st_size < cursor['offset']:
                    cursor = {'inode':stat.st_ino,'offset':max(0,stat.st_size-2*1024*1024),
                              'skip':stat.st_size>2*1024*1024}
                    self.cursors[path] = cursor
                with path.open('rb') as handle:
                    handle.seek(cursor['offset'])
                    budget = 2*1024*1024
                    while budget > 0:
                        offset = handle.tell()
                        line = handle.readline(65536)
                        if not line:
                            break
                        budget -= len(line)
                        if cursor['skip']:
                            cursor['skip'] = not line.endswith(b'\n')
                            continue
                        if not line.endswith(b'\n'):
                            if len(line) == 65536:
                                cursor['skip'] = True
                                continue
                            handle.seek(offset)
                            break
                        self.feed(line.decode('utf8','replace'),now)
                    cursor['offset'] = handle.tell()
            except OSError:
                continue
        self.pending = {key:value for key,value in self.pending.items() if now-value[1].at < 86400}
        self.resolved = {key:at for key,at in self.resolved.items() if now-at < 86400}
        self.answered = {key:value for key,value in self.answered.items() if key in self.resolved}
        self.closed_sessions = {key:at for key,at in self.closed_sessions.items() if now-at < 86400}
        return [session for _,session in self.pending.values()]


class ActivityMonitor:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.lock = threading.RLock()
        self.files = {}
        self.started = time.time()
        self.warmed = False
        self.discovered = 0
        self.paths = []
        self.events = []
        self.data = {key:{'state':'unknown','at':None,'reason':None} for key in SOURCES}
        self.desktop = ClaudeDesktopPermissions(self.home)

    def _discover(self, now):
        if self.discovered and now - self.discovered < 10:
            return
        paths = []
        for provider, (folder, pattern) in SOURCES.items():
            for path in (self.home / folder).rglob(pattern):
                try:
                    if 'subagents' not in path.parts and path.stat().st_mtime >= now - 86400:
                        paths.append((provider, path))
                except OSError:
                    continue
        self.paths, self.discovered = paths, now
        live = {path for _, path in paths}
        self.files = {path:value for path,value in self.files.items() if path in live}

    def scan(self):
        now = time.time()
        self._discover(now)
        for provider, path in self.paths:
            try:
                stat = path.stat()
                cursor = self.files.get(path)
                if cursor is None or cursor['inode'] != stat.st_ino or stat.st_size < cursor['offset']:
                    cursor = {'inode':stat.st_ino, 'offset':max(0,stat.st_size-512*1024),
                              'session':Session(provider), 'skip':stat.st_size>512*1024}
                    self.files[path] = cursor
                    if provider == 'codex':
                        try:
                            with path.open('rb') as head:
                                metadata = json.loads(head.readline(128*1024))
                            source = (metadata.get('payload') or {}).get('source')
                            cursor['session'].ignored = isinstance(source, dict) and 'subagent' in source
                        except (ValueError, AttributeError):
                            pass
                with path.open('rb') as handle:
                    handle.seek(cursor['offset'])
                    # Incremental reads; never load a whole conversation on every poll.
                    budget = 2*1024*1024
                    while budget > 0:
                        offset = handle.tell()
                        line = handle.readline(2*1024*1024)
                        if not line:
                            break
                        budget -= len(line)
                        if cursor['skip']:
                            cursor['skip'] = not line.endswith(b'\n')
                            continue
                        if not line.endswith(b'\n') and len(line) == 2*1024*1024:
                            cursor['skip'] = True
                            continue
                        if not line.endswith(b'\n'):
                            handle.seek(offset)
                            break
                        try:
                            row = json.loads(line)
                            stamp = row.get('timestamp', row.get('ts')) if isinstance(row, dict) else ''
                            token = hashlib.sha256(f'{path}:{stat.st_ino}:{offset}:{stamp}'.encode()).hexdigest()[:24]
                            cursor['session'].feed(row, token)
                        except (ValueError, TypeError, AttributeError):
                            continue
                    cursor['offset'] = handle.tell()
            except OSError:
                continue
        self.desktop.scan(now)
        self.desktop.reconcile([cursor['session'] for cursor in self.files.values()
                                if cursor['session'].provider == 'claude'])
        desktop_waiting = [session for _,session in self.desktop.pending.values()]
        with self.lock:
            for provider in SOURCES:
                sessions = [cursor['session'] for cursor in self.files.values()
                            if cursor['session'].provider == provider and not cursor['session'].ignored]
                transcript_sessions = list(sessions)
                if provider == 'claude':
                    sessions += desktop_waiting
                for session in sessions:
                    if not self.warmed:
                        session.notified = session.token
                    # A question can appear in both the transcript and desktop dialog log.
                    if (session in transcript_sessions and session.state == 'waiting' and provider == 'claude'
                            and any(s.reason == session.reason and abs(s.at-session.at)<3 for s in desktop_waiting)):
                        session.notified = session.token
                    if (session.state in ('completed','waiting') and session.token != session.notified
                            and session.at >= self.started and 0 <= now-session.at < 30
                            and (session.state != 'waiting' or now-session.at >= 1)):
                        self.events.append({'id':session.token,'provider':provider,'kind':session.state,
                                            'reason':session.reason,'at':session.at,'preview':False})
                        session.notified = session.token
                # Waiting takes precedence if several sessions of the same CLI are open.
                # Expire the display state from when waiting began, not later log activity.
                # Keep pending tool IDs so a late answer still resolves normally.
                current = [s for s in sessions if (0 <= now-s.at < WAITING_TIMEOUT
                           if s.state == 'waiting' else now-s.last_seen < 900)]
                priority = {'waiting':4,'working':3,'completed':2,'idle':1,'unknown':0}
                latest = max(current,key=lambda s:(priority[s.state],s.at),default=None)
                self.data[provider] = {'state':latest.state if latest else 'unknown',
                                       'at':latest.at if latest else None,'reason':latest.reason if latest else None}
            self.events = [e for e in self.events if 0 <= now-e['at'] < 30][-24:]
            self.warmed = True

    def snapshot(self):
        with self.lock:
            return {'providers':copy.deepcopy(self.data),
                    'events':copy.deepcopy([e for e in self.events if 0 <= time.time()-e['at'] < 30])}

    def preview(self, provider, kind):
        # 'reset' only demonstrates the quota-reset celebration; it never touches quota readings.
        if provider not in SOURCES or kind not in ('completed','waiting','reset'):
            raise ValueError('Invalid activity preview')
        with self.lock:
            event = {'id':uuid.uuid4().hex,'provider':provider,'kind':kind,'reason':'input' if kind=='waiting' else None,
                     'at':time.time(),'preview':True}
            self.events = (self.events + [event])[-24:]
            return event

    def run(self, stop):
        while not stop.is_set():
            self.scan()
            stop.wait(2)
