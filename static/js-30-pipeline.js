// ── Production pipeline ───────────────────────────────────────────────────────

function renderPipeline() {
  const el = document.getElementById('pipeline-section');
  if (!el) return;
  // The legacy stage-1/2 milestones grid has been retired. New series start
  // empty; the user adds episodes via "+ Эпизод" and may pin checkpoints / finale
  // via the toolbar. Story-landmarks panel is rendered separately.
  el.innerHTML = '';
  renderLandmarksPanel();
}

// ── Story landmarks panel (checkpoints + finale) ─────────────────────────────
function renderLandmarksPanel() {
  const el = document.getElementById('landmarks-panel');
  if (!el) return;
  const cps = (S.series.checkpoints || []).slice().sort((a,b) => a.episode - b.episode);
  const fin = S.series.finale;
  if (!cps.length && !fin) { el.innerHTML = ''; return; }

  const cpsHtml = cps.map(c => `
    <div class="landmark-card" onclick="openCheckpointModal(${c.episode})" title="Редактировать">
      <div class="landmark-badge cp">Эп. ${c.episode}</div>
      <div class="landmark-text">${esc(c.description || '—')}</div>
      <button class="landmark-del" onclick="event.stopPropagation();confirmDeleteCheckpoint(${c.episode})" title="Удалить точку">✕</button>
    </div>
  `).join('');

  const finHtml = fin ? `
    <div class="landmark-card finale" onclick="openFinaleModal()" title="Редактировать">
      <div class="landmark-badge fin">🏁 Финал · Эп. ${fin.episode}</div>
      <div class="landmark-text">${esc(fin.description || '—')}</div>
    </div>
  ` : '';

  el.innerHTML = `
    <div class="landmarks-wrap">
      <div class="landmarks-header">📌 Сюжетные ориентиры</div>
      <div class="landmarks-list">${cpsHtml}${finHtml}</div>
    </div>
  `;
}

// ── Checkpoint modal ─────────────────────────────────────────────────────────
let _editingCheckpointEp = null;

function openCheckpointModal(ep) {
  _editingCheckpointEp = (typeof ep === 'number') ? ep : null;
  const cp = (S.series.checkpoints || []).find(c => c.episode === _editingCheckpointEp);
  setVal('cp-episode', cp ? cp.episode : '');
  setVal('cp-description', cp ? (cp.description || '') : '');
  document.getElementById('modal-checkpoint-title').textContent = cp ? '🎯 Контрольная точка (редактирование)' : '🎯 Новая контрольная точка';
  document.getElementById('cp-delete-btn').style.display = cp ? '' : 'none';
  document.getElementById('cp-status').textContent = '';
  openModal('modal-checkpoint');
}

async function generateCheckpointDraft() {
  const epStr = val('cp-episode');
  const ep = parseInt(epStr, 10);
  if (!ep || ep < 1) { showToast('Сначала укажи номер серии'); return; }
  const btn = document.getElementById('cp-gen-btn');
  const status = document.getElementById('cp-status');
  btn.disabled = true; const orig = btn.innerHTML; btn.innerHTML = '<span class="spinner"></span> Думаем...';
  status.textContent = '';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title, episodeNum: ep };
    const res = await trackTask(`Контрольная точка Эп. ${ep}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/checkpoints/${ep}/generate`, {}, { timeoutMs: 90_000 })
    );
    if (res.description) {
      setVal('cp-description', res.description);
      status.textContent = '✓ Драфт готов — отредактируй или сохрани';
      status.style.color = 'var(--success)';
    }
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

async function saveCheckpoint() {
  const ep = parseInt(val('cp-episode'), 10);
  const description = val('cp-description').trim();
  if (!ep || ep < 1) { showToast('Укажи корректный номер серии'); return; }
  if (!description) { showToast('Опиши, что должно произойти, или сгенерируй драфт'); return; }
  const status = document.getElementById('cp-status');
  const saveBtn = document.getElementById('cp-save-btn');
  saveBtn.disabled = true;
  try {
    // If user changed the episode number while editing, drop the old entry first
    if (_editingCheckpointEp !== null && _editingCheckpointEp !== ep) {
      try { await api.del(`/api/series/${S.seriesId}/checkpoints/${_editingCheckpointEp}`); } catch (_) {}
    }
    const res = await api.post(`/api/series/${S.seriesId}/checkpoints`, { episode: ep, description });
    S.series.checkpoints = res.checkpoints;
    renderLandmarksPanel();
    closeModal('modal-checkpoint');
    showToast(`Контрольная точка на эп. ${ep} сохранена`);
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    saveBtn.disabled = false;
  }
}

