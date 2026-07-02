"""Building facades: grouped location exteriors (generation + routes)."""
import datetime
import json
import re
import shutil
import subprocess
import sys
import time
import uuid

import requests
from flask import jsonify, request

from sw.auth import _get_user_avai_key, _spawn_with_keys
from sw.avai import _series_image_provider, avai_generate, rtl_headers
from sw.config import RETELLER_API
from sw.core import app
from sw.era import _no_caption_text_clause, _strip_cast_names_for_visual
from sw.jsonutils import strip_json
from sw.llm import claude_ask
from sw.logging_utils import _log_event
from sw.seedance import _avai_seedance_start, _avai_seedance_status
from sw.storage import assets_dir, facades_dir, load_series, save_series, series_path
from sw.style import _location_crowd_clause, _series_style_clause
from sw.utils import asset_name, slugify

# ── Building facades ────────────────────────────────────────────────────────
# Per-series feature: cluster interior locations into their parent building
# (e.g. «Marcus's office» + «Marcus's bedroom» → «Bellacourt Mansion»), then
# generate ONE exterior facade image + a short Seedance video per building.
# Used as «open every new venue with a 4s facade shot» before the dialogue
# starts inside. Storage: assets/facades/<facade_id>/{facade.jpg, facade.mp4}.
# series.location_facades[] persists the linkage back to series.locations.

_FACADE_STATUS = {}   # sid → status dict

def _facade_status(sid):
    return _FACADE_STATUS.setdefault(sid, {
        'running': False, 'total': 0, 'done': 0, 'errors': [], 'current': None,
    })


def _claude_group_locations(locs, existing_building_names=None):
    """Cluster the given locations into building groups via Claude. Reused
    by the manual `/facades/group` endpoint and by the post-accept auto
    sweep. `existing_building_names` (optional) is passed as context so
    Claude prefers reusing an existing facade's name when a new location
    belongs to a building already in the library — keeps grouping stable
    across multiple script-accept cycles.

    Returns: list of dicts with keys
      building_name, type ('building'|'exterior'), facade_description,
      member_loc_ids, member_names.
    """
    locs_block = '\n'.join(
        f'- id={l["id"]} | "{l["name"]}" — {(l.get("description") or "")[:200]}'
        for l in locs
    )
    existing_block = ''
    if existing_building_names:
        existing_block = (
            "\nУЖЕ СУЩЕСТВУЮЩИЕ ЗДАНИЯ В ЭТОМ СЕРИАЛЕ (если новая локация "
            "принадлежит одному из них — используй его имя ДОСЛОВНО):\n"
            + '\n'.join(f'  • {n}' for n in existing_building_names) + '\n'
        )
    sys = (
        "Ты — продюсер визуальной библиотеки сериала. Тебе дают список локаций. "
        "Сгруппируй их по ЗДАНИЯМ-ОБЛАДАТЕЛЯМ. Цель: для каждого здания сгенерируем "
        "ОДИН фасадный плановый кадр снаружи, который будет показывать «вот это место» "
        "перед интерьерными сценами внутри.\n\n"
        "ПРАВИЛА:\n"
        "1. Локации-интерьеры одного и того же здания группируй вместе. Примеры:\n"
        "   • «Marcus's office» + «Marcus's bedroom» + «Marcus's wine cellar» → одно здание «Bellacourt Mansion»\n"
        "   • «Hospital reception» + «ICU ward» + «Hospital cafeteria» → одно «City Hospital»\n"
        "   • «Elena's motel room» + «Motel hallway» → одно «Roadside Motel»\n"
        "2. Самодостаточные exterior-локации (улица, виноградник, парк, набережная, лес) — каждая "
        "СВОЯ группа из 1 элемента с type='exterior'. У них уже есть наружный кадр в ref-картинках, "
        "отдельный facade не нужен.\n"
        "3. Если для двух локаций по описанию НЕ ясно одно ли это здание — держи их отдельно. "
        "Лучше лишний фасад, чем неправильное склеивание.\n"
        "4. Имя здания должно быть КОНКРЕТНЫМ (имя владельца / название учреждения / города), "
        "не «building» / «structure» / «place». «Bellacourt Mansion», не «Mansion».\n"
        "5. type='building' для зданий нуждающихся в фасадной генерации, type='exterior' для уже-наружных.\n"
        f"{existing_block}"
        f"\nЛОКАЦИИ СЕРИАЛА:\n{locs_block}\n\n"
        "Верни JSON и ТОЛЬКО JSON:\n"
        '{\n'
        '  "groups": [\n'
        '    {\n'
        '      "building_name": "Bellacourt Mansion",\n'
        '      "type": "building",\n'
        '      "facade_description": "Three-storey stone mansion covered in vines, ornate front entrance with double oak doors, gravel driveway, daylight",\n'
        '      "member_loc_ids": ["id1", "id2"]\n'
        '    }\n'
        '  ]\n'
        '}\n'
    )
    raw = claude_ask("Сгруппируй и верни JSON.", system=sys, max_tokens=4000)
    data = json.loads(strip_json(raw))
    groups = data.get('groups') or []
    known_ids = {l['id'] for l in locs}
    name_by_id = {l['id']: l['name'] for l in locs}
    for g in groups:
        g['member_loc_ids'] = [i for i in (g.get('member_loc_ids') or []) if i in known_ids]
        g['member_names']   = [name_by_id[i] for i in g['member_loc_ids']]
    return groups


