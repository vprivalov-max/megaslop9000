// ── Per-segment auto-mode skip flags ───────────────────────────────────────
function _segmentAutoSkips() {
  return (S.episode && S.episode.segment_auto_skips) || [];
}
function _isSegmentAutoSkipped(anchor) {
  return _segmentAutoSkips().includes(anchor);
}
async function _persistAutoSkips(skips) {
  if (!S.episode) return;
  S.episode.segment_auto_skips = skips;
  try {
    await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/segment-auto-skips`,
      { skips }
    );
  } catch (e) {
    console.warn('save segment auto-skips failed', e);
    showToast('⚠ Не удалось сохранить флаг auto-mode', 4000);
  }
}
async function toggleSegmentAutoInclude(anchor) {
  if (!anchor) return;
  const skips = _segmentAutoSkips().slice();
  const idx = skips.indexOf(anchor);
  if (idx >= 0) skips.splice(idx, 1);  // include — remove from skip list
  else skips.push(anchor);              // skip — add to skip list
  await _persistAutoSkips(skips);
  _renderSceneViewBody();
}

// ── Per-line overrides (currently: close-up flag) ──────────────────────────
function _lineOverrides() {
  return (S.episode && S.episode.line_overrides) || [];
}
function _isLineCloseUp(text) {
  const anchor = _lineAnchor(text);
  if (!anchor) return false;
  const found = _lineOverrides().find(o => o.anchor === anchor);
  return !!(found && (found.flags || []).includes('close_up'));
}
function _isDialogueLine(text) {
  if (!text) return false;
  const t = text.trim();
  // Same dialogue regex as _lineDuration — Title-case OR ALL-CAPS speaker name + colon
  if (!/^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-']{0,40}\s*[:：]\s/.test(t)) return false;
  return /^[A-ZА-ЯЁ]/.test(t);  // first letter must be uppercase
}
// Voice-over / off-screen / internal-monologue marker detector. These
// lines play as audio narration overlaid ON TOP of the visual action —
// the listener can hear them while seeing whatever the camera is doing.
// For chunk-duration calc this means we don't ADD action duration on
// top of VO duration; the two overlap, max() not sum.
//
// Self-contained on purpose — _isDialogueLine rejects periods in the
// speaker cue («SOPHIE (V.O.)» has dots) so we'd miss exactly the most
// common VO formatting. We just look for «<name>(<vo-marker>):» shape.
function _isVoiceOverLine(text) {
  if (!text) return false;
  const t = text.trim();
  // Must start with an uppercase-letter name, have a parenthetical with
  // a VO marker, and end the cue with a colon.
  return /^[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё_0-9 .\-']{0,40}\s*\((?:v\.?o\.?|voiceover|voice[\s\-]over|o\.?s\.?|off[\s\-]screen|narration|закадр\w*|голос за кадром|внутренний голос|мысленно|про себя)\)\s*[:：]/i
    .test(t);
}

// Detect orphan speaker cues — standalone screenplay-format speaker names
// that precede their dialogue on the NEXT line. Examples:
//   FRANK
//   I'll be in the office doing books.
//
//   Sofia (V.O.)
//   Three nights since I left.
// Without this detection the cue line is mis-billed as a short action beat
// (floored to 1.5s) AND the dialogue below is mis-billed as prose-action
// (chars/35 rate) instead of speech (words/wps) — inflating chunk duration.
function _isOrphanSpeakerCue(line) {
  if (!line) return false;
  const t = line.trim();
  if (!t) return false;
  if (t.length > 40) return false;
  // Reject scene headings / transitions (those start with INT./EXT./FADE/CUT)
  if (_matchSceneHeading(t).match) return false;
  if (/^[\s—-]*(FADE|CUT|DISSOLVE|SMASH|MATCH)\s+(IN|OUT|TO|BACK)\b/i.test(t)) return false;
  // No sentence punctuation (period, exclamation, question) — those indicate
  // a real action/dialogue line, not a cue. Colons disqualify too (that's
  // inline "NAME: text" handled by _lineDuration directly).
  if (/[.!?:]/.test(t.replace(/\([^)]*\)/g, ''))) return false;
  // Cue body shape: starts with uppercase, optional parenthetical
  // (V.O. / O.S. / CONT'D / age tag / etc.), allowed chars: letters,
  // spaces, hyphens, apostrophes. Either ALL-CAPS classic screenplay
  // form OR Title-case modern form.
  return /^[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9 \-']{0,38}(?:\s*\([^)]{0,30}\))?\s*$/.test(t);
}

// Single source of truth for chunk duration estimation from raw chunk text.
// Replicates the VO-aware logic used in _autoCollectSegments + parallel range-
// gen so retries / reuses / heal flows compute consistent durations instead of
// inheriting the (potentially inflated) duration of the chunk being replaced.
// Returns an integer in [5, 15] seconds.
function _estimateChunkDurationSec(chunkText, opts) {
  opts = opts || {};
  const text = (chunkText || '').trim();
  if (!text) return Math.max(5, Math.min(15, opts.fallback || 5));
  // Split into raw lines, skip empties and scene heading / fade markers
  // (_lineDuration returns 0 for those).
  const rawLines = text.split('\n').map(l => l.trim()).filter(Boolean);
  // Pre-pass: merge multi-line screenplay-format dialogue. Two cue flavors:
  //   (a) Orphan cue (no colon):       FRANK / DANTE (V.O.)
  //   (b) Name-colon, empty tail:      Clara: / Mrs. Vale: / **Sofia:**
  // Both merge with the next non-empty line (skipping a single parenthetical
  // tone note) so `_lineDuration` bills the payload as dialogue (words/wps)
  // instead of mis-classifying it as prose action.
  const _isCueLineForMerge = (s) => {
    const stripped = _stripMarkdownWrappers(s);
    if (_isOrphanSpeakerCue(stripped)) return true;
    const m = stripped.match(/^([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9 ()\-'.#]{0,40})\s*[:：]\s*(.*)$/);
    if (!m || !/^[A-ZА-ЯЁ]/.test(m[1])) return false;
    const tail = (m[2] || '').replace(/\([^)]*\)/g, ' ').replace(/\[[^\]]*\]/g, ' ').replace(/\*[^*]*\*/g, ' ').trim();
    return tail === '';
  };
  const mergedLines = [];
  for (let i = 0; i < rawLines.length; i++) {
    const cur = rawLines[i];
    if (_isCueLineForMerge(cur)) {
      // Optional parenthetical beat between cue and dialogue
      let parenBeat = '';
      let j = i + 1;
      if (j < rawLines.length && /^\([^)]+\)\.?$/.test(rawLines[j])) {
        parenBeat = ' ' + rawLines[j];
        j++;
      }
      // Next non-empty line is the dialogue payload — unless it's ANOTHER
      // speaker cue or a scene heading (then current cue is a stray, fall
      // through to normal action billing).
      if (j < rawLines.length
          && !_isCueLineForMerge(rawLines[j])
          && !_matchSceneHeading(rawLines[j]).match) {
        const curStripped = _stripMarkdownWrappers(cur);
        // If cue already ends with a colon (Clara: / Mrs. Vale:), don't add
        // another — just append the payload. Otherwise insert ':'.
        const sep = /[:：]\s*$/.test(curStripped) ? '' : ':';
        mergedLines.push(curStripped + sep + parenBeat + ' ' + rawLines[j]);
        i = j;  // consume both (or three with paren) lines
        continue;
      }
    }
    mergedLines.push(cur);
  }
  let voSec = 0, dialogSec = 0, actionSec = 0;
  let hasVo = false;
  for (const l of mergedLines) {
    const dur = (typeof _lineDuration === 'function') ? _lineDuration(l) : 0;
    if (!dur) continue;
    if (_isVoiceOverLine(l))      { voSec += dur; hasVo = true; }
    else if (_isDialogueLine(l))  { dialogSec += dur; }
    else                          { actionSec += dur; }
  }
  const contentSec = Math.max(voSec, actionSec) + dialogSec;
  const buffer = (hasVo && dialogSec === 0) ? 0.6 : 1.5;
  const target = Math.ceil(contentSec + buffer);
  return Math.max(5, Math.min(15, target || 5));
}
async function _persistLineOverrides(overrides) {
  if (!S.episode) return;
  S.episode.line_overrides = overrides;
  try {
    await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/line-overrides`,
      { overrides }
    );
  } catch (e) {
    console.warn('save line overrides failed', e);
    showToast('⚠ Не удалось сохранить close-up метку', 4000);
  }
}
async function toggleLineCloseUp(anchor) {
  if (!anchor) return;
  const overrides = _lineOverrides().slice();
  const idx = overrides.findIndex(o => o.anchor === anchor);
  if (idx < 0) {
    overrides.push({ anchor, flags: ['close_up'] });
  } else {
    const flags = overrides[idx].flags || [];
    const cuPos = flags.indexOf('close_up');
    if (cuPos >= 0) {
      const newFlags = flags.filter(f => f !== 'close_up');
      if (newFlags.length === 0) overrides.splice(idx, 1);
      else overrides[idx] = { ...overrides[idx], flags: newFlags };
    } else {
      overrides[idx] = { ...overrides[idx], flags: [...flags, 'close_up'] };
    }
  }
  await _persistLineOverrides(overrides);
  _renderSceneViewBody();
}
async function _persistSegmentOverrides(overrides) {
  if (!S.episode) return;
  S.episode.segment_overrides = overrides;
  try {
    await api.put(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/segment-overrides`,
      { overrides }
    );
  } catch (e) {
    console.warn('save segment overrides failed', e);
    showToast('⚠ Не удалось сохранить ручную разбивку', 4000);
  }
}
function _lineAnchor(text) {
  // Stable enough anchor for a script line: first 60 chars of trimmed text.
  // If user edits the line text, the override silently drops.
  return (text || '').trim().slice(0, 60);
}

function _renderSceneViewBody() {
  const ta = document.getElementById('ep-script');
  const view = document.getElementById('ep-script-scenes');
  if (!ta || !view) return;
  const scriptText = ta.value || '';
  const scenes = _parseScriptScenes(scriptText, _segmentOverrides());
  const chunks = (S.episode && S.episode.seedance_chunks) || [];
  const coverage = _buildSeedanceCoverage(scriptText, chunks);
  view.innerHTML = _renderScenesHTML(scenes, coverage);
}

function toggleCoverage(on) {
  SCENE_VIEW_STATE.showCoverage = !!on;
  try { localStorage.setItem('sceneCov', SCENE_VIEW_STATE.showCoverage ? '1' : '0'); } catch {}
  _renderSceneViewBody();
}

function toggleSegmentEditMode(on) {
  SCENE_VIEW_STATE.editMode = !!on;
  _renderSceneViewBody();
}

// Compose segment text from auto-collected lines, then send to Seedance compose
// textarea + scroll the compose panel into view + focus.
// Manual force-setter exposed for console debugging. Type into DevTools:
//    forceSdDuration(12)
// and watch the [sd-duration WRITE] log fire. If the slider visually changes,
// listener-based code is working. If not, something else is rendering the UI.
window.forceSdDuration = function(n) {
  const el = document.getElementById('sd-duration');
  const label = document.getElementById('sd-duration-val');
  if (!el) { console.error('sd-duration element not found'); return false; }
  const v = Math.max(5, Math.min(15, Math.round(Number(n) || 5)));
  console.log('[forceSdDuration] writing', v, 'to slider (currently', el.value, ')');
  el.value = String(v);
  if (label) label.textContent = v + 'с';
  el.dataset.autoVal = String(v);
  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  console.log('[forceSdDuration] after writes, slider reads', el.value, '/ label reads', label?.textContent);
  return el.value;
};

function sendSegmentToSeedance(segText) {
  const ta = document.getElementById('sd-chunk-text');
  if (!ta) {
    showToast('⚠ Seedance-панель не найдена на этом эпизоде', 4000);
    return;
  }
  // Lock the slider FIRST, before the textarea even gets the new text. This
  // way listener-driven auto-set (which fires on textarea `input`) sees the
  // dataset.autoVal already matching our recDur and stays out of the way.
  const durEl  = document.getElementById('sd-duration');
  const durVal = document.getElementById('sd-duration-val');
  let recDur = null;
  try {
    recDur = _estimateChunkDurationSec(segText);
  } catch (e) {
    console.warn('[sendSegmentToSeedance] duration estimate threw:', e);
  }
  console.log('[sendSegmentToSeedance] segText length=' + segText.length + ' recDur=' + recDur + ' durEl=' + !!durEl);
  if (durEl && recDur != null && Number.isFinite(recDur) && recDur > 0) {
    // Clamp to the slider's min/max so the browser doesn't silently reject.
    const min = parseInt(durEl.min || '5', 10) || 5;
    const max = parseInt(durEl.max || '15', 10) || 15;
    const v = Math.max(min, Math.min(max, Math.round(recDur)));
    durEl.value = String(v);
    durEl.setAttribute('value', String(v));   // some browsers need attr too
    durEl.dataset.autoVal = String(v);
    if (durVal) durVal.textContent = v + 'с';
    durEl.dispatchEvent(new Event('input',  { bubbles: true }));
    durEl.dispatchEvent(new Event('change', { bubbles: true }));
    console.log('[sendSegmentToSeedance] slider set to ' + v + ' (was=' + durEl.value + ' after=' + durEl.value + ')');
  }
  // Now set the textarea. Any cascading input listener that sets durEl will
  // see dataset.autoVal === recDur and bail out of the override-respect path.
  ta.value = segText;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  // Re-affirm slider once more AFTER the textarea event chain, defensively —
  // covers any listener that decided to write durEl despite our handshake.
  if (durEl && recDur != null && Number.isFinite(recDur) && recDur > 0) {
    const min = parseInt(durEl.min || '5', 10) || 5;
    const max = parseInt(durEl.max || '15', 10) || 15;
    const v = Math.max(min, Math.min(max, Math.round(recDur)));
    if (String(durEl.value) !== String(v)) {
      console.warn('[sendSegmentToSeedance] slider drifted after textarea input; restoring', durEl.value, '→', v);
      durEl.value = String(v);
      durEl.dataset.autoVal = String(v);
      if (durVal) durVal.textContent = v + 'с';
      durEl.dispatchEvent(new Event('input',  { bubbles: true }));
      durEl.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  document.getElementById('seedance-panel')?.scrollIntoView({behavior:'smooth', block:'start'});
  setTimeout(() => ta.focus(), 350);
  const durMsg = recDur != null ? ` (⏱ ${recDur}с)` : '';
  showToast('✓ Сегмент отправлен в Сиданс' + durMsg + ' — нажми "⚙ Скомпоновать (LLM)"', 4000);
}

// Internal: gather plain text for the Nth segment of the Mth scene.
function _segmentText(sceneIdx, segIdx) {
  const ta = document.getElementById('ep-script');
  if (!ta) return '';
  const scenes = _parseScriptScenes(ta.value || '', _segmentOverrides());
  const sc = scenes[sceneIdx];
  if (!sc) return '';
  const lines = sc.lines.filter(l => l.segIdx === segIdx);
  if (!lines.length) return '';
  // Prepend scene heading so composer has context for INT/EXT
  const head = sc.heading ? sc.heading + '\n\n' : '';
  return head + lines.map(l => l.text).join('\n');
}

// User clicked "send segment N of scene M to Seedance"
function sendSceneSegmentToSeedance(sceneIdx, segIdx) {
  const txt = _segmentText(sceneIdx, segIdx);
  if (!txt.trim()) {
    showToast('⚠ Сегмент пустой', 3000);
    return;
  }
  // If any line in this segment carries the close-up flag, auto-tick the
  // global "🎯 close-up only" checkbox so the next compose enforces it.
  const ta = document.getElementById('ep-script');
  let segHasCloseUp = false;
  if (ta) {
    const scenes = _parseScriptScenes(ta.value || '', _segmentOverrides());
    const sc = scenes[sceneIdx];
    if (sc) {
      segHasCloseUp = sc.lines.some(l => l.segIdx === segIdx && _isLineCloseUp(l.text));
    }
  }
  const cuCb = document.getElementById('sd-close-up-only');
  if (segHasCloseUp && cuCb && !cuCb.checked) {
    cuCb.checked = true;
    sdSavePrefs();
    showToast('🎯 Сегмент содержит close-up метки — close-up only автоматически включён в Seedance-панели', 5500);
  }
  sendSegmentToSeedance(txt);
}

// Toggle "split" override on a specific line anchor within a scene.
// Override key: (sceneIdx, autoSeg) uniquely identifies a segment-break
// across script re-parses. Anchor (first 60 chars) is kept as a sanity
// hint so an edited script can self-heal — if anchor diverges from the
// recorded one, the override silently drops on next render.
// Bug 2026-05-15: previously matched ONLY by anchor; in dialogue-heavy
// scripts where many segments start with the same speaker cue ("Ethan:",
// "Sofia:"), one merge click suppressed segment breaks on EVERY same-
// anchor line → the rest of the episode collapsed into one chunk.
function _findOverrideIdx(overrides, sceneIdx, autoSeg, anchor) {
  // Prefer exact (sceneIdx, autoSeg) match — that's the canonical key.
  let i = overrides.findIndex(o =>
    o && o.sceneIdx === sceneIdx && o.autoSeg === autoSeg
  );
  if (i >= 0) return i;
  // Legacy overrides recorded by anchor only — match if anchor agrees AND
  // the entry has no scene/autoSeg fields. This back-compat path only
  // fires for pre-2026-05-15 saved overrides.
  i = overrides.findIndex(o =>
    o && o.anchor === anchor
    && (o.sceneIdx === undefined && o.autoSeg === undefined)
  );
  return i;
}

async function toggleSegmentBreak(anchor, sceneIdx, autoSeg) {
  if (!anchor) return;
  const overrides = _segmentOverrides().slice();
  const idx = _findOverrideIdx(overrides, sceneIdx, autoSeg, anchor);
  if (idx < 0) {
    overrides.push({ anchor, sceneIdx, autoSeg, action: 'break' });
  } else if (overrides[idx].action === 'break') {
    overrides.splice(idx, 1);                      // toggle off
  } else {
    overrides[idx] = { anchor, sceneIdx, autoSeg, action: 'break' };
  }
  await _persistSegmentOverrides(overrides);
  _renderSceneViewBody();
}

async function toggleSegmentMerge(anchor, sceneIdx, autoSeg) {
  if (!anchor) return;
  const overrides = _segmentOverrides().slice();
  const idx = _findOverrideIdx(overrides, sceneIdx, autoSeg, anchor);
  if (idx < 0) {
    overrides.push({ anchor, sceneIdx, autoSeg, action: 'merge' });
  } else if (overrides[idx].action === 'merge') {
    overrides.splice(idx, 1);                      // toggle off
  } else {
    overrides[idx] = { anchor, sceneIdx, autoSeg, action: 'merge' };
  }
  await _persistSegmentOverrides(overrides);
  _renderSceneViewBody();
}

async function openBatchJsonViewer() {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  // Restore sounds-checkbox state from localStorage and wire persistence
  const _bjSoundsCb = document.getElementById('batch-json-sounds');
  if (_bjSoundsCb && !_bjSoundsCb.dataset.wired) {
    const saved = localStorage.getItem('batch_json_sounds');
    _bjSoundsCb.checked = saved === null ? true : saved === '1';
    _bjSoundsCb.addEventListener('change', () => {
      localStorage.setItem('batch_json_sounds', _bjSoundsCb.checked ? '1' : '0');
    });
    _bjSoundsCb.dataset.wired = '1';
  }
  // Re-fetch episode to get latest batch_prompts (may have been built in another session)
  let ep;
  try {
    ep = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
    S.episode = ep;
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000); return;
  }

  const promptsMap = ep.batch_prompts || {};
  const anchors = Object.keys(promptsMap);
  const meta = document.getElementById('batch-json-meta');
  const ta   = document.getElementById('batch-json-text');

  // Compute current script hash to compare
  const currentScript = (ep.script || '');
  const computeHash = async (text) => {
    if (!window.crypto?.subtle) return null;
    const buf = new TextEncoder().encode(text);
    const hash = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  };
  const curHash = await computeHash(currentScript);
  const savedHash = ep.batch_script_hash || '';
  const isStale = savedHash && curHash && savedHash !== curHash;

  if (!anchors.length) {
    meta.innerHTML = `<span style="color:var(--warning)">⚠ Batch-JSON ещё не построен.</span>
      Запусти Auto-mode параллельно (lastframe / cut-frames OFF) — batch-compose построится автоматически.
      Или нажми "🔄 Пересобрать" если хочешь сделать это вручную.`;
    ta.value = '';
    openModal('modal-batch-json');
    return;
  }

  const builtAt = ep.batch_built_at ? new Date(ep.batch_built_at).toLocaleString('ru-RU') : '?';
  const staleNote = isStale
    ? `<span style="color:var(--warning)">⚠ STALE: сценарий менялся после батча (hash ${savedHash} → ${curHash}). Пересобери.</span>`
    : `<span style="color:var(--success)">✓ Актуален (script_hash ${savedHash || '—'})</span>`;

  // Compute current segments from frontend parser to compare with batch coverage
  const currentSegs = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments() : [];
  const expectedCount = currentSegs.length;
  const coverage = expectedCount > 0
    ? `${anchors.length}/${expectedCount}`
    : `${anchors.length}`;
  const coverageWarn = expectedCount > 0 && anchors.length < expectedCount
    ? `<span style="color:var(--warning)"> ⚠ ${expectedCount - anchors.length} сегмент(ов) не покрыто — Пересобери</span>`
    : '';
  // Check fallback markers
  const viaFallback = Object.values(promptsMap).filter(p => p._via_fallback).length;
  const fallbackNote = viaFallback > 0
    ? `<br><span style="color:var(--muted)">📦 ${viaFallback} сегмент(ов) добраны через per-chunk fallback (Claude в batch'е их пропустил)</span>`
    : '';
  meta.innerHTML = `
    <strong>Сегментов:</strong> ${coverage}${coverageWarn} ·
    <strong>Built:</strong> ${builtAt} ·
    ${staleNote}
    ${ep.batch_episode_blocking ? `<br><strong>episodeBlocking words:</strong> ${ep.batch_episode_blocking.split(/\s+/).filter(Boolean).length}` : ''}
    ${fallbackNote}
  `;

  // Construct readable JSON: sort segments by sceneIdx → segIdx
  const segArr = anchors.map(a => ({ anchor: a, ...promptsMap[a] }));
  segArr.sort((a, b) => (a.sceneIdx - b.sceneIdx) || (a.segIdx - b.segIdx));
  const obj = {
    episodeBlocking: ep.batch_episode_blocking || '',
    script_hash: ep.batch_script_hash || '',
    built_at: ep.batch_built_at || '',
    segments: segArr,
  };
  ta.value = JSON.stringify(obj, null, 2);
  openModal('modal-batch-json');
}

async function copyBatchJson() {
  const ta = document.getElementById('batch-json-text');
  const text = ta?.value || '';
  if (!text) { showToast('⚠ JSON пустой', 2500); return; }
  try {
    await navigator.clipboard.writeText(text);
    showToast('✓ Скопировано', 1800);
  } catch {
    ta.select(); document.execCommand('copy');
    showToast('✓ Скопировано', 1800);
  }
}

async function rebuildBatchPrompts() {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  // Need segments from the parsed scene-view
  const ta = document.getElementById('ep-script');
  if (!ta) return;
  const segs = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments() : [];
  if (!segs.length) {
    showToast('⚠ Нет сегментов — открой "🎬 Сцены" чтобы сценарий разбился', 4000);
    return;
  }
  const useStyle = !!document.getElementById('sd-use-style')?.checked;
  const styleVal = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly = !!document.getElementById('sd-base-only')?.checked;

  // Locate UI elements for proper loading state
  const meta = document.getElementById('batch-json-meta');
  const jsonTa = document.getElementById('batch-json-text');
  const rebuildBtn = Array.from(document.querySelectorAll('#modal-batch-json button'))
    .find(b => /Пересобрать/.test(b.textContent));

  // Visual loading state — all three areas (button, meta, textarea)
  if (rebuildBtn) {
    rebuildBtn.disabled = true;
    rebuildBtn.dataset.origText = rebuildBtn.innerHTML;
    rebuildBtn.innerHTML = '<span class="spinner"></span> Пересобираю...';
  }
  const startedAt = Date.now();
  let elapsedTimer = null;
  const updateMeta = () => {
    if (!meta) return;
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    meta.innerHTML = `<span class="spinner"></span>
      <strong>batch-compose работает...</strong>
      &nbsp;<span style="color:var(--muted)">прошло ${elapsed}с</span>
      &nbsp;<span style="color:var(--muted)">· сегментов в очереди: ${segs.length}</span>
      &nbsp;<span style="color:var(--muted)">· один Claude call, обычно 15-30с</span>`;
  };
  updateMeta();
  elapsedTimer = setInterval(updateMeta, 1000);
  if (jsonTa) {
    jsonTa.dataset.origValue = jsonTa.value;
    jsonTa.style.opacity = '0.35';
    jsonTa.style.filter = 'blur(0.5px)';
    jsonTa.value = '⏳ Пересборка идёт... старый JSON будет заменён через ~15-30с.\n\nClaude получает: полный сценарий + roster персонажей + список ' + segs.length + ' сегментов\n + текущий scene_blocking из textarea выше.\n\nВ ответе: episodeBlocking + per-segment prompt+refs для всех сегментов разом.';
  }
  showToast('🧠 batch-compose: один Claude call, ~15-30с — жди завершения', 5000);

  try {
    await saveEpisodeSilent();
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/seedance/batch-compose`,
      {
        segments: segs.map(s => ({
          anchor: s.anchor, sceneIdx: s.sceneIdx, segIdx: s.segIdx,
          text: s.text, has_close_up: !!s.has_close_up, durationSec: s.durationSec,
        })),
        base_outfits_only: baseOnly,
        style: useStyle ? styleVal : '',
      },
      { timeoutMs: 900_000 }
    );
    const tookS = Math.round((Date.now() - startedAt) / 1000);
    showToast(`✓ Batch пересобран за ${tookS}с · ${res.count} сегментов`, 4000);
    if (document.getElementById('batch-json-sounds')?.checked) {
      try { Sounds.playSuccess(); } catch (e) {}
    }
    if (jsonTa) {
      jsonTa.style.opacity = '';
      jsonTa.style.filter = '';
    }
    await openBatchJsonViewer();   // reload (this also resets meta to fresh state)
  } catch (e) {
    showToast('✗ ' + (e.message || e), 8000);
    if (document.getElementById('batch-json-sounds')?.checked) {
      try { Sounds.playError(); } catch (err) {}
    }
    if (meta) meta.innerHTML = `<span style="color:var(--danger)">✗ ${e.message || e}</span>`;
    if (jsonTa) {
      jsonTa.style.opacity = '';
      jsonTa.style.filter = '';
      if (jsonTa.dataset.origValue !== undefined) jsonTa.value = jsonTa.dataset.origValue;
    }
  } finally {
    if (elapsedTimer) clearInterval(elapsedTimer);
    if (rebuildBtn) {
      rebuildBtn.disabled = false;
      rebuildBtn.innerHTML = rebuildBtn.dataset.origText || '🔄 Пересобрать';
    }
  }
}

async function generateSceneBlocking() {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  const script = (val('ep-script') || '').trim();
  if (!script) { showToast('⚠ Сценарий пустой — заполни сначала', 3000); return; }
  const btn = document.getElementById('ep-gen-blocking-btn');
  const status = document.getElementById('ep-blocking-status');
  const ta = document.getElementById('ep-scene-blocking');
  if (!btn || !ta) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  if (status) status.textContent = '⚙ Claude разбирает сцену...';
  try {
    // Save script first so server reads latest
    await saveEpisodeSilent();
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/generate-scene-blocking`,
      {}
    );
    ta.value = res.blocking || '';
    if (S.episode) S.episode.scene_blocking = res.blocking || '';
    if (status) status.textContent = `✓ Готово · ${(res.blocking || '').split(/\s+/).length} слов`;
    showToast('✓ Blocking сгенерирован');
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e.message || e);
    showToast('✗ ' + (e.message || e), 6000);
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

// Manual «✨ Авто-правка» button — applies the per-user
// «🎬 Автоматическая правка» instruction (Settings modal) to every main
// promptEn of the current episode in one Claude call. Mirrors what
// turbo Auto-mode does automatically as step 3/3.
async function applyAutoRevise(btn) {
  if (!S.episode) { showToast('⚠ Сначала открой эпизод', 3000); return; }
  const hasBatch = !!(S.episode?.batch_prompts && Object.keys(S.episode.batch_prompts).length);
  if (!hasBatch) {
    showToast('⚠ Сначала запусти batch-compose (📋 batch JSON или Auto-mode турбо)', 5000);
    return;
  }
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> правлю…'; }
  try {
    const resp = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episodeNum}/seedance/revise-batch`,
      {},
      { timeoutMs: 5 * 60 * 1000 }
    );
    // Refresh episode so any panel that reads batch_prompts shows new text.
    try {
      const fresh = await api.get(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`);
      if (fresh) S.episode = fresh;
    } catch (_) {}
    showToast(`✓ Правка применена: ${resp.count}/${resp.expected} чанков`, 4000);
  } catch (e) {
    showToast('✗ Не удалось применить правку: ' + (e.message || e), 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

async function clearSegmentOverrides() {
  if (!_segmentOverrides().length) {
    showToast('Ручных правок и так нет', 2000);
    return;
  }
  if (!confirm('Сбросить все ручные правки разбивки? Сегменты пересчитаются автоматически.')) return;
  await _persistSegmentOverrides([]);
  _renderSceneViewBody();
  showToast('✓ Ручная разбивка сброшена', 2500);
}

