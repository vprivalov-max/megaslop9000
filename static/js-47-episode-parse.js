function _parseScriptScenes(scriptText, overrides) {
  const rawLines = (scriptText || '').split('\n');
  // Two override maps:
  //   pairMap: keyed by `s${sceneIdx}a${autoSeg}` — canonical post-2026-05-15
  //            entries. Matches exactly one segment break, immune to
  //            duplicate-anchor scripts (dialogue-heavy back-and-forth).
  //   anchorMap: legacy entries without sceneIdx/autoSeg — keyed by anchor.
  //            Still respected so old projects don't lose their tweaks.
  const overridePairMap = new Map();   // 's0a3' → 'merge'|'break'
  const overrideAnchorMap = new Map(); // 'Ethan:' → 'merge'|'break'  (legacy)
  // Defensive: callers must pass an ARRAY of override entries. Some headless
  // paths (standalone parallel runner, music kick-off) historically passed `{}`
  // meaning "no overrides" — but `{}` is truthy and NOT iterable, so the for-of
  // below threw «(overrides || []) is not iterable» and crashed the whole run.
  // Coerce any non-array (incl. {} / null / undefined) to an empty list.
  for (const o of (Array.isArray(overrides) ? overrides : [])) {
    if (!o || !(o.action === 'break' || o.action === 'merge')) continue;
    if (typeof o.sceneIdx === 'number' && typeof o.autoSeg === 'number') {
      overridePairMap.set(`s${o.sceneIdx}a${o.autoSeg}`, o.action);
    } else if (o.anchor) {
      overrideAnchorMap.set(o.anchor, o.action);
    }
  }
  // Combined view for the «do we have any overrides at all?» short-circuit.
  const overrideMap = overrideAnchorMap;
  const scenes = [];
  let inCast = false;
  let inNotes = false;
  let inSynopsis = false;   // tracks multi-line «Кратко: …» / «Summary: …» blocks
  let inBlocking = false;   // tracks [BLOCKING]…[/BLOCKING] and [BLOCKING_OUT]…[/BLOCKING_OUT]
                            // — visual/spatial setup metadata, NOT story content.
                            // Lines inside contribute 0 chrono, never form a segment.
                            // Server-side compose reads them from the raw script
                            // and injects them into the Seedance prompt.
  let blockingKind = '';    // 'in' or 'out' — appended to current scene's metadata
  let pendingEpisodeBlocking = [];  // [BLOCKING] before any scene heading lands here
  let cur = null;
  let runningOffset = 0;
  // Heuristic for «is this line a real script-style content line?» — used to
  // decide when to exit a multi-line synopsis block. Dialogue («NAME:») /
  // bracketed action / parenthetical / dash-led line all count.
  const _isScriptLine = (s) => (
    /^[A-ZА-ЯЁ][A-ZА-ЯЁ\s\.\-']{1,40}:\s/.test(s) ||   // CHAR:
    /^\[[^\]]+\]/.test(s) ||                            // [stage direction]
    /^\([^)]+\)/.test(s) ||                             // (parenthetical)
    /^[-–—]\s/.test(s) ||                               // — line
    /^(?:int\.|ext\.|инт\.|экст\.|нат\.|сцена)\s/i.test(s)  // scene heading
  );
  // Tracks the screenplay-format orphan-cue → payload handoff across lines.
  // True after we've just seen a bare speaker cue ("SOFIA", "DANTE (V.O.)")
  // and are waiting for the dialogue text line that belongs to it.
  let expectingDialogue = false;

  // IMPLICIT-BLOCKING PRE-PASS. Some writer-LLMs emit a setup block WITHOUT
  // fence markers ([BLOCKING_START] / etc.), relying on content patterns like
  // `NAME: position :: OUTFIT: clothes` or standalone `LOCATION:`. Without
  // explicit fences the segmenter happily turns "LOCATION:" into a scene
  // heading and the `:: OUTFIT:` lines into bogus dialogue. Detect those
  // signatures and mark indices to skip from segmentation (they still stay
  // in the raw script so server-side compose injects them).
  const implicitBlockingIdx = new Set();
  const _isOutfitLine = (t) => /::\s*(?:OUTFIT|WEARING|WEAR|CLOTHES|COSTUME)\s*[:：]/i.test(t);
  const _isBlockingMetaKey = (t) =>
    /^(?:LOCATION|MOOD|LIGHTING|PROPS|CAMERA|FRAMING|SETTING|TIME|WEATHER|ATMOSPHERE|ATMOSFERA|ОСВЕЩЕНИЕ|РЕКВИЗИТ|ЛОКАЦИЯ|АТМОСФЕРА)\s*[:：]\s*\S/i.test(t);
  // Stative position verbs (RU + EN) at the START of `NAME: <text>` distinguish
  // a "blocking position line" from real dialogue. Real dialogue starts with
  // pronouns ("I/Я/Ты"), quote, or action — not "стоит/sits".
  // Use a lookahead boundary instead of \b because JS \b doesn't recognise
  // Cyrillic letters as word characters → \b after "стоит" never matches.
  const _POSITION_VERB_RE = /^(?:стои[тю]|стоят|сиди[тю]|сидят|лежи[тшю]|лежат|держи[тшю]|держат|смотри[тшю]|смотрят|одет[аоы]?|оперевш\w*|прислон\w*|сжима\w*|стиска\w*|наблюда\w*|замер\w*|опуст\w*|поднят\w*|опущен\w*|облокот\w*|прижим\w*|нависа\w*|нагиба\w*|склон\w*|присел\w*|развалил\w*|wears?|stands?|sits?|lies?|holds?|looks?\s+at|watches?|leans?|grips?|clenches?|presses?|tilts?|rests?|stays?|crouches?|kneels?|squats?|positions?)(?=[\s,.;:!?]|$)/i;
  // A `NAME: text` line whose text starts with a stative-position verb.
  const _isPositionLine = (t) => {
    const m = t.match(/^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 \-'.#]{0,40}\s*[:：]\s*(.+)$/);
    if (!m) return false;
    return _POSITION_VERB_RE.test(m[1].trim());
  };
  const _isBlockingContent = (t) => _isOutfitLine(t) || _isPositionLine(t) || _isBlockingMetaKey(t);
  for (let i = 0; i < rawLines.length; i++) {
    const t = rawLines[i].trim();
    // Primary trigger: outfit-line OR position-NAME line. Don't trigger on a
    // lone LOCATION: — it might be a real (inferred) scene heading.
    if (!_isOutfitLine(t) && !_isPositionLine(t)) continue;
    implicitBlockingIdx.add(i);
    // Walk BACKWARDS through blanks + blocking-content lines (meta/outfit/pos).
    for (let j = i - 1; j >= 0; j--) {
      const tj = rawLines[j].trim();
      if (!tj) continue;
      if (_isBlockingContent(tj)) { implicitBlockingIdx.add(j); continue; }
      break;
    }
    // Walk FORWARDS too — pick up subsequent blocking-content lines.
    for (let j = i + 1; j < rawLines.length; j++) {
      const tj = rawLines[j].trim();
      if (!tj) continue;
      if (_isBlockingContent(tj)) { implicitBlockingIdx.add(j); continue; }
      break;
    }
  }

  for (let _lineIdx = 0; _lineIdx < rawLines.length; _lineIdx++) {
    const rawLine = rawLines[_lineIdx];
    const lineStart = runningOffset;
    const lineEnd = runningOffset + rawLine.length;
    runningOffset = lineEnd + 1; // +1 for the \n we removed
    const t = rawLine.trim();
    // CAST block — skip entirely
    if (/^={3,}\s*EPISODE CAST/i.test(t)) { inCast = true;  continue; }
    if (/^={3,}\s*END CAST/i.test(t))     { inCast = false; continue; }
    if (inCast) continue;

    // Implicit blocking — line is part of a fence-less setup block detected
    // by the pre-pass (`:: OUTFIT:` pattern or LOCATION:/MOOD:/etc adjacent
    // to one). Treat as blockingIn for the current scene (or pending if no
    // scene open yet), then continue.
    if (implicitBlockingIdx.has(_lineIdx)) {
      if (cur) {
        (cur.blockingIn = cur.blockingIn || []).push(rawLine);
      } else {
        pendingEpisodeBlocking.push(rawLine);
      }
      continue;
    }

    // BLOCKING fence — visual/spatial setup metadata. Lines inside the fence
    // are NOT story content: 0 chrono, no segment, no scene heading detection.
    // Ultra-permissive marker detection: accepts ALL common variants different
    // LLMs / writers tend to emit:
    //   [BLOCKING]       [/BLOCKING]
    //   [BLOCKING_START] [BLOCKING_END]
    //   [BLOCKING_BEGIN] [BLOCKING_CLOSE]
    //   [BLOCKING_OPEN]  [/BLOCKING_END]
    //   plus _OUT variants for closing-scene mise-en-scène.
    // Classification: a fence is a CLOSER if it has `[/` prefix OR ends with
    // `_END]` / `_CLOSE]`. Otherwise it's an OPENER. `_OUT` anywhere → maps
    // to scene's `blockingOut` instead of `blockingIn`.
    const _isBlockingFence = /^\[\/?\s*BLOCKING(?:_OUT)?(?:_(?:START|BEGIN|OPEN|END|CLOSE))?\s*\]\s*$/i.test(t);
    if (_isBlockingFence) {
      const isClose = /^\[\s*\//.test(t) || /_(?:END|CLOSE)\s*\]/i.test(t);
      const isOut   = /BLOCKING_OUT/i.test(t);
      if (isClose) {
        inBlocking = false;
        blockingKind = '';
      } else {
        inBlocking = true;
        blockingKind = isOut ? 'out' : 'in';
      }
      continue;
    }
    if (inBlocking) {
      if (!t) continue;
      if (cur) {
        const key = blockingKind === 'out' ? 'blockingOut' : 'blockingIn';
        (cur[key] = cur[key] || []).push(rawLine);
      } else {
        pendingEpisodeBlocking.push(rawLine);
      }
      continue;
    }
    // Synopsis block — enter on «Кратко:» / «Summary:» / «Brief:» / etc.
    // Stay inside until we hit either a blank line OR a real script-style
    // line (CHAR: / [action] / scene heading). Meant for the 1-3 sentences
    // of episode synopsis that wrap onto multiple lines without re-prefix.
    if (/^(?:кратко|синопсис|summary|brief|logline|premise|tldr)\s*[:\-—]/i.test(t)) {
      inSynopsis = true;
      continue;
    }
    if (inSynopsis) {
      if (!t) { inSynopsis = false; continue; }   // blank line ends synopsis
      if (_isScriptLine(t)) { inSynopsis = false; /* fall through, process this line */ }
      else continue;   // still inside synopsis, skip
    }
    // Episode notes / trailer block — once we hit it, stop processing
    if (_SCRIPT_SKIP_PATTERNS.some(re => re.test(t))) {
      if (/^(EPISODE NOTES|━+|Hook type:|Reversal type:|Cliffhanger type:|Escalation rung:|Spoken word count:|Estimated runtime:|Setup for next episode:)/i.test(t)) {
        inNotes = true;
      }
      continue;
    }
    if (inNotes) continue;
    // Markdown headers (## ЭПИЗОД 4, ### Сцена и т.п.) — episode-level meta, not story content
    if (/^#+\s/.test(t)) continue;
    // Bold meta-labels — story-trailer / scene-trailer annotations the writer attaches:
    //   **КРАТКОЕ СОДЕРЖАНИЕ:** …, **SUMMARY:** …
    //   **LOCATION:** Manhattan, **IN THE FRAME:** MAYA, ADRIAN
    //   - **Cliffhanger:** "..."
    //   - **Emotional peak:** Vivian's admission of forcing Clara…
    //   - **Setup for Episode 5:** Legal confrontation begins…
    //
    // CRITICAL: must NOT catch dialogue cues like `**ADRIAN:** Damn it.`
    // The old open pattern `^\*\*[letters]+:\*\*` swallowed every screenplay
    // cue and dropped its dialogue line silently. Now we whitelist the actual
    // meta keywords (with optional 1-5 trailing words to cover `Setup for
    // Episode 5`). Bullet-prefixed lines stay broadly matched — by convention
    // ANY `- **Label:**` is meta, never dialogue.
    if (/^[-*•]\s+\*\*[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9\s]*:\s*\*\*/.test(t)) continue;
    if (/^\*\*\s*(?:cliffhanger|setup|summary|brief|logline|premise|tl;?dr|synopsis|hook(?:\s+type)?|reversal(?:\s+type)?|escalation(?:\s+rung)?|spoken\s+word\s+count|estimated\s+runtime|emotional(?:\s+peak)?|theme|notes?|location|setting|locale|in\s+the\s+frame|scene\s+brief|кратко(?:е\s+содержание)?|синопсис|содержание|суммари|клиффхэнгер|разворот|хук|локация|в\s+кадре)(?:\s+[\w\d]+){0,5}\s*[:：]\s*\*\*/i.test(t)) continue;
    // Horizontal separators
    if (/^[-—=]{3,}\s*$/.test(t)) continue;

    // Scene heading: open a new scene (formal INT./EXT. or inferred)
    const headMatch = _matchSceneHeading(t);
    if (headMatch.match) {
      cur = { id: scenes.length, heading: t, lines: [], totalSec: 0, inferred: headMatch.inferred };
      // Hand off any [BLOCKING] block that appeared BEFORE this heading to the
      // new scene — it was setup for THIS scene, just placed early.
      if (pendingEpisodeBlocking.length) {
        cur.blockingIn = pendingEpisodeBlocking.slice();
        pendingEpisodeBlocking = [];
      }
      scenes.push(cur);
      expectingDialogue = false;  // reset cross-scene state
      continue;
    }

    // Empty / structural lines aren't visual time AND not visible body either —
    // skip BEFORE we'd open a synthetic scene, otherwise blank lines between
    // CAST block and the first INT./EXT. would create an empty Сцена 1.
    if (!t) continue;

    // No heading seen yet AND we hit real content → open a synthetic Сцена 1
    // so the script doesn't disappear entirely from the scene view.
    if (!cur) {
      cur = { id: 0, heading: '', lines: [], totalSec: 0, inferred: true, synthetic: true };
      if (pendingEpisodeBlocking.length) {
        cur.blockingIn = pendingEpisodeBlocking.slice();
        pendingEpisodeBlocking = [];
      }
      scenes.push(cur);
    }

    // Screenplay multi-line dialogue. Two flavors of "cue waiting for payload":
    //   (a) Orphan cue, no colon:        SOFIA / DANTE (V.O.) / **Clara**
    //                                    Three nights...
    //   (b) Name-colon, empty tail:      Clara: / Mrs. Vale: / **Sofia:**
    //                                    "The moon..."
    // `_lineDuration` is stateless and cannot tell that the bare line below
    // a cue is its dialogue payload — it falls through to plain prose and
    // gets billed at 0-1.5s instead of word-count rate. Track the cue→payload
    // handoff here so dialogue is computed correctly. Markdown wrappers
    // (`**Clara:**`, `*Sofia*`) are stripped before matching.
    const _t = _stripMarkdownWrappers(t);
    const _isCueAwaitingPayload = (text) => {
      if (_isOrphanSpeakerCue(text)) return true;
      // "Clara:" / "Mrs. Vale:" / "DANTE (V.O.):" with empty after the colon
      const cm = text.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-'.#]{0,40})\s*[:：]\s*(.*)$/);
      if (!cm || !/^[A-ZА-ЯЁ]/.test(cm[1])) return false;
      const tail = (cm[2] || '').replace(/\([^)]*\)/g, ' ').replace(/\[[^\]]*\]/g, ' ').replace(/\*[^*]*\*/g, ' ').trim();
      return tail === '';
    };
    let dur;
    if (expectingDialogue) {
      if (_isCueAwaitingPayload(_t)) {
        // Another cue in a row — keep waiting for payload.
        dur = 0;
      } else if (/^\([^)]+\)\.?$/.test(_t)) {
        // Parenthetical tone note between cue and payload ("(barely breathing)") —
        // not spoken, kept as 0s; payload still pending on next line.
        dur = 0;
      } else {
        // This line IS the dialogue payload. Compute by speech rate.
        const words = _countDialogueWords(_t);
        dur = words > 0 ? 0.4 + words / SPEECH_WPS : 0.5;
        expectingDialogue = false;
      }
    } else {
      if (_isCueAwaitingPayload(_t)) {
        // Cue on its own line — 0s, expect dialogue payload next.
        dur = 0;
        expectingDialogue = true;
      } else {
        dur = _lineDuration(rawLine);
      }
    }
    cur.lines.push({
      text: rawLine, duration: dur, segIdx: 0,
      offset: lineStart, offsetEnd: lineEnd,
    });
    cur.totalSec += dur;
  }

  // Assign segment indices based on REAL on-screen time. Each Seedance chunk is
  // technically 15s but we aim for ~13s of content + ~2s breathing room so cuts
  // don't jam reactions back-to-back.
  // TWO-PASS algorithm:
  //   Pass 1 (greedy): pack lines into the current segment until it would exceed
  //     SOFT_MAX. NEVER break before the segment has accumulated MIN_SEGMENT — a
  //     1-line 4-second segment wastes a whole 15s Seedance chunk.
  //   Pass 2 (merge tiny): post-walk segments. Any segment < MIN_SEGMENT gets
  //     merged into a neighbour if the combined size stays ≤ HARD_MAX.
  const TARGET = 14.0;          // aim around this (informational)
  const LUFT_MAX_SEC = 18.5;    // Hard ceiling everywhere (Pass 1 break threshold,
                                // Pass 3 luft-merge, Pass 5 line-pull, Pass 7
                                // final sweep). Seedance hard-caps at 15s, but
                                // a 15-18.5s content chunk renders fine —
                                // characters just speak ~20% faster. Trading
                                // up to 3.5s of pacing tightness for fewer
                                // generation cycles is a clear net win.
                                // Was 17 (2s luft) → bumped to 18.5 (3.5s luft).
  const SOFT_MAX = LUFT_MAX_SEC; // Pack directly to luft from Pass 1 — no
                                // separate "soft" threshold. Pass 3/5/7 only
                                // touch up edge cases (small leftover tails).
  const MIN_SEGMENT_SEC = 5.0;  // smaller than this = wasted Seedance chunk
  const HARD_MAX_SEC = 14.5;    // absolute ceiling for Pass-2 small-segment merges
  // Speaker cue detector — a line that's ONLY a character-name cue, no
  // dialogue text. Two formats supported:
  //   (a) Classic screenplay ALL-CAPS:  ETHAN  / ETHAN (CONT'D) / ETHAN (V.O.)
  //   (b) Modern Title-case + colon:    Ethan:  / Ava (V.O.):
  // Optional parenthetical qualifier «(CONT'D)» / «(V.O.)» / «(to X)».
  // These must NEVER be separated from the dialogue line that follows —
  // otherwise the chunk starts with an orphan dialogue line whose speaker
  // landed in the previous chunk, and ends with a dangling cue whose
  // dialogue landed in the next chunk. User-reported real bug: chunk
  // contained «Yes. / Ethan: / You said you stole my access codes. / Ava:».
  const _isSpeakerCue = (text) => {
    const t = (text || '').trim();
    if (!t || t.length > 40) return false;
    // Format (b): «Name:» or «Name (V.O.):» — only a name + optional
    // qualifier, ending in a colon, NO dialogue text after the colon.
    const colonMatch = t.match(/^([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9 \-']{0,28})(\s*\([^)]{0,20}\))?\s*[:：]\s*$/);
    if (colonMatch) {
      const name = colonMatch[1].trim();
      // Reject heading/meta tokens. (JS \b doesn't work for Cyrillic, so
      // we use explicit alternation with optional non-letter suffix.)
      const META_TOKENS = /^(INT|EXT|FADE|CUT|MATCH|DISSOLVE|TIME|END|FIN|SCENE|FLASHBACK|Локация|Сцена|Time|Day|Night|Morning|Evening)(?:[^A-Za-zА-Яа-яЁё]|$)/i;
      if (/^[A-ZА-ЯЁ]/.test(name) && !META_TOKENS.test(name)) {
        return true;
      }
    }
    // Format (a): bare ALL-CAPS cue without colon.
    const base = t.replace(/\s*\([^)]+\)\s*$/, '').trim();   // strip (CONT'D) etc
    if (!base || base.length > 30) return false;
    if (/[a-zа-яё]/.test(base)) return false;                // no lowercase
    if (!/[A-ZА-ЯЁ]/.test(base)) return false;
    const tokens = base.split(/\s+/);
    if (tokens.length > 4) return false;
    if (/^(INT|EXT|FADE|CUT|MATCH|DISSOLVE|TIME|END|FIN|SCENE|FLASHBACK)\b/i.test(base)) return false;
    return true;
  };
  for (const sc of scenes) {
    // Pass 1: greedy, never break before MIN_SEGMENT. When a break would
    // land BETWEEN a speaker cue and its dialogue, pull the cue forward
    // into the new segment along with the dialogue.
    let acc = 0, seg = 0;
    for (let i = 0; i < sc.lines.length; i++) {
      const l = sc.lines[i];
      const prev = i > 0 ? sc.lines[i - 1] : null;
      if (acc >= MIN_SEGMENT_SEC && acc + l.duration > SOFT_MAX) {
        // If the previous line was a speaker cue and is currently in the
        // CURRENT segment, move it forward to the new segment so cue+dialogue
        // stay together. Decrement acc accordingly.
        if (prev && _isSpeakerCue(prev.text) && prev.segIdx === seg) {
          prev.segIdx = seg + 1;
          acc -= prev.duration;
          seg += 1;
          acc = prev.duration;   // new segment already has the cue
        } else {
          seg += 1;
          acc = 0;
        }
      }
      l.segIdx = seg;
      acc += l.duration;
    }
    // Pass 2: merge any tiny segments into a neighbour (prefer prev) up to HARD_MAX
    const segTotals = [];
    for (const l of sc.lines) {
      while (segTotals.length <= l.segIdx) segTotals.push(0);
      segTotals[l.segIdx] += l.duration;
    }
    // Walk backward so index shifts after splice don't break the iteration
    for (let i = segTotals.length - 1; i >= 0; i--) {
      if (segTotals[i] >= MIN_SEGMENT_SEC) continue;
      // Try merge with previous
      if (i > 0 && segTotals[i-1] + segTotals[i] <= HARD_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i) l.segIdx = i - 1;
          else if (l.segIdx > i) l.segIdx -= 1;
        }
        segTotals[i-1] += segTotals[i];
        segTotals.splice(i, 1);
        continue;
      }
      // Else try merge with next
      if (i < segTotals.length - 1 && segTotals[i] + segTotals[i+1] <= HARD_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i+1) l.segIdx = i;
          else if (l.segIdx > i+1) l.segIdx -= 1;
        }
        segTotals[i] += segTotals[i+1];
        segTotals.splice(i+1, 1);
        continue;
      }
      // Otherwise leave alone (single huge orphan line that can't fit anywhere)
    }
    // Pass 3 (luft-merge): walk left-to-right and merge any adjacent pair
    // whose combined duration ≤ LUFT_MAX_SEC (17s). The intent: a stretch of
    // dialogue that totals 16-17s gets stuck as [10s][6.5s] because Pass-1's
    // SOFT_MAX(13) forces a break. Sending those as a single 15s Seedance
    // call (with slightly compressed pacing) costs us one generation cycle
    // instead of two. Keep merging the same index while it still has a
    // mergeable right-neighbour.
    let _lm = 0;
    while (_lm < segTotals.length - 1) {
      if (segTotals[_lm] + segTotals[_lm + 1] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === _lm + 1) l.segIdx = _lm;
          else if (l.segIdx > _lm + 1) l.segIdx -= 1;
        }
        segTotals[_lm] += segTotals[_lm + 1];
        segTotals.splice(_lm + 1, 1);
        // stay on _lm to attempt another merge with the new right-neighbour
      } else {
        _lm++;
      }
    }

    // Pass 5 (line-level fill-from-next): when Pass-3 can't merge whole
    // chunks (combined > LUFT), still try to PULL the first dialogue/action
    // unit from the next chunk into this one. Tightens every chunk toward
    // 15s without breaking dialogue lines apart. "Unit" = all leading
    // zero-duration lines (speaker cues, parentheticals) + the next single
    // substantive line. Keeps cue+payload paired.
    const _pullUnitFromNext = (fromSegIdx) => {
      const idxs = [];
      let dur = 0;
      let firstAt = -1;
      for (let li = 0; li < sc.lines.length; li++) {
        if (sc.lines[li].segIdx === fromSegIdx) {
          if (firstAt < 0) firstAt = li;
          idxs.push(li);
          dur += sc.lines[li].duration;
          if (sc.lines[li].duration > 0.01) break;  // got the substantive line
        } else if (firstAt >= 0) {
          break;
        }
      }
      return { idxs, dur, hasSubstantive: dur > 0.01 };
    };
    let _pi = 0;
    while (_pi < segTotals.length - 1) {
      const unit = _pullUnitFromNext(_pi + 1);
      if (!unit.idxs.length || !unit.hasSubstantive) { _pi++; continue; }
      const newCur  = segTotals[_pi]     + unit.dur;
      const newNext = segTotals[_pi + 1] - unit.dur;
      // Two guard rails:
      //   (a) Current chunk must stay within luft.
      //   (b) Next chunk must either DRAIN to zero (fully absorbed) OR keep
      //       enough content to remain valid (≥ MIN_SEGMENT_SEC). Otherwise
      //       we'd create a 1-2s orphan tail that wastes a Seedance call.
      const fits = newCur <= LUFT_MAX_SEC && (newNext < 0.01 || newNext >= MIN_SEGMENT_SEC);
      if (fits) {
        for (const li of unit.idxs) sc.lines[li].segIdx = _pi;
        segTotals[_pi]     = newCur;
        segTotals[_pi + 1] = newNext;
        if (newNext <= 0.01) {
          for (const l of sc.lines) {
            if (l.segIdx > _pi + 1) l.segIdx -= 1;
          }
          segTotals.splice(_pi + 1, 1);
        }
        // stay on _pi; try pulling another unit
      } else {
        _pi++;
      }
    }

    // Pass 6 (cleanup): re-run the tiny-merge pass on the new layout.
    // After Pass 5 some chunks may have shrunk under MIN_SEGMENT — try to
    // fold them back into a neighbour if combined ≤ LUFT_MAX_SEC (more
    // generous than Pass-2's HARD_MAX since we already accepted luft).
    for (let i = segTotals.length - 1; i >= 0; i--) {
      if (segTotals[i] >= MIN_SEGMENT_SEC) continue;
      if (i > 0 && segTotals[i-1] + segTotals[i] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i) l.segIdx = i - 1;
          else if (l.segIdx > i) l.segIdx -= 1;
        }
        segTotals[i-1] += segTotals[i];
        segTotals.splice(i, 1);
        continue;
      }
      if (i < segTotals.length - 1 && segTotals[i] + segTotals[i+1] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === i+1) l.segIdx = i;
          else if (l.segIdx > i+1) l.segIdx -= 1;
        }
        segTotals[i] += segTotals[i+1];
        segTotals.splice(i+1, 1);
      }
    }

    // Pass 7 (final luft sweep): re-run Pass-3-style pairwise merge AFTER
    // line-level shuffles. Pass-5 may have pulled lines around in a way that
    // newly-adjacent pairs are now ≤ LUFT_MAX_SEC and weren't before. Also
    // catches the case where Pass 3's first sweep was blocked by intermediate
    // chunk sizes that Pass-5 has since redistributed.
    let _lm2 = 0;
    while (_lm2 < segTotals.length - 1) {
      if (segTotals[_lm2] + segTotals[_lm2 + 1] <= LUFT_MAX_SEC) {
        for (const l of sc.lines) {
          if (l.segIdx === _lm2 + 1) l.segIdx = _lm2;
          else if (l.segIdx > _lm2 + 1) l.segIdx -= 1;
        }
        segTotals[_lm2] += segTotals[_lm2 + 1];
        segTotals.splice(_lm2 + 1, 1);
      } else {
        _lm2++;
      }
    }

    // Pass 8 (de-dangle speaker cues): after every merge/shuffle above, a
    // chunk may END with a speaker cue (+ trailing tone-note parentheticals)
    // whose actual dialogue line landed in the NEXT chunk. That orphans the
    // cue — the next chunk opens with a dialogue line and nobody knows who's
    // speaking. Real user bug: chunk ends «VERA / (coldly)» and the next chunk
    // starts «Helen signed the documents…».
    //
    // Pass-1 already pulls a cue forward, but ONLY when the cue sits
    // IMMEDIATELY before the break. A parenthetical between cue and dialogue
    // («VERA / (coldly) / Helen…») defeats it, and later merge passes can
    // re-separate a cue from its payload. So we sweep one more time here,
    // after the layout is final.
    //
    // "Glue" lines = speaker cues + parenthetical tone-notes (these attach to
    // the FOLLOWING dialogue). We collect the trailing glue run at each
    // chunk's tail; if it contains a speaker cue, we move everything from the
    // first such cue onward into the next chunk. Moving these (≤0.4s each)
    // never meaningfully affects luft but guarantees cue+payload stay paired.
    {
      const _isParenOnly = (text) => /^\(.+\)\.?$/.test((text || '').trim());
      // Snapshot per-segment line indices in document order.
      const segLineIdxs = [];
      for (let li = 0; li < sc.lines.length; li++) {
        const s = sc.lines[li].segIdx;
        while (segLineIdxs.length <= s) segLineIdxs.push([]);
        segLineIdxs[s].push(li);
      }
      // Last segment has no "next" chunk to host the cue — skip it.
      for (let s = 0; s < segLineIdxs.length - 1; s++) {
        const lis = segLineIdxs[s];
        if (lis.length < 2) continue;
        // Walk backward over the trailing glue run (cues + parentheticals).
        let k = lis.length - 1;
        while (k >= 0) {
          const txt = sc.lines[lis[k]].text;
          if (_isSpeakerCue(txt) || _isParenOnly(txt) || sc.lines[lis[k]].duration <= 0.01) {
            k--;
          } else {
            break;
          }
        }
        // Trailing glue run is lis[k+1 .. end]. Find the first speaker cue in it.
        let cueAt = -1;
        for (let m = k + 1; m < lis.length; m++) {
          if (_isSpeakerCue(sc.lines[lis[m]].text)) { cueAt = m; break; }
        }
        if (cueAt <= 0) continue;            // no dangling cue, or it would empty the chunk
        // Push cue + everything after it into the next chunk (prepends in
        // correct document order since these are the segment's last lines).
        for (let m = cueAt; m < lis.length; m++) {
          sc.lines[lis[m]].segIdx = s + 1;
        }
      }
    }
    sc.segCount = sc.lines.length ? (sc.lines[sc.lines.length - 1].segIdx + 1) : 0;
  }

  // Apply manual overrides (force-break / force-merge per line anchor) AFTER
  // auto-segmentation so user's choices win. Skipped lines (meta, headings)
  // never reach lines[] so they can't carry overrides — fine, those aren't
  // displayed segments anyway.
  if (overridePairMap.size || overrideAnchorMap.size) {
    let sIdx = 0;
    for (const sc of scenes) {
      // Snapshot auto-segmentation (transitions = points where auto wanted a break)
      for (const l of sc.lines) l._autoSeg = l.segIdx;
      // For each line, pick action: prefer (sceneIdx, autoSeg) match.
      // Anchor-only fallback (legacy data) is applied at most ONCE per
      // anchor per scene — without this guard, a script where many
      // segments start with the same speaker cue ("Ethan:") would
      // re-trigger the merge on every same-anchor segment break and
      // collapse the rest of the scene into one chunk.
      const usedLegacyAnchors = new Set();
      let curSeg = 0;
      for (let i = 0; i < sc.lines.length; i++) {
        const l = sc.lines[i];
        const pairKey = `s${sIdx}a${l._autoSeg}`;
        let action = overridePairMap.get(pairKey) || null;
        if (!action) {
          const anc = _lineAnchor(l.text);
          if (!usedLegacyAnchors.has(anc)) {
            const legacyAction = overrideAnchorMap.get(anc);
            if (legacyAction) {
              action = legacyAction;
              usedLegacyAnchors.add(anc);
            }
          }
        }
        if (i === 0) {
          l.segIdx = 0;
          l._override = action || null;
          continue;
        }
        const prev = sc.lines[i - 1];
        if (action === 'break') {
          curSeg += 1;                                  // user forces split here
        } else if (action === 'merge') {
          // user forces no-break — stay with prev
        } else if (l._autoSeg !== prev._autoSeg) {
          curSeg += 1;                                  // respect auto-decision
        }
        l.segIdx = curSeg;
        l._override = action || null;
      }
      sc.segCount = sc.lines.length ? (sc.lines[sc.lines.length - 1].segIdx + 1) : 0;
      sIdx += 1;
    }
  }
  return scenes;
}

// Build coverage map: { lineOffset → {status, idx} } based on Seedance chunks.
