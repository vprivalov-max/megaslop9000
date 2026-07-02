"""Reteller TTS routes: generate, status, balance, voices, styles."""
import datetime
import json

import requests
from flask import jsonify, request

from sw.avai import rtl_headers
from sw.config import RETELLER_API
from sw.core import app
from sw.storage import (list_episodes, load_series, save_episode, save_series,
                        series_path)
from sw.story_prompts import _outfit_ids
from sw.utils import asset_name

# ── Reteller ─────────────────────────────────────────────────────────────────

@app.route('/api/series/<sid>/reteller/preview', methods=['POST'])
def reteller_preview(sid):
    data = request.json
    ep_from = int(data['from'])
    ep_to = int(data['to'])

    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404

    episodes = list_episodes(sid)
    range_eps = [e for e in episodes if ep_from <= e['number'] <= ep_to]

    # Per-character: collect all outfit IDs used across the range + episode list
    char_outfits_used = {}  # cid -> {outfit_id: [ep_numbers]}
    char_ids_used = set()
    for ep in range_eps:
        for cid in ep.get('characters_used', []):
            char_ids_used.add(cid)
        for cid, raw in (ep.get('character_outfits') or {}).items():
            char_ids_used.add(cid)
            for oid in _outfit_ids(raw):
                char_outfits_used.setdefault(cid, {}).setdefault(oid, []).append(ep['number'])

    chars_map = {c['id']: c for c in s['characters']}
    chars_used = []
    for cid in char_ids_used:
        if cid in chars_map:
            c = dict(chars_map[cid])
            c['has_refs'] = len(c.get('ref_images', [])) > 0
            c['ref_urls'] = [f'/assets/{sid}/{r}' for r in c.get('ref_images', [])]
            # Build outfit summary for this range
            outfits_map = {o['id']: o for o in c.get('outfits', [])}
            outfits_in_range = []
            for oid, ep_nums in char_outfits_used.get(cid, {}).items():
                o = outfits_map.get(oid)
                if not o:
                    continue
                photo_url = ''
                if o.get('photo'):
                    photo_url = f'/assets/{sid}/{o["photo"]}'
                elif o.get('avai_url'):
                    photo_url = o['avai_url']
                outfits_in_range.append({
                    'id': oid,
                    'label': o.get('label', ''),
                    'description': o.get('description', ''),
                    'photo_url': photo_url,
                    'has_photo': bool(photo_url),
                    'episodes': sorted(set(ep_nums)),
                })
            outfits_in_range.sort(key=lambda x: (min(x['episodes']) if x['episodes'] else 999, x['label']))
            c['outfits_in_range'] = outfits_in_range
            chars_used.append(c)

    # Locations used in range
    loc_ids_used = set()
    for ep in range_eps:
        for lid in ep.get('locations_used', []):
            loc_ids_used.add(lid)
    locs_map = {l['id']: l for l in s.get('locations', [])}
    locs_used = []
    for lid in loc_ids_used:
        if lid in locs_map:
            l = dict(locs_map[lid])
            l['has_refs'] = len(l.get('ref_images', [])) > 0
            l['ref_urls'] = [f'/assets/{sid}/{r}' for r in l.get('ref_images', [])]
            locs_used.append(l)

    style = dict(s['style'])
    style['ref_urls'] = [f'/assets/{sid}/{r}' for r in style.get('ref_images', [])]

    return jsonify({
        'episodes': range_eps,
        'characters': chars_used,
        'locations': locs_used,
        'style': style,
        'settings': s['settings']
    })

