"""Era/world detection and era-aware clothing/document/caption clauses."""
import re

# Ancient Egypt produced characters in leather jackets because the prompt
# fallback was «everyday casual attire».
# Real prod bug 2026-05-25: colleague's Ancient Egypt series rendered modern
# clothes for every character.
_ERA_KEYWORDS = {
    'ancient_egypt': ('egypt', 'pharaoh', 'pyramid', 'nile', 'фараон', 'египет', 'древнего египта', 'нил'),
    'ancient_rome':  ('rome', 'roman', 'caesar', 'gladiator', 'рим', 'цезарь', 'гладиатор'),
    'ancient_greece':('greece', 'greek', 'sparta', 'athen', 'грец', 'спарт', 'афин'),
    'medieval':      ('medieval', 'middle ages', 'knight', 'castle', 'crusade', 'kingdom', 'средневек', 'рыцар', 'замок', 'королевство'),
    'renaissance':   ('renaissance', 'tudor', 'elizabethan', 'florence', 'возрожден', 'тюдор'),
    'victorian':     ('victorian', 'georgian', 'regency', 'edwardian', 'викториан', 'эдуардовск'),
    'wild_west':     ('wild west', 'western', 'cowboy', 'gunslinger', 'frontier', 'вестерн', 'ковбой', 'дикий запад'),
    'edwardian_20s': ('1920s', 'jazz age', 'prohibition', 'roaring twenties', '20-е', 'двадцатые'),
    'wwii':          ('world war ii', 'wwii', 'second world war', '1940s', 'вторая мировая', '40-е'),
    '1950s':         ('1950s', '50s', 'post-war', 'постwar', '50-е', 'пятидесятые'),
    'cold_war_60s':  ('1960s', '60s', 'mod era', 'cold war', '60-е', 'шестидесятые'),
    '70s':           ('1970s', '70s', 'disco era', '70-е', 'семидесятые'),
    '80s':           ('1980s', '80s', 'reagan', '80-е', 'восьмидесятые'),
    '90s':           ('1990s', '90s', '90-е', 'девяностые'),
    'feudal_japan':  ('samurai', 'shogun', 'edo period', 'feudal japan', 'самурай', 'сёгун', 'феодальная япония'),
    'victorian_steampunk': ('steampunk', 'стимпанк'),
    'fantasy':       ('fantasy', 'dragon', 'elf', 'wizard', 'sorcery', 'фэнтези', 'дракон', 'эльф', 'маг', 'волшеб'),
    'post_apocalyptic': ('post-apocalyptic', 'post apocalyptic', 'wasteland', 'постапокал'),
    'sci_fi':        ('sci-fi', 'sci fi', 'science fiction', 'space opera', 'futuristic', 'фантастика', 'космич'),
}

from sw.cast import _char_name_in_text

def _detect_series_era(s):
    """Pure detection: return (era_key, matched_keyword) or (None, None).

    Word-boundary matching — `\\b…\\b` covers both Latin and Cyrillic under
    Python's default Unicode `re`. This prevents the 2026-05-30 incident
    where keyword `'нил'` (Nile river) was substring-matching inside
    «ра**нил**и» in a Russian synopsis and turning a modern crime series
    into an Ancient Egypt asset palette."""
    hay = ' '.join([
        (s.get('genre') or ''),
        (s.get('synopsis') or '')[:500],
        (s.get('title') or ''),
        (s.get('tone') or ''),
        (s.get('logline') or ''),
        (s.get('world_description') or '')[:500],
    ]).lower()
    if not hay.strip():
        return (None, None)
    for era, kws in _ERA_KEYWORDS.items():
        for kw in kws:
            try:
                pat = re.compile(rf'\b{re.escape(kw)}\b', re.UNICODE)
            except re.error:
                continue
            if pat.search(hay):
                return (era, kw)
    return (None, None)


