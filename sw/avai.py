"""AVAI image-generation API: call helper, provider resolution, config save."""
import json
from pathlib import Path

import requests

from sw.auth import _get_user_avai_key, _get_user_reteller_key
from sw.config import AVAI_API, CONFIG_FILE
from sw.state import ALLOWED_EXTENSIONS

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

def rtl_headers():
    return {'Authorization': f'Bearer {_get_user_reteller_key()}'}

def _avai_call(provider: str, prompt: str, reference_url: str = None, aspect_ratio: str = '9:16') -> str:
    """One AVAI call with the given provider. Returns image URL or raises.
    Generates at 1K JPEG instead of 2K PNG — character/location refs are
    used by Seedance internally, which downscales them anyway. 2K PNG was
    bloating each asset to 4-7MB, killing disk + bandwidth on the prod
    volume. 1K JPEG ≈ 200-500KB with no visible quality loss for ref usage.
    """
    payload = {
        'provider': provider,
        'prompt': prompt,
        'num_images': 1,
        'aspect_ratio': aspect_ratio,
        'image_size': '1K',
        'output_format': 'jpg',
    }
    if provider == 'banana':
        payload['model'] = 'pro'
    if reference_url:
        # Accept either a single URL (legacy callers) or a list/tuple
        # (cover-art uses 2 leads as refs).
        if isinstance(reference_url, (list, tuple)):
            urls = [u for u in reference_url if u]
            if urls:
                payload['contextImages'] = [{'url': u} for u in urls]
        else:
            payload['contextImages'] = [{'url': reference_url}]
    avai_key = _get_user_avai_key()
    if not avai_key:
        # Empty key → AVAI returns generic 401. Surface a specific error so the
        # frontend can route the user to Settings instead of showing a wall of
        # raw "Unauthorized" JSON. Code = AVAI_KEY_MISSING.
        raise RuntimeError('AVAI_KEY_MISSING: AVAI API key не задан в твоём аккаунте — открой Settings и введи свой ключ с avai-gen.com')
    headers = {'x-api-key': avai_key, 'content-type': 'application/json'}
    resp = requests.post(AVAI_API, json=payload, headers=headers, timeout=180)
    if resp.status_code == 401:
        # Server got the key, but it's invalid/expired. User needs to refresh
        # their key. Different code than missing — different remedy hint.
        raise RuntimeError('AVAI_KEY_INVALID: AVAI отверг твой API key (401 Unauthorized) — проверь что не истёк, и обнови в Settings → AVAI key')
    if not resp.ok:
        raise RuntimeError(f'AVAI {provider} error {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f'AVAI {provider} generation failed: {str(data)[:300]}')
    images = data.get('images') or []
    image_url = images[0] if images else ''
    # Detect content-filter / placeholder responses (e.g. '/error.jpg' from Banana
    # when Gemini rejects the prompt). These are not real URLs.
    if not image_url or not image_url.lower().startswith(('http://', 'https://')):
        raise RuntimeError(f'AVAI {provider} returned no valid URL (likely content-filter rejection)')
    return image_url


def _series_image_provider(s):
    """Read per-series user preference for image provider order.
    Returns one of: 'banana', 'seedream', '' (auto). Stored on series.json
    via the per-series toolbar dropdown."""
    pref = (s or {}).get('preferred_image_provider', '') or ''
    return pref if pref in ('banana', 'seedream') else ''


def avai_generate(prompt: str, output_path: Path, reference_url: str = None, aspect_ratio: str = '9:16', preferred_provider: str = '') -> str:
    """Generate via AVAI. Tries Banana (Gemini Image Pro) first; on failure
    falls back to Seedream (NOT Seedance — Seedance is video, we need an image)
    with the same prompt + reference. Returns the remote image URL on success,
    raises with a combined error message on total failure.
    aspect_ratio: '9:16' (vertical, default — characters/portraits) or '16:9' (horizontal — locations).
    preferred_provider: '' / 'auto' (default banana→seedream), 'banana', 'seedream', 'openai' —
    explicit user override. If specified, that provider runs FIRST."""
    errors = []
    image_url = None
    if preferred_provider == 'seedream':
        provider_chain = ('seedream', 'banana')
    elif preferred_provider == 'banana':
        provider_chain = ('banana', 'seedream')
    elif preferred_provider == 'openai':
        # OpenAI image gen (gpt-image-1 via AVAI). Falls back to Banana on failure.
        provider_chain = ('openai', 'banana')
    else:
        provider_chain = ('banana', 'seedream')
    for provider in provider_chain:
        try:
            image_url = _avai_call(provider, prompt, reference_url=reference_url, aspect_ratio=aspect_ratio)
            print(f'[avai_generate] {provider} OK → {image_url[:80]}...')
            break
        except Exception as e:
            msg = str(e)
            print(f'[avai_generate] {provider} FAILED: {msg[:200]}')
            errors.append(f'{provider}: {msg}')
            continue
    if not image_url:
        # If ANY error mentions our specific auth markers, the issue is the API
        # key — not the prompt. Show a focused message instead of suggesting
        # "rewrite description more neutrally" which doesn't fix anything.
        joined = ' | '.join(errors)
        if 'AVAI_KEY_MISSING' in joined:
            raise RuntimeError('AVAI_KEY_MISSING: AVAI API key не задан в твоём аккаунте — открой Settings и введи свой ключ с avai-gen.com')
        if 'AVAI_KEY_INVALID' in joined or '401' in joined:
            raise RuntimeError('AVAI_KEY_INVALID: AVAI отверг твой API key (401 Unauthorized). Возможные причины: ключ истёк / неверный / лимит средств исчерпан. Открой Settings → AVAI key и обнови.')
        raise RuntimeError(
            'Оба провайдера AVAI отказали. '
            + joined
            + ' — попробуй переписать описание более нейтрально '
              '(без слов lingerie / bare chest / boxers).'
        )
    # Download and save
    img_resp = requests.get(image_url, timeout=60)
    img_resp.raise_for_status()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(img_resp.content)
    # Lazy migration: AVAI returns 1K JPEG (output_format=jpg), so legacy .png
    # files at the same stem are stale 2K PNG (or older mis-named JPEGs) and
    # should be cleaned up to avoid (a) wasting disk on prod volume and
    # (b) confusing tools that pick the .png by alphabetical sort. Only purge
    # when we just wrote .jpg — never the other way.
    if output_path.suffix.lower() in ('.jpg', '.jpeg'):
        legacy_png = output_path.with_suffix('.png')
        if legacy_png.exists() and legacy_png != output_path:
            try:
                legacy_png.unlink()
            except Exception as e:
                print(f'[avai_generate] could not remove legacy {legacy_png}: {e}')
    return image_url

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


from sw.storage import (
    _safe_email_dir,
    user_root,
    series_path,
    series_file,
    episodes_dir,
    assets_dir,
    vid_dir,
    out_dir,
    facades_dir,
    PRPROJ_TEMPLATE,
    scaffold_series_folders,
    _FILE_LOCKS,
    _FILE_LOCKS_GUARD,
    _file_lock,
    _atomic_write_json,
    _load_json_resilient,
    load_series,
    save_series,
    load_episode,
    save_episode,
    _OLD_TAG_MAP,
    _normalize_blocking_tags,
    _extract_end_position,
    _expand_outfit_label_to_desc,
    _resolve_char_by_script_name,
    _normalize_outfit_label,
    _parse_scene_open_outfits,
    _outfit_word_similarity,
    _series_protagonist,
    _apply_identity_shift_state,
    _sync_script_outfits,
    list_episodes,
    is_batch_mode,
    batch_size,
    chunk_range,
    chunk_label,
    TOTAL_SUB_EPS,
    chunk_count,
    DEVICE_TAXONOMY,
    DEVICE_FUNCTIONS,
    NARRATIVE_ARCHETYPES,
    NARRATIVE_POWER_DELTA,
    NARRATIVE_EMOTIONS,
    NARRATIVE_ANT_MOMENTUM,
    ep_to_chunk,
    anchor_chunks,
    milestone_indices,
    canon_file,
    _empty_canon,
    load_canon,
    save_canon,
    _next_id,
    WORLD_RULES,
)