@app.route('/api/series/<sid>/facades/group', methods=['POST'])
def facades_group(sid):
    """Have Claude cluster the series' locations into parent buildings.
    Returns preview groupings — frontend lets the user accept / edit before
    kicking off generation. Pure read; no side effects."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    locs = [l for l in (s.get('locations') or []) if l.get('name')]
    if not locs:
        return jsonify({'error': 'У сериала нет локаций для группировки'}), 400
    try:
        groups = _claude_group_locations(locs)
        return jsonify({'groups': groups})
    except Exception as e:
        return jsonify({'error': f'Group failed: {e}'}), 500


def _facade_worker(sid, groups):
    """Background: for each `building` group, generate facade image then a
    short Seedance video. Saves to assets/facades/<facade_id>/; updates
    series.location_facades[] incrementally so the UI sees progress."""
    st = _facade_status(sid)
    work_groups = [g for g in groups
                   if g.get('type') == 'building'
                   and (g.get('member_loc_ids') or g.get('member_names'))]
    st.update({
        'running': True, 'total': len(work_groups), 'done': 0,
        'errors': [], 'started_at': datetime.datetime.utcnow().isoformat(),
        'finished_at': None, 'current': None,
    })
    try:
        s0 = load_series(sid)
        if not s0:
            st['errors'].append({'error': 'series gone'})
            return
        # Ensure the facade list exists
        s0.setdefault('location_facades', [])
        save_series(sid, s0)
        for g in work_groups:
            name = (g.get('building_name') or '').strip() or 'Unnamed Building'
            desc = (g.get('facade_description') or '').strip()
            st['current'] = name
            fid = 'fac_' + str(uuid.uuid4())[:8]
            fac_dir = facades_dir(sid) / fid
            fac_dir.mkdir(parents=True, exist_ok=True)
            facade = {
                'id': fid,
                'building_name': name,
                'facade_description': desc,
                'member_loc_ids': list(g.get('member_loc_ids') or []),
                'image_path': '',
                'image_avai_url': '',
                'video_path': '',
                'video_avai_url': '',
                'status': 'generating',
                'created_at': datetime.datetime.utcnow().isoformat(),
            }
            # Initial save so UI sees the in-progress card
            s = load_series(sid)
            s.setdefault('location_facades', []).append(facade)
            save_series(sid, s)

            # ── Image generation ──────────────────────────────────────────
            tone = s.get('tone', '')
            style_clause = _series_style_clause(s)
            fac_name = _strip_cast_names_for_visual(name, s)
            fac_desc = _strip_cast_names_for_visual(desc, s)
            img_prompt = (
                f"Exterior facade of {fac_name}. {fac_desc}. "
                f"{_location_crowd_clause(None)}"
                f"{(tone + ' atmosphere. ') if tone else ''}"
                f"Cinematic wide establishing shot of the building exterior. "
                f"Vertical 9:16 framing for short-drama.{_no_caption_text_clause()}{style_clause}"
            )
            img_prompt = re.sub(r'\s+', ' ', img_prompt).strip()
            img_path = fac_dir / 'facade.jpg'
            img_url = ''
            try:
                img_url = avai_generate(
                    img_prompt, img_path, aspect_ratio='9:16',
                    preferred_provider=_series_image_provider(s),
                )
                facade['image_avai_url'] = img_url
                facade['image_path'] = str(img_path.relative_to(series_path(sid)))
            except Exception as e:
                facade['status'] = 'failed'
                facade['error'] = f'image: {str(e)[:200]}'
                st['errors'].append({'facade_id': fid, 'building': name, 'error': str(e)[:200]})
                _merge_facade(sid, fid, facade)
                continue
            _merge_facade(sid, fid, facade)

            # ── Video generation (4-5 second static establishing) ─────────
            try:
                vid_prompt = (
                    f"Static cinematic establishing wide shot of {name} exterior. {desc}. "
                    "No people, no characters. Subtle ambient motion only — drifting clouds, "
                    "swaying foliage, very slow camera push-in. "
                    "Ambient location sounds only — wind, distant traffic, birds, rustling foliage, "
                    "rain or city hum depending on setting. No speech, no music, no dialogue. "
                    "Cinematic, 9:16."
                )
                vid_prompt = re.sub(r'\s+', ' ', vid_prompt).strip()
                avai_key = _get_user_avai_key()
                # Generate ambient audio for the establishing shot — gives the
                # facade clip atmosphere (wind / city / rain depending on
                # location) instead of dead silence before the next chunk
                # begins. Assembly already handles mixed-audio sources
                # (auto-asssemble probes per-input audio presence) so this is
                # back-compat with older facades rendered silent.
                job = _avai_seedance_start(
                    prompt=vid_prompt,
                    ref_urls=[img_url] if img_url else [],
                    duration=5,    # Seedance min 5s; covers the 4s establishing
                    resolution='720p',
                    moderation_bypass='off',
                    aspect_ratio='9:16',
                    generate_audio=True,
                    avai_key=avai_key,
                )
                vid_path_local = fac_dir / 'facade.mp4'
                vid_url = ''
                for _ in range(180):   # 12-min ceiling
                    time.sleep(4)
                    pst = _avai_seedance_status(job['job_id'], status_url=job['status_url'], avai_key=avai_key)
                    if pst.get('status') == 'completed':
                        vid_url = pst.get('video_url', '')
                        break
                    if pst.get('status') in ('failed', 'error'):
                        raise RuntimeError(pst.get('error') or 'seedance failed')
                if not vid_url:
                    raise RuntimeError('seedance timeout')
                r = requests.get(vid_url, timeout=120, stream=True)
                r.raise_for_status()
                with open(vid_path_local, 'wb') as fp:
                    for chk in r.iter_content(1 << 16):
                        fp.write(chk)
                facade['video_avai_url'] = vid_url
                facade['video_path'] = str(vid_path_local.relative_to(series_path(sid)))
                facade['status'] = 'ready'
            except Exception as e:
                facade['status'] = 'image_only'
                facade['error'] = f'video: {str(e)[:200]}'
                st['errors'].append({'facade_id': fid, 'building': name, 'error': f'video: {str(e)[:200]}'})
            _merge_facade(sid, fid, facade)
            st['done'] += 1
    finally:
        st['running'] = False
        st['finished_at'] = datetime.datetime.utcnow().isoformat()
        st['current'] = None


def _merge_facade(sid, fid, facade):
    """Re-read series, update the facade entry by id, save. Avoids clobbering
    concurrent writes from other endpoints."""
    s = load_series(sid)
    if not s: return
    facs = s.setdefault('location_facades', [])
    found = False
    for i, f in enumerate(facs):
        if f.get('id') == fid:
            facs[i] = {**f, **facade}
            found = True
            break
    if not found:
        facs.append(facade)
    save_series(sid, s)


@app.route('/api/series/<sid>/facades/generate', methods=['POST'])
def facades_generate(sid):
    """Body: { groups: [...] } where groups is the Claude-grouped list (or
    user-edited variant). Returns immediately, worker writes facades back
    incrementally; poll /facades for state."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    groups = body.get('groups') or []
    if not groups:
        return jsonify({'error': 'no groups provided'}), 400
    st = _facade_status(sid)
    if st.get('running'):
        return jsonify({'error': 'generation already running'}), 409
    _spawn_with_keys(_facade_worker, sid, groups)
    work_count = len([g for g in groups if g.get('type') == 'building'])
    return jsonify({'started': True, 'count': work_count})


