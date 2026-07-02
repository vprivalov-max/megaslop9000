// ── Series Canon (story bible / logic state) ─────────────────────────────────
async function loadCanonSummary() {
  const el = document.getElementById('canon-summary-stats');
  if (!el) return;
  try {
    const r = await fetch(`/api/series/${S.seriesId}/canon`);
    if (!r.ok) { el.textContent = 'нет данных'; return; }
    const c = await r.json();
    const wc = c.world_clock || {};
    const lastAudit = (c.audit_log || []).slice(-1)[0];
    const auditTxt = lastAudit
      ? (lastAudit.passes ? '✓' : `⚠ ${lastAudit.violations.length}`) + ` (ep${lastAudit.ep}, retries=${lastAudit.retries})`
      : '—';
    el.innerHTML =
      `день ${wc.current_day || 0} · эп ${wc.last_episode || 0} · ` +
      `фактов ${c.facts?.length || 0} · ` +
      `тредов ${(c.open_threads || []).filter(t => t.status !== 'closed').length} открыто · ` +
      `аудит: ${auditTxt}`;
  } catch (e) {
    el.textContent = 'ошибка загрузки';
  }
}

async function openCanonViewer() {
  let modal = document.getElementById('modal-canon');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'modal-canon';
    modal.className = 'modal';
    modal.innerHTML = `
      <div class="modal-content" style="max-width:760px;max-height:85vh;overflow:auto">
        <div class="modal-header">
          <h2>📚 Канон сериала</h2>
          <button class="btn-icon" onclick="closeModal('modal-canon')">✕</button>
        </div>
        <div id="canon-body" style="padding:14px"></div>
        <div class="modal-footer" style="padding:10px 14px;border-top:1px solid var(--border)">
          <button class="btn btn-ghost" onclick="rebuildCanon()">⟲ Пересобрать из всех серий</button>
          <button class="btn" onclick="closeModal('modal-canon')">Закрыть</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  openModal('modal-canon');
  const body = document.getElementById('canon-body');
  body.innerHTML = 'загрузка…';
  try {
    const c = await (await fetch(`/api/series/${S.seriesId}/canon`)).json();
    body.innerHTML = renderCanonBody(c);
  } catch (e) {
    body.innerHTML = 'ошибка: ' + e.message;
  }
}

function renderCanonBody(c) {
  const wc = c.world_clock || {};
  const tl = (c.timeline || []).map(t =>
    `<li><strong>Ep ${t.ep}</strong> (день ${t.day}): ${(t.events || []).map(esc).join('; ') || '—'}</li>`
  ).join('');
  const facts = (c.facts || []).map(f =>
    `<li><code>${f.id}</code> [ep ${f.ep}] ${esc(f.fact)}${f.supersedes ? ` <span style="color:var(--muted)">(сменяет ${f.supersedes})</span>` : ''}</li>`
  ).join('');
  const threads = (c.open_threads || []).map(t =>
    `<li><code>${t.id}</code> ep${t.opened_ep} — ${esc(t.question)} <span style="color:${t.status==='closed' ? 'var(--success,#4ade80)' : 'var(--warning,#fbbf24)'}">[${t.status}${t.resolved_ep ? ` ep${t.resolved_ep}` : ''}]</span></li>`
  ).join('');
  const cs = Object.entries(c.character_state || {}).map(([name, st]) => {
    const knows = (st.knows || []).slice(-6).map(esc).join(' · ') || '—';
    const phys = Object.entries(st.physical || {}).map(([k,v]) => `${k}=${v}`).join(', ') || '—';
    return `<li><strong>${esc(name)}</strong> — знает: ${knows}<br><span style="color:var(--muted)">состояние: ${esc(phys)} · локация: ${esc(st.location || '—')}</span></li>`;
  }).join('');
  const audit = (c.audit_log || []).slice(-10).reverse().map(a => {
    const badge = a.passes ? '<span style="color:var(--success,#4ade80)">✓</span>' : `<span style="color:var(--danger,#f87171)">⚠ ${a.violations.length}</span>`;
    const vs = a.violations.map(v => `<div style="margin-left:14px;font-size:0.78rem;color:var(--muted)">[${v.severity}] ${esc(v.type || '')}: ${esc(v.explanation || '')}</div>`).join('');
    return `<li>Ep ${a.ep} ${badge} retries=${a.retries}${vs}</li>`;
  }).join('');
  return `
    <div style="margin-bottom:12px"><strong>Мировые часы:</strong> день ${wc.current_day || 0} · последняя серия: ep ${wc.last_episode || 0}</div>
    <h4>📅 Таймлайн</h4><ul style="font-size:0.85rem">${tl || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>🔒 Канонические факты</h4><ul style="font-size:0.85rem">${facts || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>👥 Состояние персонажей</h4><ul style="font-size:0.85rem">${cs || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>🧵 Открытые/закрытые треды</h4><ul style="font-size:0.85rem">${threads || '<li style="color:var(--muted)">пусто</li>'}</ul>
    <h4>🔍 Журнал аудита (последние 10)</h4><ul style="font-size:0.85rem">${audit || '<li style="color:var(--muted)">пусто</li>'}</ul>
  `;
}

async function rebuildCanon() {
  if (!confirm('Перестроить канон из всех существующих сценариев? Текущий канон будет перезаписан.')) return;
  const body = document.getElementById('canon-body');
  body.innerHTML = 'пересобираю канон…';
  try {
    const r = await fetch(`/api/series/${S.seriesId}/canon/rebuild`, {method: 'POST'});
    const data = await r.json();
    if (data.error) { body.innerHTML = 'ошибка: ' + data.error; return; }
    body.innerHTML = renderCanonBody(data.canon);
    loadCanonSummary();
  } catch (e) {
    body.innerHTML = 'ошибка: ' + e.message;
  }
}

// ── Bible editor ──────────────────────────────────────────────────────────────
function openBibleEditor() {
  const s = S.series;
  setVal('bible-title', s.title);
  setVal('bible-genre', s.genre);
  setVal('bible-tone', s.tone);
  setVal('bible-audience', s.target_audience);
  setVal('bible-world', s.world_description);
  setVal('bible-visual-style', s.visual_style || '');
  setVal('bible-max-chars-per-scene', s.max_main_chars_per_scene || '');
  setVal('bible-target-duration-sec', s.target_duration_sec || '');
  setVal('bible-format-mode', s.format_mode || 'short_drama');
  openModal('modal-bible');
}

async function saveBible() {
  const maxCharsRaw = val('bible-max-chars-per-scene');
  const maxChars = maxCharsRaw ? Math.max(1, Math.min(6, parseInt(maxCharsRaw, 10))) : null;
  const targetDurRaw = val('bible-target-duration-sec');
  const targetDur = targetDurRaw ? Math.max(30, Math.min(240, parseInt(targetDurRaw, 10))) : null;
  const fmRaw = (val('bible-format-mode') || 'short_drama').trim();
  const fm = (fmRaw === 'instagram_series') ? 'instagram_series' : 'short_drama';
  const data = {
    title: val('bible-title'), genre: val('bible-genre'),
    tone: val('bible-tone'), target_audience: val('bible-audience'),
    world_description: val('bible-world'),
    visual_style: val('bible-visual-style'),
    max_main_chars_per_scene: maxChars,
    target_duration_sec: targetDur,
    format_mode: fm,
  };
  S.series = await api.put(`/api/series/${S.seriesId}`, data);
  closeModal('modal-bible');
  renderSeriesView();
}

// ── Characters ────────────────────────────────────────────────────────────────
// ── Quick-add character (from episode sidebar) ────────────────────────────
const QC = { file: null, generatedRel: null };

function openQuickAddChar() {
  if (!S.seriesId) return;
  QC.file = null;
  QC.generatedRel = null;
  setVal('qc-name', '');
  setVal('qc-gender', 'male');
  setVal('qc-description', '');
  document.getElementById('qc-gen-block')?.classList.add('hidden');
  document.getElementById('qc-gen-status').textContent = '';
  const prev = document.getElementById('qc-preview');
  if (prev) { prev.src = ''; prev.classList.add('hidden'); }
  const empty = document.querySelector('#qc-drop .qc-drop-empty');
  if (empty) empty.classList.remove('hidden');
  const f = document.getElementById('qc-file');
  if (f) f.value = '';
  openModal('modal-quickchar');
}

function qcHandleDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('hover');
  const f = e.dataTransfer.files?.[0];
  if (f) qcHandleFile(f);
}

function qcHandleFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    showToast('Можно перетащить только картинку');
    return;
  }
  QC.file = file;
  QC.generatedRel = null;
  const prev = document.getElementById('qc-preview');
  const empty = document.querySelector('#qc-drop .qc-drop-empty');
  const reader = new FileReader();
  reader.onload = (e) => {
    prev.src = e.target.result;
    prev.classList.remove('hidden');
    empty?.classList.add('hidden');
  };
  reader.readAsDataURL(file);
}

function qcToggleGen() {
  document.getElementById('qc-gen-block')?.classList.toggle('hidden');
}

async function qcGenerate() {
  const name = (val('qc-name') || '').trim();
  const description = (val('qc-description') || '').trim();
  if (!name) return alert('Сначала введи имя');
  if (!description) return alert('Опиши персонажа');
  const btn = document.getElementById('qc-gen-btn');
  const st  = document.getElementById('qc-gen-status');
  btn.disabled = true; btn.innerHTML = '⏳ генерирую...';
  st.textContent = '';
  try {
    // Create char first (so /generate-image can attach output to it)
    const created = await api.post(`/api/series/${S.seriesId}/characters`, {
      name, description, appearance: description, gender: val('qc-gender') || 'male',
    });
    const charId = created.character?.id || created.id;
    if (!charId) throw new Error('cannot create char');
    QC._tempCharId = charId;
    const r = await api.post(`/api/series/${S.seriesId}/characters/${charId}/generate-image`, {});
    bumpAssetVersion();
    if (!r.ready) throw new Error(r.error || 'no image');
    QC.generatedRel = r.url || '';
    QC.file = null;
    const prev = document.getElementById('qc-preview');
    const empty = document.querySelector('#qc-drop .qc-drop-empty');
    if (prev) { prev.src = QC.generatedRel; prev.classList.remove('hidden'); }
    if (empty) empty.classList.add('hidden');
    st.textContent = '✓ готово · нажми Сохранить';
    showToast('🪄 Фото сгенерировано');
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
  } finally {
    btn.disabled = false; btn.innerHTML = '🪄 Сгенерировать фото';
  }
}

async function qcSave() {
  const name = (val('qc-name') || '').trim();
  if (!name) return alert('Введи имя персонажа');
  const btn = document.getElementById('qc-save-btn');
  const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '⏳';
  try {
    let charId = QC._tempCharId;  // set if user already generated photo
    if (!charId) {
      // Create char now
      const created = await api.post(`/api/series/${S.seriesId}/characters`, {
        name,
        description: (val('qc-description') || '').trim(),
        appearance:  (val('qc-description') || '').trim(),
        gender: val('qc-gender') || 'male',
      });
      charId = created.character?.id || created.id;
      if (!charId) throw new Error('cannot create char');
    }
    // Upload dropped file if any
    if (QC.file) {
      const fd = new FormData();
      fd.append('photo', QC.file);
      const resp = await fetch(`/api/series/${S.seriesId}/characters/${charId}/upload-photo`, {
        method: 'POST', body: fd,
      });
      bumpAssetVersion();
      if (!resp.ok) throw new Error(await resp.text());
    }
    // Refresh series view
    S.series = await api.get(`/api/series/${S.seriesId}`);
    closeModal('modal-quickchar');
    QC._tempCharId = null;
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof renderEpisodeView === 'function') renderEpisodeView();
    if (S.episodeNum) { try { await loadEpisodeView(); } catch {} }
    showToast('✓ Персонаж добавлен');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    btn.disabled = false; btn.innerHTML = old;
  }
}

// ── Quick-add location (from episode sidebar) ────────────────────────────
const QL = { file: null, generatedRel: null };

function openQuickAddLoc() {
  if (!S.seriesId) return;
  QL.file = null;
  QL.generatedRel = null;
  QL._tempLocId = null;
  setVal('ql-name', '');
  setVal('ql-description', '');
  document.getElementById('ql-gen-block')?.classList.add('hidden');
  document.getElementById('ql-gen-status').textContent = '';
  const prev = document.getElementById('ql-preview');
  if (prev) { prev.src = ''; prev.classList.add('hidden'); }
  const empty = document.querySelector('#ql-drop .qc-drop-empty');
  if (empty) empty.classList.remove('hidden');
  const f = document.getElementById('ql-file');
  if (f) f.value = '';
  openModal('modal-quickloc');
}

function qlHandleDrop(e) {
  e.preventDefault();
  e.currentTarget.classList.remove('hover');
  const f = e.dataTransfer.files?.[0];
  if (f) qlHandleFile(f);
}

function qlHandleFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    showToast('Можно перетащить только картинку');
    return;
  }
  QL.file = file;
  QL.generatedRel = null;
  const prev = document.getElementById('ql-preview');
  const empty = document.querySelector('#ql-drop .qc-drop-empty');
  const reader = new FileReader();
  reader.onload = (e) => {
    prev.src = e.target.result;
    prev.classList.remove('hidden');
    empty?.classList.add('hidden');
  };
  reader.readAsDataURL(file);
}

function qlToggleGen() {
  document.getElementById('ql-gen-block')?.classList.toggle('hidden');
}

async function qlGenerate() {
  const name = (val('ql-name') || '').trim();
  const description = (val('ql-description') || '').trim();
  if (!name) return alert('Сначала введи название');
  if (!description) return alert('Опиши локацию');
  const btn = document.getElementById('ql-gen-btn');
  const st  = document.getElementById('ql-gen-status');
  btn.disabled = true; btn.innerHTML = '⏳ генерирую...';
  st.textContent = '';
  try {
    const created = await api.post(`/api/series/${S.seriesId}/locations`, {
      name, description,
    });
    const locId = created.location?.id || created.id;
    if (!locId) throw new Error('cannot create location');
    QL._tempLocId = locId;
    const r = await api.post(`/api/series/${S.seriesId}/locations/${locId}/generate-image`, {});
    bumpAssetVersion();
    if (!r.ready) throw new Error(r.error || 'no image');
    QL.generatedRel = r.url || '';
    QL.file = null;
    const prev = document.getElementById('ql-preview');
    const empty = document.querySelector('#ql-drop .qc-drop-empty');
    if (prev) { prev.src = QL.generatedRel; prev.classList.remove('hidden'); }
    if (empty) empty.classList.add('hidden');
    st.textContent = '✓ готово · нажми Сохранить';
    showToast('🪄 Фото сгенерировано');
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
  } finally {
    btn.disabled = false; btn.innerHTML = '🪄 Сгенерировать фото';
  }
}

async function qlSave() {
  const name = (val('ql-name') || '').trim();
  if (!name) return alert('Введи название локации');
  const btn = document.getElementById('ql-save-btn');
  const old = btn.innerHTML;
  btn.disabled = true; btn.innerHTML = '⏳';
  try {
    let locId = QL._tempLocId;
    if (!locId) {
      const created = await api.post(`/api/series/${S.seriesId}/locations`, {
        name, description: (val('ql-description') || '').trim(),
      });
      locId = created.location?.id || created.id;
      if (!locId) throw new Error('cannot create location');
    }
    if (QL.file) {
      const fd = new FormData();
      fd.append('photo', QL.file);
      const resp = await fetch(`/api/series/${S.seriesId}/locations/${locId}/upload-photo`, {
        method: 'POST', body: fd,
      });
      bumpAssetVersion();
      if (!resp.ok) throw new Error(await resp.text());
    }
    S.series = await api.get(`/api/series/${S.seriesId}`);
    closeModal('modal-quickloc');
    QL._tempLocId = null;
    if (typeof renderLocationsList === 'function') renderLocationsList();
    if (S.episodeNum) { try { await loadEpisodeView(); } catch {} }
    showToast('✓ Локация добавлена');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    btn.disabled = false; btn.innerHTML = old;
  }
}

function openAddCharacter() {
  S.editingCharId = null;
  document.getElementById('char-modal-title').textContent = 'Новый персонаж';
  clearFields(['char-name','char-description','char-appearance','char-voice-id']);
  setVal('char-gender', 'female');
  openModal('modal-character');
}

function openEditCharacter(charId) {
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  S.editingCharId = charId;
  document.getElementById('char-modal-title').textContent = 'Редактировать персонажа';
  setVal('char-name', c.name);
  setVal('char-description', c.description);
  setVal('char-appearance', c.appearance);
  setVal('char-gender', c.gender);
  setVal('char-voice-id', c.voice_id);
  openModal('modal-character');
}

async function saveCharacter() {
  const data = {
    name: val('char-name'), description: val('char-description'),
    appearance: val('char-appearance'), gender: val('char-gender'),
    voice_id: val('char-voice-id'),
  };
  if (!data.name) return alert('Введи имя персонажа');

  if (S.editingCharId) {
    await api.put(`/api/series/${S.seriesId}/characters/${S.editingCharId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/characters`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-character');
  renderCharactersList();
  renderEpisodesList();
}

async function deleteCharacter(charId) {
  if (!confirm('Удалить персонажа?')) return;
  await api.del(`/api/series/${S.seriesId}/characters/${charId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
  renderCharactersList();
}