# Period-specific guidance per matched era — clothing, accessories,
# silhouette cues that Banana/Seedream need to render correctly.
_ERA_GUIDES = {
        'ancient_egypt':
            "ERA CONTEXT: Ancient Egypt — period-accurate attire (linen kalasiris/schenti, "
            "gold collar (usekh), kohl eye makeup, sandals or barefoot, bronze/gold jewelry, "
            "natural fabrics, traditional headdresses for nobility). NO modern clothing, NO jeans, "
            "NO leather jackets, NO contemporary accessories.",
        'ancient_rome':
            "ERA CONTEXT: Ancient Rome — period-accurate attire (tunic, toga, palla, stola, "
            "leather sandals/caligae, simple jewelry, period hairstyles). NO modern clothing.",
        'ancient_greece':
            "ERA CONTEXT: Ancient Greece — period-accurate attire (chiton, himation, peplos, "
            "sandals, laurel wreaths for ceremonies). NO modern clothing.",
        'medieval':
            "ERA CONTEXT: Medieval European — period-accurate attire (tunics, gambeson, surcoats, "
            "kirtle, hose, leather boots, hooded cloaks, period-appropriate armor for warriors). "
            "NO modern clothing, NO synthetic fabrics, NO contemporary cuts.",
        'renaissance':
            "ERA CONTEXT: Renaissance — period-accurate attire (doublet, hose, ruff collars, "
            "farthingale skirts, embroidered fabrics, leather boots). NO modern clothing.",
        'victorian':
            "ERA CONTEXT: Victorian/Edwardian — period-accurate attire (frock coats, waistcoats, "
            "high collars, corseted bodices, bustled skirts, top hats, button boots). "
            "NO modern clothing.",
        'wild_west':
            "ERA CONTEXT: American Wild West (1860s-1890s) — period-accurate attire (denim/canvas "
            "trousers, vests, button shirts, dusters, cowboy hats, leather boots, gun belts). "
            "NO modern jeans/jackets — vintage cuts only.",
        'edwardian_20s':
            "ERA CONTEXT: 1920s Jazz Age — period-accurate attire (flapper dresses, drop waists, "
            "cloche hats, finger waves, three-piece suits, fedoras, oxford shoes). NO modern clothing.",
        '1950s':
            "ERA CONTEXT: 1950s post-war — period-accurate attire (full circle skirts, fitted "
            "bodices, petticoats, tailored suits with hats, victory-curl/pin-curl hair, saddle "
            "shoes, horn-rimmed glasses). NO modern clothing, NO contemporary cuts.",
        'wwii':
            "ERA CONTEXT: WWII / 1940s — period-accurate attire (military uniforms of the era, "
            "wide-shouldered suits, A-line skirts, victory rolls hair, utility wear). NO modern clothing.",
        'cold_war_60s':
            "ERA CONTEXT: 1960s — period-accurate attire (mod fashion, mini skirts, slim suits, "
            "go-go boots, beehive hair, bouffant). NO modern clothing.",
        '70s':
            "ERA CONTEXT: 1970s — period-accurate attire (bell-bottoms, wide collars, polyester, "
            "platform shoes, feathered hair). NO modern clothing cuts.",
        '80s':
            "ERA CONTEXT: 1980s — period-accurate attire (shoulder pads, neon, big hair, "
            "high-waisted jeans, leg warmers, oversized blazers). NO 2020s cuts.",
        '90s':
            "ERA CONTEXT: 1990s — period-accurate attire (grunge, baggy jeans, plaid flannel, "
            "slip dresses, choker necklaces). NO 2020s cuts.",
        'feudal_japan':
            "ERA CONTEXT: Feudal Japan — period-accurate attire (kimono, hakama, obi, samurai "
            "armor for warriors, period hairstyles like chonmage). NO modern clothing.",
        'victorian_steampunk':
            "ERA CONTEXT: Victorian Steampunk — Victorian silhouette + brass/copper accessories, "
            "goggles, mechanical details. No 21st-century clothing or tech.",
        'fantasy':
            "ERA CONTEXT: High fantasy — period-inspired attire (medieval/renaissance silhouettes "
            "with fantasy elements; armor for warriors, robes for mages, leather for rogues). "
            "NO modern clothing.",
        'post_apocalyptic':
            "ERA CONTEXT: Post-apocalyptic — improvised/salvaged attire (patched fabrics, layered "
            "scavenged clothing, gas masks/goggles, weathered leather). No pristine modern clothes.",
        'sci_fi':
            "ERA CONTEXT: Sci-fi / futuristic — futuristic attire (sleek bodysuits, tech "
            "accessories, smart fabrics, asymmetric cuts). NO contemporary 2020s casual wear.",
}


