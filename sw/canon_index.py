"""Plot-device and narrative-state extraction + per-series indexes
(anti-repetition cadence for writer prompts)."""
import json

from sw.jsonutils import strip_json
from sw.llm import claude_ask
from sw.storage import (DEVICE_FUNCTIONS, DEVICE_TAXONOMY,
                        NARRATIVE_ANT_MOMENTUM, NARRATIVE_ARCHETYPES,
                        NARRATIVE_EMOTIONS, NARRATIVE_POWER_DELTA,
                        load_episode, load_series, save_episode, save_series)

def _extract_devices_from_script(script: str) -> list:
    """Lightweight Haiku call: detect which DEVICE_TAXONOMY entries this script uses.
    Returns list of {"id": device_id, "fn": device_function} dicts (max 4).
    On any failure returns [].
    """
    if not script or not script.strip():
        return []
    taxonomy_str = ', '.join(DEVICE_TAXONOMY)
    functions_str = ', '.join(DEVICE_FUNCTIONS)
    prompt = (
        f'Analyze this short-drama script and identify the narrative delivery mechanisms used.\n\n'
        f'TAXONOMY (pick ONLY from this list): {taxonomy_str}\n'
        f'FUNCTIONS (pick ONLY from this list): {functions_str}\n\n'
        f'Return JSON array of up to 4 objects, each: {{"id": "<taxonomy_item>", "fn": "<function_item>"}}.\n'
        f'Only include mechanisms that are clearly present. If none match, return [].\n'
        f'Return ONLY the JSON array, no explanation.\n\n'
        f'SCRIPT:\n{script[:6000]}'
    )
    try:
        raw = claude_ask(prompt, system='', model='claude-haiku-4-5', max_tokens=300)
        raw = strip_json(raw)
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            dev_id = item.get('id', '')
            dev_fn = item.get('fn', '')
            if dev_id in DEVICE_TAXONOMY and dev_fn in DEVICE_FUNCTIONS:
                result.append({'id': dev_id, 'fn': dev_fn})
        return result[:4]
    except Exception as e:
        print(f'[devices] extract failed: {e}', flush=True)
        return []


def _extract_narrative_state_from_script(script: str) -> dict:
    """Haiku call: extract narrative state from script.
    Returns {"archetype": str, "power_delta": str, "closing_emotion": str, "antagonist_momentum": str}
    or {} on failure.
    """
    if not script or not script.strip():
        return {}
    archetypes_str   = ', '.join(NARRATIVE_ARCHETYPES)
    power_str        = ', '.join(NARRATIVE_POWER_DELTA)
    emotions_str     = ', '.join(NARRATIVE_EMOTIONS)
    ant_str          = ', '.join(NARRATIVE_ANT_MOMENTUM)
    prompt = (
        f'Analyze this short-drama episode script and classify its narrative state.\n\n'
        f'archetype — ONE of: {archetypes_str}\n'
        f'power_delta — who ends the episode with more power: ONE of: {power_str}\n'
        f'closing_emotion — dominant viewer emotion at episode end: ONE of: {emotions_str}\n'
        f'antagonist_momentum — how is the antagonist\'s trajectory: ONE of: {ant_str}\n\n'
        f'Return ONLY valid JSON, no explanation:\n'
        f'{{"archetype":"...","power_delta":"...","closing_emotion":"...","antagonist_momentum":"..."}}\n\n'
        f'SCRIPT:\n{script[:6000]}'
    )
    try:
        raw = claude_ask(prompt, system='', model='claude-haiku-4-5', max_tokens=120, timeout=30)
        raw = strip_json(raw)
        data = json.loads(raw)
        result = {}
        if data.get('archetype') in NARRATIVE_ARCHETYPES:
            result['archetype'] = data['archetype']
        if data.get('power_delta') in NARRATIVE_POWER_DELTA:
            result['power_delta'] = data['power_delta']
        if data.get('closing_emotion') in NARRATIVE_EMOTIONS:
            result['closing_emotion'] = data['closing_emotion']
        if data.get('antagonist_momentum') in NARRATIVE_ANT_MOMENTUM:
            result['antagonist_momentum'] = data['antagonist_momentum']
        return result
    except Exception as e:
        print(f'[narrative] state extract failed: {e}', flush=True)
        return {}