@app.route('/api/series/<sid>/reteller/submit', methods=['POST'])
def reteller_submit(sid):
    data = request.json
    ep_from = int(data['from'])
    ep_to = int(data['to'])
    settings_override = data.get('settings', {})

    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404

    episodes = list_episodes(sid)
    range_eps = [e for e in episodes if ep_from <= e['number'] <= ep_to]
    chars_map = {c['id']: c for c in s['characters']}
    headers = rtl_headers()
    results = []

    for ep in range_eps:
        merged = {**s['settings'], **settings_override}

        # Collect characters and their local ref files
        # Sends each outfit (and the base, when it differs) as a separate characterRef
        # named CHARNAME_OUTFITLABEL.png so Reteller's char-references list is meaningful.
        char_meta = []
        char_files = []  # list of (path, display_name) tuples
        seen_paths = set()
        ep_outfits = ep.get('character_outfits', {})
        locs_map = {l['id']: l for l in s.get('locations', [])}

        for cid in ep.get('characters_used', []):
            c = chars_map.get(cid)
            if not c:
                continue
            char_meta.append({
                'name': c['name'],
                'description': (c.get('description', '') + ' ' + c.get('appearance', '')).strip(),
                'isCharacter': True,
                'gender': c.get('gender', 'female'),
                'voiceId': c.get('voice_id', '')
            })

            # Episode may use MULTIPLE outfits per character (he/she changes clothes
            # within one episode). Send each outfit photo as a separate characterRef
            # so Reteller has a visual anchor for every look the script demands.
            outfit_ids_list = _outfit_ids(ep_outfits.get(cid))
            outfits_with_photos = []
            for oid in outfit_ids_list:
                o = next((x for x in c.get('outfits', []) if x['id'] == oid), None)
                if o and o.get('photo'):
                    outfits_with_photos.append(o)

            if outfits_with_photos:
                for outfit in outfits_with_photos:
                    p = series_path(sid) / outfit['photo']
                    if p.exists() and str(p) not in seen_paths:
                        display = f'{asset_name(c["name"], outfit["label"])}{p.suffix or ".png"}'
                        char_files.append((p, display))
                        seen_paths.add(str(p))
            else:
                # No outfit chosen or none of the chosen outfits has a photo —
                # fall back to base ref(s)
                for rel in c.get('ref_images', []):
                    p = series_path(sid) / rel
                    if p.exists() and str(p) not in seen_paths:
                        display = f'{asset_name(c["name"], "BASE")}{p.suffix or ".png"}'
                        char_files.append((p, display))
                        seen_paths.add(str(p))

        # Locations — send into characterRefs (Reteller's asset library) AND register
        # them in characterMeta with `isCharacter: False` so Reteller doesn't treat
        # the file as an orphaned upload. Without the meta entry the file lands in
        # the project but never gets exposed to the prompt-resolver.
        for lid in ep.get('locations_used', []):
            loc = locs_map.get(lid)
            if not loc:
                continue
            ref_added = False
            for rel in loc.get('ref_images', []):
                p = series_path(sid) / rel
                if p.exists() and str(p) not in seen_paths:
                    display = f'{asset_name(loc["name"])}{p.suffix or ".png"}'
                    char_files.append((p, display))
                    seen_paths.add(str(p))
                    ref_added = True
            if ref_added:
                char_meta.append({
                    'name': loc['name'],
                    'description': loc.get('description', ''),
                    'isCharacter': False,
                    'isLocation': True,
                })

        style_files = []  # list of (path, display_name)
        for rel in s['style'].get('ref_images', []):
            p = series_path(sid) / rel
            if p.exists():
                style_files.append((p, f'STYLE_{p.stem.upper()}{p.suffix or ".png"}'))

        # Map our internal video model id → exactly what Reteller's UI shows in the
        # "Animation model" dropdown. Reteller's public /docs only lists 3 names, but
        # the live UI also accepts seedance-2-ref / seedance-2-pro etc, so we pass
        # them through unchanged when the value is already a Reteller-known slug.
        _video_model_map = {
            'seedance-2-ref': 'seedance-2-ref',  # → "Seedance 2.0 Ref"
            'seedance-2':     'seedance-2',
            'seedance15':     'seedance15',
            'grok_video':     'grok_video',
            'veo31_fast':     'veo31_fast',
        }
        # Auto-heal legacy settings: older series were saved with duration=90 / no enable_grid
        # / wrong animation_model. Force-correct them here AND persist back to series.json so
        # the UI panel reflects the updated values next time the user opens settings.
        legacy_fixed = False
        if not str(s['settings'].get('animation_model','')).strip() or s['settings'].get('animation_model') == 'seedance-2':
            s['settings']['animation_model'] = 'seedance-2-ref'; legacy_fixed = True
        if s['settings'].get('duration') in (None, 90, 60, 120, 180):
            s['settings']['duration'] = 'auto-frames'; legacy_fixed = True
        if 'enable_grid' not in s['settings']:
            s['settings']['enable_grid'] = False; legacy_fixed = True
        if not s['settings'].get('no_fades'):
            s['settings']['no_fades'] = True; legacy_fixed = True
        if legacy_fixed:
            save_series(sid, s)
            merged = {**s['settings'], **settings_override}

        video_gen = _video_model_map.get(merged.get('animation_model', 'seedance-2-ref'),
                                         merged.get('animation_model', 'seedance-2-ref'))

        # Reteller settings — every field shown in the UI's "Retelling Settings" panel
        # (section 5: Video Duration / Aspect Ratio / Language / Generator / Multi-voice
        # / Animation / Cinema / Trim / No fades / Music). Field names mirror the
        # camelCase that the live API + frontend uses; values come from the series
        # defaults so a fresh draft already matches the user's preferred panel state.
        settings_payload = {
            # Section 5 — Video Duration row. "auto-frames" = Reteller's Auto-frames mode.
            'duration':            merged.get('duration', 'auto-frames'),
            'autoFrames':          merged.get('duration', 'auto-frames') == 'auto-frames',
            # Aspect Ratio
            'aspectRatio':         merged.get('aspect_ratio', '9:16'),
            # Language
            'language':            merged.get('language', 'English'),
            # Generator (image provider) — UI label "Banana Pro"
            'imageProvider':       merged.get('image_provider', 'banana'),
            # Multi-voice narration
            'multiVoice':          merged.get('multi_voice', False),
            # Animation strip
            'enableAnimation':     merged.get('enable_animation', True),
            'animationSpeed':      merged.get('animation_speed', 'fast'),       # fast | normal
            'animationResolution': merged.get('animation_resolution', '480p'),  # 480p | 720p
            'videoGenerator':      video_gen,                                   # → "Seedance 2.0 Ref"
            'animationModel':      video_gen,                                   # alias the UI sometimes reads
            # Animation grid (frame-grid overlay) — must stay OFF for Seedance Ref
            'enableGrid':          bool(merged.get('enable_grid', False)),
            'grid':                bool(merged.get('enable_grid', False)),     # alias for older Reteller schema
            # Cinema toggle
            'cinema':              merged.get('cinema', False),
            # Trim toggle
            'trim':                merged.get('trim', True),
            # No fades toggle — must be ON
            'noFades':             bool(merged.get('no_fades', True)),
            # Music toggle + volume slider (0–1; 0.3 = 30%)
            'enableMusic':         merged.get('enable_music', True),
            'musicVolume':         merged.get('music_volume', 0.30),
            # Subtitles (currently UI-hidden but the API accepts it)
            'enableSubtitles':     merged.get('enable_subtitles', False),
            # Voice / TTS — used inside Reteller for narration even when multi-voice is off
            'voice':               merged.get('voice', 'Enceladus'),
            'ttsProvider':         merged.get('tts_provider', 'elevenlabs'),
            # Style block
            'style':               s['style'].get('type', 'cinematic'),
            'imageSize':           merged.get('image_size', '1K'),
        }
        if merged.get('elevenlabs_voice_id'):
            settings_payload['elevenlabsVoiceId'] = merged['elevenlabs_voice_id']
        if s['style'].get('custom_description'):
            settings_payload['customStyleDescription'] = s['style']['custom_description']

        ep_title = f'{s["title"]} — Ep.{ep["number"]:02d} {ep["title"]}'
        # Use structured Reteller prompt if available, fall back to raw script, then synopsis
        ep_text = ep.get('reteller_prompt') or ep.get('script') or ep.get('synopsis') or f'Episode {ep["number"]}: {ep["title"]}'

        has_files = char_files or style_files
        opened_files = []

        try:
            if has_files:
                form_data = {
                    'title': ep_title,
                    # Reteller schema: `text` = source content to retell, `userInstruction` = AI directives.
                    # Our generated script IS the directive (cast block + scenes + episode notes), so it
                    # belongs in userInstruction. Sending it as `text` makes Reteller treat it as a
                    # passive content source (the "content.txt" Content source you saw in the UI).
                    'userInstruction': ep_text,
                    'autoStart': 'false',  # DRAFT mode — user reviews on Reteller's page and starts manually
                    'settings': json.dumps(settings_payload, ensure_ascii=False),
                    'characterMeta': json.dumps(char_meta, ensure_ascii=False),
                }
                multipart = []
                for p, display in char_files:
                    fobj = open(p, 'rb')
                    opened_files.append(fobj)
                    mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
                    multipart.append(('characterRefs', (display, fobj, mime)))
                for p, display in style_files:
                    fobj = open(p, 'rb')
                    opened_files.append(fobj)
                    mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
                    multipart.append(('styleRefs', (display, fobj, mime)))

                resp = requests.post(
                    f'{RETELLER_API}/projects',
                    headers=headers,
                    data=form_data,
                    files=multipart,
                    timeout=30
                )
            else:
                payload = {
                    'title':           ep_title,
                    'userInstruction': ep_text,  # AI directive (script), not raw retelling source
                    'autoStart':       False,    # DRAFT mode
                    'settings':        settings_payload,
                }
                if char_meta:
                    payload['characters'] = [{
                        'name': m['name'],
                        'description': m.get('description', ''),
                        'isCharacter': bool(m.get('isCharacter', True)),
                        'isLocation':  bool(m.get('isLocation', False)),
                        'gender':      m.get('gender', ''),
                    } for m in char_meta]

                resp = requests.post(
                    f'{RETELLER_API}/projects',
                    headers=headers,
                    json=payload,
                    timeout=30
                )
        finally:
            for fobj in opened_files:
                fobj.close()

        entry = {'episode': ep['number'], 'http_status': resp.status_code}
        if resp.ok:
            rdata = resp.json()
            project_id = rdata.get('projectId')
            project_url = rdata.get('projectUrl') or rdata.get('url') or (f'https://reteller.ai/projects/{project_id}' if project_id else '')
            entry['project_id']  = project_id
            entry['project_url'] = project_url
            entry['reteller_status'] = rdata.get('status', 'draft')

            ep['reteller']['project_id']   = project_id
            ep['reteller']['project_url']  = project_url
            ep['reteller']['status']       = rdata.get('status', 'draft')
            ep['reteller']['submitted_at'] = datetime.datetime.utcnow().isoformat()
            ep['status'] = 'draft_in_reteller'
            ep['ready'] = True  # sending to reteller marks the episode as done
            save_episode(sid, ep['number'], ep)
        else:
            entry['error'] = resp.text

        results.append(entry)

    return jsonify(results)