# Human-readable era labels — used by UI confirmation banner.
_ERA_LABELS = {
    'ancient_egypt':       'Древний Египет',
    'ancient_rome':        'Древний Рим',
    'ancient_greece':      'Древняя Греция',
    'medieval':            'Средневековье',
    'renaissance':         'Возрождение',
    'victorian':           'Викторианская эпоха',
    'wild_west':           'Дикий Запад',
    'edwardian_20s':       '1920-е',
    '1950s':               '1950-е',
    'wwii':                '1940-е / Вторая Мировая',
    'cold_war_60s':        '1960-е',
    '70s':                 '1970-е',
    '80s':                 '1980-е',
    '90s':                 '1990-е',
    'feudal_japan':        'Феодальная Япония',
    'victorian_steampunk': 'Стимпанк',
    'fantasy':             'Фэнтези',
    'post_apocalyptic':    'Постапокалипсис',
    'sci_fi':              'Sci-fi / Будущее',
}


def _series_era_hint(s):
    """Return era-context guide string for asset generation, or '' for modern.

    Respects the user's explicit choice stored on the series:
      • era_choice='modern' (or 'none')   → '' (always modern)
      • era_choice=<era key in _ERA_GUIDES> → that era's guide
      • era_choice='auto'/missing/'pending':
          – run detection;
          – if nothing detected → ''
          – if detected AND `era_confirmed` is True → guide for detected era
          – if detected AND not confirmed → '' (safe default; UI surfaces
            a confirmation banner asking the user to accept / change /
            decline before any non-modern look is applied)

    Why the «not-confirmed → ''» branch: detection has fired false-positives
    in production (substring match of «нил» in «ранили» → Ancient Egypt for
    a modern crime series). The user wants a confirmation gate before any
    non-modern style is locked in."""
    choice = (s.get('era_choice') or 'auto').strip().lower()
    if choice in ('modern', 'none', ''):
        return ''
    if choice in _ERA_GUIDES:
        return _ERA_GUIDES[choice]
    # 'auto' / 'pending' / anything else — fall back to detection
    era, _kw = _detect_series_era(s)
    if not era:
        return ''
    if not s.get('era_confirmed'):
        return ''  # gated — wait for user to confirm via UI
    return _ERA_GUIDES.get(era, '')


# Keywords that indicate clothing is already described in appearance/description.
_CLOTHING_WORDS = (
    'wearing', 'dressed', 'outfit', 'shirt', 'blouse', 'dress', 'skirt',
    'pants', 'trousers', 'jeans', 'jacket', 'coat', 'suit', 'uniform',
    'sweater', 'hoodie', 'vest', 'shorts', 'gown', 'robe', 'cloak',
    'clothes', 'clothing', 'attire', 'wardrobe', 'fabric', 'garment',
    # Russian equivalents
    'одет', 'носит', 'костюм', 'платье', 'рубашка', 'блузка', 'юбка',
    'брюки', 'джинсы', 'куртка', 'пальто', 'свитер', 'худи', 'шорты',
    'халат', 'мантия', 'одежда', 'форма',
)

