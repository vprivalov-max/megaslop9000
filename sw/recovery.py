"""Startup recovery: clean chunks stranded mid-Seedance-submission."""
import json
import threading
import time

from sw.config import DATA_ROOT
from sw.storage import _atomic_write_json

# ── Startup recovery ─────────────────────────────────────────────────────────
# When the server is killed mid-Seedance-submission, chunks can be stranded:
#   • status='submitting' without job_id  → submit thread died, mark failed
#   • status='pending'/'processing' with job_id → AVAI still working;
#     the next time a client opens the episode and polls, state self-heals.
# We only need to clean up the first case so the UI stops showing a phantom
# "submitting" card. Idempotent and bounded — won't touch healthy state.

_RECOVERY_DONE = False
_RECOVERY_LOCK = threading.Lock()
_SUBMITTING_TIMEOUT_SEC = 5 * 60  # boot-time recovery: be conservative
_SUBMITTING_TIMEOUT_RUNTIME_SEC = 300  # runtime poll: submit usually <10s, but AVAI sometimes lags w/ heavy prompts (esp. prod VPS egress)

def _recover_inflight_chunks():
    """Sweep all per-user episode files once at startup."""
    global _RECOVERY_DONE
    with _RECOVERY_LOCK:
        if _RECOVERY_DONE:
            return
        _RECOVERY_DONE = True
    if not DATA_ROOT.exists():
        return
    now = time.time()
    cleaned = 0
    scanned = 0
    for user_dir in DATA_ROOT.iterdir():
        proj = user_dir / 'projects'
        if not proj.is_dir():
            continue
        for sd in proj.iterdir():
            ep_dir = sd / 'episodes'
            if not ep_dir.is_dir():
                continue
            for ep_file in ep_dir.glob('*.json'):
                try:
                    ep = json.loads(ep_file.read_text())
                except Exception:
                    continue
                chunks = (ep.get('seedance_chunks')
                          if isinstance(ep, dict) else None) or []
                changed = False
                for c in chunks:
                    scanned += 1
                    st = c.get('status')
                    age = now - int(c.get('created_at') or 0)
                    if st == 'submitting' and not c.get('job_id') and age > _SUBMITTING_TIMEOUT_SEC:
                        c['status'] = 'failed'
                        c['error'] = 'server restarted before submission completed'
                        cleaned += 1
                        changed = True
                if changed:
                    try:
                        _atomic_write_json(ep_file, ep)
                    except Exception as e:
                        print(f'[recover] failed to save {ep_file}: {e}')
    if scanned:
        print(f'[recover] scanned {scanned} chunks, cleaned {cleaned} stranded submissions')