@app.route('/api/series/<sid>/reteller/status/<project_id>', methods=['GET'])
def reteller_status(sid, project_id):
    resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}',
        headers=rtl_headers(),
        timeout=15
    )
    if not resp.ok:
        return jsonify({'error': resp.text}), resp.status_code

    rdata = resp.json()
    status = rdata.get('status')

    # Sync episode status
    for ep in list_episodes(sid):
        if ep.get('reteller', {}).get('project_id') == project_id:
            if status in ('completed', 'error'):
                ep['reteller']['status'] = status
                if status == 'completed':
                    ep['reteller']['video_url'] = rdata.get('videoUrl')
                ep['status'] = status
                save_episode(sid, ep['number'], ep)
            break

    return jsonify(rdata)

@app.route('/api/reteller/balance')
def reteller_balance():
    resp = requests.get(f'{RETELLER_API}/balance', headers=rtl_headers(), timeout=10)
    return jsonify(resp.json() if resp.ok else {'error': resp.text})

@app.route('/api/reteller/voices')
def reteller_voices():
    resp = requests.get(f'{RETELLER_API}/voices', headers=rtl_headers(), timeout=10)
    return jsonify(resp.json() if resp.ok else {})

@app.route('/api/reteller/styles')
def reteller_styles():
    resp = requests.get(f'{RETELLER_API}/styles', headers=rtl_headers(), timeout=10)
    return jsonify(resp.json() if resp.ok else {})


