"""Today's token usage, read from the CLIs' own local session logs. No network."""
import copy
from datetime import datetime
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import threading
import time


def empty():
    return {'input':0,'cached':0,'output':0,'total':0,'hourly':[0]*24}


def add(total, usage, at=None):
    for key in ('input', 'cached', 'output', 'total'):
        total[key] += usage.get(key, 0)
    if at is not None:
        total['hourly'][datetime.fromtimestamp(at).hour] += usage.get('total', 0)
    for hour, value in enumerate(usage.get('hourly') or []):
        total['hourly'][hour] += value


def parse_time(value):
    try:
        # Trim nanoseconds to microseconds: Python < 3.11 (e.g. macOS's) rejects longer fractions.
        value = re.sub(r'(\.\d{6})\d+', r'\1', value.replace('Z', '+00:00'))
        return datetime.fromisoformat(value).timestamp()
    except (AttributeError, ValueError):
        return None


def count(value):
    return value if type(value) is int and value >= 0 else 0


ZSTD_PATHS = ('/opt/homebrew/bin/zstd', '/usr/local/bin/zstd', '/usr/bin/zstd')


def zstd_lines(path):
    # Codex compresses older rollouts in place to .jsonl.zst. Python 3.14+ decodes it natively;
    # otherwise stream through the zstd CLI (Homebrew's is off a non-login SSH PATH).
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd:
        with zstd.open(path, 'rb') as handle:
            yield from handle
        return
    tool = shutil.which('zstd') or next((p for p in ZSTD_PATHS if os.access(p, os.X_OK)), None)
    if tool is None:
        raise OSError('zstd is unavailable')
    process = subprocess.Popen([tool, '-dcq', '--', str(path)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield from process.stdout
        # A truncated or partially written archive must not be saved as a complete file.
        if process.wait(timeout=30):
            raise OSError('zstd failed')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def raw_lines(path):
    if str(path).endswith('.zst'):
        yield from zstd_lines(path)
        return
    with open(path, 'rb') as handle:
        yield from handle


def json_lines(path, markers=()):
    for line in raw_lines(path):
        # Cheap substring check before json.loads; rollouts are mostly large tool-output lines.
        if markers and not any(marker in line for marker in markers):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def claude_entries(path):
    # One API message is written as several lines (one per content block); keep the last.
    messages = {}
    for row in json_lines(path):
        if not isinstance(row, dict):
            continue
        message = row.get('message')
        # A forked session copies the parent's history (marked forkedFrom) into its own file;
        # those calls are already counted in the parent.
        if row.get('type') != 'assistant' or not isinstance(message, dict) or row.get('forkedFrom'):
            continue
        usage, at = message.get('usage'), parse_time(row.get('timestamp'))
        if not isinstance(usage, dict) or at is None:
            continue
        fresh = count(usage.get('input_tokens')) + count(usage.get('cache_creation_input_tokens'))
        cached, output = count(usage.get('cache_read_input_tokens')), count(usage.get('output_tokens'))
        messages[(message.get('id'), row.get('requestId'))] = ({
            'input':fresh, 'cached':cached, 'output':output, 'total':fresh+cached+output}, at)
    yield from messages.values()


def codex_entries(path):
    # token_count events carry a running session total; sum its increases made today.
    own = forked_at = None
    # A forked rollout first replays the parent's history, token_count events included. Newer
    # Codex writes a token_usage_record before each call; the fork's own calls carry its thread id.
    forked, records, copied, heads = False, False, [], []
    for row in json_lines(path, (b'token_count', b'token_usage_record', b'session_meta')):
        payload = row.get('payload') if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            continue
        if row.get('type') == 'session_meta':
            if own is None:
                own, forked = payload.get('id'), bool(payload.get('forked_from_id'))
                forked_at = parse_time(row.get('timestamp'))
            continue
        if row.get('type') == 'token_usage_record':
            records = True
            if forked and own is not None and payload.get('thread_id') == own:
                forked = False
            continue
        if payload.get('type') != 'token_count':
            continue
        info = payload.get('info')
        if not isinstance(info, dict):
            continue
        usage, last = info.get('total_token_usage') or {}, info.get('last_token_usage') or {}
        if not isinstance(usage, dict) or not isinstance(last, dict):
            continue
        current = {'input':count(usage.get('input_tokens')), 'cached':count(usage.get('cached_input_tokens')),
                   'output':count(usage.get('output_tokens'))}
        # A rollout can interleave a side thread's lower running total with the main one. Each event
        # continues the counter with the largest total not above its own; replayed events advance it too.
        size = current['input']+current['output']
        below = [head for head in heads if head['input']+head['output'] <= size]
        if below:
            previous = max(below, key=lambda head: head['input']+head['output'])
            heads.remove(previous)
        else:
            # A resumed/compacted file starts from the parent's running total and a side thread
            # from its own: only this event's call is new, the rest was counted already.
            previous = {'input':current['input']-count(last.get('input_tokens')),
                        'cached':current['cached']-count(last.get('cached_input_tokens')),
                        'output':current['output']-count(last.get('output_tokens'))}
        heads.append(current)
        at = parse_time(row.get('timestamp'))
        if at is not None:
            delta = {key:max(0, current[key]-previous[key]) for key in current}
            # Cached tokens are part of Codex input; keep input + cached + output == total.
            cached = min(delta['cached'], delta['input'])
            entry = {'input':delta['input']-cached, 'cached':cached, 'output':delta['output'],
                     'total':delta['input']+delta['output']}, at
            if forked:
                copied.append(entry)
            else:
                yield entry
    if forked and not records:
        # Older rollouts have no usage records. Their replay is written in one burst (milliseconds)
        # when forking, seconds before the fork's first model call returns.
        yield from (entry for entry in copied if forked_at is None or entry[1]-forked_at >= 1)


def grok_entries(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        return
    for turn in data.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        at = parse_time(turn.get('endedAt'))
        if at is None:
            continue
        cached = count(turn.get('cachedReadTokens'))
        fresh = max(0, count(turn.get('inputTokens'))-cached) + count(turn.get('cacheCreationTokens'))
        output = count(turn.get('outputTokens'))
        yield {'input':fresh, 'cached':cached, 'output':output, 'total':fresh+cached+output}, at


def sum_entries(entries, start):
    total = empty()
    for usage, at in entries:
        if at >= start:
            add(total, usage, at)
    return total


def claude_file(path, start):
    return sum_entries(claude_entries(path), start)


def codex_file(path, start):
    return sum_entries(codex_entries(path), start)


def grok_file(path, start):
    return sum_entries(grok_entries(path), start)


ENTRIES = {'codex':codex_entries, 'grok':grok_entries, 'claude':claude_entries}


SOURCES = {
    'codex':('.codex/sessions', '*.jsonl', codex_file),
    'grok':('.grok/sessions', 'usage.json', grok_file),
    'claude':('.claude/projects', '*.jsonl', claude_file),
}


class TokenCounter:
    def __init__(self, home=None):
        self.home = Path(home or Path.home())
        self.lock = threading.Lock()
        self.files = {}  # path -> (mtime, size, day start, usage)
        self.data = {'providers':{key:None for key in SOURCES}, 'day':None, 'updatedAt':None}

    def scan(self):
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        providers, seen = {}, set()
        for key, (folder, pattern, reader) in SOURCES.items():
            root = self.home/folder
            if not root.is_dir():
                providers[key] = None
                continue
            total = empty()
            for path in root.rglob(pattern):
                try:
                    stat = path.stat()
                    if stat.st_mtime < start:
                        continue
                    seen.add(path)
                    cached = self.files.get(path)
                    if cached and cached[:3] == (stat.st_mtime, stat.st_size, start):
                        usage = cached[3]
                    else:
                        usage = reader(path, start)
                        self.files[path] = (stat.st_mtime, stat.st_size, start, usage)
                    add(total, usage)
                except (OSError, ValueError):
                    continue
            providers[key] = total
        self.files = {path:value for path, value in self.files.items() if path in seen}
        with self.lock:
            self.data = {'providers':providers, 'day':datetime.fromtimestamp(start).strftime('%Y-%m-%d'),
                         'updatedAt':time.time()}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def run(self, stop):
        while not stop.is_set():
            self.scan()
            stop.wait(60)
