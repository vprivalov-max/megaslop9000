"""Script generation routes: generate-script, generate-to-landmark,
cast-block sync, gender inference, character/outfit extraction."""
# Split into ordered part-modules; importing them here preserves
# route-registration order and the public import surface.
from sw.routes.scripts_landmark import (
    _LANDMARK_LOCK,
    _LANDMARK_PROGRESS,
    _landmark_progress_get,
    _landmark_progress_set,
    _run_generate_to_landmark_bg,
    _series_beats_episode_block,
    generate_to_landmark,
    generate_to_landmark_status,
    trigger_autogen_if_enabled,
)
from sw.routes.scripts_generate import (
    generate_episode_script,
)
from sw.routes.scripts_cast import (
    _infer_gender_from_script,
    _parse_cast_block,
    sync_episode_with_cast_block,
)
