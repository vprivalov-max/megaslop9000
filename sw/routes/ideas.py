"""Series idea generation routes: ideas pipeline v2, research, history,
generate-series-from-idea."""
import datetime
import json
import random
import re

from flask import jsonify, request

from sw.config import DATA_ROOT
from sw.core import app
from sw.data.idea_prompts import (IDEAS_V2, _IDEAS_SCHEMA, _IDEAS_SYSTEM)
from sw.data.idea_seeds import (_IDEA_ANTAG_ARCHETYPES,
                                _IDEA_PREMISE_STRUCTURES,
                                _IDEA_PROTAG_ARCHETYPES, _IDEA_SETTINGS,
                                _IDEA_TONES, _IDEA_TWISTS)
from sw.era import _ERA_KEYWORDS
from sw.jsonutils import loads_lenient, strip_json
from sw.llm import (_resolve_writer_model, claude_ask_quality,
                    claude_web_research, llm_ask)
from sw.logging_utils import _log_event

# ── AI: Series idea generation ───────────────────────────────────────────────

from sw.data.idea_prompts import (
    _IDEAS_SYSTEM,
    _IDEAS_SCHEMA,
    IDEAS_V2,
    _IDEAS_SYSTEM_V2,
    _IDEAS_RESEARCH_SYSTEM,
    _IDEAS_RESEARCH_TAIL,
    _IDEAS_LOGIC_SYSTEM,
    _IDEAS_SCHEMA_V2,
    _IDEA_CORE_ENGINES,
    _IDEA_CORE_LEADS,
    _IDEA_CORE_WORLDS,
    _IDEA_CORE_TWISTS,
    _IDEA_ENGINE_FAMILIES,
    _IDEA_PROTAG_ARCS,
    _ideas_diversity_lanes,
)
_IDEAS_HISTORY_MAX = 40

def _ideas_history_path():
    return DATA_ROOT / 'ideas_history.json'

def _load_ideas_history():
    try:
        p = _ideas_history_path()
        if p.exists():
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return [str(x) for x in data if x]
    except Exception as e:
        print(f'[ideas] history load failed ({e.__class__.__name__})', flush=True)
    return []

def _ideas_signature(idea):
    if not isinstance(idea, dict):
        return ''
    title = (idea.get('title') or '').strip()
    hook = (idea.get('synopsis') or idea.get('world_description') or '').strip()
    hook = ' '.join(hook.split())[:90]
    sig = f'{title} - {hook}' if hook else title
    return sig.strip(' -')

def _save_ideas_history(ideas):
    try:
        sigs = [s for s in (_ideas_signature(i) for i in (ideas or [])) if s]
        if not sigs:
            return
        hist = _load_ideas_history()
        hist.extend(sigs)
        hist = hist[-_IDEAS_HISTORY_MAX:]
        _ideas_history_path().write_text(json.dumps(hist, ensure_ascii=False, indent=0), encoding='utf-8')
    except Exception as e:
        print(f'[ideas] history save failed ({e.__class__.__name__})', flush=True)

def _ideas_avoid_recent_block():
    hist = _load_ideas_history()
    if not hist:
        return ''
    recent = hist[-18:]
    lines = '\n'.join(f'- {s}' for s in recent)
    return (
        "RECENTLY SHOWN - DO NOT REPEAT (hard): the user has already seen these concepts in "
        "previous batches. Every one of your 5 ideas must be clearly distinct from ALL of them - "
        "different title, different hook, different mechanic. Do not reflavour or rename any of these:\n"
        f"{lines}\n\n"
    )


def _ideas_research_digest(brief):
    """Stage 1: trend digest. Tries live web search (Anthropic web_search),
    falls back silently to model knowledge. Always returns a string."""
    research_prompt = (
        "Find the BEST-performing vertical short dramas right now for this brief:\n"
        f"{brief}\n\n"
        "Search the live web for current TOP-RATED and MOST-VIEWED titles across ReelShort, DramaBox, "
        "GoodShort, ShortMax, NetShort and viral verticals on YouTube / TikTok. "
        + _IDEAS_RESEARCH_TAIL
    )
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=5)
        if digest and len(digest.strip()) > 40:
            print('[ideas] research: web', flush=True)
            return digest.strip()
        print('[ideas] research: web returned thin result -> fallback', flush=True)
    except Exception as e:
        print(f'[ideas] research: web failed ({e.__class__.__name__}) -> fallback', flush=True)
    fb_prompt = (
        "From your own knowledge of the best-performing vertical short dramas (ReelShort / DramaBox / "
        f"GoodShort style), for this brief:\n{brief}\n\n"
        + _IDEAS_RESEARCH_TAIL
    )
    digest = claude_ask_quality(fb_prompt, system=_IDEAS_RESEARCH_SYSTEM)
    print('[ideas] research: fallback', flush=True)
    return (digest or '').strip()


def _run_ideas_pipeline_v2(writer_model, user_controls, genres, idea_hint,
                           era_world_override=False, avoid_rule=''):
    """3-stage idea generation. Returns a list of <=5 idea dicts (legacy schema
    fields preserved). Raises on hard failure so the caller can fall back to the
    legacy single-call path."""
    # Stage 0 — research brief from user controls.
    bits = []
    if genres:
        bits.append('genres: ' + ', '.join(genres))
    if idea_hint:
        bits.append('creator hint: ' + idea_hint)
    if era_world_override:
        bits.append('a non-modern era/world is set — surface period/world-appropriate hits')
    brief = '; '.join(bits) if bits else 'the overall most popular, highest-rated and most-viewed vertical short dramas right now, across all genres'

    # Stage 1 — trend digest (web -> fallback).
    digest = _ideas_research_digest(brief)

    # Stage 2 — generate 5 diverse ideas.
    gen_prompt = (
        "Generate exactly 5 short-drama series concepts for vertical mobile video.\n\n"
        + user_controls
        + "TOP-PERFORMING SHORT DRAMAS RIGHT NOW (researched live from the market — study what makes these "
          "win, then write FRESH concepts in the same vein; never copy a title or plot):\n"
        + digest + "\n\n"
        + _ideas_avoid_recent_block()
        + "Build the 5 so each is inspired by a DIFFERENT one of the hits above — mirror the real RANGE that is "
          "winning, do NOT funnel them into one repeated template. Follow the creator's chosen genre / preferences "
          "/ era EXACTLY (above). Each must be a clearly different story: different premise, lead and hook.\n\n"
        + "Each synopsis = the HOOK only, 1-2 short sentences, ~40 words MAX — a punchy logline a viewer "
          "grasps in three seconds, NOT a plot recap. Lead with the gut-punch and stop. Set the \"style\" field to \"logline\".\n\n"
        + "JSON SAFETY: output strict valid JSON. Do NOT use the double-quote character inside any "
          "field value — if someone speaks, paraphrase or use single quotes. No line breaks inside values.\n\n"
        + f"Return JSON matching this schema:\n{_IDEAS_SCHEMA_V2}"
    )
    ideas = loads_lenient(strip_json(llm_ask(writer_model, gen_prompt, system=_IDEAS_SYSTEM_V2)))
    ideas = ideas.get('ideas', ideas) if isinstance(ideas, dict) else ideas
    if not isinstance(ideas, list) or not ideas:
        raise ValueError('ideas stage-2 returned no list')
    print(f'[ideas] stage-2 generated {len(ideas)} ideas', flush=True)

    # Stage 3 — logic-check + polish (honors writer_model).
    era_note = ''
    if era_world_override:
        era_note = (
            "- ERA/WORLD: a non-modern era/world is in play (see directive at top of the "
            "original brief). Any idea that reads like a present-day realistic story, or uses a "
            "prop/role/event impossible in that era/world, must be rewritten so the era/world is "
            "unmistakable in the first sentence.\n"
        )
    genre_note = ''
    if genres:
        genre_note = "- GENRES: " + ', '.join(genres) + " must genuinely be present in every idea.\n"
    avoid_note = ''
    if avoid_rule:
        _ban = " ".join(avoid_rule.split())[:700]
        avoid_note = ("- BAN LIST (hard, includes synonyms/translations): " + _ban + " If ANY idea contains a banned concept — even in subtext — rewrite that idea onto a completely different premise.\n")
    check_prompt = (
        "Here are 5 short-drama concepts as JSON. Audit and polish them.\n\n"
        + json.dumps({'ideas': ideas}, ensure_ascii=False)
        + "\n\nFor EACH idea, verify and FIX in place:\n"
          "- LOGIC: timeline holds, who-knows-what is consistent, the premise does not collapse "
          "under one obvious question. If it breaks, rewrite the idea so it holds.\n"
          "- SIMPLICITY: one clear premise a person can picture; no fact-stacking, no piled-on "
          "jobs or backstories.\n"
          "- VOICE: the synopsis must read like a person describing a show to a friend, NOT a "
          "checklist of answers («she works as X, she is N months pregnant»). Rewrite stiff/listy "
          "synopses into natural, propulsive prose.\n"
          "- DISTINCTNESS: if two ideas are reflavoured copies, rewrite one onto a different engine.\n"
          "- LENGTH (HARD): every synopsis MUST be 1-2 short sentences, ~40 words MAX. If a draft runs longer, "
          "or chains morning-after / weeks-later / years-later beats, CUT it down to the single gut-punch hook. "
          "Aggressively shorten — short and primal beats long and clever.\n"
          "- ON-BRIEF: every idea must fit the creator's chosen genre / preferences / era (see below). If an "
          "idea drifts off the chosen genre, rewrite it to fit. If nothing was chosen, keep it in the vein of the "
          "proven hits the writer was given.\n"
          "- VARIETY: the 5 must mirror the real range of what is winning — do NOT let them collapse into one "
          "repeated template (e.g. every lead a wronged woman who turns out to be secretly powerful). If they bunch "
          "up, rewrite the duplicates into genuinely different stories.\n"
        + era_note
        + genre_note
        + avoid_note
        + "\nKeep exactly 5 ideas. Return JSON in the SAME schema (title, genre, tone, "
          "target_audience, world_description, synopsis, synopsis_ru; style optional). Output strict valid JSON — never use the double-quote character inside a field value; use single quotes for any spoken line."
    )
    try:
        checked = loads_lenient(strip_json(llm_ask(writer_model, check_prompt, system=_IDEAS_LOGIC_SYSTEM)))
        checked = checked.get('ideas', checked) if isinstance(checked, dict) else checked
        if isinstance(checked, list) and checked:
            print(f'[ideas] stage-3 logic-check returned {len(checked)} ideas', flush=True)
            return checked
        print('[ideas] stage-3 returned empty -> keeping stage-2 ideas', flush=True)
    except Exception as e:
        print(f'[ideas] stage-3 logic-check failed ({e.__class__.__name__}) -> keeping stage-2 ideas', flush=True)
    return ideas


