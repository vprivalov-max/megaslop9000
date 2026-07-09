"""GREEN LIGHT playbook — the writer's "training" signal.

Two proven-performance sources are distilled into ONE compact craft playbook that
is injected into every script/synopsis generation prompt:

  1. GREEN LIGHT series — the studio's OWN series a human marked `color == 'green'`
     (e.g. Twice_The_Heir, Bound by the Jade Dragon). Green = confirmed strong
     real-world statistics, so their opening episodes are treated as golden
     reference. Weighted heaviest.

  2. Top short-drama analyses — data/top_dramas.json. Each analyzed drama carries
     per-episode beats (first 5, and optionally 6-10) plus a detailed synopsis.
     These give breadth across many viral hits.

"Training" here is in-context, not fine-tuning: we ask Claude ONCE to distill the
recurring craft techniques (hooks, escalation ladders, humiliation/爽 staging,
reversal placement, cliffhanger types, pacing) from these two corpora into a
reusable playbook, cache it per-user, and prepend it to generation prompts. The
cache is rebuilt on demand (button) whenever analyses or green series change.
"""
import datetime
import json

from sw.config import DATA_ROOT
from sw.llm import claude_ask_quality
from sw.logging_utils import _log_event
from sw.storage import list_episodes, user_root

# ── Corpus size caps (logged when hit — never silently truncate) ──────────────
_MAX_GREEN_SERIES      = 8       # most-recently-updated green series feeding the distill
_MAX_GREEN_EPISODES    = 10      # opening episodes per green series (hooks live up front)
_PER_SCRIPT_CHARS      = 6000    # cap per episode script excerpt
_GREEN_CORPUS_CHARS    = 90000   # total green-series corpus ceiling
_DRAMA_CORPUS_CHARS    = 60000   # total top-drama-analyses corpus ceiling


def _playbook_path():
    """Per-user cache: <DATA_ROOT>/<email-slug>/greenlight_playbook.json.
    user_root() is <DATA_ROOT>/<slug>/projects, so its parent is the user dir."""
    return user_root().parent / 'greenlight_playbook.json'


def _top_dramas_path():
    return DATA_ROOT / 'top_dramas.json'


def load_playbook():
    """Return the cached playbook dict, or None if never built."""
    p = _playbook_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        _log_event('WARN', 'greenlight_playbook_load_failed', err=str(e)[:200])
        return None


def get_playbook_block() -> str:
    """Prompt-injection block. Empty string when no playbook has been built yet,
    so generation is unchanged until the user builds one."""
    pb = load_playbook()
    text = (pb or {}).get('playbook', '').strip() if pb else ''
    if not text:
        return ''
    return (
        '╔══════════════════════════════════════════════════════════════════════╗\n'
        '║ GREEN LIGHT PLAYBOOK — proven craft from top-performing short dramas   ║\n'
        '║ Distilled from GREEN LIGHT (statistically strong) series + analyses of ║\n'
        '║ viral hits. Treat these as high-authority craft guidance, second only  ║\n'
        '║ to the hard logic/canon/trajectory constraints in this prompt.         ║\n'
        '╚══════════════════════════════════════════════════════════════════════╝\n'
        f'{text}\n'
        '═══════════════════════════════════════════════════════════════════════\n\n'
    )


# ── Corpus collection ─────────────────────────────────────────────────────────

def _iter_green_series():
    """Yield (sid, series_dict) for every non-archived series the user marked
    GREEN LIGHT (color == 'green'), most-recently-modified first."""
    root = user_root()
    if not root.exists():
        return
    found = []
    for d in sorted(root.iterdir()):
        sf = d / 'series.json'
        if not sf.exists():
            continue
        try:
            s = json.loads(sf.read_text(encoding='utf-8'))
        except Exception:
            continue
        if (s.get('color') or '') != 'green' or s.get('archived'):
            continue
        try:
            mtime = int(sf.stat().st_mtime)
        except Exception:
            mtime = 0
        found.append((mtime, d.name, s))
    found.sort(key=lambda x: -x[0])
    for _, sid, s in found:
        yield sid, s


