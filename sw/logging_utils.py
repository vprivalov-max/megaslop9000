"""Per-user structured logging: JSONL event log + retention cleanup loop."""
import datetime
import json
import os
import re
import threading
import time

from sw.auth import current_user_email
from sw.config import DATA_ROOT

_LOG_RETENTION_DAYS = int(os.environ.get('LOG_RETENTION_DAYS', '7'))

def _cleanup_old_logs():
    """Walks every <DATA_ROOT>/<user>/_logs/ folder and deletes JSONL files
    older than _LOG_RETENTION_DAYS. Best-effort — failures swallowed.
    Runs at startup and once per day via _LOG_CLEANUP_TIMER."""
    if _LOG_RETENTION_DAYS <= 0:
        return
    if not DATA_ROOT.exists():
        return
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=_LOG_RETENTION_DAYS)
    deleted = 0
    scanned = 0
    for user_dir in DATA_ROOT.iterdir():
        log_dir = user_dir / '_logs'
        if not log_dir.is_dir():
            continue
        for log_file in log_dir.glob('*.jsonl'):
            # Skip macOS AppleDouble (._*) sidecars — not real log files.
            if log_file.name.startswith('._'):
                continue
            scanned += 1
            try:
                # Filename is YYYY-MM-DD.jsonl — fast path: parse the date.
                stem = log_file.stem
                try:
                    file_date = datetime.datetime.strptime(stem, '%Y-%m-%d')
                except ValueError:
                    # Fallback to mtime if filename isn't ISO-date
                    file_date = datetime.datetime.utcfromtimestamp(log_file.stat().st_mtime)
                if file_date < cutoff:
                    log_file.unlink()
                    deleted += 1
            except Exception:
                pass
    if scanned:
        print(f'[log-cleanup] scanned {scanned} files, deleted {deleted} older than {_LOG_RETENTION_DAYS}d', flush=True)

def _start_log_cleanup_loop():
    """Spawns a daemon thread that runs cleanup once now + every 24h after."""
    if _LOG_RETENTION_DAYS <= 0:
        return
    def _loop():
        while True:
            try:
                _cleanup_old_logs()
            except Exception as e:
                print(f'[log-cleanup] loop tick failed: {e}', flush=True)
            time.sleep(24 * 60 * 60)  # 24h
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# ── Per-user structured logging ─────────────────────────────────────────────
# Writes one JSONL line per request to <DATA_ROOT>/<email-slug>/_logs/YYYY-MM-DD.jsonl
# so the primary operator can debug other users' issues without SSHing into the
# VPS to grep docker logs. Captures: timestamp, route, method, status, duration,
# user_email, error trace (when 5xx). Also `_log_event(...)` lets app code emit
# structured events (e.g. "[autogen] FAILED item/...") into the same file.

_LOG_LOCK = threading.Lock()

def _user_log_dir(email):
    safe = re.sub(r'[^a-z0-9]+', '_', (email or 'anon').lower()).strip('_') or 'anon'
    return DATA_ROOT / safe / '_logs'

def _log_event(level, event, email=None, **fields):
    """Append a structured log line to the user's daily JSONL log file.
    Safe to call from any thread or background worker. Best-effort — failures
    are swallowed (we don't want logging to crash a request)."""
    try:
        if email is None:
            try: email = current_user_email() or 'anon'
            except Exception: email = 'anon'
        d = _user_log_dir(email)
        d.mkdir(parents=True, exist_ok=True)
        today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
        line = json.dumps({
            'ts': datetime.datetime.utcnow().isoformat(timespec='milliseconds') + 'Z',
            'level': level,
            'event': event,
            'email': email,
            **fields,
        }, ensure_ascii=False)
        with _LOG_LOCK:
            with open(d / f'{today}.jsonl', 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass

