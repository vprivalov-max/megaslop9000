"""Series cover art (short-drama poster) generation routes."""
import hashlib
import re
import time

from flask import jsonify, request

from sw.anthro import _anthro_preflight
from sw.avai import _series_image_provider, avai_generate
from sw.core import app
from sw.era import _series_era_hint
from sw.jsonutils import loads_lenient
from sw.llm import claude_ask_fast, claude_ask_quality
from sw.seedance import _avai_upload_local_image
from sw.storage import assets_dir, load_series, save_series, series_path
from sw.style import _series_style_clause, _series_visual_style

# ── Series cover art (short-drama poster) ───────────────────────────────────
# One JPEG at assets/cover.jpg, used as the background of the project card on
# the main menu and openable in a viewer modal with Regenerate / Download.
# Banana ('pro' model) accepts up to 2 contextImages → we pull the top 2
# characters with portraits as visual refs so the leads look on-model.

def _cover_lead_refs(s, sid):
    """Pick up to 2 main characters' AVAI portrait URLs to use as references.
    Order: characters that already have an `avai_base_url` (portrait was
    generated via AVAI), then characters with local `ref_images` (uploaded)
    which we upload to AVAI on the fly. Skips characters with no portrait."""
    refs = []
    leads = []
    for c in (s.get('characters') or []):
        if c.get('avai_base_url'):
            refs.append(c['avai_base_url'])
            leads.append(c)
            if len(refs) >= 2:
                return refs, leads
    # Fallback: upload first local ref image for any remaining slots.
    for c in (s.get('characters') or []):
        if c in leads:
            continue
        rels = c.get('ref_images') or []
        if not rels:
            continue
        local = series_path(sid) / rels[0]
        if not local.exists():
            continue
        try:
            url = _avai_upload_local_image(local)
            refs.append(url)
            leads.append(c)
            # Cache it on the character so we don't re-upload next time.
            c['avai_base_url'] = url
        except Exception as e:
            print(f'[cover] upload ref for {c.get("name")} failed: {e}', flush=True)
            continue
        if len(refs) >= 2:
            break
    return refs, leads


# Fields whose change should invalidate a cached cover art-direction brief.
def _cover_art_direction_source(s):
    """Signature of the bible fields the cover art-direction depends on.
    When any of them change, the cached brief is re-derived so the cover keeps
    tracking the story."""
    parts = [
        (s.get('title') or '').strip(),
        (s.get('synopsis') or '').strip(),
        (s.get('world_description') or '').strip(),
        (s.get('genre') or '').strip(),
        (s.get('tone') or '').strip(),
        (s.get('era_choice') or '').strip(),
        '1' if s.get('era_confirmed') else '0',
        _series_era_hint(s),
        _series_visual_style(s),
    ]
    return hashlib.sha1('␟'.join(parts).encode('utf-8')).hexdigest()


# What a well-formed art-direction brief must contain. Each value is a short
# concrete art-direction phrase, NOT prose — it gets dropped verbatim into the
# image prompt.
_COVER_AD_KEYS = ('palette', 'lighting', 'composition', 'typography',
                  'atmosphere', 'background')


def _series_cover_art_direction(s, force=False):
    """Per-series cover art-direction brief, tailored to the story's atmosphere.

    The old cover prompt baked ONE recipe (teal/magenta palette, look-into-
    camera close-up, white drop-shadow title) into every poster, so covers
    came out indistinguishable and ignored genre/era/mood. This asks Claude to
    design a bespoke brief — palette, lighting, composition archetype,
    genre-matched TYPOGRAPHY, atmosphere and background — from the bible.

    Cached on series.json keyed on a signature of the bible fields it depends
    on (`cover_art_direction_source`); re-derived only when those change or
    when `force=True`. Returns a dict with `_COVER_AD_KEYS`, or {} on failure
    (caller falls back to a generic clause)."""
    sig = _cover_art_direction_source(s)
    cached = s.get('cover_art_direction')
    if (not force and isinstance(cached, dict)
            and s.get('cover_art_direction_source') == sig
            and all(cached.get(k) for k in _COVER_AD_KEYS)):
        return cached

    title = (s.get('title') or '').strip() or 'Untitled'
    synopsis = (s.get('synopsis') or '').strip()[:900]
    world = (s.get('world_description') or '').strip()[:400]
    genre = (s.get('genre') or '').strip()
    tone = (s.get('tone') or '').strip()
    era_hint = _series_era_hint(s)
    visual_style = _series_visual_style(s)

    ctx = [f'TITLE: {title}']
    if genre:        ctx.append(f'GENRE: {genre}')
    if tone:         ctx.append(f'TONE: {tone}')
    if synopsis:     ctx.append(f'SYNOPSIS: {synopsis}')
    if world:        ctx.append(f'WORLD: {world}')
    if era_hint:     ctx.append(f'PERIOD/ERA: {era_hint}')
    if visual_style: ctx.append(f'VISUAL STYLE: {visual_style}')
    ctx_block = '\n'.join(ctx)

    system = (
        'You are an award-winning key-art director for short-form vertical '
        'mobile drama series (ReelShort / DramaBox). You design the cover '
        'poster that makes THIS specific story unmistakable at a glance. '
        'Every series you brief must look DISTINCT from every other — never '
        'fall back on a generic template. In particular DO NOT default to the '
        'overused teal-and-magenta-with-gold-accents palette, the generic '
        '"two leads staring into camera" close-up, or plain white drop-shadow '
        'lettering unless the story genuinely calls for exactly that. Match '
        'the palette, lighting, composition, TYPOGRAPHY and mood to the '
        "story's genre, era and emotional core. Typography especially must "
        'fit the genre — e.g. elegant high-contrast serif for period '
        'romance, distressed condensed sans for revenge thrillers, ornate '
        'gilded blackletter for historical/royal sagas, sleek neon/chrome for '
        'sci-fi, warm rounded script for family melodrama, hand-painted '
        'brush for wuxia/eastern. Be concrete and specific.'
    )
    prompt = (
        f'{ctx_block}\n\n'
        'Design the cover-poster art direction for this series. Respond with '
        'STRICT JSON only (no markdown, no commentary) with EXACTLY these '
        'keys, each a single concrete art-direction phrase (12-30 words), '
        'written to be dropped directly into an image-generation prompt:\n'
        '{\n'
        '  "palette": "specific colors + relationships that fit this story\'s '
        'mood (name actual hues, not just \'warm\'); avoid the generic '
        'teal/magenta/gold default unless truly fitting",\n'
        '  "lighting": "lighting setup + color grade that sells the genre and '
        'era (key direction, contrast, practical sources, grade)",\n'
        '  "composition": "the hero staging / poster archetype for this story '
        '(not necessarily a centered look-into-camera close-up) — framing, '
        'where leads sit, what tension it conveys",\n'
        '  "typography": "title lettering style that matches the genre/era — '
        'typeface character (serif/sans/script/blackletter/etc.), weight, '
        'treatment (foil, distress, glow, engraved), color and placement",\n'
        '  "atmosphere": "overall emotional mood + texture/film-grain/'
        'weather/particle cues that set the tone",\n'
        '  "background": "what the evocative background depicts — the '
        'world/setting hint behind the leads"\n'
        '}'
    )
    try:
        raw = claude_ask_quality(prompt, system=system)
        brief = loads_lenient(raw)
        if not isinstance(brief, dict):
            raise ValueError('brief is not an object')
        out = {k: (str(brief.get(k) or '').strip()) for k in _COVER_AD_KEYS}
        if not all(out.values()):
            raise ValueError('brief missing keys: '
                             + ','.join(k for k in _COVER_AD_KEYS if not out[k]))
        s['cover_art_direction'] = out
        s['cover_art_direction_source'] = sig
        return out
    except Exception as e:
        print(f'[cover/art-direction] failed: {e}', flush=True)
        return {}


