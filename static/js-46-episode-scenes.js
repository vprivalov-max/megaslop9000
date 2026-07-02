// ── Scene & 15s-segment view ─────────────────────────────────────────────
const SCENE_COLORS = [
  'rgba(124,92,252,0.10)',  // purple
  'rgba(0,212,170,0.10)',   // teal
  'rgba(255,165,2,0.10)',   // orange
  'rgba(46,213,115,0.10)',  // green
  'rgba(255,71,87,0.10)',   // red
  'rgba(54,162,235,0.10)',  // blue
  'rgba(232,67,147,0.10)',  // pink
];
const SCENE_BORDERS = [
  '#7c5cfc', '#00d4aa', '#ffa502', '#2ed573', '#ff4757', '#36a2eb', '#e84393',
];

// Match SLUGLINE (location heading) — start of a new scene.
//   English: INT., EXT., INT./EXT., I/E.
//   Russian: ИНТ., ИНТА. (typo seen in scripts), ЭКСТ., ЭКС., НАТ., НАТУРА.,
//            ВНУТР., ИНТЕРЬЕР, ВНЕ, СНАРУЖИ
// `[\s*_#>]*` allows leading markdown decorators (**, __, #, >) before the cue
const SCENE_HEADING_RE = /^[\s*_#>]*(INT\.|EXT\.|INT\.?\s*\/\s*EXT\.?|I\/E\.|ИНТ\.|ИНТА\.|ЭКСТ\.|ЭКС\.|НАТ\.|НАТУРА\.|ВНУТР\.|ИНТЕРЬЕР|ВНЕ\.|СНАРУЖИ)\s+/i;

// Inferred scene heading: when the writer didn't bother with INT./EXT./ИНТ.
// — but the line still clearly opens a new scene. Sub-patterns:
//   • "Локация: ..." or "LOCATION: ..." context preamble
//   • Numbered: "СЦЕНА 5", "Сцена 5.", "SCENE 12"
//   • Time-coded beat: "0:00—0:05 — Hook" / "0:05–0:15 — Arrival" / "1:30 —
//     On the way" — common in short-drama / vertical TikTok formats where
//     the writer marks beats by timestamp instead of slug. Each beat is
//     typically a new shot/location, so treat as scene break.
const SCENE_HEADING_INFER_RE = /^[\s*_#>]*(Локация\s*[:：]|Location\s*[:：]|СЦЕНА\s*\d|Сцена\s*\d|SCENE\s*\d|\d{1,2}:\d{2}\s*[—–\-])/i;

// Control / structural tokens that LOOK slug-ish but aren't scene starts.
const _SLUG_BLOCKLIST_RE = /^(REVERSAL|END|FIN|КОНЕЦ|TBD|TBC|БИТ|BIT|HOOK|TWIST|CLIFFHANGER|КЛИФФХЭНГЕР|РАЗВОРОТ|ПАУЗА|ТИШИНА|FLASHBACK|FLASH BACK|MONTAGE|МОНТАЖ|VOICE OVER|V\.O\.|O\.S\.)$/i;

// Standalone ALL-CAPS slug like "ДОМ АННЫ — НОЧЬ" or "OFFICE — DAY".
// Must look like a place/time tag: 5..80 chars, no lowercase letters, no colon.
// CRITICAL: bare single-word ALL-CAPS lines (SOPHIE, ISABELLE, MIA, etc.) are
// speaker labels in standard screenplay format — NOT scene slugs. Real scene
// slugs almost always have at least a dash (location + time-of-day) or 2+
// words. Single token = name cue. This guard fixes the common bug where
// every speaker label opened a new "scene".
function _isAllCapsSlug(t) {
  if (!t) return false;
  if (t.length < 5 || t.length > 80) return false;
  if (/[:：\[\]#]/.test(t)) return false;       // character cues, dialogue, brackets, markdown #
  if (/[a-zа-яё]/.test(t)) return false;        // any lowercase → not a slug
  if (!/[A-ZА-ЯЁ]/.test(t)) return false;       // need at least one letter
  if (/^(FADE|CUT|DISSOLVE|SMASH|MATCH)\b/i.test(t)) return false;
  if (_SLUG_BLOCKLIST_RE.test(t.replace(/[\.\—\-\s]+$/, ''))) return false;
  // Need either a dash (—/–/-) OR 2+ space-separated tokens. Otherwise
  // it's most likely a speaker cue like "SOPHIE" / "ISABELLE".
  const hasDash = /[—–\-]/.test(t);
  const tokens = t.split(/\s+/).filter(Boolean);
  if (!hasDash && tokens.length < 2) return false;
  // Even with 2 tokens, both can be a name+lastname («JANE DOE»). Require
  // at least one «time-of-day» / «scene-context» keyword OR a dash to avoid
  // false-positives for char cues with surnames.
  const hasContext = /\b(DAY|NIGHT|MORNING|EVENING|DAWN|DUSK|AFTERNOON|MIDNIGHT|ДЕНЬ|НОЧЬ|УТРО|ВЕЧЕР|ПОЛДЕНЬ|РАССВЕТ|ЗАКАТ|СУМЕРКИ|ПОЛНОЧЬ|CONTINUOUS|LATER|MOMENTS LATER|FLASHBACK|ROOM|HOUSE|OFFICE|STREET|КОМНАТА|ДОМ|ОФИС|УЛИЦА|ИНТ|ЭКСТ|КВАРТИРА|КАФЕ|САД|БАР|ПАЛАЦ|ДВОРЕЦ|БАЛЬНАЯ|СПАЛЬНЯ|КУХНЯ|ГОСТИНАЯ|КОРИДОР|ЛЕСТНИЦА|САЛОН|ХОЛЛ|КАБИНЕТ|ВАННАЯ)\b/i.test(t);
  if (!hasDash && !hasContext) return false;
  return true;
}

// Bracketed slug: "[КАФЕ — НОЧЬ]" — must be uppercase-only inside.
// Long mixed-case bracketed lines are action prose, not headings.
function _isBracketSlug(t) {
  const m = t.match(/^\[\s*([^\]]{3,80})\s*\]\s*$/);
  if (!m) return false;
  const inner = m[1].trim();
  if (/[a-zа-яё]/.test(inner)) return false;
  if (_SLUG_BLOCKLIST_RE.test(inner)) return false;
  if (/^(FADE|CUT|DISSOLVE|SMASH|MATCH)\b/i.test(inner)) return false;
  return true;
}

// Combined check: is this line some flavour of scene heading?
// Returns { match: bool, inferred: bool } so the renderer can flag inferred ones.
function _matchSceneHeading(t) {
  if (SCENE_HEADING_RE.test(t)) return { match: true, inferred: false };
  if (SCENE_HEADING_INFER_RE.test(t)) return { match: true, inferred: true };
  if (_isAllCapsSlug(t)) return { match: true, inferred: true };
  if (_isBracketSlug(t)) return { match: true, inferred: true };
  return { match: false, inferred: false };
}

// Lines we filter OUT entirely from scene/segment view (cast, notes, separators).
const _SCRIPT_SKIP_PATTERNS = [
  /^={3,}\s*EPISODE CAST/i,        // start of cast block (handled with toggle)
  /^={3,}\s*END CAST/i,
  /^EPISODE NOTES\b/i,
  /^━+/,
  /^Hook type:/i, /^Reversal type:/i, /^Cliffhanger type:/i,
  /^Escalation rung:/i, /^Spoken word count:/i,
  /^Estimated runtime:/i, /^Setup for next episode:/i,
  // Episode synopsis prefixes — these are meta-description that scriptwriters
  // (and our own /generate-script-batch) put at the top of each episode body.
  // They are NOT screen content and must not become Seedance segments.
  // Captures both «Кратко: ...» (one-liner) and any continuation lines
  // until the next script-style line — handled in the loop via a tracker
  // flag, not just regex match here.
  /^(?:кратко|синопсис|summary|brief|logline|premise|tldr)\s*[:\-—]/i,
  // Episode meta-header block — short-drama scripts often start with:
  //   Episode 1 — Welcome to Palm City
  //     o Length: ~60 seconds
  //     o Dialogue: English
  //     o Style: animated crime drama, tropical 80s city, anthropomorphic
  // These are metadata, NOT visible screen content. Without skipping they
  // turn into a phantom chunk-0 with ~5s duration that renders nothing
  // meaningful. Added 2026-05-19 after user-reported empty chunk in Vice Beasts ep 1.
  /^Episode\s+\d+\s*[—–\-:]/i,
  /^Эпизод\s+\d+\s*[—–\-:]/i,
  // Bulleted meta keys (with `o`, `○`, `•`, `-`, `*` prefix OR no prefix):
  /^\s*[o○•·]\s+(Length|Duration|Dialogue|Language|Style|Tone|Theme|Mood|Genre|Setting|Format|Logline|Pacing)\s*[:：]/i,
  /^(Length|Duration|Dialogue|Language|Style|Tone|Theme|Mood|Genre|Setting|Format|Pacing)\s*[:：]\s*\S/i,
];

// Heuristic per-line duration in seconds (only counts what's actually on screen).
// Per-line on-screen duration (seconds). The 15-sec segmentation packs lines
// into chunks based on these numbers, so what counts here = what eats the budget.
// RULE (calibrated to English short-drama TikTok pacing):
//   • Dialogue replicas are timed by SPEECH_WPS (~3.5 words/sec ≈ 210 wpm —
//     short-drama delivery speed; faster than conversational ~2.4 wps).
//   • Action lines (bracketed OR prose) cost a FIXED 1.5s regardless of length.
//   • Bracketed action notes INSIDE a dialogue line ('[hands letter to her]')
//     are NOT counted as spoken words — they're stage directions, not speech.
//   • Scene headings, transitions, separators: 0s.
const ACTION_BEAT_SEC = 1.5;
const SPEECH_WPS = 2.65;  // ~159 wpm — calibrated to external pro chronometer on actual script dialogue

// Helpers shared between _lineDuration and the segment-builder loop.
// All quote/dash chars short-drama AI scripts use in practice.
const _QUOTE_OPEN_RE  = /^[\s]*["'«»“”„‟‘’‚‛‹›「『]/;
const _QUOTE_CLOSE_RE = /["'«»“”„‟‘’‚‛‹›」』]\s*[.!?…]?\s*$/;
const _DASH_DIALOGUE_RE = /^[—–]\s+\S/;

// Strip wrapping markdown (`**bold**`, `*italic*`, `__bold__`, `_italic_`)
// from the line. AI-generated screenplays often emit `**Clara:**` for cues,
// `**"Hello"**` for emphasized dialogue, or italic-wrapped parentheticals
// like `*(she winks)*` / `**MAYA** *(irritated):*`. Without stripping,
// downstream regexes that anchor on a letter all fail and the line falls
// into generic prose (wrong duration).
function _stripMarkdownWrappers(s) {
  if (!s) return s;
  // Strip a leading **…** that covers the start of the line.
  s = s.replace(/^\*\*([^*]+)\*\*/, '$1');
  s = s.replace(/^__([^_]+)__/, '$1');
  // Strip italic-wrapped parentheticals ANYWHERE in the line:
  //   *(text)*  → (text)
  //   *(text):* → (text):    (italic wraps paren AND its colon — common
  //                            in `**MAYA** *(irritated):* Oh really?`)
  s = s.replace(/\*(\([^)]+\):?)\*/g, '$1');
  s = s.replace(/_(\([^)]+\):?)_/g, '$1');
  // Strip a leading *…* (single asterisk italic) if it wraps a name+colon
  // or a name only — narrow pattern to avoid stripping mid-sentence emphasis.
  s = s.replace(/^\*([A-Za-zА-Яа-яЁё][^*]{0,60})\*/, '$1');
  s = s.replace(/^_([A-Za-zА-Яа-яЁё][^_]{0,60})_/, '$1');
  return s.trim();
}

// Count dialogue words after stripping non-spoken decorations.
function _countDialogueWords(text) {
  if (!text) return 0;
  const stripped = text
    .replace(/^[\s]*["'«»“”„‟‘’‚‛‹›「『]+/, '')        // leading quotes
    .replace(/["'«»“”„‟‘’‚‛‹›」』]+\s*[.!?…]?\s*$/, '')// trailing quotes
    .replace(/\([^)]*\)/g, ' ')                       // (parens — tone notes)
    .replace(/\[[^\]]*\]/g, ' ')                      // [brackets — stage]
    .replace(/\*[^*]*\*/g, ' ')                       // *italics — emphasis*
    .trim();
  return stripped ? stripped.split(/\s+/).filter(Boolean).length : 0;
}

function _lineDuration(line) {
  let t = (line || '').trim();
  if (!t) return 0;
  // Strip leading line-numbering prefix: "1. ", "2) ", "12: ", "3 - " — common
  // when scripts come back with enumerated dialogue/action.
  t = t.replace(/^\d+[.\):\-—–]\s+/, '');
  // Strip wrapping markdown (`**Clara:**`, `*Sofia*`) so downstream regexes
  // anchored on a letter still match.
  t = _stripMarkdownWrappers(t);
  if (!t) return 0;

  if (_matchSceneHeading(t).match) return 0;
  if (/^[-—=]{3,}\s*$/.test(t)) return 0;
  if (/^\[REVERSAL\]\s*$/i.test(t)) return 0;
  if (/^[\s—-]*(FADE|CUT|DISSOLVE|SMASH|MATCH)\s+(IN|OUT|TO|BACK)\b/i.test(t)) return 0;
  // Beat-time headings: "[0:00 — 0:15] HOOK" / "[0:15 - 0:35] BUILD" — meta
  // markers not visible as on-screen content.
  if (/^\[\s*\d+:\d+\s*[—\-–]\s*\d+:\d+\s*\]/.test(t)) return 0;

  // Action line: bracketed prose `[Волк входит и...]`. Scale by length —
  // real action takes time proportional to what's described; a one-liner
  // ≈1.5s, a long sentence ≈4-6s.
  if (/^\[/.test(t) && /\]\s*$/.test(t)) {
    const inner = t.replace(/[\[\]]/g, '').trim();
    if (!inner) return 0;
    return _scaleActionDuration(inner);
  }

  // Dialogue line: "Name: (parens) [stage] actual text" — count by speech rate.
  // Accepts BOTH all-caps (AVA:, MAYA:) and Title-case (Adrian:, Clara:) — modern
  // short-drama scripts use Title-case for character cues, classic screenplay
  // format uses all-caps. Discriminator is the colon — prose lines don't have one.
  const m = t.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-'.#]{0,40})\s*[:：]\s*(.*)$/);
  if (m && /^[A-ZА-ЯЁ]/.test(m[1])) {
    const rest = m[2] || '';
    const words = _countDialogueWords(rest);
    if (!words) return 0.5;
    return 0.4 + words / SPEECH_WPS;
  }

  // European/Russian literary dialogue: «— Привет!» / "— How are you?"
  // Em-dash or en-dash followed by space and dialogue text. Each dash line
  // is a separate beat (speaker alternates by convention).
  if (_DASH_DIALOGUE_RE.test(t)) {
    const inner = t.replace(/^[—–]\s+/, '');
    const words = _countDialogueWords(inner);
    if (words > 0) return 0.4 + words / SPEECH_WPS;
  }

  // Novel-style fully-quoted dialogue line: «"Hello there."» / «"Привет."»
  // No explicit cue — the whole line is just the quoted utterance. Count as
  // dialogue.
  if (_QUOTE_OPEN_RE.test(t) && _QUOTE_CLOSE_RE.test(t)) {
    const words = _countDialogueWords(t);
    if (words > 0) return 0.4 + words / SPEECH_WPS;
  }

  // Novel-style dialogue + attribution: «"I love you," she said.» Line starts
  // with a quote, has a closing quote, then space + more text (attribution).
  // The comma usually sits INSIDE the closing quote («"I love you,"»), so we
  // can't anchor on a comma after — instead we just require the close-quote
  // be followed by space + any non-space char (the attribution continues).
  // We extract just the quoted utterance; the attribution is amortized by
  // the per-line 0.4s pre-pause.
  const quoteAttrMatch = t.match(/^[\s]*["'«»“”„‟‘’‚‛‹›「『](.+?)["'«»“”„‟‘’‚‛‹›」』]\s+\S/);
  if (quoteAttrMatch) {
    const words = _countDialogueWords(quoteAttrMatch[1]);
    if (words > 0) return 0.4 + words / SPEECH_WPS;
  }

  // Standalone parenthetical: prefer event detection over length-based scaling
  // so descriptive parens read as 0s and action parens get 1.5s, consistent
  // with how unwrapped prose lines are billed.
  //   (angry)                       → 0.4s  (short tone-note, no event)
  //   (she winks)                   → 1.5s  (motion verb)
  //   (Adrian laughs in surprise)   → 1.5s  (motion verb, short paren)
  //   (a hot summer afternoon...)   → 0s    (no event, pure description)
  const parenMatch = t.match(/^\((.+)\)\.?$/);
  if (parenMatch) {
    const inner = parenMatch[1].trim();
    const evDur = _scaleProseDuration(inner);
    if (evDur > 0) return evDur;
    return inner.length <= 25 ? 0.4 : 0;
  }

  // Directorial markers — «Pause.» / «Beat.» / «Silence.» / «Тишина.»
  // These are screenplay timing notes, not narrative events. Give a small
  // beat (the cut/cross-fade between actions) but don't bill as an event.
  if (/^(pause|beat|silence|тишина|пауза)[.!]?\s*$/i.test(t)) return 0.5;

  // Orphan screenplay speaker cue — standalone name with no dialogue on the
  // same line ("VIVIENNE", "ETHAN (V.O.)"). 0s; payload line carries time.
  if (_isOrphanSpeakerCue(t)) return 0;

  // Plain prose narration — describes a visual state. 0s unless it contains
  // an event/motion verb (handled in _scaleProseDuration).
  return _scaleProseDuration(t);
}

// Bracketed stage-direction duration: explicit physical actions [Wolf enters].
// These are timed sequences, so scale with complexity; max 6s.
function _scaleActionDuration(text) {
  const chars = (text || '').length;
  return Math.max(ACTION_BEAT_SEC, Math.min(6, 1 + chars / 35));
}

// Unbracketed prose lines fall into two categories:
//   • Scene/character DESCRIPTION — "A car stands on the road.",
//     "Vivienne is lying halfway under it.", "Cash lies on the pavement."
//     These describe a visual state. The camera captures it in one frame
//     regardless of description length → 0 screen-time.
//   • EVENT lines — "A raccoon walks by.", "The raccoon freezes."
//     Something actually HAPPENS on screen → ACTION_BEAT_SEC (1.5s).
//
// Distinction: does the line contain a motion / change-of-state verb?
// If yes → event → 1.5s. If no → pure description → 0s.
// Motion / change-of-state / event verbs. Presence in a prose line means
// something HAPPENS on screen (vs static scene description). Roots only —
// the trailing `\w*` matches all conjugations (walks, walked, walking).
// Cyrillic roots are appended without `\b` (JS `\b` doesn't bracket Cyrillic).
const _PROSE_EVENT_RE = /\b(walk|run|enter|exit|approach|come|go|leave|depart|arrive|return|rush|dart|dash|sprint|flee|escape|jump|leap|hop|spring|bounc|fall|fell|stumbl|trip|slip|slid|roll|crawl|kneel|crouch|squat|grab|snatch|seiz|reach|extend|stretch|pull|push|shov|throw|toss|hurl|fling|catch|hit|punch|kick|slap|smack|whack|strike|swing|spin|twist|whirl|turn|rotat|pivot|freez|halt|stop|paus|brak|mov|cross|step|drop|lift|raise|lower|hoist|climb|mount|descend|slam|burst|smash|crash|crack|shatter|break|tear|rip|snap|bend|fold|crumple|lung|stagger|wobble|sway|sway|collaps|topple|tumbl|nod|shak|tremb|shiver|quiv|wav|gestur|point|wink|blink|stare|gaz|glanc|peek|peer|squint|ogle|watch|observ|spot|notic|appear|emerg|surfac|materializ|manifest|disappear|vanish|fade|dissolv|reveal|expos|hid|conceal|wince|grimac|flinch|jerk|recoil|shudder|smil|grin|smirk|frown|scowl|glare|sneer|beam|chuckl|giggl|snicker|laugh|cry|cri|sob|weep|wail|moan|groan|grunt|gasp|sigh|pant|wheez|huff|puff|breath|exhal|inhal|cough|sneeze|hiccup|sniff|hiss|growl|roar|bark|yelp|yowl|whimper|whin|whisper|murmur|mumbl|mutter|stammer|stutter|exclaim|shout|scream|yell|holler|bellow|call|hum|whistl|sing|chant|recit|spill|leak|drip|trickl|spray|spurt|gush|flow|stream|pour|splash|squirt|soak|drench|coat|cover|wrap|wind|fasten|tie|untie|button|zip|unzip|knock|tap|rap|drum|bang|thump|kiss|hug|embrac|peck|nuzzl|grip|clutch|squeez|crush|hold|carr|drag|tow|haul|press|lean|recline|rest|lay|laid|sit|stand|ris|set|plac|put|drop|tak|grasp|deliver|give|hand|pass|offer|extend|accept|receive|gather|collect|stack|pil|spread|scatter|sprinkl|dust|sweep|brush|wip|polish|scrub|clean|wash|rins|drink|sip|gulp|swallow|chew|bit|nibbl|gnaw|lick|tast|eat|swallow|read|writ|typ|click|sign|stamp|mark|draw|paint|sketch|ride|driv|board|park|wear|don|remov|strip|undress|dress|clip|scrap|scratch|polish|shav|shed|stride|march|pac|wander|amble|stroll|saunter|trudg|trot|gallop|charg|stomp|tiptoe|sneak|creep|slither|wriggl|float|hover|fly|soar|swoop|div|plung|sink|drown|swim|paddl|wad|surf|sail|cruis|drift|reflect|los|win|find|chas|persu|defend|attack|block|dodg|guard|shield|protect|aim|fir|shoot|spotlight|shine|light|ignit|dim|stares?|glanc|admir|inspect|examin|study|scan|surv|focus)\w*\b|\blooks?\s+(at|up|down|over|around|toward|away|back|forward|inside|outside|within)\b|(?:^|[^А-Яа-яЁё])(идт|идёт|идут|шёл|шла|шли|шед|приш|приход|подойд|подойт|подош|подход|подход|уход|ушёл|ушла|ушли|войт|вош|вход|выйт|выш|вых|сел|сел|сядь|сядет|сидел|вста|встал|встаёт|встан|повор|поверн|поверт|посмотр|смотр|смотрел|смотрит|поглянул|глядит|глянул|взял|берёт|брал|бер|откр|закр|подн|опуст|пов|потян|тян|толкн|толка|удар|ударил|двинул|сказа|говор|шепну|шепч|крикн|крич|восклик|улыб|обня|поцелова|обнимат|кивн|кивает|махн|маша|махал|упал|пад|подым|раскр|закры|вышел|вышла|пришёл|пришла|ушёл|ушла|идя|бежа|бежал|бежит|бегут|схвати|схватил|хватает|схватив|поднял|подним|опустил|подош|подошёл|подходит|ткнул|тыкает|тычет|стало|стал|стала|сделал|делает|сделав|загляну|глядя|залез|залазит|пишет|написал|читает|прочёл|прочит|плач|плакал|плачет|смеет|смеёт|смея|засмеял|улыбнул|улыбается|ухмыл)/i;

function _scaleProseDuration(text) {
  if (!text || !text.trim()) return 0;
  return _PROSE_EVENT_RE.test(text) ? ACTION_BEAT_SEC : 0;
}

// Find a chunk's [start, end] byte-offset in the script via long-line anchors.
// Mirrors the server-side logic in app.py compose endpoint.
function _findChunkRange(scriptText, chunkText) {
  if (!chunkText || !scriptText) return [-1, -1];
  const chunkLines = chunkText.split('\n');
  // Pick anchor candidates: long-enough script-content lines whose chunk-line
  // index we remember so we can expand the range from there.
  const cands = [];
  const candChunkIdx = [];
  for (let i = 0; i < chunkLines.length; i++) {
    const s = chunkLines[i].trim();
    if (s.length < 25) continue;
    if (_matchSceneHeading(s).match) continue;
    cands.push(s);
    candChunkIdx.push(i);
    if (cands.length >= 6) break;
  }
  // Locate anchor in scriptText: prefer unique match, fall back to first.
  let anchorOffset = -1;
  let anchorChunkIdx = -1;
  for (let k = 0; k < cands.length; k++) {
    const idx = scriptText.indexOf(cands[k]);
    if (idx === -1) continue;
    if (scriptText.indexOf(cands[k], idx + 1) === -1) {
      anchorOffset = idx;
      anchorChunkIdx = candChunkIdx[k];
      break;
    }
  }
  if (anchorOffset < 0) {
    for (let k = 0; k < cands.length; k++) {
      const idx = scriptText.indexOf(cands[k]);
      if (idx !== -1) { anchorOffset = idx; anchorChunkIdx = candChunkIdx[k]; break; }
    }
  }
  if (anchorOffset < 0) return [-1, -1];
  // Build (start, end) offsets for every script line — used to step line-by-
  // line in either direction while expanding the matched range.
  const scriptLines = scriptText.split('\n');
  const lineRanges = [];
  {
    let off = 0;
    for (const ln of scriptLines) {
      lineRanges.push([off, off + ln.length]);
      off += ln.length + 1;
    }
  }
  // Find the script-line whose offset matches the anchor.
  let anchorScriptIdx = -1;
  for (let i = 0; i < lineRanges.length; i++) {
    if (lineRanges[i][0] === anchorOffset) { anchorScriptIdx = i; break; }
    if (lineRanges[i][0] > anchorOffset) break;
  }
  if (anchorScriptIdx < 0) {
    // Anchor not on a line boundary (shouldn't happen for line-anchored finds).
    // Fall back to the heuristic end-of-chunkText length.
    return [anchorOffset, anchorOffset + chunkText.length];
  }
  // Expand BACKWARDS / FORWARDS line-by-line. Blank lines in EITHER stream
  // (chunk_text packs lines tightly, scriptText has blank rows between
  // speaker groups) are skipped without breaking the walk. Any non-blank
  // mismatch stops the expansion.
  const _trim = (a) => (a || '').trim();
  // Backwards
  let firstScriptIdx = anchorScriptIdx;
  {
    let ci = anchorChunkIdx - 1, si = anchorScriptIdx - 1;
    while (ci >= 0 && si >= 0) {
      const ct = _trim(chunkLines[ci]);
      const st = _trim(scriptLines[si]);
      if (st === '' && ct !== '') { si--; continue; }   // blank in script
      if (ct === '' && st !== '') { ci--; continue; }   // blank in chunk
      if (ct === '' && st === '') { ci--; si--; continue; }
      if (st === ct) { firstScriptIdx = si; ci--; si--; }
      else break;
    }
  }
  // Forwards
  let lastScriptIdx = anchorScriptIdx;
  {
    let ci = anchorChunkIdx + 1, si = anchorScriptIdx + 1;
    while (ci < chunkLines.length && si < scriptLines.length) {
      const ct = _trim(chunkLines[ci]);
      const st = _trim(scriptLines[si]);
      if (st === '' && ct !== '') { si++; continue; }
      if (ct === '' && st !== '') { ci++; continue; }
      if (ct === '' && st === '') { ci++; si++; continue; }
      if (st === ct) { lastScriptIdx = si; ci++; si++; }
      else break;
    }
  }
  return [lineRanges[firstScriptIdx][0], lineRanges[lastScriptIdx][1]];
}

