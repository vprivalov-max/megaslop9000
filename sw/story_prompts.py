"""Shared prompt-building helpers: cast blocks, canon formatting, format-mode."""
from pathlib import Path

from sw.data.format_modes import _FORMAT_MODE_RULES

def _canonical_cast_block(s):
    """Synopsis-time cast pin: short list of canonical character names + role tags so that
    every synopsis-generating Claude call uses the SAME names instead of inventing fresh
    ones (which is how Emma/Victoria/Liam quietly became Elena/Marcus/Claire). Returns ''
    when the series has no characters yet (very first synopsis at idea time)."""
    chars = (s or {}).get('characters', []) or []
    if not chars:
        return ''
    lines = []
    for c in chars:
        name = c.get('name', '').strip()
        if not name:
            continue
        role = (c.get('description') or '').strip().split('.')[0][:120]
        lines.append(f'  • {name}' + (f' — {role}' if role else ''))
    if not lines:
        return ''
    return (
        'CANONICAL CAST — these are the ONLY named characters that exist in this series. '
        'Every synopsis MUST use these exact names. NEVER invent alternative names for the same '
        'roles (do not rename the heroine, the sister, the fiancé, etc.). If a character is not '
        'in the list and the scene needs one, label them by role only (Doctor, Driver, Mother).\n'
        + '\n'.join(lines) + '\n\n'
    )