def _build_cover_prompt(s, leads):
    """Compose a short-drama poster prompt from the series bible + lead
    characters. The visual recipe (palette / lighting / composition /
    typography / atmosphere) comes from a per-series art-direction brief so
    every cover tracks its own story instead of sharing one template."""
    title = (s.get('title') or '').strip() or 'Untitled'
    synopsis = (s.get('synopsis') or '').strip()
    world = (s.get('world_description') or '').strip()
    genre = (s.get('genre') or '').strip()
    tone = (s.get('tone') or '').strip()
    style_clause = _series_style_clause(s)
    era_hint = _series_era_hint(s)
    ad = _series_cover_art_direction(s)

    # Per-character one-liner: «Name — appearance (short)»
    lead_lines = []
    for c in leads:
        name = (c.get('name') or '').strip()
        app = (c.get('appearance') or c.get('description') or '').strip()
        # Trim long appearance to keep the prompt focused.
        if len(app) > 220:
            app = app[:217].rstrip() + '...'
        if name and app:
            lead_lines.append(f'{name} — {app}')
        elif name:
            lead_lines.append(name)
    leads_clause = ''
    if lead_lines:
        leads_clause = (
            'HERO CAST — feature these lead character(s) (match the '
            'reference images for face / hair / build): '
            + '; '.join(lead_lines)
            + '. '
        )

    syn_short = synopsis[:400].strip()
    world_short = world[:200].strip()
    story_clause = ''
    if syn_short:
        story_clause = f'STORY VIBE: {syn_short} '
    if world_short:
        story_clause += f'World: {world_short}. '

    genre_clause = ''
    bits = [b for b in (genre, tone) if b]
    if bits:
        genre_clause = f'Genre/mood: {" / ".join(bits)}. '

    safe_title = title.replace('"', '\\"')

    if ad:
        # Bespoke art direction drives palette / light / comp / type / mood.
        typography = ad['typography']
        art_block = (
            f'TITLE — render the words "{safe_title}" as the main title '
            f'lettering. TYPOGRAPHY (match exactly): {typography} '
            f'Title must be perfectly legible, correctly spelled, no typos, '
            f'no extra words. '
            f'{leads_clause}'
            f'{story_clause}'
            f'{genre_clause}'
            f'COMPOSITION: {ad["composition"]} '
            f'COLOR PALETTE: {ad["palette"]} '
            f'LIGHTING & GRADE: {ad["lighting"]} '
            f'ATMOSPHERE: {ad["atmosphere"]} '
            f'BACKGROUND: {ad["background"]} '
        )
    else:
        # Fallback when the LLM brief is unavailable — still better than the
        # old fixed teal/magenta recipe by leaning on genre/tone text.
        art_block = (
            f'TITLE — render the words "{safe_title}" as bold large display '
            f'typography, styled to fit the genre/mood above, perfectly '
            f'legible, no typos, no extra words. '
            f'{leads_clause}'
            f'{story_clause}'
            f'{genre_clause}'
            f'Composition: leads staged with intense emotion, dramatic '
            f'cinematic key-light, high contrast, shallow depth of field, '
            f'a palette and lighting that match the story\'s genre and mood, '
            f'evocative background hinting at the world of the story. '
        )

    era_clause = f'{era_hint} ' if era_hint else ''

    prompt = (
        f'Vertical 3:4 key-art poster for a short-form mobile drama series '
        f'(ReelShort / DramaBox style), cinematic and emotional. '
        f'{art_block}'
        f'{era_clause}'
        f'No watermarks, no captions other than the title, no episode '
        f'numbers, no UI elements, no frame borders. '
        f'{style_clause}'
    )
    return re.sub(r'\s+', ' ', prompt).strip()


def _translate_synopsis_to_en(text: str) -> str:
    """Translate a synopsis blurb to English via Claude Haiku. Idempotent on
    text already in English (Claude returns it unchanged)."""
    text = (text or '').strip()
    if not text:
        return ''
    try:
        out = claude_ask_fast(
            f'Translate the following short series logline / synopsis to natural English. '
            f'Return ONLY the translation, no quotes, no preface, no labels. If the text '
            f'is already in English, return it unchanged.\n\n{text}',
            system='You are a professional translator for short-form drama loglines. Preserve tone and meaning, output English prose only.'
        )
        return (out or '').strip().strip('"').strip()
    except Exception as e:
        print(f'[cover/translate] failed: {e}', flush=True)
        return ''


