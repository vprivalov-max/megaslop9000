function _buildSeedanceCoverage(scriptText, chunks) {
  const ranges = [];
  for (const c of chunks || []) {
    const [s, e] = _findChunkRange(scriptText, c.chunk_text || '');
    if (s >= 0) ranges.push({ start: s, end: e, status: c.status, idx: c.idx, video: !!c.video_path });
  }
  return ranges;
}
function _statusForLine(line, coverage) {
  // Find all ranges covering this line. Multiple chunks can share the same
  // chunk_text (original + retries) — they all cover the same line range.
  // OLD bug: used strict `>` on start, so the FIRST-iterated range won and
  // stayed even if a later retry succeeded. Result: failed-then-succeeded
  // segments stayed red in UI.
  // FIX: prefer status by priority (completed > pending > failed) regardless
  // of which chunk came first.
  const covering = coverage.filter(r =>
    r.start <= line.offset + 5 && r.end >= line.offsetEnd - 5
  );
  if (!covering.length) return null;
  // 1) Any completed range wins. Pick latest by idx if multiple completed.
  const completed = covering.filter(r => r.status === 'completed');
  if (completed.length) {
    return completed.reduce((best, r) => (r.idx ?? 0) > (best.idx ?? 0) ? r : best);
  }
  // 2) Any in-progress (running/pending/submitted/etc.) wins over failed.
  const inProgress = covering.filter(r => r.status !== 'failed');
  if (inProgress.length) {
    return inProgress.reduce((best, r) => (r.idx ?? 0) > (best.idx ?? 0) ? r : best);
  }
  // 3) Everything failed — show the latest failed for user info.
  return covering.reduce((best, r) => (r.idx ?? 0) > (best.idx ?? 0) ? r : best);
}

