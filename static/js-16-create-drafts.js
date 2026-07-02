// ── Append-to-existing-series flow ──────────────────────────────────────────
// Twin of the import-from-script flow above, but adds episodes to the CURRENT
// series instead of creating a new one. Numbering continues from the highest
// existing episode (so a 38-episode series + 12 new ones → episodes 39-50).

// ── Generic modal draft helpers ───────────────────────────────────────────────
function _draftSave(prefix, fields) {
  try {
    const obj = {};
    for (const id of fields) {
      const el = document.getElementById(id);
      if (el) obj[id] = el.type === 'checkbox' ? el.checked : (el.value || '');
    }
    localStorage.setItem(prefix, JSON.stringify(obj));
  } catch {}
}
function _draftRestore(prefix, fields) {
  try {
    const raw = localStorage.getItem(prefix);
    if (!raw) return;
    const obj = JSON.parse(raw);
    for (const id of fields) {
      if (!(id in obj)) continue;
      const el = document.getElementById(id);
      if (!el) continue;
      if (el.type === 'checkbox') el.checked = !!obj[id];
      else el.value = obj[id];
    }
  } catch {}
}
function _draftClear(prefix) {
  try { localStorage.removeItem(prefix); } catch {}
}
function _draftWire(prefix, fields, extraCb) {
  for (const id of fields) {
    const el = document.getElementById(id);
    if (!el || el.dataset.draftWired === prefix) continue;
    el.dataset.draftWired = prefix;
    const save = () => { _draftSave(prefix, fields); if (extraCb) extraCb(); };
    el.addEventListener('change', save);
    el.addEventListener('input',  save);
  }
}

// ── Append-script modal draft ─────────────────────────────────────────────────
const _APPEND_DRAFT_KEY = 'modalDraft:append-script';
const _APPEND_DRAFT_FIELDS = [
  'append-gen-count', 'append-gen-duration-sec', 'append-gen-lines-count',
  'append-gen-style', 'append-gen-max-chars', 'append-gen-no-interruptions',
  'append-script-text', 'append-gen-direction',
];
// Keep old aliases so nothing breaks
function _appendPersistKey(id) { return `appendGenPrefs:${id}`; }
function _appendRestorePrefs() { _draftRestore(_APPEND_DRAFT_KEY, _APPEND_DRAFT_FIELDS); }
function _appendWireAutosave() { _draftWire(_APPEND_DRAFT_KEY, _APPEND_DRAFT_FIELDS); }
function _appendDraftClear()   { _draftClear(_APPEND_DRAFT_KEY); }