_TOP_DRAMAS_SCHEMA = """{
  "dramas": [
    {
      "title": "Real show title",
      "premise": "One-line hook/premise in English",
      "premise_ru": "То же по-русски, живо",
      "genre": "Genre / tone",
      "why_hook": "Why it hooks viewers (short phrase)",
      "popularity": "Views / rating / chart signal if known, else empty string"
    }
  ]
}"""


def _research_top_dramas(genres=None, idea_hint='', era_hint='', n=10):
    """Live-web research of the ACTUAL top-performing short dramas, returned as a
    structured list (powers the 'Find top short dramas' button). Genre / hint /
    era filter the search when given; otherwise the overall best across genres."""
    bits = []
    if genres:
        bits.append('genres: ' + ', '.join(genres))
    if idea_hint:
        bits.append('creator hint: ' + idea_hint)
    if era_hint:
        bits.append(era_hint)
    brief = '; '.join(bits) if bits else 'the overall most popular, highest-rated and most-viewed vertical short dramas right now, across all genres'
    # Step 1 — live web research returns a PROSE list of real shows (the search
    # tool wraps output in commentary, so we do NOT ask it for JSON here).
    research_prompt = (
        "Find the BEST-performing vertical short dramas right now for this brief:\n"
        f"{brief}\n\n"
        "Search the live web for current TOP-RATED and MOST-VIEWED titles across ReelShort, DramaBox, "
        "GoodShort, ShortMax, NetShort and viral verticals on YouTube / TikTok. List 8-12 of the strongest "
        "REAL shows; for each give: title, one-line premise, genre/tone, why it hooks viewers, and any "
        "popularity signal (views / rating / chart position) you can find. Be concrete with real titles."
    )
    digest = ''
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=6)
        if digest and len(digest.strip()) > 40:
            print('[top-dramas] research: web', flush=True)
        else:
            digest = ''
    except Exception as e:
        print(f'[top-dramas] web failed ({e.__class__.__name__}) -> fallback', flush=True)
        digest = ''
    if not digest:
        digest = claude_ask_quality(research_prompt, system=_IDEAS_RESEARCH_SYSTEM) or ''
        print('[top-dramas] research: fallback', flush=True)
    # Step 2 — structure the prose into strict JSON with a plain (no-tool) call.
    fmt_prompt = (
        "Here is research on the current top-performing vertical short dramas:\n\n"
        f"{digest}\n\n"
        f"Convert it into STRICT valid JSON, {n}-12 entries, matching this schema. Output ONLY the JSON "
        f"(no prose, no code fences) and do NOT use the double-quote character inside any value:\n{_TOP_DRAMAS_SCHEMA}"
    )
    raw = claude_ask_quality(fmt_prompt, system='You output ONLY strict valid JSON — no prose, no code fences, no commentary.') or ''
    data = loads_lenient(strip_json(raw))
    dramas = data.get('dramas', data) if isinstance(data, dict) else data
    if not isinstance(dramas, list):
        raise ValueError('top-dramas: no list parsed')
    out = []
    for d in dramas:
        if not isinstance(d, dict):
            continue
        title = (d.get('title') or '').strip()
        if not title:
            continue
        out.append({
            'title': title,
            'premise': (d.get('premise') or '').strip(),
            'premise_ru': (d.get('premise_ru') or '').strip(),
            'genre': (d.get('genre') or '').strip(),
            'why_hook': (d.get('why_hook') or d.get('why') or '').strip(),
            'popularity': (d.get('popularity') or '').strip(),
        })
    return out