async function deleteCheckpoint() {
  if (_editingCheckpointEp === null) return;
  if (!confirm(`Удалить контрольную точку на эп. ${_editingCheckpointEp}?`)) return;
  try {
    const res = await api.del(`/api/series/${S.seriesId}/checkpoints/${_editingCheckpointEp}`);
    S.series.checkpoints = res.checkpoints;
    renderLandmarksPanel();
    closeModal('modal-checkpoint');
  } catch (e) {
    alert('Не удалось удалить: ' + e.message);
  }
}

async function confirmDeleteCheckpoint(ep) {
  if (!confirm(`Удалить контрольную точку на эп. ${ep}?`)) return;
  try {
    const res = await api.del(`/api/series/${S.seriesId}/checkpoints/${ep}`);
    S.series.checkpoints = res.checkpoints;
    renderLandmarksPanel();
  } catch (e) {
    alert('Не удалось удалить: ' + e.message);
  }
}

// ── Finale modal ─────────────────────────────────────────────────────────────
function openFinaleModal() {
  const fin = S.series.finale;
  setVal('fin-episode', fin ? fin.episode : '');
  setVal('fin-description', fin ? (fin.description || '') : '');
  document.getElementById('fin-delete-btn').style.display = fin ? '' : 'none';
  document.getElementById('fin-status').textContent = '';
  // Reset mismatch warning, then check trajectory validation in background
  const warnBox = document.getElementById('fin-mismatch-warning');
  if (warnBox) warnBox.style.display = 'none';
  if (S.seriesId) checkFinaleTrajectory();
  openModal('modal-finale');
}

async function checkFinaleTrajectory() {
  try {
    const data = await api.get(`/api/series/${S.seriesId}/trajectory-validation`);
    const warnBox = document.getElementById('fin-mismatch-warning');
    const detail  = document.getElementById('fin-mismatch-detail');
    if (!warnBox || !detail) return;
    if (!data.has_problem) { warnBox.style.display = 'none'; return; }
    // Build a clear message: where the bad names came from, what they were, what cast has
    const parts = data.mismatches.map(m =>
      `<b>${esc(m.where)}</b>: упоминаются имена <code>${m.unknown_names.map(esc).join(', ')}</code> — их нет в касте.`
    );
    parts.push(
      `Каст сериала: <code>${(data.cast_names || []).map(esc).join(', ') || '—'}</code>`,
      `Это значит что финал/чекпоинт был сгенерирован с неправильными именами. Генератор сценария будет пытаться угадать кого ты имел в виду по роли, но лучше <b>перегенерировать финал</b> кнопкой ниже — он подхватит реальный каст.`
    );
    detail.innerHTML = parts.join('<br>');
    warnBox.style.display = '';
  } catch (e) {
    // silent — non-critical UI
  }
}

async function generateFinaleDraft() {
  const ep = parseInt(val('fin-episode'), 10);
  if (!ep || ep < 1) { showToast('Сначала укажи номер финальной серии'); return; }
  const btn = document.getElementById('fin-gen-btn');
  const status = document.getElementById('fin-status');
  btn.disabled = true; const orig = btn.innerHTML; btn.innerHTML = '<span class="spinner"></span> Пишем финал...';
  status.textContent = '';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title, episodeNum: ep };
    const res = await trackTask(`Финал Эп. ${ep}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/finale/generate`, { episode: ep }, { timeoutMs: 90_000 })
    );
    if (res.description) {
      setVal('fin-description', res.description);
      status.textContent = '✓ Драфт готов — отредактируй или сохрани';
      status.style.color = 'var(--success)';
    }
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