function _renderScenesHTML(scenes, coverage = []) {
  if (!scenes.length) {
    return '<div class="muted" style="padding:16px">Сценарий пустой.</div>';
  }
  const showCov   = !!SCENE_VIEW_STATE.showCoverage && coverage.length;
  const editMode  = !!SCENE_VIEW_STATE.editMode;
  const overrideCount = _segmentOverrides().length;
  let html = '';
  // Toolbar — always shown in scene view
  const stats = { completed: 0, pending: 0, failed: 0 };
  for (const r of coverage) {
    if (r.status === 'completed') stats.completed++;
    else if (r.status === 'failed') stats.failed++;
    else stats.pending++;
  }
  // Auto-mode state for the button label — per-episode aware so the button on
  // episode N reflects N's own run, even when other episodes run in parallel.
  const autoActive = (typeof _autoRunForEpisode === 'function')
    ? !!_autoRunForEpisode(S?.seriesId, S?.episode?.number)
    : ((typeof AUTO !== 'undefined') && AUTO.active);
  const autoErr    = (localStorage.getItem('auto_error_mode') || 'heal');  // 'heal' | 'stop'
  const autoMode   = (localStorage.getItem('auto_mode_kind')  || 'sequential'); // 'sequential' | 'turbo'
  const seqTip = 'ПОСЛЕДОВАТЕЛЬНО — каждый чанк ждёт предыдущий, ему передаётся last frame видео + кадры перед склейками для непрерывности. Дольше, но качество и консистентность мизансцены лучше. Галочки lastframe/cutframes в Seedance-панели игнорируются — режим всегда включён.';
  const turboTip = 'ТУРБО — все чанки уходят в очередь Seedance параллельно через единый batch-JSON эпизода (один Claude-вызов на весь эпизод). Использует scene blocking для общей геометрии локации. Быстрее в N раз, но без видео-continuity между чанками — возможны мелкие drift-ы.';
  html += `<div class="ep-scene-toolbar">
    <button id="auto-mode-btn" class="${autoActive ? 'btn-danger' : 'btn-accent'}" onclick="_autoModeToggle()"
      title="Запустить генерацию ВСЕЙ серии в Seedance — пройдёт по всем сегментам сценария и отдаст каждый чанк в Seedance. Режим (последовательно / турбо) выбирается справа.">
      ${autoActive
        ? '⏸ Стоп — остановить генерацию серии'
        : '🎬 Сгенерировать всю серию в Seedance'}
    </button>
    <span class="auto-mode-kind" title="Переключатель режима генерации. Наведи на ⓘ для подробностей.">
      <label class="auto-mode-radio recommended" title="${esc(seqTip)}">
        <input type="radio" name="auto-mode-kind" ${autoMode === 'sequential' ? 'checked' : ''} onchange="_autoSaveModeKind('sequential')">
        <span class="amr-text">🐢 Последовательно</span>
        <span class="amr-rec">★ рекомендуется</span>
      </label>
      <label class="auto-mode-radio" title="${esc(turboTip)}">
        <input type="radio" name="auto-mode-kind" ${autoMode === 'turbo' ? 'checked' : ''} onchange="_autoSaveModeKind('turbo')">
        <span class="amr-text">⚡ Турбо</span>
      </label>
    </span>
    <span class="auto-err-mode" title="Что делать если Seedance вернёт moderation error">
      <label><input type="radio" name="auto-err" id="auto-error-mode-heal" ${autoErr === 'heal' ? 'checked' : ''} onchange="_autoSaveErrMode('heal')"> 🩹 Авто-лечение</label>
      <label><input type="radio" name="auto-err" id="auto-error-mode-stop" ${autoErr === 'stop' ? 'checked' : ''} onchange="_autoSaveErrMode('stop')"> ⏹ Стоп + сигнал</label>
    </span>
    <span id="auto-status" class="auto-status muted"></span>
    <span class="ep-tb-separator"></span>
    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer" title="Включает кнопки разделения/объединения сегментов. Выключи чтобы выделять текст не задевая UI.">
      <input type="checkbox" id="ep-edit-toggle" ${editMode ? 'checked' : ''} onchange="toggleSegmentEditMode(this.checked)">
      ✂ Ручная разбивка сегментов
    </label>
    ${overrideCount ? `<button class="btn-ghost btn-sm" onclick="clearSegmentOverrides()" title="Сбросить все ручные правки и пересчитать заново">Сбросить (×${overrideCount})</button>` : ''}
    ${coverage.length ? `
    <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;margin-left:14px">
      <input type="checkbox" id="ep-cov-toggle" ${showCov ? 'checked' : ''} onchange="toggleCoverage(this.checked)">
      Подсветить что уже сгенерено в Seedance
    </label>
    <span class="muted" style="font-size:0.78rem">
      · ${stats.completed} ✓  · ${stats.pending} ⏳  · ${stats.failed} ✗
    </span>` : ''}
  </div>`;
  scenes.forEach((sc, sIdx) => {
    const bg = SCENE_COLORS[sIdx % SCENE_COLORS.length];
    const bdr = SCENE_BORDERS[sIdx % SCENE_BORDERS.length];
    const heading = sc.heading || `Сцена ${sIdx + 1} (без заголовка)`;
    const inferredBadge = sc.inferred
      ? `<span class="ep-scene-inferred" title="Заголовок определён автоматически — INT./EXT. в сценарии не указан">auto</span>`
      : '';
    html += `<div class="ep-scene${editMode ? ' edit-mode' : ''}" style="background:${bg};border-left:4px solid ${bdr}">`;
    html += `<div class="ep-scene-header">
        <span class="ep-scene-tag">Сцена ${sIdx + 1}</span>
        <span class="ep-scene-loc">${esc(heading.slice(0, 120))}</span>
        ${inferredBadge}
        <span class="ep-scene-meta">~${sc.totalSec.toFixed(0)}с · ${sc.segCount} сегмент(ов) ×15с</span>
      </div>`;
    let lastSeg = -1;
    let segAcc = 0;
    sc.lines.forEach((l, lIdx) => {
      const startsNewSeg = (l.segIdx !== lastSeg);
      // Edit mode: BETWEEN two lines INSIDE same segment, render thin "split here" handle
      if (!startsNewSeg && editMode && lIdx > 0) {
        const anchor = _lineAnchor(l.text);
        const isOvr = l._override === 'break';
        // Escape BOTH single quotes (JS string delim inside onclick) AND
        // double quotes (HTML attribute delim) AND backslashes/ampersand.
        // Real production bug: dialogue line starting with «"The child …»
        // broke the onclick attribute at the first " — click silently
        // did nothing. _attrSafe handles all four meta-chars in order.
        const safeAnchor = _attrSafe(anchor);
        const autoSegArg = (typeof l._autoSeg === 'number') ? l._autoSeg : l.segIdx;
        html += `<div class="ep-split-handle ${isOvr ? 'ovr' : ''}"
          onclick="toggleSegmentBreak('${safeAnchor}', ${sIdx}, ${autoSegArg})"
          title="${isOvr ? 'Убрать ручной разрыв здесь' : 'Разделить сегмент перед этой строкой'}"
        >${isOvr ? '✓ разрыв здесь — клик чтобы убрать' : '✂ разделить здесь'}</div>`;
      }
      if (startsNewSeg) {
        if (lastSeg !== -1) html += `</div></div>`; // close prev seg-body + ep-seg
        lastSeg = l.segIdx;
        segAcc = 0;
        // Compute this segment's total seconds for the toolbar summary
        const segLines = sc.lines.filter(x => x.segIdx === l.segIdx);
        const segTotal = segLines.reduce((s, x) => s + x.duration, 0);
        // Two-tier warning. Segments in (15, 18.5]s were intentionally luft-merged
        // by Pass 3/5/7 (one fewer Seedance call, characters speak ~20% faster
        // — accepted up to 3.5s overflow). Only >18.5s is a real overflow that
        // won't fit cleanly and needs manual splitting.
        const overflowWarn = segTotal > 18.5
          ? `<span class="ep-seg-warn" title="Содержимое сильно выходит за 15-сек лимит Seedance — нужна ручная разбивка">${segTotal.toFixed(1)}с ⚠</span>`
          : segTotal > 15.0
          ? `<span class="ep-seg-dur ep-seg-luft" title="Luft-merge: 15-18.5с контента в 15-сек чанке — пацинг будет слегка ускоренный">${segTotal.toFixed(1)}с ⚡</span>`
          : `<span class="ep-seg-dur" title="Расчётная длительность сегмента">${segTotal.toFixed(1)}с</span>`;
        // First-line anchor of this segment for the MERGE button (per-line, OK
        // if it shifts when the user edits text — merge is a local operation).
        // See _attrSafe note above: must escape both " (HTML attr delim) and
        // ' (JS string delim) so a dialogue line starting with «"» doesn't
        // truncate the onclick handler.
        const firstAnchor = _lineAnchor(l.text);
        const safeFirstAnchor = _attrSafe(firstAnchor);
        // Per-segment AUTO-SKIP anchor MUST be unique across the episode.
        // Bug 2026-05-12: using firstAnchor here collapsed all segments whose
        // first line started with the same text (e.g. several segments
        // starting with «Elena:» or «MARCUS») onto a single skip key — un-
        // ticking one's AUTO checkbox flipped them all. Use the scene/segment
        // coordinate as the stable identity. Old skip entries written with
        // the previous (text-based) format silently fall through — segments
        // default to «included» and the user can re-skip if needed.
        const autoSkipKey = `s${sIdx}g${l.segIdx}`;
        const isMergedHere = l._override === 'merge';
        const isAutoSkipped = _isSegmentAutoSkipped(autoSkipKey);
        const mergeAutoSeg = (typeof l._autoSeg === 'number') ? l._autoSeg : l.segIdx;
        const editBtns = editMode && lIdx > 0
          ? `<button class="ep-seg-mini" onclick="toggleSegmentMerge('${safeFirstAnchor}', ${sIdx}, ${mergeAutoSeg})"
              title="${isMergedHere ? 'Восстановить разделение' : 'Объединить с предыдущим сегментом'}"
            >${isMergedHere ? '↩ разъединить' : '🔗 ↑ объединить'}</button>`
          : '';
        const autoCb = `<label class="ep-seg-auto-cb" title="${isAutoSkipped ? 'Сегмент пропускается в Auto-mode — клик включит обратно' : 'Сегмент будет сгенерён при запуске Auto-mode — клик исключит его'}">
            <input type="checkbox" ${isAutoSkipped ? '' : 'checked'} onchange="toggleSegmentAutoInclude('${autoSkipKey}')">
            <span>auto</span>
          </label>`;
        html += `<div class="ep-seg${isAutoSkipped ? ' auto-skipped' : ''}" data-seg="${l.segIdx + 1}" data-scene-idx="${sIdx}" data-seg-idx="${l.segIdx}">
          <div class="ep-seg-bracket" title="Seedance-сегмент ${l.segIdx + 1}">${l.segIdx + 1}</div>
          <div class="ep-seg-body">
            <div class="ep-seg-toolbar">
              <button class="ep-seg-send" onclick="sendSceneSegmentToSeedance(${sIdx}, ${l.segIdx})"
                title="Скопировать сегмент в Seedance compose и прокрутить вниз">
                🎬 в Сиданс
              </button>
              <button class="ep-seg-send" onclick="showChunksForSegment(${sIdx}, ${l.segIdx})"
                title="Прокрутить вниз к сгенерированным видео-чанкам для этого сегмента и подсветить их">
                📺 К видео
              </button>
              ${autoCb}
              ${overflowWarn}
              ${editBtns}
            </div>`;
      }
      segAcc += l.duration;
      let _cls = 'ep-line';
      let _tag = '';
      if (showCov) {
        const cov = _statusForLine(l, coverage);
        if (cov) {
          _cls += cov.status === 'completed' ? ' sd-done' : cov.status === 'failed' ? ' sd-failed' : ' sd-pending';
          const ic = cov.status === 'completed' ? '✓' : cov.status === 'failed' ? '✗' : '⏳';
          _tag = `<span class="ep-line-tag" title="Seedance #${cov.idx} · ${cov.status}">${ic} #${cov.idx}</span>`;
        }
      }
      if (l._override === 'break') _cls += ' ovr-break';
      else if (l._override === 'merge') _cls += ' ovr-merge';
      // Per-line close-up toggle for dialogue lines (always visible — small icon
      // on the left, doesn't interfere with text selection unless clicked).
      const isDialogue = _isDialogueLine(l.text);
      const isCloseUp = isDialogue && _isLineCloseUp(l.text);
      if (isCloseUp) _cls += ' line-close-up';
      const closeUpBtn = isDialogue
        ? `<button class="ep-line-cu-btn ${isCloseUp ? 'active' : ''}"
            onclick="toggleLineCloseUp('${_lineAnchor(l.text).replace(/'/g, "\\'")}')"
            title="${isCloseUp ? 'Убрать метку close-up' : 'Пометить эту реплику как close-up — спикер один в кадре крупным планом, фон размыт'}"
          >${isCloseUp ? '🎯' : '○'}</button>`
        : '';
      html += `<div class="${_cls}">${closeUpBtn}${_tag}${esc(l.text || ' ')}</div>`;
    });
    if (lastSeg !== -1) html += `</div></div>`;
    html += `</div>`;
  });
  return html;
}

