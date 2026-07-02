"""Import-series flow: script splitting, per-episode entity extraction worker,
batch script generation, append-from-script, create/clone series, adapt and
phrase-check, series get/update/era/anthro/style-samples/delete."""
# Split into ordered part-modules; importing them here preserves
# route-registration order and the public import surface.
from sw.routes.import_series_worker import (
    _ACTION_LINE_RE,
    _CJK_RE,
    _CYRILLIC_RE,
    _DIALOGUE_LINE_RE,
    _EPISODE_BOUNDARY_PATTERNS,
    _HANGUL_RE,
    _HIRAGANA_RE,
    _IMPORT_LOCKS,
    _IMPORT_STATUS,
    _KATAKANA_RE,
    _LATIN_RE,
    _detect_dialogue_language,
    _import_status,
    _import_worker,
    _llm_extract_episode_entities,
    _split_script_into_episodes,
    episodes_logic_apply_multi,
    episodes_logic_check_multi,
    import_from_script_apply_fixes,
    import_from_script_logic_check,
)
from sw.routes.import_series_flow import (
    _ADAPT_TO_STANDARD_SYSTEM,
    _MOD_TRIGGER_GROUPS,
    _NON_SPEAKER_LABELS,
    _PHRASE_CHECK_SYSTEM,
    _REWRITE_SYSTEM,
    _SOFTEN_MAP,
    _TRANSLATE_DIALOGUES_SYSTEM,
    _author_rewrites,
    _fallback_suggestions,
    _lexical_moderation_scan,
    _merge_moderation_warnings,
    _modkey,
    _soften_line,
    adapt_script_to_standard,
    check_moderation,
    import_from_script,
    import_from_script_preview,
    import_from_script_translate_dialogues,
    import_status,
)
from sw.routes.import_series_batch import (
    append_from_script,
    generate_script_batch,
)
from sw.routes.import_series_create import (
    apply_revisions_to_episode,
    backfill_devices,
    clone_series_from,
    create_series,
    reextract_series,
)
from sw.routes.import_series_meta import (
    _rmtree_hard,
    delete_series,
    get_series,
    regenerate_style_samples,
    serve_style_sample,
    serve_style_sample_cache,
    set_series_anthro,
    set_series_era,
    style_presets,
    style_sample,
    update_series,
)
