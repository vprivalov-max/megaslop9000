"""Per-episode and per-series write locks (serialize read-modify-write of JSON)."""
import threading

_EPISODE_LOCKS = {}
_EPISODE_LOCKS_GUARD = threading.Lock()

def _episode_lock(sid, num):
    """Get-or-create the lock for (sid, episode_num). Used to serialize
    read-modify-write of an episode's JSON file."""
    key = (sid, int(num))
    with _EPISODE_LOCKS_GUARD:
        lock = _EPISODE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _EPISODE_LOCKS[key] = lock
    return lock


_SERIES_LOCKS = {}
_SERIES_LOCKS_GUARD = threading.Lock()

def _series_lock(sid):
    """Get-or-create the lock for a series sid. Used to serialize read-
    modify-write of series.json (characters, locations, settings, etc.)
    when multiple bg workers update concurrently — e.g. import-from-script
    spawns BOTH `_import_worker` (script-based extraction) AND
    `_backfill_uploaded_char_appearances` (Vision-based appearance fill);
    both touch series.characters and need to interleave safely."""
    with _SERIES_LOCKS_GUARD:
        lock = _SERIES_LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            _SERIES_LOCKS[sid] = lock
    return lock
