"""Incremental, read-only daily token extraction. Also runs remotely on Python 3.9."""
from datetime import date, datetime
import hashlib
from itertools import chain
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from tokens import ENTRIES, SOURCES


def scan_daily(home, known, start, end, timezone):
    zone = ZoneInfo(timezone)
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    batch = {'files':[], 'errors':0, 'updatedAt':time.time()}
    seen = set()
    for provider, (folder, pattern, _) in SOURCES.items():
        root = Path(home)/folder
        archived = Path(home)/'.codex/archived_sessions'
        if not root.is_dir() and not (provider == 'codex' and archived.is_dir()):
            continue
        paths = root.rglob(pattern)
        if provider == 'codex':
            # Older rollouts are compressed in place to .jsonl.zst, then possibly archived.
            paths = chain(paths,root.rglob(pattern+'.zst'),archived.rglob(pattern),archived.rglob(pattern+'.zst'))
        for path in paths:
            try:
                # Only hashes and aggregate counts cross the SSH connection. A rollout keeps
                # one identity whether plain, compressed or archived, so it is never added twice.
                identity = path.name.removesuffix('.zst') if provider == 'codex' else str(path.relative_to(root))
                file_id = hashlib.sha256((provider+'/'+identity).encode()).hexdigest()
                if file_id in seen:
                    continue
                seen.add(file_id)
                stat = path.stat()
                fingerprint = [stat.st_mtime_ns, stat.st_size]
                if known.get(file_id) == fingerprint+[end]:
                    continue
                days = {}
                for usage, at in ENTRIES[provider](path):
                    day = datetime.fromtimestamp(at, zone).date()
                    if not first <= day <= last:
                        continue
                    total = days.setdefault(day.isoformat(),dict.fromkeys(('input','cached','output','total'),0))
                    for field in total:
                        total[field] += usage[field]
                # Keep complete records even during active writes. Saving the earlier
                # fingerprint makes the next scan retry; monotonic upserts protect history.
                after = path.stat()
                if (stat.st_mtime_ns,stat.st_size) != (after.st_mtime_ns,after.st_size):
                    batch['errors'] += 1
                batch['files'].append({'id':file_id,'provider':provider,
                    'mtimeNs':stat.st_mtime_ns,'size':stat.st_size,'through':end,'days':days})
            except (OSError, ValueError, TypeError, OverflowError):
                batch['errors'] += 1
    return batch