def auto_facades_for_new_locations(sid):
    """Idempotent post-sweep hook: find locations that have a generated
    interior but aren't yet a member of any existing facade group, ask
    Claude to cluster them (using existing building names as anchors so
    similar locs reuse an existing facade), then either MERGE into the
    existing facade by name or spawn `_facade_worker` for genuinely new
    buildings.

    Called from the tail of `auto_generate_missing_assets` so facade gen
    fires automatically after interiors finish. Safe to call repeatedly —
    on a second invocation with no new locations it's a no-op.

    Honors:
      • `series.auto_facades = False`  → skip (explicit user opt-out)
      • `location._skip_autogen = True` → loc never participates
      • `_facade_status(sid).running`   → don't double-run
    """
    s = load_series(sid)
    if not s:
        return
    if s.get('auto_facades') is False:
        return  # explicit opt-out at series level
    if s.get('auto_generate_assets') is False:
        return  # broader opt-out — user disabled autogen entirely
    st = _facade_status(sid)
    if st.get('running'):
        print(f'[auto-facades {sid}] worker already running — skipping', flush=True)
        return

    existing_facades = s.get('location_facades') or []
    existing_member_ids = {
        mid for f in existing_facades for mid in (f.get('member_loc_ids') or [])
    }
    existing_name_to_facade = {
        (f.get('building_name') or '').strip().lower(): f
        for f in existing_facades if f.get('building_name')
    }
    all_locs = s.get('locations') or []
    new_locs = [
        l for l in all_locs
        if l.get('id') and l['id'] not in existing_member_ids
        and not l.get('_skip_autogen')
        and (l.get('ref_images') or [])   # interior already rendered
    ]
    if not new_locs:
        return  # nothing to do — fully idempotent

    # Mark `running` during the Claude-grouping phase too — otherwise the
    # facades modal renders «ещё нет сгенерированных фасадов» for the 3-5s
    # while Claude clusters, then suddenly flips to «генерирую» when
    # _facade_worker takes over. Setting it here gives the UI a continuous
    # signal that something IS happening. Reset to False in the no-work-
    # found branches below.
    st.update({'running': True, 'phase': 'grouping', 'current': 'Группирую локации…',
               'total': 0, 'done': 0, 'errors': []})
    print(f'[auto-facades {sid}] {len(new_locs)} new location(s) need facade — grouping…', flush=True)
    try:
        existing_names = sorted({f.get('building_name', '').strip() for f in existing_facades if f.get('building_name')})
        groups = _claude_group_locations(new_locs, existing_building_names=existing_names)
    except Exception as e:
        print(f'[auto-facades {sid}] grouping failed: {e}', flush=True)
        _log_event('WARN', 'auto_facades_group_failed', sid=sid, err=str(e)[:200])
        st.update({'running': False, 'phase': None, 'current': None,
                   'errors': [{'error': f'grouping failed: {str(e)[:200]}'}]})
        return

    # Split: groups whose building_name matches an existing facade → merge.
    # Groups with a new building_name → queue for _facade_worker.
    work_groups = []
    s_disk = load_series(sid)
    if not s_disk:
        return
    s_disk.setdefault('location_facades', [])
    merges = 0
    for g in groups:
        if g.get('type') != 'building':
            continue  # exteriors don't need facade gen
        bname = (g.get('building_name') or '').strip()
        if not bname:
            continue
        new_mids = [m for m in (g.get('member_loc_ids') or []) if m not in existing_member_ids]
        if not new_mids:
            continue
        existing = existing_name_to_facade.get(bname.lower())
        if existing:
            # Merge: extend the existing facade's member list. No re-render.
            for f in s_disk.get('location_facades', []):
                if f.get('id') == existing.get('id'):
                    f.setdefault('member_loc_ids', [])
                    for mid in new_mids:
                        if mid not in f['member_loc_ids']:
                            f['member_loc_ids'].append(mid)
                    merges += 1
                    break
        else:
            work_groups.append({
                'building_name': bname,
                'type': 'building',
                'facade_description': (g.get('facade_description') or '').strip(),
                'member_loc_ids': new_mids,
            })
    if merges:
        save_series(sid, s_disk)
        print(f'[auto-facades {sid}] merged into {merges} existing facade(s)', flush=True)

    if work_groups:
        print(f'[auto-facades {sid}] kicking off facade gen for {len(work_groups)} new building(s): '
              + ', '.join(g["building_name"] for g in work_groups), flush=True)
        # _facade_worker re-sets status.running with its own total/done counters,
        # so the grouping-phase flag we set above is transparently superseded.
        _spawn_with_keys(_facade_worker, sid, work_groups)
    else:
        # Pure-merge path or grouping yielded only exteriors — no renders to
        # do. Clear the grouping-phase flag we set at the top so the UI stops
        # showing «Группирую…».
        st.update({'running': False, 'phase': None, 'current': None})


