"""Vision-based description of user-uploaded character/outfit photos."""
from concurrent.futures import ThreadPoolExecutor

from sw.llm import claude_ask_vision
from sw.locks import _series_lock
from sw.seedance import _avai_upload_local_image
from sw.storage import load_series, save_series, series_path

def _describe_character_visual(image_url: str, char_name: str = '') -> str:
    """Vision-extract a FULL character appearance description from a user-
    uploaded portrait. Used by import-from-script to backfill an empty
    `appearance` field on chars the user pre-uploaded (only ref_images
    saved, no text → BINDING line was just the name → Seedance lost the
    text-side anchor for outfit/style continuity).

    Returns a Russian descriptive sentence covering: возраст, телосложение,
    волосы, лицо, одежда. Suitable as drop-in for `char.appearance` which
    feeds `_canonical_char_description` → Seedance BINDING.
    """
    if not image_url or not image_url.lower().startswith(('http://', 'https://')):
        return ''
    name_hint = f' Персонаж в сериале называется "{char_name}".' if char_name else ''
    prompt = (
        "Опиши внешность человека на фото для AI-генерации последующих "
        "изображений того же персонажа в разных сценах." + name_hint + "\n\n"
        "Ровно 1-2 предложения на русском, comma-separated descriptors, "
        "максимум 250 символов. Покрой:\n"
        "  • возраст (примерный диапазон, e.g. «около 30 лет», «лет 50»)\n"
        "  • пол / телосложение (e.g. «худощавый мужчина», «стройная женщина»)\n"
        "  • волосы (цвет, длина, причёска)\n"
        "  • лицо (один-два запоминающихся черта: «волевая челюсть», «миндалевидные глаза»)\n"
        "  • одежда (top + bottom + ключевые аксессуары)\n\n"
        "Примеры выходов:\n"
        "  «Молодая женщина около 28 лет, стройная, длинные тёмно-каштановые волосы убраны в "
        "    хвост, миндалевидные карие глаза, тёмно-синие медицинские scrubs, бейдж на груди»\n"
        "  «Мужчина средних лет, около 45, плотного телосложения, короткие седеющие волосы, "
        "    тёплые серые глаза, угольно-серый трёхпредметный костюм, белая рубашка»\n"
        "  «Подросток лет 16, худощавый, коротко стриженые чёрные волосы, бледная кожа, "
        "    потёртая джинсовая куртка, серая толстовка»\n\n"
        "ВАЖНО: НЕ описывай фон, освещение, позу, выражение эмоций, мимику. "
        "ТОЛЬКО физический look + одежда. Без кавычек, без префикса «Это:», "
        "без «На фото мы видим». Только описание."
    )
    try:
        text = claude_ask_vision(prompt, [image_url]).strip()
        text = text.replace('\n', ' ').strip().strip('"').strip('«»').strip("'").rstrip('.')
        # Hard-cap so we don't pollute BINDING with a wall of text.
        if len(text) > 280:
            text = text[:277] + '...'
        # A user-uploaded portrait may be risqué; keep the persisted description
        # moderation-safe so it never trips the filter on downstream prompts.
        return _sanitize_appearance_for_moderation(text)
    except Exception as e:
        print(f'[char_vision] failed for {char_name or image_url}: {e}', flush=True)
        return ''


