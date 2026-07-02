import json
import os
import sys

# ── macOS fork-safety ────────────────────────────────────────────────────
# This process is multithreaded (ThreadPoolExecutor drives the QC pipeline
# and autogen). When a worker thread shells out via subprocess (fork+exec of
# ffmpeg / `open` / ffprobe), macOS's Objective-C runtime detects a fork from
# a multithreaded Obj-C-initialized process and SIGKILLs the child *between*
# fork and exec — crash signature:
#     Termination Reason: Namespace OBJC, Code 1
#     "crashed on child side of fork pre-exec"  (Thread: ThreadPoolExecutor-0_0)
# The Obj-C runtime gets initialized in the parent by `requests`/urllib doing
# a CFNetwork system-proxy lookup. OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
# tells the runtime not to abort the forked child.
#
# CRITICAL: libobjc reads this env var exactly once, when its runtime
# initializes (_objc_init) — which on a Python.framework build can happen at
# interpreter startup, BEFORE this module runs. Setting os.environ here would
# then be too late and the crash returns. So when launched directly (dev:
# `python app.py`) and the var isn't already present, we re-exec the
# interpreter with the var set in the environment — guaranteeing it's seen
# before libobjc initializes. The value-check guards against an exec loop and
# also covers Werkzeug's debug reloader child (which inherits the env).
if __name__ == '__main__' and os.environ.get('OBJC_DISABLE_INITIALIZE_FORK_SAFETY') != 'YES':
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    os.execv(sys.executable, [sys.executable] + sys.argv)
# Production runs under gunicorn (imports this module, __name__ != '__main__')
# on Linux, where this whole class of crash doesn't exist — setdefault is a
# harmless no-op signal there. Local `gunicorn app:app` on macOS would still
# want the var in its launch env; the re-exec above only covers `python app.py`.
os.environ.setdefault('OBJC_DISABLE_INITIALIZE_FORK_SAFETY', 'YES')

import re
import uuid
import random
import secrets
import shutil
import copy
import datetime
import time
import subprocess
import threading
import hashlib
import requests
from collections import Counter
from pathlib import Path
from functools import wraps
from urllib.parse import urlencode
from flask import (Flask, request, jsonify, render_template, send_from_directory,
                   session, redirect, url_for, abort)
from werkzeug.utils import secure_filename
try:
    from authlib.integrations.flask_client import OAuth
    _AUTHLIB_AVAILABLE = True
except ImportError:
    _AUTHLIB_AVAILABLE = False


from sw.utils import (
    _TRANSLIT,
    slugify,
    asset_name,
)

from sw.scriptparse import (
    _SCENE_HEADING_FORMAL_RE,
    _SCENE_HEADING_INFER_RE,
    _SLUG_BLOCKLIST_RE,
    _TRANSITION_PREFIX_RE,
    _LOWERCASE_LETTER_RE,
    _UPPERCASE_LETTER_RE,
    _is_all_caps_slug,
    _is_bracket_slug,
    is_scene_heading,
)

from sw.core import app, STATIC_VERSION

from sw.config import (
    BASE,
    DATA_ROOT,
    LEGACY_PROJECTS,
    CONFIG_FILE,
    RETELLER_API,
    AVAI_API,
    _read_config_field,
    _load_secret,
    PRIMARY_USER_EMAIL,
    _user_keys_path,
    _load_user_keys,
    _save_user_keys,
)

from sw.routes.auth_admin import (
    client_log,
    _log_request_start,
    _log_request_end,
    _log_uncaught,
    admin_logs,
    admin_users,
    _require_auth,
    login,
    auth_google_start,
    auth_google_callback,
    auth_logout,
    api_me,
    healthz,
    validate_avai_key,
)
from sw.routes.mlg import (
    _mlg_stats_path,
    _load_mlg_stats,
    _save_mlg_stats,
    api_mlg_kill,
    api_mlg_leaderboard,
)
# ── Config ──────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {'reteller_key': ''}