async function saveFinale() {
  const ep = parseInt(val('fin-episode'), 10);
  const description = val('fin-description').trim();
  if (!ep || ep < 1) { showToast('Укажи корректный номер финальной серии'); return; }
  if (!description) { showToast('Опиши финал или сгенерируй драфт'); return; }
  const saveBtn = document.getElementById('fin-save-btn');
  const status = document.getElementById('fin-status');
  saveBtn.disabled = true;
  try {
    const res = await api.put(`/api/series/${S.seriesId}/finale`, { episode: ep, description });
    S.series.finale = res.finale;
    renderLandmarksPanel();
    closeModal('modal-finale');
    showToast(`Финал на эп. ${ep} сохранён`);
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    saveBtn.disabled = false;
  }
}

async function deleteFinale() {
  if (!confirm('Убрать финал? (точку можно будет указать заново)')) return;
  try {
    await api.del(`/api/series/${S.seriesId}/finale`);
    S.series.finale = null;
    renderLandmarksPanel();
    closeModal('modal-finale');
  } catch (e) {
    alert('Не удалось удалить: ' + e.message);
  }
}

// ── Stage 1: Milestones ───────────────────────────────────────────────────────
const MILESTONES = [1, 10, 20, 30, 40, 50, 60, 70]; // legacy single-mode constant
const MILESTONES_REQUIRED = [1, 10, 70];

function renderStage1(el) {
  const s = S.series;
  const ms = s.milestone_synopses || {};
  const milestones = milestoneIndices(s);
  const required = requiredMilestones(s);
  const allDone = required.every(n => ms[String(n)]);
  const total = chunkCount(s);
  const batch = isBatchMode(s);
  const firstN = milestones[0];
  const lastN = milestones[milestones.length - 1];
  const turnN = batch ? epToChunk(s, 10) : 10;
  const genLabel = batch
    ? `⚡ Сгенерировать ${chunkLabel(s, firstN, {short:true})}, ${chunkLabel(s, turnN, {short:true})}, ${chunkLabel(s, lastN, {short:true})}`
    : '⚡ Сгенерировать Эп. 1, 10, 70';
  el.innerHTML = `
    <div class="pipeline-stage">
      <div class="pipeline-stage-header">
        <div class="pipeline-stage-num">1</div>
        <div class="pipeline-stage-title">Контрольные точки${batch ? ` <span style="font-size:0.78rem;color:var(--muted);font-weight:400">· ${total} чанков по ${batchSize(s)} серий</span>` : ''}</div>
      </div>
      <div class="pipeline-stage-body">

        <div>
          <div style="font-size:0.78rem;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.7px">Синопсис</div>
          <div class="pipeline-synopsis-box">${esc(s.synopsis || '—')}</div>
        </div>

        <div class="pipeline-row">
          <button class="btn-idea-gen" id="pl-gen-ms-btn" onclick="plGenerateMilestones()">${genLabel}</button>
        </div>

        <div class="milestones-grid" id="pl-milestones-grid">
          ${milestones.map(n => {
            const isFirst = n === firstN, isLast = n === lastN;
            const tag = isFirst ? 'Пилот' : (isLast ? 'Финал' : '');
            const badgeText = batch ? `${chunkLabel(s, n, {short:true})}` : `Эп. ${n}`;
            return `
            <div class="milestone-card" id="ms-card-${n}">
              <div class="milestone-ep-badge is-milestone">${badgeText}${tag ? `<br><span style="font-size:0.65rem;font-weight:400;color:var(--muted)">${tag}</span>` : ''}</div>
              <textarea class="milestone-textarea" id="ms-ta-${n}" placeholder="Синопсис ${batch ? chunkLabel(s, n).toLowerCase() : 'эпизода ' + n}..." onchange="plSaveMilestone(${n})">${esc(ms[String(n)] || '')}</textarea>
              <div class="milestone-actions">
                <button class="milestone-regen-btn" id="ms-regen-${n}" onclick="plRegenMilestone(${n})" title="Перегенерировать">↻</button>
                ${ms[String(n)] ? `<button class="milestone-clear-btn" onclick="plClearMilestone(${n})" title="Очистить">✕</button>` : ''}
              </div>
            </div>`;
          }).join('')}
        </div>

        <div id="pl-stage2-status" class="pipeline-status"></div>

        <div style="display:flex;justify-content:flex-end">
          <button class="pipeline-confirm-btn" ${allDone ? '' : 'disabled'} id="pl-confirm-ms-btn" onclick="plConfirmMilestones()">→ Подтвердить и перейти к эпизодам</button>
        </div>
      </div>
    </div>
  `;
}