// ── Create-series modal draft (import-mode fields) ────────────────────────────
const _CREATE_DRAFT_KEY = 'modalDraft:create-series';
const _CREATE_DRAFT_FIELDS = [
  'import-series-title', 'import-series-script',
];
function _createSeriesDraftRestore() {
  _draftRestore(_CREATE_DRAFT_KEY, _CREATE_DRAFT_FIELDS);
  // Restore mode — if we had script content, switch to import mode
  try {
    const raw = localStorage.getItem(_CREATE_DRAFT_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    if (obj['import-series-script']?.trim()) setSeriesCreateMode('import');
  } catch {}
}
function _createSeriesDraftWire() {
  _draftWire(_CREATE_DRAFT_KEY, _CREATE_DRAFT_FIELDS);
  // Also save on mode-switch (wired via setSeriesCreateMode callers)
}
function _createSeriesDraftClear() { _draftClear(_CREATE_DRAFT_KEY); }

async function backfillDevices() {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  showToast('🔄 Анализирую сюжетные приёмы…', 3000);
  try {
    const r = await api.post(`/api/series/${S.seriesId}/backfill-devices`, {});
    showToast(`✓ Реестр устройств обновлён (${r.updated} серий)`, 4000);
  } catch (e) {
    showToast('✗ Ошибка: ' + (e?.message || e), 5000);
  }
}

function openAppendScript() {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  // Clear only UI state — script text is restored from draft below
  const previewEl = document.getElementById('append-script-preview');
  if (previewEl) previewEl.innerHTML = '';
  const logicEl = document.getElementById('append-script-logic');
  if (logicEl) logicEl.innerHTML = '';
  const adaptEl = document.getElementById('append-adapt-result');
  if (adaptEl) adaptEl.innerHTML = '';
  const statusEl = document.getElementById('append-gen-status');
  if (statusEl) statusEl.textContent = '';
  openModal('modal-append-script');
  // Restore draft (script text, direction, gen params) from localStorage
  _appendRestorePrefs();
  _appendWireAutosave();
  // Set mode based on restored content: paste if script has text, else default paste
  const ta = document.getElementById('append-script-text');
  const hasDraft = (ta?.value || '').trim().length > 0;
  const dirTa = document.getElementById('append-gen-direction');
  const hasDirDraft = (dirTa?.value || '').trim().length > 0;
  setAppendMode(hasDirDraft && !hasDraft ? 'generate' : 'paste');
  appendUpdateStats();
}

// Switch between «📋 Вставить готовый» and «✨ Сгенерировать новые» modes.
// In generate mode the top section unfolds with form + ✨ button. The bottom
// textarea+preview+logic-check stays visible in BOTH modes because generated
// script lands in the same textarea — user reviews/edits it before commit.
function setAppendMode(mode) {
  const pasteBtn = document.getElementById('append-mode-paste-btn');
  const genBtn   = document.getElementById('append-mode-generate-btn');
  const genBlock = document.getElementById('append-generate-block');
  const pasteHelp = document.getElementById('append-paste-help');
  const isGen = mode === 'generate';
  if (pasteBtn) pasteBtn.classList.toggle('active', !isGen);
  if (genBtn)   genBtn.classList.toggle('active', isGen);
  if (genBlock) genBlock.style.display = isGen ? '' : 'none';
  if (pasteHelp) pasteHelp.style.display = isGen ? 'none' : '';

  // Adapt the generate-mode help text + direction-field label/placeholder
  // based on whether the series already has episodes. From-scratch (no eps)
  // needs a starting brief; continuation needs a direction hint.
  if (isGen) {
    const existingCount = (S.episodes || []).filter(e => (e.script || '').trim()).length;
    const fromScratch = existingCount === 0;
    const help = document.getElementById('append-gen-help');
    const dirLabel = document.getElementById('append-gen-direction-label');
    const dirField = document.getElementById('append-gen-direction');
    const dirHint = document.getElementById('append-gen-direction-hint');
    if (fromScratch) {
      if (help) help.innerHTML =
        '<strong style="color:#fbbf24">🆕 Сериал пустой — будем писать с нуля.</strong> ' +
        'Claude напишет первые серии используя жанр / тон / синопсис из Bible (если заполнены) + твоё описание. ' +
        'После генерации текст ляжет в textarea — можно его проверить на логику, отредактировать и добавить в сериал.';
      if (dirLabel) dirLabel.textContent = 'О чём сериал · какая завязка · кто герои';
      if (dirField) dirField.placeholder =
        'Опиши идею сериала и старт сюжета. Например:\n' +
        '«Молодая мать узнаёт что её ребёнок поменян в роддоме. Настоящего ребёнка воспитывает богатая семья. ' +
        'Она устраивается к ним домработницей чтобы быть рядом — но влюбляется в отчима. Cliffhanger пилота: ' +
        'муж богатой семьи узнаёт её, потому что это его бывшая.»\n\n' +
        'Чем конкретнее — тем точнее. Можно указать главных героев, центральный конфликт, тон.';
      if (dirHint) dirHint.innerHTML =
        '💡 Если оставить пустым — Claude возьмёт <strong>синопсис из Bible</strong> сериала. ' +
        'Если и там пусто — запрос не пройдёт.';
    } else {
      if (help) help.innerHTML =
        `Claude напишет ${existingCount > 0 ? '<strong>продолжение существующих ' + existingCount + ' серий</strong>' : 'сценарий'} — ` +
        'использует тех же персонажей и локации, сохранит твой стиль, добавит cliffhanger в каждую серию. ' +
        'После генерации текст подставится в textarea ниже — там можно его проверить на логику, отредактировать и затем добавить в сериал.';
      if (dirLabel) dirLabel.textContent = 'Куда сюжет идёт дальше (опционально)';
      if (dirField) dirField.placeholder =
        'Например: «Vivian арестовывают, но открывается что её прикрывал Lawrence Cole — Adrian копает компромат на Cole, ' +
        'Sophie начинает работать с Adrian-ом. Cliffhanger финала: появляется завещание которое никто не видел.»\n\n' +
        'Если оставить пустым — Claude сам придумает развитие из контекста уже написанных серий.';
      if (dirHint) dirHint.textContent = 'Чем конкретнее — тем точнее. Можно указать беды/повороты/cliffhanger финала.';
    }
    // Surface landmark-attached generation if finale / nearest-checkpoint is set ahead.
    try { refreshAppendLandmarkBlock(); } catch (e) { /* ignore */ }
  }
}

// Show/hide the "🎯 До финала / До контрольной точки" buttons in the "Добавить сценарий"
// modal based on whether the series has a finale/checkpoint pinned past the last written ep.
function refreshAppendLandmarkBlock() {
  const block = document.getElementById('append-landmark-block');
  const info  = document.getElementById('append-landmark-info');
  const finBtn = document.getElementById('append-to-finale-btn');
  const cpBtn  = document.getElementById('append-to-checkpoint-btn');
  if (!block) return;
  const s = S.series || {};
  const eps = S.episodes || [];
  const writtenEps = eps.filter(e => (e.script || '').trim()).map(e => e.number).sort((a, b) => a - b);
  const lastWritten = writtenEps.length ? writtenEps[writtenEps.length - 1] : 0;
  const nextEp = lastWritten + 1;

  const fin = s.finale;
  const finAhead = fin && (fin.description || '').trim() && fin.episode >= nextEp;
  const cps = (s.checkpoints || []).filter(c => (c.description || '').trim() && c.episode >= nextEp).sort((a, b) => a.episode - b.episode);
  const nearestCp = cps[0];

  // Also consider landmarks at-or-before lastWritten as "overwrite candidates"
  const finAtOrAny = fin && (fin.description || '').trim();
  const cpsAtOrAny = (s.checkpoints || []).filter(c => (c.description || '').trim()).sort((a, b) => a.episode - b.episode);
  const fallbackFin = finAtOrAny ? fin : null;
  const fallbackCp  = nearestCp || (cpsAtOrAny.length ? cpsAtOrAny[cpsAtOrAny.length - 1] : null);

  if (!fallbackFin && !fallbackCp) {
    block.style.display = 'none';
    return;
  }
  block.style.display = '';
  const lines = [];
  if (lastWritten) {
    lines.push(`Последняя написанная: <b>Ep ${lastWritten}</b> · Следующая: <b>Ep ${nextEp}</b>`);
  } else {
    lines.push(`Следующая серия для генерации: <b>Ep ${nextEp}</b>`);
  }
  if (fallbackFin) {
    const target = fallbackFin.episode;
    const overwrite = target <= lastWritten;
    lines.push(`🏁 Финал: <b>Ep ${target}</b>` +
      (overwrite ? ' <span style="color:#fbbf24">(уже написан — будет ПЕРЕЗАПИСЬ)</span>' : ''));
  }
  if (fallbackCp) {
    const target = fallbackCp.episode;
    const overwrite = target <= lastWritten;
    lines.push(`📍 Чекпоинт: <b>Ep ${target}</b>` +
      (overwrite ? ' <span style="color:#fbbf24">(уже написан — будет ПЕРЕЗАПИСЬ)</span>' : ''));
  }
  if (info) info.innerHTML = lines.join('<br>');

  // Overwrite-from row — visible when ANY relevant landmark is at-or-before lastWritten
  const finIsOverwrite = fallbackFin && fallbackFin.episode <= lastWritten;
  const cpIsOverwrite  = fallbackCp && fallbackCp.episode <= lastWritten;
  const overwriteRow   = document.getElementById('append-landmark-overwrite-row');
  const overwriteInput = document.getElementById('append-landmark-start-ep');
  if (overwriteRow && overwriteInput) {
    if (finIsOverwrite || cpIsOverwrite) {
      overwriteRow.style.display = '';
      const targetForDefault = fallbackFin ? fallbackFin.episode : fallbackCp.episode;
      const defaultStart = Math.max(1, targetForDefault - 2);
      const cur = parseInt(overwriteInput.value, 10);
      if (!cur || cur > targetForDefault || cur < 1) {
        overwriteInput.value = String(defaultStart);
      }
    } else {
      overwriteRow.style.display = 'none';
    }
  }

  const getStartFor = (target) => {
    if (target > lastWritten) return nextEp;       // fresh-extend mode
    const raw = parseInt(overwriteInput?.value || '0', 10);
    return raw && raw >= 1 && raw <= target ? raw : Math.max(1, target - 2);
  };

  if (finBtn) {
    if (fallbackFin) {
      const target = fallbackFin.episode;
      const start = getStartFor(target);
      const span = target - start + 1;
      finBtn.textContent = `🏁 До финала (${span} сер., Ep ${start}…${target})`;
      finBtn.style.display = span >= 1 && span <= 8 ? '' : 'none';
      finBtn.title = span > 8 ? `Слишком далеко (${span} серий). Лимит 8 за раз.` : '';
      finBtn.dataset.targetEp = String(target);
      finBtn.dataset.startEp  = String(start);
    } else {
      finBtn.style.display = 'none';
    }
  }
  if (cpBtn) {
    if (fallbackCp) {
      const target = fallbackCp.episode;
      const start = getStartFor(target);
      const span = target - start + 1;
      cpBtn.textContent = `📍 До чекпоинта (${span} сер., Ep ${start}…${target})`;
      cpBtn.style.display = span >= 1 && span <= 8 ? '' : 'none';
      cpBtn.title = span > 8 ? `Слишком далеко (${span} серий). Лимит 8 за раз.` : '';
      cpBtn.dataset.targetEp = String(target);
      cpBtn.dataset.startEp  = String(start);
    } else {
      cpBtn.style.display = 'none';
    }
  }
  // Wire input to re-render the buttons live
  if (overwriteInput && !overwriteInput._wired) {
    overwriteInput.addEventListener('input', () => refreshAppendLandmarkBlock());
    overwriteInput._wired = true;
  }
}

// Run /generate-to-landmark — generates episodes one-by-one from next-unwritten to landmark,
// with synopsis overwrite from bridge plan beats. Results in directly-created episodes (no textarea).
async function appendGenerateToLandmark(landmarkType) {
  if (!S.seriesId) return;

  const btn = document.getElementById(landmarkType === 'finale' ? 'append-to-finale-btn' : 'append-to-checkpoint-btn');
  const targetEp = parseInt(btn?.dataset?.targetEp, 10);
  const startEp  = parseInt(btn?.dataset?.startEp, 10);
  if (!targetEp) {
    showToast(`Не удалось определить целевую серию для ${landmarkType}`);
    return;
  }
  if (!startEp || startEp < 1) {
    showToast('Не удалось определить серию старта');
    return;
  }
  const eps = S.episodes || [];
  const writtenEps = eps.filter(e => (e.script || '').trim()).map(e => e.number);
  const lastWritten = writtenEps.length ? Math.max(...writtenEps) : 0;
  const isOverwrite = startEp <= lastWritten;

  const span = targetEp - startEp + 1;
  if (span < 1) {
    showToast('Целевая серия уже позади');
    return;
  }
  if (span > 8) {
    showToast(`Слишком большой диапазон (${span}). Макс 8 за раз.`);
    return;
  }
  const label = landmarkType === 'finale' ? 'финала' : 'контрольной точки';
  const overwriteWarn = isOverwrite
    ? `\n⚠ ПЕРЕЗАПИСЬ: серии Ep ${startEp}…${targetEp} уже имеют сценарии. Старые версии уйдут в history каждой серии.`
    : '';
  if (!await appConfirm({
    title: `Сгенерировать ${span} серий до ${label}?`,
    message:
      `Диапазон: Ep ${startEp}…${targetEp}.${overwriteWarn}\n\n` +
      `Для каждой серии:\n` +
      `  1) Bridge-план разложит ${label} на пошаговые beat'ы\n` +
      `  2) Синопсис будет ЗАПИСАН из bridge-beat'а (с состоянием мира в начале серии)\n` +
      `  3) Сценарий сгенерируется с максимальным весом конвергенции\n\n` +
      `Это займёт примерно ${Math.ceil(span * 60)}-${span * 120}с. Серии создаются напрямую.`,
    okText: 'Поехали',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;

  const finBtn = document.getElementById('append-to-finale-btn');
  const cpBtn  = document.getElementById('append-to-checkpoint-btn');
  const statusEl = document.getElementById('append-landmark-status');
  [finBtn, cpBtn].forEach(b => { if (b) b.disabled = true; });
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span> Генерирую ${span} серий (Ep ${startEp}…${targetEp}) до ${label}…`;

  try {
    const body = {
      landmark_type: landmarkType,
      model: _selectedWriterModel('writer-model-create'),
      start_episode: startEp,
    };
    if (landmarkType === 'checkpoint') body.landmark_episode = targetEp;
    // POST kicks off the background worker and returns immediately (202)
    const kickoff = await api.post(`/api/series/${S.seriesId}/generate-to-landmark`, body);
    if (kickoff.error && !kickoff.started) {
      throw new Error(kickoff.error);
    }
    // Poll status every 2.5s — show "Сейчас: Ep X из Y" updates live
    const pollUrl = `/api/series/${S.seriesId}/generate-to-landmark/status`;
    let lastCurrent = null;
    const startTs = Date.now();
    while (true) {
      await new Promise(r => setTimeout(r, 2500));
      let st;
      try {
        st = await api.get(pollUrl);
      } catch (e) {
        if (statusEl) statusEl.innerHTML = `<span style="color:#f87171">✗ Сеть: ${esc(e?.message || e)}</span>`;
        continue;
      }
      if (!st || st.state === 'idle') continue;
      const elapsed = Math.round((Date.now() - startTs) / 1000);
      const done = st.completed || 0;
      const tot = st.total || span;
      const cur = st.current_ep;
      if (st.state === 'running' || st.state === 'starting') {
        if (statusEl) {
          let html = `<span class="spinner"></span> Сейчас пишу Ep <b>${cur ?? '—'}</b> · готово ${done}/${tot} · прошло ${elapsed}с`;
          if (st.results?.length) {
            const okList = st.results.filter(r => r.ok).map(r => r.episode).join(', ');
            if (okList) html += `<br><span style="color:#4ade80">✓ готовы: Ep ${okList}</span>`;
          }
          statusEl.innerHTML = html;
        }
        lastCurrent = cur;
        continue;
      }
      // Terminal: done | failed
      const errLine = st.error ? `<br><span style="color:#fbbf24">⚠ ${esc(st.error)}</span>` : '';
      const successCount = (st.results || []).filter(r => r.ok).length;
      if (statusEl) {
        const colour = st.state === 'done' ? '#4ade80' : '#f87171';
        statusEl.innerHTML = `<span style="color:${colour}">${st.state === 'done' ? '✓' : '✗'} ${st.state === 'done' ? 'Готово' : 'Остановилось'}: ${successCount} из ${tot} серий сгенерировано (Ep ${st.start_episode}…${st.target_episode}).${errLine}</span>`;
      }
      showToast(`${st.state === 'done' ? '✓' : '⚠'} ${successCount}/${tot} серий до ${label}`, 6000);
      break;
    }
    // Refresh series + episode list so new episodes appear
    if (typeof loadSeries === 'function') await loadSeries(S.seriesId);
    if (typeof renderEpisodesList === 'function') renderEpisodesList();
  } catch (e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#f87171">✗ Ошибка: ${esc(e?.message || e)}</span>`;
    showToast('Ошибка генерации: ' + (e?.message || e), 6000);
  } finally {
    [finBtn, cpBtn].forEach(b => { if (b) b.disabled = false; });
  }
}

// Call Claude to write N new episodes continuing the series. Result lands in
// the textarea so user can preview/logic-check/edit/commit via the existing
// paste-flow buttons (no separate commit path — same «Добавить серии»).