function toggleSceneView() {
  const ta = document.getElementById('ep-script');
  const view = document.getElementById('ep-script-scenes');
  const btn = document.getElementById('ep-scenes-btn');
  if (!ta || !view || !btn) return;
  const isOpen = !view.classList.contains('hidden');
  if (isOpen) {
    view.classList.add('hidden');
    ta.classList.remove('hidden');
    btn.classList.remove('active');
    btn.innerHTML = '🎬 Сцены';
  } else {
    try {
      const saved = localStorage.getItem('sceneCov');
      if (saved !== null) SCENE_VIEW_STATE.showCoverage = (saved === '1');
    } catch {}
    _renderSceneViewBody();
    view.classList.remove('hidden');
    ta.classList.add('hidden');
    btn.classList.add('active');
    btn.innerHTML = '✏ Редактировать';
  }
}

const SCENE_VIEW_STATE = { showCoverage: true, editMode: false };

// Make a string safe for embedding as a JS-string argument inside an HTML
// onclick="..." attribute. Order matters:
//   1) ampersand FIRST so we don't double-encode the entities we add next
//   2) double-quote → &quot; so the surrounding attribute (which uses ") doesn't terminate early
//   3) single-quote → \' so the inner JS string (delim ') doesn't terminate early
//   4) backslash → escape so existing \ in the string doesn't become a JS escape sequence
function _attrSafe(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '&quot;')
    .replace(/'/g, "\\'");
}

// Manual segment overrides per-episode. Loaded from S.episode.segment_overrides
// at scene-view open time, mutated by user click, persisted via PUT.
// Each override: {anchor: "first ~60 chars of trimmed line", action: "break"|"merge"}
//   - "break"  → that line MUST start a new segment (split before it)
//   - "merge"  → that line MUST stay with the previous segment (no break)
function _segmentOverrides() {
  return (S.episode && S.episode.segment_overrides) || [];
}

