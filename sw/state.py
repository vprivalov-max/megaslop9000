"""Shared runtime state: render queue, upload whitelist."""
import os
import threading

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}


# ── Render queue ─────────────────────────────────────────────────────────────
# Cap concurrent ffmpeg renders so N users don't all pin the CPU at once.
# Each render takes 5–30s of pure CPU; serializing past 2 prevents stalls and
# OOM. Configurable via env.
_RENDER_CONCURRENCY = max(1, int(os.environ.get('RENDER_CONCURRENCY', '2')))
RENDER_SEMAPHORE = threading.BoundedSemaphore(_RENDER_CONCURRENCY)

def _render_queue_depth():
    """Approximate number of waiters. Bounded semaphores don't expose this
    directly, so we just report whether the queue is saturated."""
    # _value is the number of free slots (CPython internal).
    free = getattr(RENDER_SEMAPHORE, '_value', _RENDER_CONCURRENCY)
    return {'concurrency': _RENDER_CONCURRENCY, 'free': free, 'busy': _RENDER_CONCURRENCY - free}