def _collect_green_scripts():
    """Build the GREEN LIGHT corpus text: each proven series' bible + opening
    episode synopses & scripts. Returns (text, meta) where meta lists the series
    used and flags any caps hit."""
    parts = []
    used = []
    total = 0
    truncated = False
    series_count = 0
    for sid, s in _iter_green_series():
        if series_count >= _MAX_GREEN_SERIES:
            truncated = True
            break
        series_count += 1
        header = (
            f'### GREEN LIGHT SERIES: "{s.get("title", sid)}"\n'
            f'Genre: {s.get("genre", "")} | Tone: {s.get("tone", "")}\n'
            f'Arc: {s.get("arc", "")}\n'
        )
        eps = list_episodes(sid)
        # list_episodes is sorted by filename → episode order.
        ep_count = 0
        ep_texts = []
        for ep in eps:
            if ep_count >= _MAX_GREEN_EPISODES:
                break
            syn = (ep.get('synopsis') or '').strip()
            scr = (ep.get('script') or '').strip()
            if not syn and not scr:
                continue
            ep_count += 1
            block = f'\n-- {ep.get("title") or "Episode"} --\n'
            if syn:
                block += f'SYNOPSIS: {syn[:1500]}\n'
            if scr:
                block += f'SCRIPT:\n{scr[:_PER_SCRIPT_CHARS]}\n'
            ep_texts.append(block)
        if ep_count == 0:
            continue
        chunk = header + ''.join(ep_texts) + '\n'
        if total + len(chunk) > _GREEN_CORPUS_CHARS:
            truncated = True
            break
        parts.append(chunk)
        total += len(chunk)
        used.append({'sid': sid, 'title': s.get('title', sid), 'episodes': ep_count})
    meta = {'series': used, 'truncated': truncated}
    return '\n'.join(parts), meta


def _fmt_drama_analysis(d):
    """Render one top-drama entry's stored analysis into compact craft-relevant
    text (synopsis + per-episode beats). Returns '' if nothing analyzed."""
    a = d.get('analysis') or {}
    if not a:
        return ''
    lines = [f'### DRAMA: "{a.get("title") or d.get("title", "")}"']
    if d.get('genre'):
        lines.append(f'Genre: {d.get("genre")}')
    if d.get('popularity'):
        lines.append(f'Traction: {d.get("popularity")}')
    if a.get('hook'):
        lines.append(f'Hook: {a.get("hook")}')
    if a.get('central_conflict'):
        lines.append(f'Central conflict: {a.get("central_conflict")}')
    syn = (a.get('detailed_synopsis') or '').strip()
    if syn:
        lines.append(f'Synopsis: {syn[:1400]}')
    # Per-episode beats: first_5_episodes and any later ranges stored on the entry.
    for key in ('first_5_episodes', 'episodes_6_10', 'next_5_episodes'):
        beats = a.get(key)
        if isinstance(beats, list) and beats:
            lines.append(f'{key.replace("_", " ").title()}:')
            for i, b in enumerate(beats, 1):
                if isinstance(b, dict):
                    txt = b.get('beat') or b.get('summary') or b.get('synopsis') or json.dumps(b, ensure_ascii=False)
                else:
                    txt = str(b)
                lines.append(f'  {i}. {str(txt)[:400]}')
    return '\n'.join(lines) + '\n'


def _collect_drama_analyses():
    """Build the top-drama-analyses corpus. Returns (text, meta)."""
    try:
        store = json.loads(_top_dramas_path().read_text(encoding='utf-8'))
    except Exception:
        return '', {'count': 0, 'truncated': False}
    parts = []
    total = 0
    count = 0
    truncated = False
    for d in (store.get('dramas') or []):
        txt = _fmt_drama_analysis(d)
        if not txt:
            continue
        if total + len(txt) > _DRAMA_CORPUS_CHARS:
            truncated = True
            break
        parts.append(txt)
        total += len(txt)
        count += 1
    return '\n'.join(parts), {'count': count, 'truncated': truncated}


# ── Distillation ──────────────────────────────────────────────────────────────

_DISTILL_SYSTEM = (
    "You are a senior short-drama (TikTok/Reels vertical) story editor. You reverse-engineer "
    "why top-performing 60-second serialized dramas work, and you turn that into terse, "
    "actionable craft rules a screenwriter can apply mechanically. You do NOT summarize plots — "
    "you extract transferable TECHNIQUE."
)


