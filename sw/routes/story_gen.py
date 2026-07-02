"""Story generation routes: arcs, milestones, synopses, script history,
doctor, character extraction."""
# Split into ordered part-modules; importing them here preserves
# route-registration order and the public import surface.
from sw.routes.story_gen_arcs import (
    confirm_arc,
    doctor_episode_script,
    extract_from_story,
    generate_arcs,
    generate_milestones,
    get_script_history_full,
    list_script_history,
    regenerate_milestone,
    restore_script_version,
    update_milestone,
)
from sw.routes.story_gen_synopses import (
    confirm_milestones,
    extract_characters_from_script,
    generate_episode_synopses,
    generate_next_episode_synopsis,
)