def _clothing_clause(appearance: str, description: str = '', era_hint: str = '') -> str:
    """Return a clothing-fallback clause when neither appearance nor description
    mentions clothing. Prevents models from defaulting to lingerie/swimwear.

    If `era_hint` is set (series is historical/non-modern), the fallback is
    period-aware: «Fully clothed in period-appropriate attire matching the
    series setting.» Without era_hint, defaults to «everyday casual» which
    biases toward modern clothing — wrong for Ancient Egypt, Wild West, etc.
    """
    combined = (appearance + ' ' + description).lower()
    if any(w in combined for w in _CLOTHING_WORDS):
        return ''
    if era_hint:
        return 'Fully clothed in period-appropriate attire matching the series setting. '
    return 'Fully clothed in everyday casual attire. '


# Document-prop keywords. When an item's name OR description matches one of
# these, Banana defaults to «antique parchment with wax seal» because most
# training data tagged «document/contract/inheritance/registry» is historical.
# We catch this and inject a modern-document directive instead.
_DOCUMENT_KEYWORDS = (
    # English roots
    'document', 'contract', 'paper', 'letter', 'note', 'folder', 'file',
    'dossier', 'registry', 'register', 'certificate', 'form', 'report',
    'draft', 'statement', 'agreement', 'deed', 'will', 'testament', 'license',
    'permit', 'passport', 'manuscript', 'ledger', 'log', 'record', 'envelope',
    'invoice', 'receipt', 'bill', 'lease', 'résumé', 'resume', 'cv',
    'application', 'memo', 'dispatch', 'photograph', 'photo', 'photos',
    'newspaper', 'magazine', 'flyer', 'pamphlet', 'brochure', 'card', 'pass',
    # Russian roots
    'документ', 'докум', 'контракт', 'договор', 'бумаг', 'письм', 'записк',
    'папка', 'досье', 'регистр', 'реестр', 'сертификат', 'свидетельств',
    'форма', 'отчёт', 'отчет', 'заявлен', 'черновик', 'акт', 'заявка',
    'лицензия', 'паспорт', 'манускрипт', 'рукопис', 'грамот', 'дело',
    'протокол', 'конверт', 'счёт', 'счет', 'квитанция', 'дневник',
    'фотограф', 'фото', 'снимок', 'газета', 'журнал', 'листовк', 'буклет',
    'карточка', 'пропуск', 'удостоверение', 'визитка',
)


def _modern_document_directive(item) -> str:
    """If the item looks like a document/paper prop, return a directive that
    forces a modern crisp office-aesthetic look. Banana/Gemini Image Pro
    defaults to «yellowed antique parchment with wax seal + cursive» for
    anything that smells like «document», «contract», «registry», «грамота».
    This pulls it back to «something you'd see on a desk today».

    Skip when:
      • The item already has user-set `image_constraints` — user has spoken,
        don't contradict their explicit wish.
      • Name/description hit none of the document keywords.
    """
    if (item.get('image_constraints') or '').strip():
        return ''
    haystack = (
        (item.get('name') or '') + ' ' + (item.get('description') or '')
    ).lower()
    if not any(kw in haystack for kw in _DOCUMENT_KEYWORDS):
        return ''
    return (
        " MODERN DOCUMENT STYLE — STRICTLY ENFORCE: crisp clean modern paper "
        "(A4 / US letter size if applicable), contemporary printed or laser-"
        "printed text in standard digital typography (Times / Arial / Helvetica), "
        "white or very pale cream paper, sharp clean edges, today's office "
        "aesthetic — looks like it was printed yesterday. "
        "ABSOLUTELY NO: yellowing, no aging, no fading, no tea-stained paper, "
        "no parchment, no scrolls, no medieval / 19th-century styling, "
        "no leather-bound antique books, no calligraphic cursive handwriting, "
        "no illuminated manuscript decorations, no wax seals (red / brown / any), "
        "no ribbon binding, no string-tied stacks, no rough deckle edges, "
        "no quill / inkwell / fountain-pen drama. "
        "If signatures appear — modern blue or black ballpoint / fountain-pen ink "
        "on the signature line, NOT elaborate cursive flourishes. "
        "If multiple pages — neatly stacked or stapled like in an office, "
        "not bundled with string. "
    )