def _distill_prompt(green_text, green_meta, drama_text, drama_meta):
    green_titles = ', '.join(x['title'] for x in green_meta.get('series', [])) or '(none yet)'
    return (
        "Below are two corpora of proven short-drama material. Distill them into a single "
        "reusable CRAFT PLAYBOOK that will be injected into every future script- and "
        "synopsis-generation prompt for new series.\n\n"
        "═══ CORPUS A — GREEN LIGHT SERIES (HIGHEST AUTHORITY) ═══\n"
        f"These are OUR OWN series with confirmed strong audience statistics ({green_titles}). "
        "Weight the patterns you see here MOST heavily — they are proven with real numbers. "
        "Study their opening episodes: how they hook in the first 5 seconds, how they stage "
        "humiliation and the 爽 (satisfying) payoff, where reversals land, how each episode "
        "cuts on a cliffhanger.\n"
        f"{green_text or '(no GREEN LIGHT series available yet — rely on Corpus B)'}\n\n"
        "═══ CORPUS B — ANALYSES OF VIRAL TOP DRAMAS (BREADTH) ═══\n"
        "Per-episode beats and synopses of many internet-trending short dramas. Use these for "
        "breadth of proven patterns across genres.\n"
        f"{drama_text or '(no drama analyses available)'}\n\n"
        "═══ YOUR OUTPUT — THE PLAYBOOK ═══\n"
        "Write a compact, high-density playbook in English (craft terms only, ~700-1200 words). "
        "Use these sections, each as a tight bulleted list of CONCRETE, transferable rules "
        "(not plot summaries, no series names in the rules):\n"
        "1. COLD-OPEN HOOKS — the specific opening moves that work in the first 3-5 seconds.\n"
        "2. ESCALATION & 爽 (SATISFYING) STAGING — how tension/humiliation is piled on and how "
        "the payoff/comeback is delivered for maximum viewer satisfaction.\n"
        "3. REVERSALS & TWISTS — typical placement, cadence, and shapes of the turns.\n"
        "4. CLIFFHANGER TYPES — the recurring end-of-episode cut patterns that drive retention.\n"
        "5. CHARACTER & RELATIONSHIP DYNAMICS — the archetypes and power-shifts that recur.\n"
        "6. PACING & DIALOGUE — line length, scene density, what to cut.\n"
        "7. GREEN LIGHT EDGE — the 3-6 techniques MOST distinctive to Corpus A (our proven "
        "winners) that a new script should deliberately copy.\n\n"
        "Output ONLY the playbook (markdown headings + bullets). No preamble, no closing remarks."
    )


def build_playbook(force: bool = True) -> dict:
    """Distill the GREEN LIGHT + top-drama corpora into a cached craft playbook.
    Returns the stored dict (with a `playbook` string and source metadata).
    Raises on LLM failure so the route can surface it."""
    green_text, green_meta = _collect_green_scripts()
    drama_text, drama_meta = _collect_drama_analyses()

    if not green_text and not drama_text:
        raise ValueError(
            'Нет данных для обучения: ни одной серии не помечено GREEN LIGHT и нет '
            'проанализированных топ-драм. Пометь сильные серии зелёным и/или проанализируй '
            'топовые шорт-драмы, затем пересобери playbook.'
        )

    prompt = _distill_prompt(green_text, green_meta, drama_text, drama_meta)
    playbook_text = (claude_ask_quality(prompt, system=_DISTILL_SYSTEM) or '').strip()
    if not playbook_text:
        raise ValueError('Distillation returned empty playbook.')

    record = {
        'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'playbook': playbook_text,
        'sources': {
            'green_series': green_meta.get('series', []),
            'green_truncated': green_meta.get('truncated', False),
            'dramas_analyzed': drama_meta.get('count', 0),
            'dramas_truncated': drama_meta.get('truncated', False),
        },
    }
    p = _playbook_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    _log_event('INFO', 'greenlight_playbook_built',
               green_series=len(green_meta.get('series', [])),
               dramas=drama_meta.get('count', 0),
               chars=len(playbook_text))
    return record