@app.route('/api/series/<sid>/facades', methods=['GET'])
def facades_list(sid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    # Belt-and-suspenders auto-trigger: if the user is looking at the facades
    # panel and there are interior locations that aren't a member of any
    # facade group yet, kick off `auto_facades_for_new_locations` in the
    # background. The function is fully idempotent (running-flag check +
    # no-op when nothing new), so polling this endpoint every 6s while the
    # modal is open is safe. Catches series whose autogen sweep ended before
    # the facade auto-trigger was wired up — and any future case where the
    # sweep-tail trigger failed silently.
    if s.get('auto_facades') is not False:
        existing_facades = s.get('location_facades') or []
        member_ids = {mid for f in existing_facades for mid in (f.get('member_loc_ids') or [])}
        has_ungrouped = any(
            l.get('id') and l['id'] not in member_ids
            and not l.get('_skip_autogen')
            and (l.get('ref_images') or [])
            for l in (s.get('locations') or [])
        )
        if has_ungrouped and not _facade_status(sid).get('running'):
            try:
                _spawn_with_keys(auto_facades_for_new_locations, sid)
                # Surface an «about to start» hint immediately so the first
                # response (before the worker thread has ticked) tells the UI
                # to poll. Worker itself overwrites status.running with the
                # full grouping/rendering state within milliseconds. We use
                # a hint flag instead of running=True to avoid tripping the
                # worker's «already running — skip» guard.
                _facade_status(sid)['_pending_autostart'] = True
            except Exception as e:
                print(f'[facades_list {sid}] auto-trigger spawn failed: {e}', flush=True)
    live = _facade_status(sid)
    pending = bool(live.pop('_pending_autostart', False))
    st_out = dict(live)
    if pending and not st_out.get('running'):
        # Pre-populate the running flag for THIS response so the frontend's
        # facadesRefresh() sees running=true and starts polling. Subsequent
        # polls read the worker's real state.
        st_out.update({'running': True, 'phase': 'grouping',
                       'current': 'Группирую локации…',
                       'total': 0, 'done': 0, 'errors': []})
    return jsonify({
        'facades': s.get('location_facades') or [],
        'status': st_out,
        'folder': str(facades_dir(sid).resolve()),
    })


@app.route('/api/series/<sid>/facades/<fid>', methods=['DELETE'])
def facades_delete(sid, fid):
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    s['location_facades'] = [f for f in (s.get('location_facades') or []) if f.get('id') != fid]
    save_series(sid, s)
    d = facades_dir(sid) / fid
    if d.exists():
        try: shutil.rmtree(d)
        except Exception: pass
    return jsonify({'ok': True})


@app.route('/api/series/<sid>/facades/<fid>/regenerate', methods=['POST'])
def facades_regenerate(sid, fid):
    """Re-run image+video for one facade. Optional body overrides {building_name,
    facade_description, member_loc_ids}."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    fac = next((f for f in (s.get('location_facades') or []) if f.get('id') == fid), None)
    if not fac:
        return jsonify({'error': 'facade not found'}), 404
    body = request.json or {}
    name = (body.get('building_name') or fac.get('building_name') or '').strip()
    desc = (body.get('facade_description') or fac.get('facade_description') or '').strip()
    members = body.get('member_loc_ids') if 'member_loc_ids' in body else fac.get('member_loc_ids')
    # Wipe the existing entry so worker creates a fresh one with a new fid
    s['location_facades'] = [f for f in s['location_facades'] if f.get('id') != fid]
    save_series(sid, s)
    d = facades_dir(sid) / fid
    if d.exists():
        try: shutil.rmtree(d)
        except Exception: pass
    st = _facade_status(sid)
    if st.get('running'):
        return jsonify({'error': 'generation already running'}), 409
    group = {
        'building_name': name,
        'type': 'building',
        'facade_description': desc,
        'member_loc_ids': list(members or []),
    }
    _spawn_with_keys(_facade_worker, sid, [group])
    return jsonify({'started': True})


@app.route('/api/series/<sid>/facades/generate-for-location', methods=['POST'])
def facades_generate_for_location(sid):
    """Generate ONE facade for ONE specific location (or attach the location
    to a new single-member facade group). Lets the user iterate per-loc
    instead of «сгенерировать все».

    Body: { loc_id: str, building_name?: str, facade_description?: str }
    If building_name/facade_description are missing, Claude derives them
    from the location's name + description on-the-fly."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    body = request.json or {}
    loc_id = (body.get('loc_id') or '').strip()
    if not loc_id:
        return jsonify({'error': 'loc_id required'}), 400
    loc = next((l for l in (s.get('locations') or []) if l.get('id') == loc_id), None)
    if not loc:
        return jsonify({'error': 'location not found'}), 404
    name = (body.get('building_name') or '').strip()
    desc = (body.get('facade_description') or '').strip()
    if not name or not desc:
        # Quick Claude call — turn the location's own info into a building
        # name + facade description. Cheap, single-shot Haiku-equivalent.
        try:
            sys = (
                "Ты — продюсер визуальной библиотеки сериала. Тебе дают ОДНУ локацию "
                "(имя + описание интерьера/места действия). Если это интерьер — "
                "определи название здания-обладателя (вилла, больница, отель, школа) "
                "и опиши его ФАСАД СНАРУЖИ. Если это уже наружная локация — оставь "
                "имя как есть, опиши вид издалека. Только JSON.\n"
                f'Локация: "{loc["name"]}" — {(loc.get("description") or "")[:300]}\n\n'
                'Верни:\n{\n'
                '  "building_name": "...",\n'
                '  "facade_description": "..." (1-2 предложения, английский)\n'
                '}\n'
            )
            raw = claude_ask("Reply with JSON only.", system=sys, max_tokens=600)
            data = json.loads(strip_json(raw))
            name = name or (data.get('building_name') or loc['name']).strip()
            desc = desc or (data.get('facade_description') or '').strip()
        except Exception as e:
            return jsonify({'error': f'Derive failed: {e}'}), 500
    st = _facade_status(sid)
    if st.get('running'):
        return jsonify({'error': 'generation already running'}), 409
    group = {
        'building_name': name or loc['name'],
        'type': 'building',
        'facade_description': desc or f"Exterior of {loc['name']}.",
        'member_loc_ids': [loc_id],
    }
    _spawn_with_keys(_facade_worker, sid, [group])
    return jsonify({'started': True, 'building_name': group['building_name']})


@app.route('/api/series/<sid>/facades/folder', methods=['POST'])
def facades_open_folder(sid):
    """macOS / Linux / Windows-friendly: opens the facades folder in the OS
    file manager when the app runs locally. On the deployed server this is
    a no-op; the UI uses the returned `folder` path as a copyable hint."""
    s = load_series(sid)
    if not s:
        return jsonify({'error': 'not found'}), 404
    d = facades_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    folder = str(d.resolve())
    try:
        if sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        elif sys.platform == 'win32':
            subprocess.Popen(['explorer', folder])
        elif sys.platform.startswith('linux'):
            subprocess.Popen(['xdg-open', folder])
    except Exception:
        pass
    return jsonify({'folder': folder})


@app.route('/api/series/<sid>/locations/<loc_id>/save-frame/<project_id>', methods=['POST'])
def save_location_frame(sid, loc_id, project_id):
    s = load_series(sid)
    loc = next((l for l in s.get('locations', []) if l['id'] == loc_id), None)
    if not loc:
        return jsonify({'error': 'not found'}), 404

    hdrs = rtl_headers()
    status_resp = requests.get(f'{RETELLER_API}/projects/{project_id}', headers=hdrs, timeout=15)
    if not status_resp.ok:
        return jsonify({'error': status_resp.text}), status_resp.status_code
    if status_resp.json().get('status') != 'completed':
        return jsonify({'ready': False, 'status': status_resp.json().get('status')})

    frames_resp = requests.get(
        f'{RETELLER_API}/projects/{project_id}/assets/list?types=frames',
        headers=hdrs, timeout=15
    )
    if not frames_resp.ok:
        return jsonify({'error': frames_resp.text}), 500

    frame_assets = [a for a in frames_resp.json().get('assets', []) if a['type'] == 'frames']
    if not frame_assets:
        return jsonify({'ready': False, 'status': 'no_frames'})

    img_resp = requests.get(frame_assets[0]['url'], timeout=30)
    if not img_resp.ok:
        return jsonify({'error': 'download failed'}), 500

    loc_slug = slugify(loc['name'])
    loc_dir = assets_dir(sid) / 'locations' / loc_slug
    loc_dir.mkdir(parents=True, exist_ok=True)
    filename = f'{asset_name(loc["name"])}.jpg'  # e.g. THE_NETWORKING_EVENT_VENUE.jpg
    (loc_dir / filename).write_bytes(img_resp.content)

    rel_path = f'assets/locations/{loc_slug}/{filename}'
    loc.setdefault('ref_images', []).append(rel_path)
    save_series(sid, s)
    return jsonify({'ready': True, 'url': f'/assets/{sid}/{rel_path}'})