# Genitive/possessive endings we strip off a cast name so "Виски Маркуса" and
# "Marcus's whiskey" both collapse to the bare object. Russian genitive +
# common case endings; English handled separately via the apostrophe form.
_NAME_INFLECTIONS = (
    'а', 'я', 'ы', 'и', 'у', 'ю', 'е', 'ом', 'ём', 'ой', 'ей', 'ью',
    'ах', 'ях', 'ов', 'ев', 'ин', 'ина', 'ум',
)

def _strip_cast_names_for_visual(text, s):
    """Remove KNOWN cast names (and their possessive/genitive inflections) from
    a visual-subject string.

    Root cause of the «надпись на предмете» bug: asset names are possessive
    labels — «Виски Маркуса», «Квартира Маркуса», «Marcus's whiskey». They are
    fed as the LEADING subject of the image prompt, and Banana/Gemini/Seedance
    read a leading noun phrase as a CAPTION and literally stamp it onto the
    render (a whisky label reading «Виски Маркуса», a nameplate on the building
    reading «Квартира Маркуса»). Stripping the owner's name leaves the bare
    object/place — exactly what should be drawn. Targeted to cast names only,
    so it can't mangle generic descriptions.
    """
    if not text:
        return text
    names = sorted(
        ((c.get('name') or '').strip() for c in (s.get('characters') or [])),
        key=len, reverse=True,
    )
    for nm in names:
        if len(nm) < 3:
            continue  # too short → false-positive risk inside other words
        esc = re.escape(nm)
        # English possessive: Marcus's / Marcus' / Marcus’s
        text = re.sub(rf"\b{esc}['’]s?\b", '', text, flags=re.IGNORECASE)
        # Bare name + optional RU genitive/case ending: Маркуса, Маркусу, Marcus
        endings = '|'.join(sorted(_NAME_INFLECTIONS, key=len, reverse=True))
        text = re.sub(rf"\b{esc}(?:{endings})?\b", '', text, flags=re.IGNORECASE)
    # Tidy up the holes left behind ("Виски  ." → "Виски").
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'\s+([.,;:])', r'\1', text)
    return text.strip(' .,-—«»"')


_TEXT_REQUEST_WORDS = (
    'text', 'sign', 'signage', 'label', 'lettering', 'word', 'caption',
    'logo', 'brand', 'plaque', 'banner', 'inscription', 'engrav', 'written',
    'надпис', 'текст', 'вывеск', 'этикетк', 'логотип', 'буква', 'слов',
    'табличк', 'баннер', 'гравиров', 'написан',
)

def _no_caption_text_clause(user_constraints: str = '') -> str:
    """Hard directive forbidding the image model from stamping the asset's name
    (or any person's name) onto props / buildings / locations as literal text.

    This is COSMETIC-text suppression, not document suppression — document
    props that legitimately need printed text get _modern_document_directive
    instead and must SKIP this clause (see call sites).

    If the user's own constraints explicitly ask for text/signage/a label,
    return nothing — don't fight an explicit wish."""
    if user_constraints and any(w in user_constraints.lower() for w in _TEXT_REQUEST_WORDS):
        return ''
    return (
        " NO TEXT ON THE IMAGE — STRICTLY ENFORCE: do not render any text, "
        "letters, words, names, captions, titles, labels, nameplates, logos, "
        "brand names, signage or writing anywhere in the frame. Never spell "
        "out the name of this object / place or any person's name on it. Any "
        "surface that would normally carry text (a bottle label, a shop sign, "
        "a door plaque, a banner) must be left blank or show only abstract, "
        "illegible, non-lettered marks. "
    )


# ── Characters ───────────────────────────────────────────────────────────────

