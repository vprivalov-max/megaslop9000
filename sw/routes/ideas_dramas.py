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
    # lazy: forward call into the beats/era section (split module)
    from sw.routes.ideas_generate import _era_setting_block
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
    # lazy: forward call into the beats/era section (split module)
    from sw.routes.ideas_generate import _era_setting_block
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
    # lazy: forward call into the beats/era section (split module)
    from sw.routes.ideas_generate import _era_setting_block
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