def _backfill_uploaded_char_appearances(sid, char_ids):
    """Background worker for import-from-script: for each char id, lazy-upload
    the first ref image to AVAI storage, call Vision to extract a Russian
    `appearance` description, and persist back to series. Safe to run in
    parallel with `_import_worker` — each takes a fresh `load_series` and
    only mutates fields the worker doesn't touch.

    Uses a ThreadPoolExecutor with 3 lanes so 5+ uploads finish in ~10-15s
    instead of 30-60s sequential. Each char's Vision call is independent."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(cid):
        try:
            s = load_series(sid)
            if not s:
                return
            char = next((c for c in (s.get('characters') or []) if c['id'] == cid), None)
            if not char:
                return
            if (char.get('appearance') or '').strip():
                return   # already populated — don't overwrite user-edited or worker-set text
            refs = char.get('ref_images') or []
            if not refs:
                return
            primary_rel = refs[0]
            primary_abs = series_path(sid) / primary_rel
            if not primary_abs.exists():
                return
            # AVAI public URL (Claude Vision needs HTTPS, can't read local files).
            try:
                avai_url = char.get('avai_base_url') or _avai_upload_local_image(primary_abs)
            except Exception as e:
                print(f'[backfill_char] AVAI upload failed for {char.get("name")}: {e}', flush=True)
                return
            description = _describe_character_visual(avai_url, char.get('name', ''))
            if not description:
                return
            # Persist atomically — re-load to avoid clobbering parallel updates.
            with _series_lock(sid):
                s2 = load_series(sid)
                if not s2:
                    return
                ch2 = next((c for c in (s2.get('characters') or []) if c['id'] == cid), None)
                if not ch2:
                    return
                # Don't overwrite if some other code path filled it meanwhile.
                if not (ch2.get('appearance') or '').strip():
                    ch2['appearance'] = description
                    if avai_url and not ch2.get('avai_base_url'):
                        ch2['avai_base_url'] = avai_url
                    save_series(sid, s2)
                    print(f'[backfill_char] filled appearance for {ch2.get("name")}: {description[:80]}...', flush=True)
        except Exception as e:
            print(f'[backfill_char] crashed for cid={cid}: {type(e).__name__}: {e}', flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(_one, char_ids))


def _describe_outfit_visual(image_url: str) -> str:
    """Vision-extract a tight clothing description from a character/outfit photo.
    Returns short string like 'navy medical scrubs, ID badge on chest, hair in
    messy bun'. Used as a TEXT REINFORCEMENT alongside the @Image-ref so Seedance
    gets aligned signals (visual + textual say the same thing) — fixes outfit
    drift that the visual ref alone doesn't prevent."""
    if not image_url or not image_url.lower().startswith(('http://', 'https://')):
        return ''
    prompt = (
        "Describe ONLY what this person is wearing and their immediate look "
        "(hair styling, makeup if striking) in 8-15 words. English. Comma-separated.\n\n"
        "Format: '<garment top>, <garment bottom or accessories>, <hair>'\n"
        "Examples:\n"
        "  navy blue medical scrubs, hospital ID badge on chest, hair in messy bun\n"
        "  charcoal three-piece suit, white shirt, slicked-back hair\n"
        "  cream silk blouse, dark fitted skirt, hair in loose waves\n"
        "  denim jacket, white tee, dark blue jeans, short tousled hair\n\n"
        "DO NOT describe face features, age, gender, expression, body, "
        "background, or pose. ONLY clothing + hair styling. Under 15 words. "
        "No quotes, no leading 'The person is wearing' — just the description."
    )
    try:
        text = claude_ask_vision(prompt, [image_url]).strip()
        text = text.replace('\n', ' ').strip().strip('"').strip("'").rstrip('.')
        return text[:200]
    except Exception as e:
        print(f'[outfit_vision] failed for {image_url}: {e}', flush=True)
        return ''


from sw.imagetags import (
    _remap_image_tags,
    _build_image_tag_remap,
)

from sw.textrules_banlists import (
    _SD_BANLIST,
    _seedance_apply_banlist,
    _SD_FORBIDDEN_AUTONOMY_RE,
    _seedance_validate_autonomy,
    _seedance_validate_durations,
    _SD_WARDROBE_WORDS,
    _seedance_validate_wardrobe,
    _seedance_filter_blocking_for_chunk,
    _extract_script_blocking,
    _seedance_inject_blocking,
    _SD_HARD_CUTS_CLAUSE,
    _seedance_inject_hard_cuts,
    _prev_episode_ending_context,
    _build_episode_tag_mapping,
    _VAGUE_CLOTHING_TAIL_RE,
    _strip_vague_clothing_tail,
    _GARMENT_NOUN,
    _CLOTHING_CLAUSE_RE,
    _BARE_GARMENT_CLAUSE_RE,
    _strip_concrete_clothing,
    _HAIR_LOOK_WORDS,
    _OUTFIT_HAIR_RE,
    _HAIR_ADJ,
    _HAIR_CLAUSE_RE,
    _outfit_hair_phrase,
    _override_hair_in_appearance,
)
from sw.textrules_sanitizer import (
    _ID_HAIR_TOKENS,
    _ID_HAIR_CHANGE_RE,
    _ID_HAIR_WORD_RE,
    _ID_ALIAS_RE,
    _ID_RESTORE_RE,
    _norm_hair_token,
    _detect_identity_shift,
    _UNDRESSED_STATE_RE,
    _DRESSED_OUTFIT_RE,
    _UNDRESSED_OUTFIT_RE,
    _detect_char_undressed_states,
    _undress_state_clothing,
    _binding_desc_with_undress,
    _APPEARANCE_ADJ_MAP_RU,
    _APPEARANCE_ADJ_MAP_EN,
    _APPEARANCE_NUDE_RE,
    _APPEARANCE_NUDE_RU_RE,
    _APPEARANCE_SOFTEN,
    _APPEARANCE_EN_ADJ_RE,
    _sanitize_appearance_for_moderation,
    _canonical_char_description,
)
from sw.jsonutils import (
    strip_json,
    _repair_llm_json,
    _strip_markdown_fence,
    loads_lenient,
)
from sw.avai import (
    save_config,
    rtl_headers,
    _avai_call,
    _series_image_provider,
    avai_generate,
    allowed_file,
)