async function plGenerateMilestones() {
  const btn = document.getElementById('pl-gen-ms-btn');
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = ''; status.className = 'pipeline-status';
  try {
    const ms = await api.post(`/api/series/${S.seriesId}/generate-milestones`, {});
    S.series = await api.get(`/api/series/${S.seriesId}`);
    renderStage2(document.getElementById('pipeline-section'));
    const labelList = requiredMilestones(S.series).map(n => chunkLabel(S.series, n, {short:true})).join(', ');
    status.textContent = `✓ ${labelList} сгенерированы. Остальные точки можно заполнить вручную или через ↻`; status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
    const labelList = requiredMilestones(S.series).map(n => chunkLabel(S.series, n, {short:true})).join(', ');
    btn.disabled = false; btn.innerHTML = `⚡ Сгенерировать ${labelList}`;
  }
}

async function plSaveMilestone(n) {
  const ta = document.getElementById(`ms-ta-${n}`);
  if (!ta) return;
  await api.put(`/api/series/${S.seriesId}/milestones/${n}`, { synopsis: ta.value });
  S.series.milestone_synopses = S.series.milestone_synopses || {};
  S.series.milestone_synopses[String(n)] = ta.value;
  const confirmBtn = document.getElementById('pl-confirm-ms-btn');
  if (confirmBtn) confirmBtn.disabled = !requiredMilestones(S.series).every(m => (S.series.milestone_synopses[String(m)] || '').trim());
}

async function plClearMilestone(n) {
  await api.put(`/api/series/${S.seriesId}/milestones/${n}`, { synopsis: '' });
  S.series.milestone_synopses = S.series.milestone_synopses || {};
  S.series.milestone_synopses[String(n)] = '';
  const ta = document.getElementById(`ms-ta-${n}`);
  if (ta) ta.value = '';
  // Re-render to remove the clear button
  renderStage2(document.getElementById('pipeline-section'));
}

async function plRegenMilestone(n) {
  const btn = document.getElementById(`ms-regen-${n}`);
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.textContent = '...';
  try {
    await plSaveMilestone(n);
    const res = await api.post(`/api/series/${S.seriesId}/milestones/${n}/regenerate`, {});
    bumpAssetVersion();
    const ta = document.getElementById(`ms-ta-${n}`);
    if (ta) ta.value = res.synopsis;
    S.series.milestone_synopses = S.series.milestone_synopses || {};
    S.series.milestone_synopses[String(n)] = res.synopsis;
    status.textContent = `✓ Эп. ${n} перегенерирован`; status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
  } finally {
    btn.disabled = false; btn.textContent = '↻';
  }
}

async function plConfirmMilestones() {
  for (const n of milestoneIndices(S.series)) {
    const ta = document.getElementById(`ms-ta-${n}`);
    if (ta && ta.value.trim()) await plSaveMilestone(n);
  }
  const status = document.getElementById('pl-stage2-status');
  try {
    S.series = await api.post(`/api/series/${S.seriesId}/confirm-milestones`, {});
    // Characters/locations are NO LONGER auto-extracted from synopsis here —
    // they'd often include people/places that never make it into the actual
    // script. The episode view's "Извлечь персонажей и локации" button reads
    // from the script instead, which is the single source of truth.
    renderCharactersList();
    renderLocationsList();
    renderItemsList();
    renderPipeline();
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    renderEpisodesList();
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
  }
}

