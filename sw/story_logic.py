"""Story logic pipeline: writer/brief/audit prompt systems, logic-hole audit,
script doctor, crowding/no-name/overlength detectors, logic brief, canon
extraction and rollback."""
# Split into ordered part-modules; importing them here preserves
# route-registration order and the public import surface.
from sw.story_logic_audit import (
    MILESTONE_EPS,
    _AUDIT_SYSTEM,
    _BRIEF_SYSTEM,
    _EXTRACT_SYSTEM,
    _GENERIC_ROLE_WORDS,
    _LOGIC_HOLE_AUDIT_SYSTEM,
    _NAME_FILLER,
    _NAME_TITLES,
    _SCRIPT_DOCTOR_SYSTEM,
    _WRITER_SYSTEM,
    _build_crowd_constraint_block,
    _build_plot_device_history,
    _count_speaking_characters_per_scene,
    _extract_cast_block_names,
    _extract_devices_from_script,
    _extract_narrative_state_from_script,
    _extract_speaker_and_blocking_labels,
    _format_canon_for_prompt,
    _label_is_unnamed,
    _update_devices_index,
    _update_narrative_index,
    audit_logic_holes,
    detect_scene_overcrowding,
    detect_unnamed_characters,
    doctor_script,
)
from sw.story_logic_canon import (
    _build_narrative_state_block,
    _script_runtime_metrics,
    audit_script,
    build_logic_brief,
    detect_script_overlength,
    extract_canon_updates,
    rollback_canon_for_episode,
)