from sw.llm import (
    _MODEL_ALIAS,
    WRITER_MODEL_DEFAULT,
    WRITER_MODEL_WHITELIST,
    _get_openai_client,
    _openai_ask,
    _resolve_writer_model,
    llm_ask,
    _get_anthropic_client,
    claude_ask,
    anthropic_ask,
    claude_ask_fast,
    claude_ask_quality,
    claude_web_research,
    claude_ask_vision,
)
from sw.vision import (
    _describe_character_visual,
    _backfill_uploaded_char_appearances,
    _describe_outfit_visual,
)
from sw.routes.core_routes import (
    index,
    translate_text,
    config_route,
    user_auto_revise_route,
)
from sw.routes.series_list import (
    list_series,
    rename_out_files,
    update_series_meta,
)

from sw.routes.landmarks import (
    _norm_checkpoint,
    add_or_update_checkpoint,
    delete_checkpoint,
    generate_checkpoint,
    set_finale,
    delete_finale,
    trajectory_validation,
    generate_finale,
    _extract_landmark_character_mismatch,
    build_trajectory_block,
    finale_episode_num,
    is_finale_episode,
    build_finale_contract_block,
    _BRIDGE_CACHE,
    build_finale_bridge_plan,
    toggle_archive,
    toggle_pin,
)
from sw.routes.import_series import (
    _EPISODE_BOUNDARY_PATTERNS,
    _split_script_into_episodes,
    _import_status,
    _llm_extract_episode_entities,
    _import_worker,
    episodes_logic_check_multi,
    episodes_logic_apply_multi,
    import_from_script_logic_check,
    import_from_script_apply_fixes,
    import_from_script_preview,
    _TRANSLATE_DIALOGUES_SYSTEM,
    import_from_script_translate_dialogues,
    _ADAPT_TO_STANDARD_SYSTEM,
    adapt_script_to_standard,
    check_moderation,
    import_from_script,
    import_status,
    generate_script_batch,
    append_from_script,
    backfill_devices,
    reextract_series,
    create_series,
    clone_series_from,
    apply_revisions_to_episode,
    get_series,
    update_series,
    set_series_era,
    set_series_anthro,
    style_presets,
    style_sample,
    regenerate_style_samples,
    serve_style_sample,
    serve_style_sample_cache,
    _rmtree_hard,
    delete_series,
)

from sw.routes.entities import (
    add_character,
    fix_anthro_species,
    update_character,
    delete_character,
    add_location,
    update_location,
    delete_location,
    upload_location_asset,
    delete_location_asset,
    add_item,
    update_item,
    delete_item,
    upload_item_asset,
    delete_item_asset,
    get_char,
    get_outfit,
    add_outfit,
    update_outfit,
    delete_outfit,
    link_outfit_to_base,
    generate_outfit_image,
    save_outfit_frame,
    generate_character_image,
    regenerate_character,
    save_character_frame,
)
    # Download first frame
from sw.routes.images_loc import (
    upload_char_photo,
    upload_loc_photo,
    open_folder,
    generate_location_image,
    regenerate_location,
)
from sw.routes.images_cover import (
    _cover_lead_refs,
    _cover_art_direction_source,
    _COVER_AD_KEYS,
    _series_cover_art_direction,
    _build_cover_prompt,
    _translate_synopsis_to_en,
    get_series_synopsis_en,
    generate_series_cover,
)
from sw.routes.facades import (
    _FACADE_STATUS,
    _facade_status,
    _claude_group_locations,
    facades_group,
    _facade_worker,
    _merge_facade,
    facades_generate,
    auto_facades_for_new_locations,
    facades_list,
    facades_delete,
    facades_regenerate,
    facades_generate_for_location,
    facades_open_folder,
    save_location_frame,
)
from sw.routes.images_items import (
    upload_item_photo,
    generate_item_image,
    regenerate_item,
    _TRANSLIT_DEDUP,
    _norm_for_dedup,
    _llm_dedupe_against_existing,
    _fuzzy_find_item,
    dedupe_series_items,
    detect_items_in_episode,
)
from sw.autogen import (
    _AUTOGEN_LOCKS,
    _AUTOGEN_STATUS,
    _autogen_status,
    _gen_char_base_inline,
    _gen_outfit_inline,
    _gen_loc_inline,
    _gen_item_inline,
    _AUTOGEN_SAVE_LOCKS,
    _AUTOGEN_PARALLELISM,
    auto_generate_missing_assets,
    trigger_autogen_if_enabled,
    toggle_auto_generate,
    autogen_status_endpoint,
    trigger_autogen_sweep,
    reanalyze_outfits,
)