def _outfit_ids(val):
    """Normalize episode.character_outfits[cid] into a list of outfit ids.
    Backward-compatible: accepts legacy single-string, list, or None/empty.
    Going forward storage is always list. Reads tolerate both."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    return [str(val)]


def _build_cast_block(s, ep):
    """Build CAST & LOCATIONS context for script generation with exact names, outfits and real asset filenames."""
    chars = s.get('characters', [])
    locs  = s.get('locations', [])
    ep_outfits = ep.get('character_outfits', {})
    ep_chars   = ep.get('characters_used', [])
    ep_locs    = ep.get('locations_used', [])

    lines = []
    for c in chars:
        outfit_ids = _outfit_ids(ep_outfits.get(c['id']))
        ep_outfits_objs = [
            o for oid in outfit_ids
            for o in c.get('outfits', []) if o['id'] == oid
        ]
        # Primary outfit (for OUTFIT label / asset filename) = first selected, else base
        outfit = ep_outfits_objs[0] if ep_outfits_objs else None
        outfit_label = outfit['label'] if outfit else 'base'
        outfit_desc  = outfit.get('description', '') if outfit else c.get('appearance', '')
        marker = '★' if c['id'] in ep_chars else ' '

        # Real asset filename — use outfit photo if set, else first ref image
        if outfit and outfit.get('photo'):
            asset_filename = Path(outfit['photo']).name
        elif c.get('ref_images'):
            asset_filename = Path(c['ref_images'][0]).name
        else:
            asset_filename = None
        asset_str = f' | ASSET_FILE: {asset_filename}' if asset_filename else ''

        # List all available outfits so Claude can pick the right one for the scene
        available = ['base'] + [o['label'] for o in c.get('outfits', []) if o.get('label')]
        available_str = f' | AVAILABLE_OUTFITS: {", ".join(available)}'

        # Episode-selected outfits (when more than one — character changes clothes within episode)
        selected_str = ''
        if len(ep_outfits_objs) > 1:
            sel_labels = ', '.join(o['label'] for o in ep_outfits_objs)
            selected_str = (
                f' | EPISODE_OUTFITS: {sel_labels} '
                f'(⚠ character WEARS DIFFERENT CLOTHES across scenes — you MUST list this character {len(ep_outfits_objs)} times '
                f'in the === EPISODE CAST === block, once per outfit, each with the matching outfit_label and OUTFIT_DESC)'
            )

        # When no outfit pre-selected, signal to writer that "base" is the default
        # but scene context may require inventing a new one (bedroom→pajamas, etc.)
        outfit_hint = outfit_label if outfit else f'{outfit_label} ← default; override if scene requires it'
        lines.append(
            f'  {marker} NAME: "{c["name"]}" | OUTFIT: "{outfit_hint}"{available_str}{asset_str}{selected_str}'
            + (f' ({outfit_desc[:80]})' if outfit_desc else '')
        )

    cast_block = ('SERIES CHARACTERS — use these EXACT names and outfits in the script:\n'
                  + '\n'.join(lines)) if lines else ''

    loc_lines = []
    for l in locs:
        marker = '★' if l['id'] in ep_locs else ' '
        if l.get('ref_images'):
            loc_asset = Path(l['ref_images'][0]).name
            asset_str = f' | ASSET_FILE: {loc_asset}'
        else:
            asset_str = ''
        loc_lines.append(f'  {marker} "{l["name"]}": {l.get("description","")[:80]}{asset_str}')
    loc_block = 'LOCATIONS (★ = used in this episode):\n' + '\n'.join(loc_lines) if loc_lines else ''

    return '\n\n'.join(filter(None, [cast_block, loc_block]))


def _format_canon_for_prompt(canon, max_facts=40, max_timeline=10):
    """Render canon as a compact text block for Claude prompts."""
    wc = canon.get('world_clock', {})
    parts = [f"WORLD CLOCK: day {wc.get('current_day', 0)} (last episode: ep {wc.get('last_episode', 0)})"]

    timeline = canon.get('timeline', [])[-max_timeline:]
    if timeline:
        parts.append("RECENT TIMELINE:")
        for t in timeline:
            evs = '; '.join(t.get('events', []))
            parts.append(f"  ep{t.get('ep')} day{t.get('day')}: {evs}")

    facts = [f for f in canon.get('facts', []) if f.get('locked', True)][-max_facts:]
    if facts:
        parts.append("LOCKED CANON FACTS:")
        for f in facts:
            sup = f' (supersedes {f["supersedes"]})' if f.get('supersedes') else ''
            parts.append(f"  {f.get('id')}: {f.get('fact')}{sup}")

    cs = canon.get('character_state', {})
    if cs:
        parts.append("CHARACTER STATE:")
        for name, st in cs.items():
            knows = ', '.join(st.get('knows', [])[-8:]) or '—'
            phys = ', '.join(f"{k}={v}" for k, v in (st.get('physical') or {}).items()) or '—'
            loc = st.get('location') or '—'
            parts.append(f"  {name}: knows[{knows}] physical[{phys}] loc={loc}")

    threads = [t for t in canon.get('open_threads', []) if t.get('status') != 'closed']
    if threads:
        parts.append("OPEN THREADS (consider closing or explicitly deferring):")
        for t in threads:
            parts.append(f"  {t.get('id')} (opened ep{t.get('opened_ep')}): {t.get('question')}")

    return '\n'.join(parts)


def _format_mode_of(s) -> str:
    """Return the format mode for a series, defaulting to 'short_drama'."""
    if not s:
        return 'short_drama'
    m = (s.get('format_mode') or 'short_drama').strip().lower()
    return m if m in _FORMAT_MODE_RULES else 'short_drama'


def _format_mode_block(s, sections=None) -> str:
    """Build a compact format-mode directive block for injection into a prompt.

    sections — optional list of rule keys to include (default = all). Common
    subsets: ['title_rule','synopsis_rule'] for idea/synopsis prompts;
    ['episode_rule','pace_rule'] for script-writing prompts.
    """
    mode = _format_mode_of(s) if isinstance(s, dict) else (s if isinstance(s, str) else 'short_drama')
    if mode not in _FORMAT_MODE_RULES:
        mode = 'short_drama'
    rules = _FORMAT_MODE_RULES[mode]
    keys = sections or ['title_rule', 'synopsis_rule', 'episode_rule', 'pace_rule']
    lines = [f'═══ FORMAT MODE: {mode.replace("_", " ").upper()} ═══']
    lines.append(rules['one_liner'])
    for k in keys:
        v = rules.get(k)
        if v:
            lines.append(f'• {v}')
    lines.append('═══════════════════════════════════════════════')
    return '\n'.join(lines) + '\n\n'