def _update_devices_index(sid: str, num: int, devices: list):
    """Update series devices_index with extracted devices from episode num.
    Idempotent: skips if ep already registered for that device.
    Validates device_id against DEVICE_TAXONOMY — unknown ids are skipped.
    """
    if not devices:
        return
    s = load_series(sid)
    if s is None:
        return
    s.setdefault('devices_index', {})
    s.setdefault('cadence_policy', {'default_min_gap': 4, 'hard_limit': 3})

    ep = load_episode(sid, num)
    if ep is not None and not ep.get('plot_devices'):
        ep['plot_devices'] = devices
        save_episode(sid, num, ep)

    changed = False
    for d in devices:
        dev_id = d.get('id', '')
        dev_fn = d.get('fn', '')
        if dev_id not in DEVICE_TAXONOMY:
            print(f'[devices] WARN: unknown device_id "{dev_id}" — skipping', flush=True)
            continue
        entries = s['devices_index'].setdefault(dev_id, [])
        # Idempotent: skip if this ep is already recorded
        if any(e.get('ep') == num for e in entries):
            continue
        entries.append({'ep': num, 'fn': dev_fn})
        changed = True

    if changed:
        save_series(sid, s)


def _update_narrative_index(sid: str, num: int, state: dict):
    """Update series narrative_index with extracted state from episode num.
    narrative_index is a list of {"ep": int, "archetype": str, "power_delta": str,
    "closing_emotion": str, "antagonist_momentum": str} entries, sorted by ep.
    Idempotent: replaces existing entry for same ep num.
    """
    if not state:
        return
    s = load_series(sid)
    if s is None:
        return
    s.setdefault('narrative_index', [])
    # Remove old entry for this ep (if any) then append new
    s['narrative_index'] = [e for e in s['narrative_index'] if e.get('ep') != num]
    entry = {'ep': num, **state}
    s['narrative_index'].append(entry)
    s['narrative_index'].sort(key=lambda e: e.get('ep', 0))
    # Also store on episode itself
    ep = load_episode(sid, num)
    if ep is not None:
        ep['narrative_state'] = state
        save_episode(sid, num, ep)
    save_series(sid, s)


def _build_plot_device_history(sid: str, num: int) -> str:
    """Build the plot-device constraint block for episode num's generation prompt.
    Returns empty string if no history exists (zero regression).
    """
    s = load_series(sid)
    if s is None:
        return ''
    index = s.get('devices_index') or {}
    if not index:
        return ''

    policy = s.get('cadence_policy') or {}
    min_gap = int(policy.get('default_min_gap', 4))
    hard_limit = int(policy.get('hard_limit', 3))

    lines = []
    for dev_id in DEVICE_TAXONOMY:
        entries = index.get(dev_id)
        if not entries:
            continue
        count = len(entries)
        last_ep = max(e.get('ep', 0) for e in entries)
        gap = num - last_ep  # episodes since last use

        if count >= hard_limit:
            status = f'🚫 {dev_id} × {count} (last: ep{last_ep}) — FORBIDDEN (used {count}+ times)'
        elif gap < min_gap:
            status = f'⚠️  {dev_id} × {count} (last: ep{last_ep}) — caution (used recently)'
        else:
            status = f'✓  {dev_id} × {count} (ep{last_ep}) — available'
        lines.append(status)

    if not lines:
        return ''

    return (
        '═══ PLOT DEVICES ALREADY USED — VARIETY REQUIRED ═══\n'
        + '\n'.join(lines)
        + '\n\nRULE: 🚫 devices are FORBIDDEN this episode. ⚠️ devices: avoid unless completely transformed.\n'
        'Use a mechanism not in this list, or pick from ✓ column.\n'
        '═══════════════════════════════════════════════════\n\n'
    )