from sw.routes.canon import (
    get_canon,
    rebuild_canon,
    generate_asset_prompt,
)


from sw.routes.ideas import (
    _IDEAS_HISTORY_MAX,
    _ideas_history_path,
    _load_ideas_history,
    _ideas_signature,
    _save_ideas_history,
    _ideas_avoid_recent_block,
    _ideas_research_digest,
    _run_ideas_pipeline_v2,
    _TOP_DRAMAS_SCHEMA,
    _research_top_dramas,
    research_top_dramas,
    _IDEAS_SIMILAR_SYSTEM,
    _ideas_from_drama,
    ideas_from_drama,
    _DRAMA_ANALYSIS_SCHEMA,
    _top_dramas_path,
    _load_top_dramas,
    _save_top_dramas,
    _drama_slug,
    _analyze_drama,
    top_dramas_get,
    top_dramas_scan,
    top_dramas_analyze,
    _resolve_beats,
    _beats_ideas_block,
    _source_outline_episode_block,
    _series_beats_episode_block,
    _modal_setting_to_era_choice,
    _era_setting_block,
    _IDEA_ANTI_MONOTONY,
    _IDEA_ROMANCE_DIRECTIVE,
    _IDEA_FAMILY_SPREAD,
    list_story_beats,
    generate_series_ideas,
    generate_series_from_idea,
)

from sw.story_logic import (
    MILESTONE_EPS,
    _WRITER_SYSTEM,
    _BRIEF_SYSTEM,
    _AUDIT_SYSTEM,
    _LOGIC_HOLE_AUDIT_SYSTEM,
    audit_logic_holes,
    _SCRIPT_DOCTOR_SYSTEM,
    doctor_script,
    _EXTRACT_SYSTEM,
    _build_crowd_constraint_block,
    _count_speaking_characters_per_scene,
    detect_scene_overcrowding,
    _GENERIC_ROLE_WORDS,
    _NAME_TITLES,
    _NAME_FILLER,
    _label_is_unnamed,
    _extract_cast_block_names,
    _extract_speaker_and_blocking_labels,
    detect_unnamed_characters,
    _script_runtime_metrics,
    detect_script_overlength,
    _build_narrative_state_block,
    build_logic_brief,
    audit_script,
    extract_canon_updates,
    rollback_canon_for_episode,
)
from sw.story_writer import (
    _SCRIPT_SYSTEM,
    _build_script_system,
    _BATCH_SCRIPT_SYSTEM,
    _build_batch_script_system,
)
from sw.routes.story_gen import (
    generate_arcs,
    confirm_arc,
    generate_milestones,
    update_milestone,
    regenerate_milestone,
    extract_from_story,
    list_script_history,
    get_script_history_full,
    restore_script_version,
    doctor_episode_script,
    extract_characters_from_script,
    confirm_milestones,
    generate_next_episode_synopsis,
    generate_episode_synopses,
)
from sw.routes.scripts import (
    _LANDMARK_PROGRESS,
    _LANDMARK_LOCK,
    _landmark_progress_set,
    _landmark_progress_get,
    _run_generate_to_landmark_bg,
    generate_to_landmark_status,
    generate_to_landmark,
    generate_episode_script,
    sync_episode_with_cast_block,
    _infer_gender_from_script,
    _parse_cast_block,
)

from sw.routes.reteller_prompts import (
    _RTL_PROMPT_SYSTEM,
    episode_reteller_prompt,
    range_reteller_prompt,
)