// ── Stage 2: Episode synopses 1-10 (or first N chunks in batch mode) ────────
function renderStage2(el) {
  const s = S.series;
  const slots = stage2ChunkRange(s);  // e.g. [1,2] in batch_size=5, or 1..10 in single mode
  const lastSlot = slots[slots.length - 1];
  const eps = S.episodes.filter(e => e.number <= lastSlot).sort((a,b) => a.number - b.number);
  const allHaveSynopsis = slots.every(n => eps.find(e => e.number === n)?.synopsis);
  const batch = isBatchMode(s);
  const subEpRange = batch ? `1–${batchSize(s) * lastSlot}` : '1–10';
  const stageTitle = batch
    ? `Синопсисы чанков 1–${lastSlot} (серии ${subEpRange})`
    : 'Синопсисы эпизодов 1–10';
  const genBtnLabel = batch
    ? `⚡ Сгенерировать синопсисы чанков 1–${lastSlot}`
    : '⚡ Сгенерировать синопсисы 1–10';

  el.innerHTML = `
    <div class="pipeline-stage">
      <div class="pipeline-stage-header">
        <div class="pipeline-stage-num">2</div>
        <div class="pipeline-stage-title">${stageTitle}</div>
        ${allHaveSynopsis ? '<span style="color:var(--success);font-size:0.82rem">✓ Готово — можно писать сценарии</span>' : ''}
      </div>
      <div class="pipeline-stage-body">
        <div class="pipeline-row">
          <button class="btn-idea-gen" id="pl-gen-ep-syn-btn" onclick="plGenerateEpSynopses()">${genBtnLabel}</button>
          <span style="font-size:0.8rem;color:var(--muted)">Уже написанные не будут перезаписаны</span>
        </div>
        <div class="ep-synopsis-rows">
          ${slots.map(n => {
            const ep = eps.find(e => e.number === n);
            const syn = ep?.synopsis || '';
            const badge = batch ? chunkLabel(s, n, {short:true}) : `Эп. ${n}`;
            return `<div class="ep-synopsis-row">
              <div class="ep-synopsis-badge">${badge}</div>
              <div class="ep-synopsis-text ${syn ? '' : 'empty'}">${syn ? esc(syn) : 'Нет синопсиса'}</div>
            </div>`;
          }).join('')}
        </div>
        <div id="pl-stage2-status" class="pipeline-status"></div>
      </div>
    </div>
  `;
}

async function plExtractFromStory() {
  const btn = document.getElementById('pl-extract-btn');
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Извлекаем...';
  status.textContent = ''; status.className = 'pipeline-status';
  try {
    const res = await api.post(`/api/series/${S.seriesId}/extract-from-story`, {});
    S.series = res.series;
    renderCharactersList();
    renderLocationsList();
    renderItemsList();
    const chars = res.added_characters.join(', ');
    const locs  = res.added_locations.join(', ');
    status.textContent = `✓ Добавлено: персонажи [${chars || 'нет новых'}], локации [${locs || 'нет новых'}]`;
    status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
  } finally {
    btn.disabled = false; btn.innerHTML = '🤖 Извлечь персонажей и локации из сюжета';
  }
}

async function plGenerateEpSynopses() {
  const btn = document.getElementById('pl-gen-ep-syn-btn');
  const status = document.getElementById('pl-stage2-status');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = ''; status.className = 'pipeline-status';
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title };
    await trackTask('Синопсисы серий 1–10', ctx, () =>
      api.post(`/api/series/${S.seriesId}/generate-episode-synopses`, {})
    );
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    renderStage2(document.getElementById('pipeline-section'));
    renderEpisodesList();
    status.textContent = isBatchMode(S.series) ? '✓ Синопсисы чанков готовы. Открывай чанк и генерируй сценарий!' : '✓ Синопсисы готовы. Открывай эпизод и генерируй сценарий!';
    status.className = 'pipeline-status ok';
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message; status.className = 'pipeline-status err';
    const slots = stage2ChunkRange(S.series);
    btn.disabled = false; btn.innerHTML = isBatchMode(S.series) ? `⚡ Сгенерировать синопсисы чанков 1–${slots[slots.length-1]}` : '⚡ Сгенерировать синопсисы 1–10';
  }
}