@app.route('/api/series/<sid>/cover/synopsis-en', methods=['GET'])
def get_series_synopsis_en(sid):
    """Returns the series' synopsis + world_description + genre/tone/audience
    chips in English. Caches every translation on series.json with a
    `<field>_en_source` companion so we re-translate only when the source
    text changes."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    fields = ('synopsis', 'world_description', 'genre')
    changed = False
    # If genre is blank, infer one short English label from the synopsis so the
    # viewer has something to show. Bible-author can still override manually.
    if not (s.get('genre') or '').strip() and (s.get('synopsis') or '').strip():
        try:
            inferred = claude_ask_fast(
                'Read this short-drama series synopsis and reply with ONE short '
                'English genre label (2-4 words, e.g. "Cinderella revenge", '
                '"Family melodrama", "Billionaire romance", "Revenge thriller"). '
                'Reply with only the label, no quotes, no period.\n\n'
                + (s.get('synopsis') or '').strip()[:1200],
                system='You classify short-form mobile drama series into concise English genre labels.'
            )
            inferred = (inferred or '').strip().strip('"').strip().split('\n')[0][:60]
            if inferred:
                s['genre'] = inferred
                s['genre_en'] = inferred
                s['genre_en_source'] = inferred
                changed = True
        except Exception as e:
            print(f'[cover/genre-infer] failed: {e}', flush=True)
    for f in fields:
        src = (s.get(f) or '').strip()
        en_key = f + '_en'
        src_key = en_key + '_source'
        if src and (s.get(src_key) != src or not s.get(en_key)):
            en = _translate_synopsis_to_en(src)
            if en:
                s[en_key] = en
                s[src_key] = src
                changed = True
    if changed:
        save_series(sid, s)
    # Return both source + EN so the frontend can refresh stale chips after
    # server-side genre inference, not just the translations.
    out = {f + '_en': s.get(f + '_en') or '' for f in fields}
    out.update({f: s.get(f) or '' for f in fields})
    return jsonify(out)


@app.route('/api/series/<sid>/cover/generate', methods=['POST'])
def generate_series_cover(sid):
    """Generate (or regenerate) the series cover poster. 3:4 JPEG 1K.
    Body (optional): {
        wishes: 'extra art direction from the user',
        new_art_direction: bool  # force a fresh per-series art-direction brief
                                  # instead of reusing the cached one
    }
    Persists rel path to series.cover_image + bumps cover_image_version."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    _nd_anthro, _fl_anthro = _anthro_preflight(s, s.get('characters') or [])
    if _nd_anthro:
        return jsonify({'needs_anthro_decision': True, 'flagged_chars': _fl_anthro,
                        'message': 'Похоже, в этом сериале есть НЕ-люди (' + ', '.join(_fl_anthro) + '). Подтвердите тип мира, прежде чем генерировать.'}), 409
    body = request.get_json(silent=True) or {}
    wishes = (body.get('wishes') or '').strip()

    # Re-derive the art-direction brief on demand (user wants a different look).
    if body.get('new_art_direction'):
        _series_cover_art_direction(s, force=True)

    refs, leads = _cover_lead_refs(s, sid)
    prompt = _build_cover_prompt(s, leads)
    if wishes:
        prompt += f' Additional art direction from the user (follow strictly): {wishes}.'

    out_path = assets_dir(sid) / 'cover.jpg'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Pass refs as a list — _avai_call handles list-or-str.
        image_url = avai_generate(
            prompt, out_path,
            reference_url=refs if refs else None,
            aspect_ratio='3:4',
            preferred_provider=_series_image_provider(s),
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    rel_path = str(out_path.relative_to(series_path(sid)))
    s['cover_image'] = rel_path
    s['cover_image_url'] = image_url
    s['cover_image_version'] = int(time.time())
    save_series(sid, s)
    return jsonify({
        'ready': True,
        'url': f'/assets/{sid}/{rel_path}',
        'image_url': image_url,
        'image_version': s['cover_image_version'],
        'used_refs': len(refs),
    })