from sw.routes.episodes import (
    get_episodes,
    _CREATE_EP_LOCKS,
    _CREATE_EP_IDEMPOTENCY,
    _CREATE_EP_IDEMPOTENCY_TTL,
    create_episode,
    get_episode,
    update_episode,
    update_segment_auto_skips,
    update_line_overrides,
    update_segment_overrides,
    clear_episode,
    delete_episode,
)

from sw.routes.assets import (
    upload_character_asset,
    delete_character_asset,
    upload_style_asset,
    delete_style_asset,
    serve_asset,
    set_skip_autogen,
    relink_assets,
    debug_asset,
)


from sw.routes.reteller import (
    reteller_preview,
    reteller_submit,
    reteller_status,
    reteller_balance,
    reteller_voices,
    reteller_styles,
)
# ════════════════════════════════════════════════════════════════════════════
from sw.seedance import (
    _seedance_chunks,
    _next_chunk_idx,
    _extract_last_frame,
    _detect_cuts,
    _extract_keyframes_at_cuts,
    _purge_continuity_sidecars,
    _AVAI_AUDIT_LOG,
    _AVAI_KILL_SWITCH,
    _avai_rate_lock,
    _avai_recent_submits,
    _AVAI_MAX_PER_FP_10MIN,
    _AVAI_MAX_PER_MINUTE,
    _AVAI_KILLSWITCH_5MIN,
    AVAICircuitBreakerError,
    _SEEDANCE_MODERATION_CHECKER_SYS,
    SEEDANCE_PRECHECK_ENABLED,
    _seedance_moderation_precheck,
    _avai_kill_switch_status,
    _avai_fingerprint,
    _avai_circuit_breaker_check,
    _avai_seedance_start,
    _avai_seedance_status,
    _avai_upload_local_image,
    QC_MAX_RETRIES,
    _QC_NON_ENGLISH_RE,
    _qc_extract_audio,
    _qc_extract_frame_at,
    _qc_whisper_detect,
    _qc_vision_grid_and_subs,
    _qc_check_prompt_english,
    _qc_run_chunk,
    _qc_can_pass,
    _ensure_loc_avai_url,
    _ensure_char_avai_base_url,
    _resolve_ref_url,
    _download_video,
)

from sw.routes.seedance_assets import (
    seedance_upload_ref,
    seedance_list,
    auto_assemble_episode,
    seedance_download_zip,
    generate_scene_blocking,
)


# Reference-style batch-compose rules — adapted from
# /tmp/shadow-founder/services/chunk-builder.ts EPISODE_PLAN_RULES.
# Adapted: variable segment count (not fixed 4+1+1), English promptEn output.
from sw.routes.seedance_batch import (
    _SD_BATCH_RULES,
    _SD_REVISE_BATCH_RULES,
    seedance_revise_batch,
    seedance_batch_compose,
)


from sw.routes.seedance_compose import seedance_compose

import sw.routes.timeline  # registers timeline routes
from sw.routes.seedance_run import (
    seedance_start,
    seedance_poll,
    avai_kill_switch_get,
    avai_kill_switch_clear,
    seedance_qc,
    seedance_delete,
    _heal_chunk_via_claude,
    _HEAL_PROMPT_SYSPROMPT,
    seedance_heal_prompt,
    seedance_rewrite_chunk,
    seedance_patch_ending,
)


if __name__ == '__main__':
    # Dev mode entrypoint. In production we run under gunicorn (see Dockerfile),
    # which hits the `else` branch below.
    debug = os.environ.get('FLASK_DEBUG', '1').lower() not in ('', '0', 'false', 'no')
    port = int(os.environ.get('PORT', '8080'))
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        _recover_inflight_chunks()
        _start_log_cleanup_loop()
    print(f'Series Writer запущен → http://localhost:{port}  (debug={debug})')
    app.run(debug=debug, port=port, host='0.0.0.0', threaded=True)
else:
    # Production: gunicorn imports this module. Run recovery + start log
    # cleanup loop once on boot.
    # SW_SKIP_BOOT=1 imports the module without side effects (route-map checks).
    if os.environ.get('SW_SKIP_BOOT') != '1':
        _recover_inflight_chunks()
        _start_log_cleanup_loop()
