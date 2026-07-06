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


def _norm_drama_title(title):
    """Normalized key for de-duping / exclusion (case + punctuation insensitive)."""
    import re as _re
    return _re.sub(r'[^a-z0-9]+', '', (title or '').lower())


def _exclude_titles_block(exclude_titles):
    """Prompt block that tells the researcher to AVOID titles already surfaced,
    so repeated scans push past the same evergreen hits and find fresh ones."""
    titles = [t for t in (exclude_titles or []) if str(t).strip()]
    if not titles:
        return ''
    # Cap the injected list so the prompt stays lean; the newest exclusions
    # (end of the list) matter most, so keep the tail.
    shown = titles[-120:]
    lines = '\n'.join(f'- {t}' for t in shown)
    return (
        "\n\nALREADY-FOUND — DO NOT RETURN ANY OF THESE (hard exclusion): earlier scans already surfaced the "
        "shows below. The user has seen them. Return only DIFFERENT, fresh titles that are NOT in this list "
        "(and not mere renamings/sequels of them). Dig deeper into the charts, other genres, newer releases and "
        "rising titles to find genuinely NEW shows:\n"
        f"{lines}\n"
    )


def _research_top_dramas(genres=None, idea_hint='', era_hint='', n=10, exclude_titles=None):
    """Live-web research of the ACTUAL top-performing short dramas, returned as a
    structured list (powers the 'Find top short dramas' button). Genre / hint /
    era filter the search when given; otherwise the overall best across genres.
    `exclude_titles` are shows already surfaced in prior scans — the researcher is
    told to skip them and find NEW ones, so the board keeps growing instead of
    repeating the same evergreen hits."""
    bits = []
    if genres:
        bits.append('genres: ' + ', '.join(genres))
    if idea_hint:
        bits.append('creator hint: ' + idea_hint)
    if era_hint:
        bits.append(era_hint)
    brief = '; '.join(bits) if bits else 'the overall most popular, highest-rated and most-viewed vertical short dramas right now, across all genres'
    exclude_block = _exclude_titles_block(exclude_titles)
    # Step 1 — live web research returns a PROSE list of real shows (the search
    # tool wraps output in commentary, so we do NOT ask it for JSON here).
    research_prompt = (
        "Find the BEST-performing vertical short dramas right now for this brief:\n"
        f"{brief}\n\n"
        "Search the live web for current TOP-RATED and MOST-VIEWED titles across ReelShort, DramaBox, "
        "GoodShort, ShortMax, NetShort and viral verticals on YouTube / TikTok. List 8-12 of the strongest "
        "REAL shows; for each give: title, one-line premise, genre/tone, why it hooks viewers, and any "
        "popularity signal (views / rating / chart position) you can find. Be concrete with real titles."
        + exclude_block
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
        + exclude_block
    )
    raw = claude_ask_quality(fmt_prompt, system='You output ONLY strict valid JSON — no prose, no code fences, no commentary.') or ''
    data = loads_lenient(strip_json(raw))
    dramas = data.get('dramas', data) if isinstance(data, dict) else data
    if not isinstance(dramas, list):
        raise ValueError('top-dramas: no list parsed')
    # Belt-and-suspenders: drop anything already seen even if the model slipped it back in.
    _excluded = {_norm_drama_title(t) for t in (exclude_titles or []) if str(t).strip()}
    out = []
    for d in dramas:
        if not isinstance(d, dict):
            continue
        title = (d.get('title') or '').strip()
        if not title:
            continue
        if _norm_drama_title(title) in _excluded:
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
    store = _load_top_dramas()
    exclude = _seen_titles_from_store(store)
    try:
        dramas = _research_top_dramas(genres=genres, idea_hint=idea_hint, era_hint=era_hint, exclude_titles=exclude)
        print(f'[top-dramas] returned {len(dramas)} dramas (excluded {len(exclude)} seen)', flush=True)
        # Remember what we just showed so the next research call rotates onward.
        seen = list(store.get('seen_titles') or [])
        seen_keys = {_norm_drama_title(x) for x in seen}
        for d in dramas:
            t = d.get('title')
            if t and _norm_drama_title(t) not in seen_keys:
                seen.append(t)
                seen_keys.add(_norm_drama_title(t))
        store['seen_titles'] = seen[-_SEEN_TITLES_MAX:]
        _save_top_dramas(store)
        return jsonify({'dramas': dramas})
    except Exception as e:
        _log_event('WARN', 'top_dramas_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500



_IDEAS_SIMILAR_SYSTEM = """You convert a proven short-drama HIT into a ready-to-use series concept for our app, keeping the hit's premise EXACTLY — 1 to 1.

ABSOLUTE RULE: do NOT reinterpret, do NOT invent a new profession, company, setting or twist that is not in the given premise, and do NOT change who hides what or who discovers what. The concept must describe the SAME story as the hit, in the same shape. If the hit is «a wife discovers her humble husband is secretly a billionaire», the synopsis is exactly that — a wife discovers her ordinary husband is secretly a billionaire — NOT a janitor, NOT a taxi driver, NOT a new twist. Keep it that clean and that faithful.

The ONLY new things you create: an original short English title (do NOT reuse the hit's own title) and the genre / tone / target_audience / world_description fields. Voice natural and short, PG-13 register, never vulgar sexual verbs in any language. synopsis in English and synopsis_ru in natural Russian, BOTH stating the premise 1-to-1. Return ONLY valid JSON for the given schema."""


_IDEAS_ASIAN_SYSTEM = """You convert a proven short-drama HIT into a ready-to-use series concept, KEEPING THE MAIN STORY 1-TO-1 while RE-SETTING it into an East-Asian world with an all-Asian cast.

WHAT STAYS IDENTICAL (the plot spine — do NOT change): the setup, the roles and their relationships, who hides what, who discovers what, the central conflict, the reveal, and the escalation. The story that happens is the SAME story as the hit — same beats, same twist. If the hit is «a wife discovers her humble husband is secretly a billionaire», the recast is still exactly that — a wife discovers her ordinary husband is secretly a billionaire — never a new profession, never a new twist.

WHAT YOU RE-SKIN (the cultural surface only): relocate the story into ONE coherent East-Asian setting (e.g. China, South Korea, or Japan — pick the single one that best fits the genre and commit to it fully). EVERY character is ethnically Asian, with natural Asian names appropriate to the chosen country. The world, locations, customs, honorifics, family/social dynamics, food, festivals and texture are authentically Asian. Weave in culturally Asian themes that fit the same plot (filial duty, face/honor, family hierarchy, arranged-marriage pressure, chaebol/dynasty power, etc.) WITHOUT altering the plot spine.

The ONLY new things you create: an original short English title (do NOT reuse the hit's own title), plus genre / tone / target_audience / world_description. The `world_description` MUST open by stating explicitly that the series is set in [the chosen Asian country] and that ALL characters are Asian, so downstream generation casts them correctly. Voice natural and short, PG-13 register, never vulgar sexual verbs in any language. synopsis in English and synopsis_ru in natural Russian, BOTH telling the SAME story as the hit, now in the Asian setting with Asian character names. Return ONLY valid JSON for the given schema."""


def _ideas_from_drama(drama, genres=None, era_hint='', writer_model='', asian_recast=False):
    """Turn ONE chosen hit into a ready-to-use series concept that keeps its premise
    1-TO-1 (powers the per-card 'Сделать подобный сериал' button). No reinterpretation,
    no invented specifics — the output IS the chosen drama's idea, just retitled.
    When `asian_recast` is set: the PLOT stays 1-to-1 but the story is re-set in an
    East-Asian world with an all-Asian cast (характеры-азиаты, азиатская тематика)."""
    title   = (drama.get('title') or '').strip()
    premise = (drama.get('premise_ru') or drama.get('premise') or '').strip()
    genre   = (drama.get('genre') or '').strip()
    controls = []
    if genres:
        controls.append('Chosen genres (must fit): ' + ', '.join(genres))
    if era_hint:
        controls.append(era_hint)
    controls_s = ('\n'.join(controls) + '\n\n') if controls else ''
    if asian_recast:
        prompt = (
            "The proven HIT to turn into a series, KEEPING ITS PLOT 1-TO-1 but RE-SET in an East-Asian world:\n"
            f"TITLE: {title}\nGENRE: {genre}\nPREMISE: {premise}\n\n"
            + controls_s
            + "Produce exactly 1 series concept that tells the SAME story as this hit — identical setup, roles, "
              "conflict, who-hides-what, reveal and escalation — but relocated into ONE coherent East-Asian setting "
              "(China / South Korea / Japan — pick one and commit). EVERY character is ethnically Asian with natural "
              "Asian names. Keep the plot spine untouched; only the cultural surface (setting, names, customs, "
              "themes) becomes Asian. Do NOT invent a new profession / twist / reveal that is not in the premise.\n\n"
            + "The world_description MUST open by stating the series is set in [chosen Asian country] and that ALL "
              "characters are Asian. synopsis + synopsis_ru = 1-2 short sentences telling that EXACT same story in "
              "the Asian setting, with Asian names. Set the \"style\" field to \"logline\".\n\n"
            + "JSON SAFETY: strict valid JSON; no double-quote character inside any value; no line breaks inside values.\n\n"
            + f"Return JSON matching this schema (a single idea inside the ideas array):\n{_IDEAS_SCHEMA_V2}"
        )
        system = _IDEAS_ASIAN_SYSTEM
    else:
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
        system = _IDEAS_SIMILAR_SYSTEM
    ideas = loads_lenient(strip_json(llm_ask(writer_model, prompt, system=system)))
    ideas = ideas.get('ideas', ideas) if isinstance(ideas, dict) else ideas
    if not isinstance(ideas, list) or not ideas:
        raise ValueError('ideas-from-drama: no list parsed')
    print(f'[ideas-from-drama] {"asian-recast" if asian_recast else "1-to-1"} concept from {title!r}', flush=True)
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
    asian_recast = bool(data_in.get('asian_recast'))
    try:
        ideas = _ideas_from_drama(drama, genres=genres, era_hint=era_hint, writer_model=writer_model, asian_recast=asian_recast)
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


# Cap on the persisted display board. Older entries stay in `seen_titles` (so the
# researcher keeps excluding them) even after they scroll off the visible board.
_TOP_DRAMAS_DISPLAY_MAX = 80
_SEEN_TITLES_MAX = 400


def _load_top_dramas():
    try:
        p = _top_dramas_path()
        if p.exists():
            d = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(d, dict):
                d.setdefault('seen_titles', [])
                return d
    except Exception as e:
        print(f'[top-dramas] load failed ({e.__class__.__name__})', flush=True)
    return {'scanned_at': '', 'genres': [], 'dramas': [], 'seen_titles': []}


def _save_top_dramas(store):
    try:
        _top_dramas_path().write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception as e:
        print(f'[top-dramas] save failed ({e.__class__.__name__})', flush=True)


def _seen_titles_from_store(store):
    """Every title ever surfaced — union of the accumulated `seen_titles` and the
    titles currently on the board — used to exclude repeats on the next scan."""
    seen = list(store.get('seen_titles') or [])
    seen += [d.get('title') for d in (store.get('dramas') or []) if isinstance(d, dict) and d.get('title')]
    # De-dupe preserving order (keep first occurrence).
    out, keys = [], set()
    for t in seen:
        k = _norm_drama_title(t)
        if not k or k in keys:
            continue
        keys.add(k)
        out.append(t)
    return out


def _merge_dramas(existing, fresh):
    """Merge freshly-found dramas with the existing board: NEW ones go on top,
    the previously-found ones are kept and pushed down (never dropped, up to the
    display cap). De-dupe by normalized title; preserve any stored `analysis` on
    dramas that reappear. Returns the merged display list."""
    existing = [d for d in (existing or []) if isinstance(d, dict) and d.get('title')]
    fresh = [d for d in (fresh or []) if isinstance(d, dict) and d.get('title')]
    existing_by_key = {_norm_drama_title(d.get('title')): d for d in existing}
    merged, keys = [], set()
    # New finds first (skip any that are actually already on the board).
    for d in fresh:
        k = _norm_drama_title(d.get('title'))
        if not k or k in keys or k in existing_by_key:
            continue
        keys.add(k)
        merged.append(d)
    # Then the previously-found ones, in their existing order (pushed down).
    for d in existing:
        k = _norm_drama_title(d.get('title'))
        if k in keys:
            continue
        keys.add(k)
        merged.append(d)
    return merged[:_TOP_DRAMAS_DISPLAY_MAX]


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


_DRAMA_RANGE_SCHEMA = """{
  "episodes": ["Серия N: что происходит + клиффхэнгер", "Серия N+1: ...", "..."]
}"""


def _analyze_drama_range(drama, ep_from, ep_to, context=''):
    """Web-research how episodes [ep_from, ep_to] of ONE real show unfold and return
    a list of per-episode beats (same shape as _analyze_drama's first_5_episodes, but
    for an arbitrary range). Powers continuing a series past its original outline.

    `context` = what is already known / what the earlier episodes covered, so the new
    beats continue coherently instead of restarting the story."""
    title   = (drama.get('title') or '').strip()
    premise = (drama.get('premise_ru') or drama.get('premise') or '').strip()
    genre   = (drama.get('genre') or '').strip()
    n = ep_to - ep_from + 1
    ctx_block = f'\n\nWhat the earlier episodes already covered (continue from here, do NOT restart):\n{context}' if context else ''
    research_prompt = (
        f'Study the vertical short drama "{title}" ({genre}) as thoroughly as possible.\n'
        f'Known premise: {premise}{ctx_block}\n\n'
        f'Search the live web for how episodes {ep_from} through {ep_to} of THIS specific show unfold: '
        'what happens in each of those episodes, how the conflict escalates, the twists and the cliffhanger '
        'that closes each one. Be concrete and detailed, episode by episode.'
    )
    digest = ''
    try:
        digest = claude_web_research(research_prompt, system=_IDEAS_RESEARCH_SYSTEM, max_uses=6)
        if digest and len(digest.strip()) > 40:
            print(f'[analyze-drama-range] web ok for {title!r} eps {ep_from}-{ep_to}', flush=True)
        else:
            digest = ''
    except Exception as e:
        print(f'[analyze-drama-range] web failed ({e.__class__.__name__})', flush=True)
        digest = ''
    if not digest:
        digest = claude_ask_quality(research_prompt, system=_IDEAS_RESEARCH_SYSTEM) or ''
        print(f'[analyze-drama-range] fallback for {title!r} eps {ep_from}-{ep_to}', flush=True)
    fmt_prompt = (
        f'Here is research about episodes {ep_from}-{ep_to} of the short drama "{title}":\n\n{digest}\n\n'
        f'Produce the per-episode breakdown as STRICT JSON. episodes = exactly {n} entries, one per episode '
        f'for episodes {ep_from} through {ep_to} in order, each a concrete beat ending on a cliffhanger. '
        'Output ONLY the JSON, no prose, and do NOT use the double-quote character inside any value:\n'
        f'{_DRAMA_RANGE_SCHEMA}'
    )
    raw = claude_ask_quality(fmt_prompt, system='You output ONLY strict valid JSON — no prose, no code fences, no commentary.') or ''
    data = loads_lenient(strip_json(raw))
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError('analyze-drama-range: no object parsed')
    eps = data.get('episodes') or []
    if not isinstance(eps, list):
        eps = []
    return [str(e).strip() for e in eps if str(e).strip()]


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
    prev = _load_top_dramas()
    exclude = _seen_titles_from_store(prev)
    try:
        dramas = _research_top_dramas(
            genres=genres, idea_hint=(data_in.get('idea') or '').strip(),
            era_hint=era_hint, exclude_titles=exclude,
        )
    except Exception as e:
        _log_event('WARN', 'top_dramas_scan_fail', err=str(e)[:200])
        return jsonify({'error': str(e)}), 500
    # Merge: new finds on top, previously-found kept and pushed down (not removed).
    merged = _merge_dramas(prev.get('dramas'), dramas)
    for i, d in enumerate(merged):
        if not d.get('id'):
            d['id'] = _drama_slug(d.get('title'), i)
    # Accumulate the full seen-history so future scans keep excluding these titles
    # even once they scroll off the visible board.
    seen = _seen_titles_from_store(prev)
    for d in dramas:
        t = d.get('title')
        if t and _norm_drama_title(t) not in {_norm_drama_title(x) for x in seen}:
            seen.append(t)
    store = {
        'scanned_at': datetime.datetime.utcnow().isoformat() + 'Z',
        'genres': genres,
        'dramas': merged,
        'seen_titles': seen[-_SEEN_TITLES_MAX:],
    }
    _save_top_dramas(store)
    print(f'[top-dramas] scan: {len(dramas)} new, {len(merged)} on board, {len(store["seen_titles"])} seen', flush=True)
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
