"""Visual style presets and per-series style clauses."""

# ── Style helpers ────────────────────────────────────────────────────────────

_DEFAULT_VISUAL_STYLE = (
    "Photorealistic, cinematic quality, high detail. "
    "Realistic short-drama TV-series look (TikTok/Reels), natural skin textures, "
    "subtle film grain, professional cinematography lighting."
)

_VISUAL_STYLE_PRESETS = {
    'cinematic': {
        'label': 'Кинематограф',
        'desc':  'Cinematic film look — shallow depth of field, professional color grading (teal/orange or analog film), 35mm aesthetic, soft natural lighting, subtle film grain. Photorealistic skin and materials.',
        'sample': '/style-samples/cinematic.jpg',
    },
    'photorealistic': {
        'label': 'Фотореализм',
        'desc':  'Photorealistic, sharp focus, neutral color grading, even lighting. Skin pores, fabric weave, micro-detail visible. No stylization.',
        'sample': '/style-samples/photorealistic.jpg',
    },
    'anime': {
        'label': 'Аниме',
        'desc':  'Anime style, cel-shaded, clean line art, vibrant flat colors, large expressive eyes, stylized proportions, smooth gradients. Studio-quality animation frame look.',
        'sample': '/style-samples/anime.jpg',
    },
    'pixar': {
        'label': '3D Pixar',
        'desc':  'Pixar 3D animation style, soft volumetric lighting, exaggerated facial expressions, slightly stylised proportions, vibrant saturated palette, cinematic composition.',
        'sample': '/style-samples/pixar.jpg',
    },
    'noir': {
        'label': 'Film Noir',
        'desc':  'Film noir, high-contrast black-and-white, dramatic chiaroscuro lighting, venetian blind shadows, smoky atmosphere, 1940s aesthetic.',
        'sample': '/style-samples/noir.jpg',
    },
    'auto': {
        'label': 'Авто (AI выберет)',
        'desc':  '',
        'sample': '',
    },
}

def _series_visual_style(s):
    """Returns the project's visual style override or the realistic default.
    Two storage paths kept in sync:
      - s['visual_style'] : free-text description (what generation prompts read)
      - s['style']['type']: preset key OR 'custom' (what UI binds to)
    When type is set to a preset, visual_style is force-synced to the preset's
    desc string so picking 'cinematic' actually drives the gen prompts."""
    val = ((s or {}).get('visual_style') or '').strip()
    return val or _DEFAULT_VISUAL_STYLE

def _series_style_clause(s):
    """Inline-style clause for character/location image generation prompts."""
    v = _series_visual_style(s)
    if not v:
        return ""
    # Some hint phrasing depending on whether project picked a stylised look
    low = v.lower()
    stylised = any(k in low for k in (
        'pixar', 'anime', 'manga', 'cartoon', 'claymation', 'oil painting',
        'cyberpunk', 'noir', 'film noir', 'watercolor', 'graphic novel',
        'studio ghibli', 'arcane', 'comic',
    ))
    if stylised:
        return (
            f"VISUAL STYLE — strict: {v}. The whole image MUST be rendered in this style "
            f"(materials, faces, lighting, palette). Do NOT mix with photorealism."
        )
    return f"Visual style: {v}"


def _location_crowd_clause(loc):
    """Decide how a location's establishing shot should be populated.

    The original rule was a blanket "No people, no characters in frame" — meant
    to keep the MAIN cast out of establishing stills (they get rendered later in
    shots). But that also stripped out the ambient crowd/audience that makes a
    venue read as alive, leaving courtrooms, theatres and streets eerily empty.

    The intent: keep the named/foreground cast out, but let anonymous background
    extras populate venues that would realistically have them. Genuinely private
    or intimate spaces (someone's apartment, a private office) stay empty. The
    image model sees the location name + description in the prompt, so it has the
    context to judge public-vs-private; we just instruct it explicitly.

    A per-location `image_constraints` field can override (e.g. "empty courtroom",
    "deserted street") — that text is injected separately and takes precedence.
    """
    return (
        "No main or foreground characters in frame (the named cast is rendered "
        "separately). However, populate the scene with anonymous background "
        "extras appropriate to this kind of place so it feels naturally alive — "
        "e.g. spectators filling the seats of an auditorium, a gallery of people "
        "in a courtroom in session, patrons in a restaurant, passersby and "
        "traffic on a street — while keeping any central stage / focal action "
        "area clear. EXCEPTION: if this is a private or intimate space that "
        "realistically has no bystanders (someone's apartment, a private office, "
        "a bedroom, a closed back room), or if the description implies it is "
        "empty/deserted, then render it with no people at all. "
    )


# Period/era markers — looked up in series genre + synopsis to bias character
# generation away from modern-default clothing. Without this, a series set in
from sw.era import (
    _ERA_KEYWORDS,
    _detect_series_era,
    _ERA_GUIDES,
    _ERA_LABELS,
    _series_era_hint,
    _CLOTHING_WORDS,
    _clothing_clause,
    _DOCUMENT_KEYWORDS,
    _modern_document_directive,
    _NAME_INFLECTIONS,
    _strip_cast_names_for_visual,
    _TEXT_REQUEST_WORDS,
    _no_caption_text_clause,
)