@app.route('/api/research-top-dramas', methods=['POST'])
def research_top_dramas():
    """Return a structured list of the current top short dramas for the picker."""
    data_in = request.json or {}
    genres = data_in.get('genres') or []
    idea_hint = (data_in.get('idea') or '').strip()
    era_dir = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    era_hint = 'a non-modern era/world is set — surface period/world-appropriate hits' if era_dir else ''
    try:
        dramas = _research_top_dramas(genres=genres, idea_hint=idea_hint, era_hint=era_hint)
        print(f'[top-dramas] returned {len(dramas)} dramas', flush=True)
        return jsonify({'dramas': dramas})
    except Exception as e:
        _log_event('WARN', 'top_dramas_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500



_IDEAS_SIMILAR_SYSTEM = """You convert a proven short-drama HIT into a ready-to-use series concept for our app, keeping the hit's premise EXACTLY — 1 to 1.

ABSOLUTE RULE: do NOT reinterpret, do NOT invent a new profession, company, setting or twist that is not in the given premise, and do NOT change who hides what or who discovers what. The concept must describe the SAME story as the hit, in the same shape. If the hit is «a wife discovers her humble husband is secretly a billionaire», the synopsis is exactly that — a wife discovers her ordinary husband is secretly a billionaire — NOT a janitor, NOT a taxi driver, NOT a new twist. Keep it that clean and that faithful.

The ONLY new things you create: an original short English title (do NOT reuse the hit's own title) and the genre / tone / target_audience / world_description fields. Voice natural and short, PG-13 register, never vulgar sexual verbs in any language. synopsis in English and synopsis_ru in natural Russian, BOTH stating the premise 1-to-1. Return ONLY valid JSON for the given schema."""


def _ideas_from_drama(drama, genres=None, era_hint='', writer_model=''):
    """Turn ONE chosen hit into a ready-to-use series concept that keeps its premise
    1-TO-1 (powers the per-card 'Сделать подобный сериал' button). No reinterpretation,
    no invented specifics — the output IS the chosen drama's idea, just retitled."""
    title   = (drama.get('title') or '').strip()
    premise = (drama.get('premise_ru') or drama.get('premise') or '').strip()
    genre   = (drama.get('genre') or '').strip()
    controls = []
    if genres:
        controls.append('Chosen genres (must fit): ' + ', '.join(genres))
    if era_hint:
        controls.append(era_hint)
    controls_s = ('\n'.join(controls) + '\n\n') if controls else ''
    prompt = (
        "The proven HIT to turn into a series, KEEPING ITS PREMISE 1-TO-1:\n"
        f"TITLE: {title}\nGENRE: {genre}\nPREMISE: {premise}\n\n"
        + controls_s
        + "Produce exactly 1 series concept whose premise is the SAME as this hit — identical setup, roles and "
          "reveal. Do NOT reinterpret it, do NOT invent a profession / company / setting / twist that is not in "
          "the premise above, do NOT change who hides what or who discovers what. Keep it as clean and general as "
          "the premise itself. Create only an original short title plus genre / tone / target_audience / "
          "world_description.\n\n"
        + "synopsis + synopsis_ru = 1-2 short sentences stating that EXACT premise (the same story as the hit). "
          "Set the \"style\" field to \"logline\".\n\n"
        + "JSON SAFETY: strict valid JSON; no double-quote character inside any value; no line breaks inside values.\n\n"
        + f"Return JSON matching this schema (a single idea inside the ideas array):\n{_IDEAS_SCHEMA_V2}"
    )
    ideas = loads_lenient(strip_json(llm_ask(writer_model, prompt, system=_IDEAS_SIMILAR_SYSTEM)))
    ideas = ideas.get('ideas', ideas) if isinstance(ideas, dict) else ideas
    if not isinstance(ideas, list) or not ideas:
        raise ValueError('ideas-from-drama: no list parsed')
    print(f'[ideas-from-drama] 1-to-1 concept from {title!r}', flush=True)
    return ideas


@app.route('/api/ideas-from-drama', methods=['POST'])
def ideas_from_drama():
    """Generate 5 concepts in the vein of ONE chosen top drama."""
    data_in = request.json or {}
    drama = data_in.get('drama') or {}
    if not isinstance(drama, dict) or not (drama.get('title') or drama.get('premise') or drama.get('premise_ru')):
        return jsonify({'error': 'no drama provided'}), 400
    genres = data_in.get('genres') or []
    writer_model = _resolve_writer_model(data_in)
    era_dir = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    era_hint = 'a non-modern era/world is set — keep every idea in that period/world' if era_dir else ''
    try:
        ideas = _ideas_from_drama(drama, genres=genres, era_hint=era_hint, writer_model=writer_model)
        return jsonify(ideas)
    except Exception as e:
        _log_event('WARN', 'ideas_from_drama_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500


_DRAMA_ANALYSIS_SCHEMA = """{
  "title": "The show title",
  "detailed_synopsis": "4-6 sentence rich English synopsis — detailed enough to build the first 5 episodes from",
  "detailed_synopsis_ru": "То же по-русски, подробно и живо",
  "main_characters": ["Name — who they are and what they want", "..."],
  "central_conflict": "1-2 sentences on the core conflict",
  "hook": "why viewers binge it",
  "setting": "where / when it takes place",
  "first_5_episodes": ["Серия 1: что происходит + клиффхэнгер", "Серия 2: ...", "Серия 3: ...", "Серия 4: ...", "Серия 5: ..."]
}"""


def _top_dramas_path():
    return DATA_ROOT / 'top_dramas.json'


def _load_top_dramas():
    try:
        p = _top_dramas_path()
        if p.exists():
            d = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(d, dict):
                return d
    except Exception as e:
        print(f'[top-dramas] load failed ({e.__class__.__name__})', flush=True)
    return {'scanned_at': '', 'genres': [], 'dramas': []}


def _save_top_dramas(store):
    try:
        _top_dramas_path().write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception as e:
        print(f'[top-dramas] save failed ({e.__class__.__name__})', flush=True)


def _drama_slug(title, i):
    import re as _re
    base = _re.sub(r'[^a-z0-9]+', '-', (title or '').lower()).strip('-')[:40]
    return base or f'drama-{i}'


def _analyze_drama(drama):
    """Deep two-step study of ONE real show -> detailed synopsis + first-5-episode
    setup (powers the per-card 'Изучить подробнее')."""
    title   = (drama.get('title') or '').strip()
    premise = (drama.get('premise_ru') or drama.get('premise') or '').strip()
    genre   = (drama.get('genre') or '').strip()
    research_prompt = (
        f'Study the vertical short drama "{title}" ({genre}) as thoroughly as possible.\n'
        f'Known premise: {premise}\n\n'
        'Search the live web for everything about THIS specific show: its full plot and premise, the main '
        'characters and what each of them wants, the central conflict, the hook that makes viewers binge, the '
        'setting, and how the opening episodes actually unfold. Be concrete and detailed.'
    )
    digest = ''
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=6)
        if digest and len(digest.strip()) > 40:
            print(f'[analyze-drama] web ok for {title!r}', flush=True)
        else:
            digest = ''
    except Exception as e:
        print(f'[analyze-drama] web failed ({e.__class__.__name__})', flush=True)
        digest = ''
    if not digest:
        digest = claude_ask_quality(research_prompt, system=_IDEAS_RESEARCH_SYSTEM) or ''
        print(f'[analyze-drama] fallback for {title!r}', flush=True)
    fmt_prompt = (
        f'Here is research about the short drama "{title}":\n\n{digest}\n\n'
        'Produce a DETAILED breakdown as STRICT JSON. detailed_synopsis must be rich (4-6 sentences) and '
        'concrete enough to build the SETUP across the first 5 episodes from it. first_5_episodes = exactly 5 '
        'entries, one per episode, each a concrete beat ending on a cliffhanger. Output ONLY the JSON, no prose, '
        f'and do NOT use the double-quote character inside any value:\n{_DRAMA_ANALYSIS_SCHEMA}'
    )
    raw = claude_ask_quality(fmt_prompt, system='You output ONLY strict valid JSON — no prose, no code fences, no commentary.') or ''
    data = loads_lenient(strip_json(raw))
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError('analyze-drama: no object parsed')
    eps = data.get('first_5_episodes') or []
    if not isinstance(eps, list):
        eps = []
    chars = data.get('main_characters') or []
    if not isinstance(chars, list):
        chars = []
    return {
        'title': (data.get('title') or title).strip(),
        'detailed_synopsis': (data.get('detailed_synopsis') or '').strip(),
        'detailed_synopsis_ru': (data.get('detailed_synopsis_ru') or '').strip(),
        'main_characters': [str(c).strip() for c in chars if str(c).strip()],
        'central_conflict': (data.get('central_conflict') or '').strip(),
        'hook': (data.get('hook') or '').strip(),
        'setting': (data.get('setting') or '').strip(),
        'first_5_episodes': [str(e).strip() for e in eps if str(e).strip()],
    }


@app.route('/api/top-dramas', methods=['GET'])
def top_dramas_get():
    """Return the persisted top-dramas board (survives page reload)."""
    return jsonify(_load_top_dramas())


@app.route('/api/top-dramas/scan', methods=['POST'])
def top_dramas_scan():
    """Re-scan the live market for top short dramas and persist the result."""
    data_in = request.json or {}
    genres = data_in.get('genres') or []
    era_dir = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    era_hint = 'a non-modern era/world is set — surface period/world-appropriate hits' if era_dir else ''
    try:
        dramas = _research_top_dramas(genres=genres, idea_hint=(data_in.get('idea') or '').strip(), era_hint=era_hint)
    except Exception as e:
        _log_event('WARN', 'top_dramas_scan_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
    for i, d in enumerate(dramas):
        d['id'] = _drama_slug(d.get('title'), i)
    store = {
        'scanned_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'genres': genres,
        'dramas': dramas,
    }
    _save_top_dramas(store)
    print(f'[top-dramas] scanned + saved {len(dramas)}', flush=True)
    return jsonify(store)


@app.route('/api/top-dramas/analyze', methods=['POST'])
def top_dramas_analyze():
    """Deep-analyze one drama (by stored id or by inline drama) and persist it."""
    data_in = request.json or {}
    did = (data_in.get('id') or '').strip()
    drama = data_in.get('drama') or {}
    store = _load_top_dramas()
    target = None
    if did:
        for d in store.get('dramas', []):
            if d.get('id') == did:
                target = d
                break
    if target is None and isinstance(drama, dict) and (drama.get('title') or drama.get('premise')):
        target = drama
    if not target:
        return jsonify({'error': 'drama not found'}), 404
    try:
        analysis = _analyze_drama(target)
    except Exception as e:
        _log_event('WARN', 'top_dramas_analyze_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
    if did:
        for d in store.get('dramas', []):
            if d.get('id') == did:
                d['analysis'] = analysis
                break
        _save_top_dramas(store)
    return jsonify({'analysis': analysis})


from sw.data.idea_seeds import (
    _IDEA_SETTINGS,
    _IDEA_TWISTS,
    _IDEA_TONES,
    _IDEA_PREMISE_STRUCTURES,
    _IDEA_PROTAG_ARCHETYPES,
    _IDEA_ANTAG_ARCHETYPES,
    _IDEA_AVOID_REPETITIVE_FRAMES,
)
# ─── ERA + WORLD SETTING ──────────────────────────────────────────────────
# Two new axes the user can pick BEFORE generating ideas. The keys come from
# the frontend selects (templates/index.html → #series-era / #series-world).
# Default is always 'modern' + 'realistic' (the most-used combo). For each
# non-default pick we inject a hard directive so EVERY generated idea is
# period/world-consistent (title, character names allowed by era, props,
# technology, social rules). 'custom' uses the user's free-text verbatim.
from sw.data.era_beats import (
    _ERA_CHOICES,
    _ERA_LABELS_RU,
    _WORLD_CHOICES,
    _WORLD_LABELS_RU,
    _BEAT_OPTIONS,
    _BEATS_BY_ID,
    _BEAT_MAX,
)
def _resolve_beats(tokens):
    """Map an ORDERED list of beat tokens (from the create-series modal) to
    ordered dicts {id, ru, beat}. A token is either a known catalog id or a
    free-text custom beat (passed through verbatim). Preserves order, drops
    blanks and exact duplicates, caps at _BEAT_MAX."""
    if not tokens:
        return []
    seen, out = set(), []
    for tok in tokens:
        if isinstance(tok, dict):
            tok = tok.get('id') or tok.get('ru') or tok.get('custom') or ''
        tok = (tok or '').strip()
        if not tok or tok.lower() in seen:
            continue
        seen.add(tok.lower())
        if tok in _BEATS_BY_ID:
            o = _BEATS_BY_ID[tok]
            out.append({'id': o['id'], 'ru': o['ru'], 'beat': o['beat']})
        else:
            # Custom free-text beat — the user's own hook moment.
            out.append({'id': '', 'ru': tok, 'beat': tok})
        if len(out) >= _BEAT_MAX:
            break
    return out


def _beats_ideas_block(tokens):
    """Directive for the 5-ideas / from-idea generators: build every concept so
    the opening arc unfolds through the chosen beats in this EXACT order.
    Returns '' when nothing was picked."""
    picks = _resolve_beats(tokens)
    if not picks:
        return ''
    lines = '\n'.join(f'  {i+1}. {p["ru"]} — {p["beat"]}' for i, p in enumerate(picks))
    first = picks[0]['ru']
    return (
        "━━━ КОНСТРУКТОР СЦЕНАРИЯ — ФУНДАМЕНТ КАЖДОЙ ИДЕИ (СТРОГИЙ ПОРЯДОК) ━━━\n"
        "The user has assembled an ORDERED skeleton of opening hook-beats. Build all 5 concepts so "
        "the story unfolds through these beats in THIS EXACT ORDER — beat 1 is (or directly triggers) "
        "the inciting incident, and each later beat follows in sequence as the opening arc escalates. "
        "Do NOT reorder them and do NOT resolve a later beat before an earlier one.\n"
        f"{lines}\n"
        f"Each synopsis must clearly set up beat 1 («{first}») as the opening hook and gesture at the "
        "escalation to come. Vary setting / protagonist / world across the 5, but every idea rides the "
        "SAME beat order. Honor any era/world/genre constraints above at the same time.\n\n"
    )


def _source_outline_episode_block(s, first_new_num, last_new_num):
    """If the series was created from a deep-analyzed top drama, emit the per-episode
    outline for episodes in [first_new_num, last_new_num], with a hard rename rule so
    the result is an ORIGINAL adaptation (own cast/details), not a copy of the source."""
    outline = s.get('source_episode_outline') or []
    if not isinstance(outline, list) or not outline:
        return ''
    lines = []
    for i, beat in enumerate(outline):
        ep = i + 1
        if not beat or ep < first_new_num or ep > last_new_num:
            continue
        lines.append(f"Эп.{ep}: {beat}")
    if not lines:
        return ''
    return (
        "\n📋 ПЛАН-ЗАВЯЗКА ПО СЕРИЯМ (адаптация успешной шорт-драммы — следуй сюжетным битам как ОСНОВЕ каждой серии):\n"
        + "\n".join(lines) + "\n"
        "⚠ ЭТО ОРИГИНАЛЬНАЯ АДАПТАЦИЯ, НЕ КОПИЯ:\n"
        "• Имена персонажей в плане выше — ПЛЕЙСХОЛДЕРЫ исходника. Используй ТОЛЬКО имена из РОСТЕРА ПЕРСОНАЖЕЙ этого сериала (или придумай свои) — НИКОГДА не переноси имена из плана.\n"
        "• Сохраняй сюжетные биты, повороты и клиффхэнгер каждой серии, но меняй мелкие конкретные детали (места, бренды, обстоятельства), чтобы это была своя история, а не пересказ.\n"
        "• Каждая из этих серий ОБЯЗАНА реализовать свой бит плана в правильном порядке.\n\n"
    )


def _series_beats_episode_block(s):
    """Block injected into episode generators so the opening episodes deliver the
    stored beat sequence IN ORDER, then hand off to free improvisation once the
    last beat has happened. Free-paced: 1-3 episodes per beat, writer decides.
    Returns '' when the series has no stored beat sequence."""
    picks = _resolve_beats(s.get('beat_sequence') or [])
    if not picks:
        return ''
    lines = '\n'.join(f'  {i+1}. {p["ru"]} — {p["beat"]}' for i, p in enumerate(picks))
    last = picks[-1]['ru']
    return (
        "\n━━━ КОНСТРУКТОР СЦЕНАРИЯ — КОСТЯК ОТКРЫВАЮЩИХ СЕРИЙ (СТРОГИЙ ПОРЯДОК) ━━━\n"
        "При создании сериала задана упорядоченная последовательность хук-нод. Открывающие серии "
        "ОБЯЗАНЫ проходить их строго в этом порядке:\n"
        f"{lines}\n"
        "ПРАВИЛА:\n"
        "• Иди по нодам ПО ПОРЯДКУ. Не переставляй, не пропускай, не отыгрывай позднюю ноду раньше ранней.\n"
        "• Темп свободный: на одну ноду может уйти 1-3 серии — полностью отыграй (заверши) текущую ноду, "
        "прежде чем переходить к следующей.\n"
        "• Смотри предыдущие серии: определи, какие ноды уже отыграны, и продолжай со следующей неотыгранной.\n"
        f"• Как только отыграна ПОСЛЕДНЯЯ нода («{last}») — костяк закончился: дальше пиши свободно, "
        "импровизируй как обычно (открытые линии, эмоциональные арки, неожиданные повороты).\n\n"
    )



# Map the create-series modal era/world picks to an asset-generation era_choice
# (a key in _ERA_GUIDES, or 'modern'). Returned value is stored on the series so
# character/portrait generation uses the right period WITHOUT re-asking the user
# via the confirmation banner. Returns None when the pick is ambiguous/custom —
# then we leave era_choice='auto' and the normal detection+banner flow applies.
def _modal_setting_to_era_choice(era_key, era_custom, world_key, world_custom, synopsis_text=''):
    era_key   = (era_key or 'modern').strip().lower()
    world_key = (world_key or 'realistic').strip().lower()
    # World axis dominates when non-realistic and maps cleanly to a guide.
    world_map = {'fantasy': 'fantasy', 'scifi': 'sci_fi', 'postapoc': 'post_apocalyptic'}
    if world_key in world_map:
        return world_map[world_key]
    # supernatural / dystopian worlds: clothing is usually modern-or-era-driven —
    # fall through to the era axis (no forced world guide).
    era_map = {
        'modern': 'modern', 'near_future': 'sci_fi', 'far_future': 'sci_fi',
        '1980s': '80s', '1950s': '1950s', '1920s': 'edwardian_20s',
        'victorian': 'victorian', 'medieval': 'medieval',
    }
    if era_key in era_map:
        return era_map[era_key]
    if era_key == 'ancient':
        # Antiquity is ambiguous (Egypt / Greece / Rome). Sniff the synopsis;
        # default to Rome (togas) which reads as generic antiquity.
        hay = (synopsis_text or '').lower()
        if any(k in hay for k in ('egypt', 'pharaoh', 'nile', 'египет', 'фараон', 'нил')):
            return 'ancient_egypt'
        if any(k in hay for k in ('greece', 'greek', 'sparta', 'athen', 'грец', 'спарт', 'афин')):
            return 'ancient_greece'
        return 'ancient_rome'
    if era_key == '__custom__':
        # Try to recognize the free text against the asset-era keyword table.
        hay = f"{era_custom} {world_custom}".lower()
        for era, kws in _ERA_KEYWORDS.items():
            if any(re.search(rf'\b{re.escape(k)}\b', hay, re.UNICODE) for k in kws):
                return era
        return None  # unknown custom → leave to auto-detect + banner
    return None

def _era_setting_block(era_key: str, era_custom: str, world_key: str, world_custom: str) -> str:
    """Build the period/world directive injected into idea generation. Returns
    '' for the default modern+realistic combo (no directive needed — the base
    prompts already assume that world). For any non-default pick, returns a
    HARD directive every idea must obey."""
    era_key   = (era_key or 'modern').strip()
    world_key = (world_key or 'realistic').strip()
    era_custom   = (era_custom or '').strip()
    world_custom = (world_custom or '').strip()

    era_desc = None
    if era_key == '__custom__' and era_custom:
        era_desc = f'CUSTOM ERA defined by the user: "{era_custom}". Honor it precisely — period-correct props, technology, clothing, social rules.'
    elif era_key in _ERA_CHOICES and era_key != 'modern':
        era_desc = _ERA_CHOICES[era_key]

    world_desc = None
    if world_key == '__custom__' and world_custom:
        world_desc = f'CUSTOM SETTING defined by the user: "{world_custom}". Build every idea inside this world.'
    elif world_key in _WORLD_CHOICES and world_key != 'realistic':
        world_desc = _WORLD_CHOICES[world_key]

    if not era_desc and not world_desc:
        return ''  # default modern + realistic — no directive needed

    lines = ['━━━ ERA & WORLD SETTING — MANDATORY FOR EVERY IDEA ━━━']
    if era_desc:
        lines.append(f'TIME PERIOD: {era_desc}')
    if world_desc:
        lines.append(f'WORLD TYPE: {world_desc}')
    lines.append(
        'EVERY one of the 5 ideas MUST be set in this period/world — no exceptions, no "modern day" slip-ups. '
        'Props, technology, clothing, professions, social rules, and the plot engine must all be period/world-correct. '
        'Character names must fit the era (no anachronistic names). '
        'The `world_description` field MUST open by establishing this era/setting explicitly, and `synopsis` / `synopsis_ru` must read as belonging to it. '
        'Do NOT let a banned modern device (smartphone, social media, DNA test, etc.) sneak in if the era predates it — '
        'translate the same beat into a period-correct equivalent (an overheard confession, a returning letter-bearer, a witness).\n'
    )
    return '\n'.join(lines) + '\n'


# ─── ANTI-MONOTONY: thriller cap + freshness directive ────────────────────
# The user reported (a) too many thrillers / psychological thrillers and
# (b) the 5 synopses feel too similar and not interesting. This block is
# injected into idea generation to force genre spread and punchier hooks.
_IDEA_ANTI_MONOTONY = (
    "━━━ GENRE SPREAD — HARD CAP ON THRILLER ━━━\n"
    "AT MOST 1 of the 5 ideas may be a thriller / psychological-thriller / suspense / crime-mystery. "
    "The OTHER 4 must each have a CLEARLY DIFFERENT primary genre — pick from: romance, "
    "betrayal/affair melodrama, revenge, Cinderella/rags-to-riches, found-family, comedy/dramedy, "
    "forbidden love, second-chance, scandal, family-secrets, power-struggle, coming-of-age. "
    "If you notice 2+ ideas drifting into 'dark / tense / someone is hiding a deadly secret / "
    "she's being watched' territory — rewrite all but one into a warmer, more emotional, or more "
    "romantic register. Variety of FEELING across the 5 is as important as variety of plot.\n\n"
    "━━━ ANTI-SAMENESS CHECK (the 5 must NOT feel interchangeable) ━━━\n"
    "Before output, read all 5 synopses as a set. If swapping two protagonists' names would make "
    "the synopses interchangeable — they are too similar; rewrite. Each idea must differ on AT "
    "LEAST THREE of: setting, era-flavor, primary emotion, who holds power, the central relationship, "
    "and the engine (love vs revenge vs survival vs mystery vs comedy). "
    "No 'boring' or generic premises — every synopsis must contain ONE concrete, surprising, "
    "specific detail that makes a viewer stop scrolling. Vague = rejected.\n\n"
)

# ─── ROMANCE / AFFAIR / INTIMACY — periodic, organic ──────────────────────
# The user noted that affairs, kisses, passion, betrayal-of-the-heart never
# happen. We want these to show up REGULARLY but organically (not forced into
# every idea). Stays within the video generator's content bounds: on-screen
# kissing / embracing / passion / charged tension are allowed; explicit sexual
# acts and nudity are NOT — intimacy beyond a kiss is implied off-screen
# (cut-to-black, morning-after). This directive shapes idea generation.
_IDEA_ROMANCE_DIRECTIVE = (
    "━━━ ROMANCE, DESIRE & BETRAYAL — BUILD THEM IN ━━━\n"
    "Short drama runs on the heart. Across the 5 ideas, romantic/sexual tension and betrayal of "
    "the heart should be present and VISIBLE — not sanitized away:\n"
    "  • At least 2-3 of the 5 ideas must carry a real romantic or desire-driven thread "
    "(attraction, a forbidden pull, a slow-burn, a marriage with real heat, a love triangle).\n"
    "  • At least 1 of the 5 should center on or prominently feature INFIDELITY / an AFFAIR — "
    "a cheating spouse, an emotional affair discovered, a partner caught with someone else, "
    "the 'other woman/man' POV, or a marriage cracking from a betrayal of the heart. "
    "This is a core melodrama engine that has been missing — use it.\n"
    "  • Make passion concrete: a stolen kiss, a charged near-miss, a confrontation about a "
    "betrayal, a one-night entanglement with consequences. These belong in the world_description "
    "and synopsis where the premise calls for them.\n"
    "  • CONTENT BOUND: kissing, embracing, passion, attraction and affairs are all fair game on "
    "screen. Explicit sexual acts / nudity are NOT depicted — intimacy beyond a kiss is implied "
    "(a closing door, a morning-after). Write to that line, don't write past it.\n\n"
)

# ─── ENGINE-FAMILY SPREAD — variety by round-robin, NOT by banning ────────
# The user is sick of the «I scrub floors» / «hired as a nanny» / secret-heiress
# / fake-marriage sameness, but does NOT want those tropes banned — they want
# the 5 ideas to come from DIFFERENT families so any one trope appears at most
# once and naturally dissolves into a varied batch.
_IDEA_FAMILY_SPREAD = (
    "━━━ ENGINE VARIETY — EACH OF THE 5 FROM A DIFFERENT FAMILY ━━━\n"
    "Assign each of the 5 ideas to a DIFFERENT story-engine family. Use each family AT MOST ONCE "
    "so no single trope dominates the batch:\n"
    "  A) service-job + hidden truth (maid / nanny / janitor / waitress / driver whose real "
    "identity or power no one knows)\n"
    "  B) fake / contract / substitute / arranged marriage\n"
    "  C) revenge or comeback after being wronged / fall-from-grace\n"
    "  D) affair / infidelity / forbidden desire / love triangle\n"
    "  E) survival / trapped-together / disaster / pressure-cooker\n"
    "  F) mystery / single case / whodunit / something doesn't add up\n"
    "  G) found-family / unlikely alliance forming\n"
    "  H) rivalry / power struggle / hostile takeover / sabotage from within\n"
    "  I) second-chance / reunion / a presumed-dead person returns\n"
    "  J) identity reveal / body-or-life swap / mistaken for someone else\n"
    "RULE: pick 5 DIFFERENT families. Families A and B (service-job-secret and the marriage "
    "tropes) are the MOST overused — together they may appear AT MOST ONCE total across the 5. "
    "Never open more than one synopsis with a menial-job-secret setup, and never start a Russian "
    "synopsis with «мою полы» / «устроилась няней» / «вышла замуж за». The remaining ideas must "
    "come from the fresher families (C–J). (If the user selected specific genres, keep the genre "
    "but still vary the family within it.)\n\n"
)

# ═══════════════════════════════════════════════════════════════════════════
# SETTING-SPECIFIC PREMISE POOLS
# ═══════════════════════════════════════════════════════════════════════════
# Distilled from research into REAL hit vertical short-dramas (ReelShort,
# DramaBox, GoodShort, ShortMax, Chinese 短剧). Each entry is a concrete,
# build-ready story ENGINE — not a vague theme. When the user picks a
# non-default era/world, we sample from the matching pool and inject these as
# POSITIVE seeds, instead of leaving the model to lazily reskin its modern
# defaults (which produced "moping floors / hired as a nanny" sameness for
# every setting). Variety here is the whole point — they are deliberately
# different from each other in protagonist, injustice, relationship, and twist.
# ─────────────────────────────────────────────────────────────────────────

from sw.data.premise_pools import (
    _POOL_SUPERNATURAL,
    _POOL_FANTASY,
    _POOL_SCIFI,
    _POOL_POSTAPOC,
    _POOL_DYSTOPIAN,
    _POOL_ANCIENT_DYNASTIC,
    _POOL_MEDIEVAL,
    _POOL_VICTORIAN,
    _POOL_1920S,
    _POOL_1950S,
    _POOL_PERIOD_GENERAL,
    _POOL_MODERN_EXTRA,
    _CUSTOM_SETTING_KEYWORDS,
    _keyword_pool,
    _resolve_setting_premise_pool,
    _setting_premise_seeds_block,
)
@app.route('/api/story-beats', methods=['GET'])
def list_story_beats():
    """Catalog of curated hook-beats (ноды) for the create-series scenario
    constructor. Returns id + RU chip label + RU group section (the English
    `beat` directive stays server-side — only used inside prompts)."""
    return jsonify([
        {'id': o['id'], 'ru': o['ru'], 'group': o['group']}
        for o in _BEAT_OPTIONS
    ])


@app.route('/api/generate-series-ideas', methods=['POST'])
def generate_series_ideas():
    data_in = request.json or {}
    writer_model = _resolve_writer_model(data_in)
    genres = data_in.get('genres') or []
    idea_hint = (data_in.get('idea') or '').strip()  # optional free-text from the idea input field
    # Format mode: 'short_drama' (TikTok addictive serial) or 'instagram_series' (sitcom-style standalone).
    format_mode = (data_in.get('format_mode') or 'short_drama').strip().lower()
    if format_mode not in _FORMAT_MODE_RULES:
        format_mode = 'short_drama'
    format_block = _format_mode_block(format_mode)
    format_ideas_directive = _FORMAT_MODE_RULES[format_mode]['ideas_directive']
    # Free-text avoid-list: user-curated tropes/words that must NOT appear in
    # any of the 5 ideas (titles, synopses, character roles). Comma-separated
    # or newline-separated. E.g. «близнецы, пастор, billionaire CEO».
    avoid_raw = (data_in.get('avoid') or '').strip()

    # Era + world setting (picked in the create-series modal before generating).
    # Defaults: modern + realistic → _era_setting_block returns '' (no directive).
    era_setting_directive = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    # Scenario constructor: ORDERED hook-beats (ноды) picked in the modal — the
    # 5 ideas must unfold through them in this exact order.
    beats_directive = _beats_ideas_block(data_in.get('beats') or [])

    # Detect non-standard format from the idea hint to avoid injecting human-drama seeds
    _idea_lower = idea_hint.lower()
    _nonstandard_format = idea_hint and any(w in _idea_lower for w in [
        'мультик', 'мульт', 'анимац', 'cartoon', 'animated', 'anime', 'аниме',
        'pixar', 'пиксар', 'furry', 'фури', 'фурри', 'fantasy', 'фэнтези',
        'sci-fi', 'science fiction', 'космос', 'space', 'horror', 'хоррор',
        'superhero', 'супергерой', 'игра', 'game', 'видеоигр',
    ])

    if genres:
        genre_rule = (
            f"STRICT GENRE REQUIREMENT: Every single one of the 5 ideas MUST incorporate ALL of these genres: {', '.join(genres)}.\n"
            f"This is not optional. Each idea must feel like a genuine mix of {' + '.join(genres)}.\n"
            "If an idea does not fit ALL selected genres, replace it — do not submit it.\n\n"
        )
    else:
        genre_rule = ""

    # Build avoid-rule. Split user's text into tokens, normalise, and pass as
    # a HARD ban list. Claude is told to reject ideas that contain any of
    # these words/concepts and regenerate.
    avoid_rule = ""
    if avoid_raw:
        # Accept commas, newlines, semicolons, slashes. Drop empties + dedup.
        tokens = [t.strip() for t in re.split(r'[,;\n/]+', avoid_raw) if t.strip()]
        if tokens:
            ban_list = ', '.join(f'«{t}»' for t in tokens[:40])
            avoid_rule = (
                f"HARD BAN LIST (пользователь устал от этих троп — НИ ОДНА из 5 идей НЕ должна содержать эти концепты):\n"
                f"{ban_list}\n"
                "Проверяй title, synopsis_ru, и все ключевые роли/архетипы каждой идеи. Если идея содержит "
                "что-то из бан-списка (даже если только в подтексте архетипа) — выбрось её и сгенери замену.\n"
                "Распознавай синонимы и переводы: если бан = «pastor», то «cleric / priest / preacher / "
                "religious leader / cult founder» тоже под запретом. Если бан = «близнецы», то «twin / "
                "doppelganger / mirror sibling / identical» тоже.\n\n"
            )

    # Six-axis sampling — produces ~ millions of unique combos so back-to-back
    # batches don't repeat. Each idea gets ONE pick from every axis.
    seed_settings   = random.sample(_IDEA_SETTINGS, 5)
    seed_twists     = random.sample(_IDEA_TWISTS, 5)
    seed_tones      = random.sample(_IDEA_TONES, 5)
    seed_premises   = random.sample(_IDEA_PREMISE_STRUCTURES, 5)
    seed_protag     = random.sample(_IDEA_PROTAG_ARCHETYPES, 5)
    seed_antag      = random.sample(_IDEA_ANTAG_ARCHETYPES, 5)
    constraints = '\n'.join(
        f'{i+1}. Setting: {seed_settings[i]}\n'
        f'   Premise structure: {seed_premises[i]}\n'
        f'   Twist element: {seed_twists[i]}\n'
        f'   Protagonist archetype: {seed_protag[i]}\n'
        f'   Antagonist archetype: {seed_antag[i]}\n'
        f'   Mood: {seed_tones[i][0]}'
        for i in range(5)
    )
    format_convention = _get_format_convention(idea_hint)
    # Whether a non-default era/world was picked (post-apoc, 1920s, fantasy…).
    # When it is, the concrete modern-drama seeds (corporate boardroom, forensic
    # accountant, podcast confession, tech billionaire…) actively FIGHT the era
    # directive: they are far more vivid/specific than the abstract "make it
    # post-apocalyptic" line, so the model anchors on them and drifts straight
    # back into modern realism. So we suppress those seeds and keep only mood,
    # exactly like the non-standard-format path does.
    _era_world_override = bool(era_setting_directive)
    # For non-standard formats (animation, furry, sci-fi, etc.) the human-drama seeds
    # are irrelevant — replace them with just mood seeds to avoid archetype contamination.
    if _nonstandard_format:
        mood_seeds = '\n'.join(f'{i+1}. Mood: {seed_tones[i][0]}' for i in range(5))
        seeds_section = (
            f"USER FORMAT BRIEF (PRIMARY — все 5 идей ОБЯЗАНЫ соответствовать этому формату): \"{idea_hint}\"\n\n"
            + (format_convention if format_convention else
               "ВАЖНО: формат пользователя определяет всё — жанр, сеттинг, архетипы персонажей. "
               "НЕ используй человеческие drama-архетипы (CEO, горничная, мачеха, миллиардер) если только "
               "идея пользователя явно не включает людей. Придумывай архетипы исходя из заданного формата.\n")
            + "\nMOOD SEEDS (один на идею):\n"
            f"{mood_seeds}\n\n"
        )
    elif _era_world_override:
        # Era/world picked → the modern-drama seeds would contaminate. Replace
        # them with CURATED era/world premise seeds (real hit-drama engines that
        # actually fit the period/world) + mood, plus an instruction to invent
        # era-appropriate settings/professions/antagonists rather than reskin.
        mood_seeds = '\n'.join(f'{i+1}. Mood: {seed_tones[i][0]}' for i in range(5))
        premise_seeds = _setting_premise_seeds_block(
            data_in.get('era'), data_in.get('era_custom'),
            data_in.get('world_setting'), data_in.get('world_custom'),
        )
        seeds_section = (
            (f"USER IDEA HINT (учти при генерации): \"{idea_hint}\"\n\n" if idea_hint else "")
            + "⚠ The ERA & WORLD SETTING above is the PRIMARY constraint — it overrides everything else.\n"
            "Do NOT reuse stock modern-day short-drama settings or roles (corporate boardroom, CEO, "
            "billionaire, nanny, hospital ER, podcast, social-media scandal, forensic accountant, etc.) "
            "unless they genuinely exist in the chosen era/world. INVENT settings, professions, social "
            "structures, props and antagonists that BELONG to that period/world. Keep the same emotional "
            "DNA of short drama (humiliation, betrayal, power flip, forbidden love, revenge) but dress every "
            "beat in era/world-correct clothing.\n\n"
            + premise_seeds
            + "MOOD SEEDS (one per idea — pair each with a different premise seed above):\n"
            f"{mood_seeds}\n\n"
        )
    else:
        seeds_section = (
            (f"USER IDEA HINT (учти при генерации): \"{idea_hint}\"\n\n" if idea_hint else "")
            + "INSPIRATION SEEDS (one per idea — these are LIGHT prompts, pick what's useful, "
            "ignore what overcomplicates):\n"
            f"{constraints}\n\n"
        )

    # ── IDEAS V2: 3-stage pipeline (research → 5 diverse ideas → logic-check) ──
    # Reuses every context block computed above so all user controls still steer.
    # On any failure, falls through to the legacy single mega-prompt below.
    if IDEAS_V2:
        _user_controls = (
            format_block
            + f"FORMAT-SPECIFIC DIRECTIVE: {format_ideas_directive}\n\n"
            + era_setting_directive
            + beats_directive
            + genre_rule
            + avoid_rule
            + seeds_section
        )
        try:
            _ideas = _run_ideas_pipeline_v2(
                writer_model=writer_model,
                user_controls=_user_controls,
                genres=genres,
                idea_hint=idea_hint,
                era_world_override=_era_world_override,
                avoid_rule=avoid_rule,
            )
            try:
                _save_ideas_history(_ideas)
            except Exception:
                pass
            return jsonify(_ideas)
        except Exception as _e:
            print(f'[ideas] V2 pipeline failed -> legacy single-call fallback: {_e}', flush=True)

    prompt = (
        "Generate exactly 5 series concepts for short-form vertical video.\n\n"
        + format_block
        + f"FORMAT-SPECIFIC DIRECTIVE: {format_ideas_directive}\n\n"
        + era_setting_directive
        + beats_directive
        # Anti-monotony (thriller cap + sameness check) and the romance/affair
        # directive only apply in free-creative mode. When the user has
        # explicitly picked genres OR a beat sequence they're steering on purpose —
        # don't override (capping thrillers / forcing variety would fight a
        # deliberate "Thriller" pick or the chosen ordered beat skeleton).
        + (_IDEA_ANTI_MONOTONY if (not _nonstandard_format and not genres and not beats_directive) else "")
        + (_IDEA_ROMANCE_DIRECTIVE if (not _nonstandard_format and not genres and not beats_directive) else "")
        # Engine-family spread enforces variety (each of 5 from a different
        # family, overused tropes capped at 1) WITHOUT banning anything. Applies
        # even when genres are picked — it varies the family within the genre.
        # Suppressed when a beat sequence is picked: forcing 5 different families
        # would contradict "all 5 ride the SAME ordered beat skeleton".
        + (_IDEA_FAMILY_SPREAD if (not _nonstandard_format and not beats_directive) else "")
        + genre_rule
        + avoid_rule
        + seeds_section
        + "How to use the seeds:\n"
        "- Treat each row as 6 OPTIONAL ingredients. Pick 2-3 that combine cleanly into ONE simple premise.\n"
        "- IGNORE seeds that would force complexity. Better a clean «setting + protagonist + 1 twist» than\n"
        "  a Frankenstein with every seed jammed in.\n"
        "- The synopsis must read like one clear hook (see SYNOPSIS RULES + COMPLEXITY HARD CAPS in system).\n"
        "- DO NOT stack the seeds into one nested backstory. Simpler is always better.\n\n"
        "Diversity check before output (mandatory):\n"
        "- No two ideas may share the same setting category (urban-elite vs blue-collar vs institutional vs road/island vs creative-niche).\n"
        "- No two ideas may use the same TITLE TEMPLATE — pick from different rows of the title rules above.\n"
        "- AT LEAST 2 of the 5 must NOT center primarily on a romantic relationship as the engine — pick revenge, mystery, found-family, custody, or comeback as the spine.\n"
        "- AT LEAST 1 idea must use a non-billionaire/non-CEO/non-mafia antagonist.\n"
        "- If two ideas feel like reflavoured copies — rewrite one with a different premise structure.\n\n"
        "SIMPLICITY CHECK before output (mandatory — re-read each synopsis):\n"
        "- Could you describe the show in ONE sentence to a friend? If no, it's too tangled — strip back.\n"
        "- Count named characters per synopsis. Strictly ≤ 3. More = simplify.\n"
        "- Count «turns out / actually / and also» phrases. Strictly ≤ 1 per synopsis.\n"
        "- If the protagonist has 2+ jobs/roles stacked («ex-cartel-accountant in witness protection who is\n"
        "  also a rival surgeon») — strip to ONE identity.\n"
        "- Count em-dashes («—»). ≤ 2 per synopsis. More = stacking.\n\n"
        "Rules:\n"
        "- All titles and English fields must be in English\n"
        "- synopsis_ru must be in Russian — short (2-3 sentences), vivid, makes you want to watch\n"
        "- No generic titles. No predictable plots. Surprise me — but stay SIMPLE.\n\n"
        # FINAL era/world reinforcement — placed last on purpose: later
        # instructions dominate, and this is the rule that kept getting ignored
        # (post-apoc / 1920s ideas drifting back to plain modern-day synopses).
        + (
            "🚨 FINAL CHECK — ERA & WORLD (do this LAST, before returning JSON):\n"
            "Re-read all 5 synopses. ANY synopsis that reads like a present-day realistic story — "
            "or that contains a prop/role/event impossible in the chosen era/world (smartphone, social "
            "media, DNA test, modern corporation, etc. when the era predates them; or a mundane modern "
            "setting when a fantasy/post-apocalyptic/sci-fi world was chosen) — is WRONG. Rewrite it from "
            "scratch so the era/world is unmistakable in the first sentence. The chosen ERA & WORLD SETTING "
            "is non-negotiable and applies to ALL 5 ideas.\n\n"
            if _era_world_override else ""
        )
        + f"Return JSON matching this schema:\n{_IDEAS_SCHEMA}"
    )
    try:
        data = json.loads(strip_json(llm_ask(writer_model, prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data.get('ideas', data))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


from sw.data.format_modes import (
    _FROM_IDEA_ANGLES,
    _FORMAT_CONVENTIONS,
    _get_format_convention,
    _FORMAT_MODE_RULES,
)
from sw.story_prompts import (
    _format_mode_of,
    _format_mode_block,
)
@app.route('/api/generate-series-from-idea', methods=['POST'])
def generate_series_from_idea():
    data_in = request.json or {}
    writer_model = _resolve_writer_model(data_in)
    idea   = data_in.get('idea', '').strip()
    genres = data_in.get('genres') or []
    if not idea:
        return jsonify({'error': 'Опиши идею'}), 400
    # Format mode: 'short_drama' (default) or 'instagram_series'
    format_mode = (data_in.get('format_mode') or 'short_drama').strip().lower()
    if format_mode not in _FORMAT_MODE_RULES:
        format_mode = 'short_drama'
    format_block = _format_mode_block(format_mode)
    format_ideas_directive = _FORMAT_MODE_RULES[format_mode]['ideas_directive']
    era_setting_directive = _era_setting_block(
        data_in.get('era'), data_in.get('era_custom'),
        data_in.get('world_setting'), data_in.get('world_custom'),
    )
    beats_directive = _beats_ideas_block(data_in.get('beats') or [])
    angle    = random.choice(_FROM_IDEA_ANGLES)
    setting  = random.choice(_IDEA_SETTINGS)
    twist    = random.choice(_IDEA_TWISTS)
    premise  = random.choice(_IDEA_PREMISE_STRUCTURES)
    protag   = random.choice(_IDEA_PROTAG_ARCHETYPES)
    antag    = random.choice(_IDEA_ANTAG_ARCHETYPES)
    tone     = random.choice(_IDEA_TONES)[0]
    genre_rule = (
        f"The series MUST be a blend of these genres: {', '.join(genres)}. "
        "Every element of the concept should feel like it belongs in all of them simultaneously. "
    ) if genres else ""
    # Detect if the idea specifies a non-standard format (animation, anime, furry, etc.)
    # so we know whether the human-drama seeds are relevant or should be skipped.
    _idea_lower = idea.lower()
    _nonstandard_format = any(w in _idea_lower for w in [
        'мультик', 'мульт', 'анимац', 'cartoon', 'animated', 'anime', 'аниме',
        'pixar', 'пиксар', 'furry', 'фури', 'фурри', 'fantasy', 'фэнтези',
        'sci-fi', 'science fiction', 'космос', 'space', 'horror', 'хоррор',
        'superhero', 'супергерой', 'игра', 'game', 'видеоигр',
    ])
    format_convention = _get_format_convention(idea)
    # Era/world picked → suppress the modern-drama seeds (Setting/Protagonist/
    # Antagonist) which otherwise drag the concept back to present-day realism.
    _era_world_override = bool(era_setting_directive)

    if _nonstandard_format:
        # Seeds are for human drama archetypes — skip them entirely when format is non-standard.
        # Let the idea brief + genre convention dominate completely.
        seeds_block = (
            f"Creative angle to explore: {angle}\n"
            f"Mood / emotional register: {tone}\n\n"
            + (format_convention if format_convention else
               "NOTE: The user's idea defines a specific format (animation, fantasy, sci-fi, etc.). "
               "Do NOT force human-drama archetypes (CEO, billionaire, maid, stepmother, etc.) into this concept. "
               "Character archetypes, setting, and premise must match the user's stated format.\n")
            + "\n"
        )
    elif _era_world_override:
        # Drop the modern Setting/Protagonist/Antagonist seeds — give curated
        # era/world premise seeds + angle/mood, and instruct the model to invent
        # era/world-appropriate everything rather than reskin a modern story.
        premise_seeds = _setting_premise_seeds_block(
            data_in.get('era'), data_in.get('era_custom'),
            data_in.get('world_setting'), data_in.get('world_custom'),
            n=4,
        )
        seeds_block = (
            f"Creative angle to explore: {angle}\n"
            f"Mood / emotional register: {tone}\n\n"
            "⚠ The ERA & WORLD SETTING above is the PRIMARY constraint. Do NOT reuse stock modern-day "
            "settings or roles (CEO, billionaire, nanny, hospital, podcast, social-media scandal) unless they "
            "genuinely exist in that era/world. INVENT settings, professions, props and antagonists that BELONG "
            "to the chosen period/world, while keeping the emotional DNA of short drama.\n\n"
            + premise_seeds
        )
    else:
        seeds_block = (
            f"Creative angle to explore: {angle}\n\n"
            f"Optional inspiration seeds — use 2-3 that fit cleanly, discard the rest:\n"
            f"  • Setting: {setting}\n"
            f"  • Premise structure: {premise}\n"
            f"  • Twist element: {twist}\n"
            f"  • Protagonist archetype: {protag}\n"
            f"  • Antagonist archetype: {antag}\n"
            f"  • Mood: {tone}\n\n"
        )

    # Adjust synopsis length per format
    _synopsis_len_rule = (
        "synopsis should be 2-3 plain-language sentences (Instagram series format — see rules above)."
        if format_mode == 'instagram_series' else
        "synopsis should be 3-5 sentences summarizing the full series arc."
    )
    prompt = (
        format_block
        + f"FORMAT-SPECIFIC DIRECTIVE: {format_ideas_directive}\n\n"
        + era_setting_directive
        + beats_directive
        + f"USER'S IDEA (PRIMARY BRIEF — honor this above everything else): \"{idea}\"\n\n"
        + genre_rule
        + seeds_block
        + "RULES:\n"
        "- The user's idea is the brief. Seeds and genre tags are SECONDARY creative pressure — "
        "discard any seed that conflicts with what the user described.\n"
        "- The format (animation vs live-action drama vs thriller vs fantasy) must match the user's idea.\n"
        "- TITLE must follow the format-mode title rule above and stay SHORT — 3-7 words, hard cap 8. "
        "One sharp hook, not the whole plot crammed into a run-on sentence.\n"
        "- Avoid generic plots. Give it a title that sets a clear visual expectation.\n\n"
        + (
            "🚨 FINAL CHECK — ERA & WORLD: the concept MUST be unmistakably set in the chosen era/world "
            "(see ERA & WORLD SETTING above), established in the first sentence of world_description and "
            "synopsis. No present-day-realism drift, no anachronistic props.\n\n"
            if _era_world_override else ""
        )
        + "Create a UNIQUE series concept for short-form vertical video that feels fresh and specific. "
        "Return JSON with exactly these fields: "
        "title, genre, tone, target_audience, world_description, synopsis. "
        f"{_synopsis_len_rule}"
    )
    try:
        data = json.loads(strip_json(llm_ask(writer_model, prompt, system=_IDEAS_SYSTEM)))
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

