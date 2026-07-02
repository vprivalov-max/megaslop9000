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

// ── Locations ─────────────────────────────────────────────────────────────────
function openAddLocation() {
  S.editingLocId = null;
  document.getElementById('loc-modal-title').textContent = 'Новая локация';
  clearFields(['loc-name','loc-description']);
  openModal('modal-location');
}

function openEditLocation(locId) {
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  S.editingLocId = locId;
  document.getElementById('loc-modal-title').textContent = 'Редактировать локацию';
  setVal('loc-name', l.name);
  setVal('loc-description', l.description);
  openModal('modal-location');
}

async function saveLocation() {
  const data = { name: val('loc-name'), description: val('loc-description') };
  if (!data.name) return alert('Введи название локации');
  if (S.editingLocId) {
    await api.put(`/api/series/${S.seriesId}/locations/${S.editingLocId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/locations`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-location');
  renderLocationsList();
}

async function deleteLocation(locId) {
  if (!confirm('Удалить локацию?')) return;
  await api.del(`/api/series/${S.seriesId}/locations/${locId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  renderLocationsList();
}

let currentLocId = null;

function openLocAssets(locId) {
  currentLocId = locId;
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  document.getElementById('loc-assets-title').textContent = `Фото: ${l.name}`;
  renderLocAssetsGrid(l);
  setVal('regen-loc-wishes', l.image_constraints || '');
  const inp = document.getElementById('loc-asset-file-input');
  inp.onchange = () => uploadLocationRefs(locId, inp.files);
  openModal('modal-loc-assets');
}

async function regenerateLocation() {
  if (!currentLocId) return;
  const wishes = (val('regen-loc-wishes') || '').trim();
  const btn = document.getElementById('regen-loc-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ генерирую...'; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/locations/${currentLocId}/regenerate`,
      { wishes }
    );
    if (!r.ready) throw new Error(r.error || 'unknown');
    bumpAssetVersion();   // refresh ALL assetUrl()-rendered images
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const l = (S.series.locations || []).find(x => x.id === currentLocId);
    if (l) renderLocAssetsGrid(l);
    if (typeof renderLocationsList === 'function') renderLocationsList();
    if (typeof renderEpLocations === 'function') renderEpLocations();  // episode-view sidebar
    showToast('✓ Локация перегенерирована');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}

function renderLocAssetsGrid(loc) {
  const grid = document.getElementById('loc-assets-grid');
  const refs = loc.ref_images || [];
  const v = loc.image_version ? `?v=${loc.image_version}` : '';
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}${v}`;
    return `
      <div class="photo-thumb-wrap" onclick="openLocLightbox('${loc.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteLocRef('${loc.id}','${fname}')">✕</button>
      </div>`;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

// Location-photo lightbox: image + regenerate panel (mirrors openCharLightbox).
function openLocLightbox(locId, url) {
  closeLightbox();
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  currentLocId = locId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать локацию</h3>
        <div class="hint">
          Старое фото удалится, новое сгенерируется с учётом пожеланий.
          Пожелания сохранятся в локации и будут применяться при всех будущих генерациях.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-loc-wishes" rows="5"
            placeholder="Например:&#10;«больше окон»&#10;«утренний свет»&#10;«без людей в кадре»&#10;«теплая палитра»">${esc(l.image_constraints || '')}</textarea>
        </div>
        <div id="lb-regen-loc-status" class="lb-status"></div>
        <button id="lb-regen-loc-btn" class="btn-regen" onclick="regenerateLocationFromLightbox()">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

async function regenerateLocationFromLightbox() {
  if (!currentLocId) return;
  const wishes = (document.getElementById('lb-regen-loc-wishes').value || '').trim();
  const status = document.getElementById('lb-regen-loc-status');
  const btn = document.getElementById('lb-regen-loc-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  status.textContent = 'Перегенерируем фото локации (~15-30 сек)...';
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/locations/${currentLocId}/regenerate`,
      { wishes }
    );
    if (!res.ready) throw new Error(res.error || 'unknown');
    bumpAssetVersion();   // refresh ALL assetUrl()-rendered images
    status.className = 'lb-status ok';
    status.textContent = '✓ Локация обновлена';
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const l = (S.series.locations || []).find(x => x.id === currentLocId);
    if (l) renderLocAssetsGrid(l);
    renderLocationsList();
    if (typeof renderEpLocations === 'function') renderEpLocations();  // episode-view sidebar
    setTimeout(() => closeLightbox(), 800);
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + (e.message || e);
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

async function uploadLocationRefs(locId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/location/${locId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const l = (S.series.locations || []).find(x => x.id === locId);
  renderLocAssetsGrid(l);
  renderLocationsList();
}

async function deleteLocRef(locId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/location/${locId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const l = (S.series.locations || []).find(x => x.id === locId);
  renderLocAssetsGrid(l);
  renderLocationsList();
}

// ── Building facades ──────────────────────────────────────────────────────────
// Groups interior locations by parent building (e.g. all rooms of a mansion),
// generates one exterior facade image + 4s Seedance video per building. Used as
// establishing-shot fodder. Workflow:
//   1. openFacadesPanel() → loads existing facades from backend, renders the
//      hierarchy. Buttons: «Сгруппировать», «Сгенерировать все», «Открыть папку».
//   2. facadesGroup() → POST /facades/group, Claude clusters the loc roster,
//      preview UI shows each proposed building with its member interiors.
//   3. User clicks «Сгенерировать все» → POST /facades/generate, worker runs
//      in background. Polling refreshes the card grid as facades complete.
const FACADES_STATE = { groupsPreview: null, polling: null };

async function openFacadesPanel() {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  openModal('modal-facades');
  await facadesRefresh();
}

async function facadesRefresh() {
  try {
    const r = await api.get(`/api/series/${S.seriesId}/facades`);
    _renderFacadesList(r.facades || [], r.status || {});
    const hint = document.getElementById('facades-folder-hint');
    if (hint && r.folder) {
      hint.textContent = '📂 ' + r.folder;
      hint.style.display = '';
    }
    // Auto-poll while worker is running so progress shows up live.
    if (r.status?.running) {
      _facadesStartPoll();
    } else {
      _facadesStopPoll();
    }
  } catch (e) {
    showToast('Ошибка загрузки: ' + (e?.message || e));
  }
}

function _facadesStartPoll() {
  if (FACADES_STATE.polling) return;
  FACADES_STATE.polling = setInterval(() => facadesRefresh(), 6000);
}
function _facadesStopPoll() {
  if (!FACADES_STATE.polling) return;
  clearInterval(FACADES_STATE.polling);
  FACADES_STATE.polling = null;
}

function _renderFacadesList(facades, status) {
  const host = document.getElementById('facades-list');
  const stEl = document.getElementById('facades-status');
  const genBtn = document.getElementById('facades-generate-btn');
  if (!host) return;
  // Locations that aren't in ANY existing facade — surface them so the user
  // can spawn a facade per-location without «сгенерировать всё».
  const allLocs = (S.series?.locations || []);
  const usedLocIds = new Set();
  for (const f of facades) for (const id of (f.member_loc_ids || [])) usedLocIds.add(id);
  const ungroupedLocs = allLocs.filter(l => !usedLocIds.has(l.id));
  // Status banner
  if (status?.running) {
    stEl.style.display = '';
    if (status.phase === 'grouping') {
      // Pre-render phase: Claude is clustering locations into building groups.
      // No total/done counter is meaningful yet — show a generic message so
      // the user knows something is happening (the modal would otherwise
      // render «ещё нет фасадов» for 3-5s while Claude responds).
      stEl.innerHTML = `<span class="spinner"></span> 🤖 Авто-группирую локации по зданиям…`;
    } else {
      stEl.innerHTML = `<span class="spinner"></span> Генерирую: ${status.done || 0}/${status.total || 0}${status.current ? ' · ' + esc(status.current) : ''}`;
    }
  } else if (status?.errors?.length) {
    stEl.style.display = '';
    stEl.innerHTML = `⚠ Завершено с ошибками (${status.errors.length}): ${status.errors.slice(0, 2).map(e => esc(e.building || '') + ' — ' + esc(e.error || '')).join('; ')}`;
  } else {
    stEl.style.display = 'none';
  }
  // Toggle generate button — disabled while running OR while no preview groups
  // are loaded AND no facades exist yet (need to group first time).
  if (genBtn) {
    const hasPreview = !!FACADES_STATE.groupsPreview;
    genBtn.disabled = !!status?.running || !hasPreview;
    genBtn.title = status?.running ? 'Генерация уже идёт' : (hasPreview ? '' : 'Сначала нажми «Сгруппировать локации»');
  }
  // Build name → loc lookup for hierarchy display
  const locById = {};
  for (const l of (S.series?.locations || [])) locById[l.id] = l;
  // Build the ungrouped-locations block (always shown if there are any —
  // gives per-location «Сгенерить фасад» so user doesn't have to do all).
  const ungroupedHtml = ungroupedLocs.length ? `
    <div class="facade-ungrouped-block">
      <div class="facade-ungrouped-title">🏗 Локации без фасада (${ungroupedLocs.length})</div>
      <div class="facade-ungrouped-grid">
        ${ungroupedLocs.map(l => {
          const ref = (l.ref_images || [])[0];
          const src = ref ? `/assets/${S.seriesId}/${ref}?v=${Date.now()}` : '';
          return `
            <div class="facade-ungrouped-card" title="${esc(l.description || '')}">
              ${src ? `<img src="${esc(src)}" alt="">` : `<div class="facade-ungrouped-stub">${esc((l.name||'?')[0])}</div>`}
              <div class="facade-ungrouped-name">${esc(l.name)}</div>
              <button class="btn-ghost btn-sm" onclick="facadesGenerateForLoc('${esc(l.id)}', this)" style="margin-top:6px;font-size:0.72rem;padding:3px 6px">🎬 Сгенерить фасад</button>
            </div>`;
        }).join('')}
      </div>
    </div>` : '';
  if (!facades.length) {
    // While auto-grouping is in flight, suppress the «ещё нет» CTA — the
    // banner above already says «Авто-группирую…» and the locations list
    // will populate within seconds. Showing both would be contradictory.
    const emptyCta = status?.running
      ? '<div style="font-size:0.86rem;color:var(--muted);padding:24px;text-align:center">⏳ Группировка идёт автоматически — фасады появятся через несколько секунд.</div>'
      : '<div style="font-size:0.86rem;color:var(--muted);padding:24px;text-align:center">Ещё нет сгенерированных фасадов. Нажми «🤖 Сгруппировать локации» чтобы Claude разбил по зданиям ИЛИ выбери конкретную локацию выше для одного фасада.</div>';
    host.innerHTML = ungroupedHtml + emptyCta;
    return;
  }
  host.innerHTML = ungroupedHtml + facades.map(f => {
    const imgSrc = f.image_path ? `/assets/${S.seriesId}/${f.image_path}?v=${Date.now()}` : '';
    const vidSrc = f.video_path ? `/assets/${S.seriesId}/${f.video_path}?v=${Date.now()}` : '';
    const status = f.status || 'unknown';
    const statusColor = status === 'ready' ? 'var(--success)'
                      : status === 'image_only' ? 'var(--warning)'
                      : status === 'failed' ? 'var(--danger)'
                      : 'var(--muted)';
    const statusLabel = status === 'ready' ? '✓ Готов'
                      : status === 'image_only' ? '⚠ Только картинка (видео упало)'
                      : status === 'failed' ? '✗ ' + (f.error || 'Ошибка')
                      : status === 'generating' ? '⏳ Генерирую…'
                      : status;
    const members = (f.member_loc_ids || []).map(id => locById[id]).filter(Boolean);
    const memberCards = members.map(m => {
      const mImg = (m.ref_images || [])[0];
      const mSrc = mImg ? `/assets/${S.seriesId}/${mImg}?v=${Date.now()}` : '';
      return `
        <div class="facade-child" title="${esc(m.name)}">
          ${mSrc ? `<img src="${esc(mSrc)}" alt="">` : `<div class="facade-child-stub">${esc((m.name||'?')[0])}</div>`}
          <div class="facade-child-name">${esc(m.name)}</div>
        </div>`;
    }).join('') || '<div style="font-size:0.74rem;color:var(--muted);font-style:italic">(нет привязанных интерьеров)</div>';
    return `
      <div class="facade-card">
        <div class="facade-card-main">
          <div class="facade-media">
            ${vidSrc
              ? `<video src="${esc(vidSrc)}" muted loop autoplay playsinline></video>`
              : imgSrc ? `<img src="${esc(imgSrc)}" alt="">`
                       : `<div class="facade-stub">${status === 'generating' ? '<span class="spinner"></span>' : '🏛'}</div>`}
          </div>
          <div class="facade-info">
            <div class="facade-name">${esc(f.building_name || 'Untitled')}</div>
            <div class="facade-status" style="color:${statusColor}">${esc(statusLabel)}</div>
            <div class="facade-desc">${esc((f.facade_description || '').slice(0, 200))}</div>
            <div class="facade-actions">
              <button class="btn-ghost btn-sm" onclick="facadesRegenerate('${esc(f.id)}')">🔄 Перегенерить</button>
              <button class="btn-ghost btn-sm" onclick="facadesDelete('${esc(f.id)}')" style="color:var(--danger)">✕ Удалить</button>
              ${vidSrc ? `<a class="btn-ghost btn-sm" href="${esc(vidSrc)}" download>⬇ Скачать видео</a>` : ''}
            </div>
          </div>
        </div>
        <div class="facade-children">
          <div class="facade-children-label">Интерьеры внутри:</div>
          <div class="facade-children-grid">${memberCards}</div>
        </div>
      </div>`;
  }).join('');
}

async function facadesGroup() {
  const btn = document.getElementById('facades-group-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Анализирую…';
  try {
    const r = await api.post(`/api/series/${S.seriesId}/facades/group`, {});
    if (r.error) throw new Error(r.error);
    FACADES_STATE.groupsPreview = r.groups || [];
    _renderGroupsPreview(FACADES_STATE.groupsPreview);
    // Refresh button state
    await facadesRefresh();
  } catch (e) {
    alert('Группировка не удалась: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

function _renderGroupsPreview(groups) {
  const host = document.getElementById('facades-groups-preview');
  if (!host) return;
  if (!groups?.length) { host.style.display = 'none'; return; }
  host.style.display = '';
  const buildings = groups.filter(g => g.type === 'building');
  const exteriors = groups.filter(g => g.type !== 'building');
  host.innerHTML = `
    <div style="font-weight:600;color:var(--text);margin-bottom:8px">Предложенная группировка (нажми «Сгенерировать» чтобы запустить):</div>
    ${buildings.length ? `
      <div style="margin-bottom:10px">
        <div style="font-size:0.82rem;color:var(--muted);margin-bottom:6px">🏛 Зданий для генерации фасадов: ${buildings.length}</div>
        ${buildings.map((g, i) => `
          <div class="facade-preview-row">
            <div style="flex:1">
              <div style="font-weight:600">${esc(g.building_name)}</div>
              <div style="font-size:0.78rem;color:var(--muted);margin:2px 0">${esc((g.facade_description || '').slice(0, 220))}</div>
              <div style="font-size:0.74rem;color:var(--muted)">↪ ${g.member_names?.map(esc).join(' · ') || '(пусто)'}</div>
            </div>
            <div style="display:flex;flex-direction:column;gap:4px">
              <button class="btn-ghost btn-sm" onclick="facadesGenerateOne(${i})" title="Сгенерить только этот фасад" style="font-size:0.74rem;padding:3px 8px">🎬 Этот</button>
              <button class="btn-icon" onclick="facadesPreviewRemove(${i})" title="Не генерить этот фасад">✕</button>
            </div>
          </div>`).join('')}
      </div>` : ''}
    ${exteriors.length ? `
      <div>
        <div style="font-size:0.78rem;color:var(--muted)">🌳 Уже наружные (пропустим): ${exteriors.map(g => esc(g.building_name)).join(', ')}</div>
      </div>` : ''}
  `;
}

function facadesPreviewRemove(idx) {
  if (!FACADES_STATE.groupsPreview) return;
  FACADES_STATE.groupsPreview.splice(idx, 1);
  _renderGroupsPreview(FACADES_STATE.groupsPreview);
}

async function facadesGenerate() {
  if (!FACADES_STATE.groupsPreview?.length) {
    alert('Сначала нажми «🤖 Сгруппировать локации»');
    return;
  }
  const buildings = FACADES_STATE.groupsPreview.filter(g => g.type === 'building');
  if (!buildings.length) {
    alert('Нет зданий для генерации (все локации уже exterior).');
    return;
  }
  if (!confirm(`Сгенерировать ${buildings.length} фасад(ов)? Это займёт ~2-3 минуты на каждое здание (картинка + 4-сек видео).`)) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/facades/generate`, { groups: FACADES_STATE.groupsPreview });
    if (r.error) throw new Error(r.error);
    showToast(`▶ Запущено: ${r.count} зданий, обновление каждые 6с`, 4000);
    FACADES_STATE.groupsPreview = null;
    document.getElementById('facades-groups-preview').style.display = 'none';
    _facadesStartPoll();
    await facadesRefresh();
  } catch (e) {
    alert('Запуск не удался: ' + (e?.message || e));
  }
}

// Generate exactly one facade from the preview list (one building from the
// current Claude grouping). Useful when you want to try one before committing
// to the whole batch.
async function facadesGenerateOne(idx) {
  if (!FACADES_STATE.groupsPreview) return;
  const g = FACADES_STATE.groupsPreview[idx];
  if (!g || g.type !== 'building') return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/facades/generate`, { groups: [g] });
    if (r.error) throw new Error(r.error);
    showToast(`▶ Запущен фасад «${g.building_name}»`, 4000);
    // Drop the started group from preview so the user can't double-submit
    FACADES_STATE.groupsPreview.splice(idx, 1);
    _renderGroupsPreview(FACADES_STATE.groupsPreview);
    _facadesStartPoll();
    await facadesRefresh();
  } catch (e) {
    alert('Не удалось: ' + (e?.message || e));
  }
}

// Per-location quick-gen — picks one location card from the ungrouped strip,
// asks the backend to derive building_name + facade_description from that
// location alone, and kicks off generation as a single-member facade.
async function facadesGenerateForLoc(locId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
  }
  try {
    const r = await api.post(`/api/series/${S.seriesId}/facades/generate-for-location`, { loc_id: locId });
    if (r.error) throw new Error(r.error);
    showToast(`▶ Запущен фасад: ${r.building_name || ''}`, 4000);
    _facadesStartPoll();
    await facadesRefresh();
  } catch (e) {
    alert('Не удалось: ' + (e?.message || e));
    if (btn) { btn.disabled = false; btn.innerHTML = '🎬 Сгенерить фасад'; }
  }
}

async function facadesRegenerate(fid) {
  if (!confirm('Перегенерить фасад? Старая картинка и видео будут удалены.')) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/facades/${fid}/regenerate`, {});
    if (r.error) throw new Error(r.error);
    showToast('▶ Перегенерация запущена');
    _facadesStartPoll();
    await facadesRefresh();
  } catch (e) {
    alert('Не удалось: ' + (e?.message || e));
  }
}

async function facadesDelete(fid) {
  if (!confirm('Удалить фасад? Файлы тоже удалятся.')) return;
  try {
    await api.del(`/api/series/${S.seriesId}/facades/${fid}`);
    await facadesRefresh();
  } catch (e) {
    alert('Не удалось: ' + (e?.message || e));
  }
}

async function facadesOpenFolder() {
  try {
    const r = await api.post(`/api/series/${S.seriesId}/facades/folder`, {});
    showToast('📂 ' + (r.folder || 'Папка открыта'), 4000);
  } catch (e) {
    showToast('Не удалось открыть: ' + (e?.message || e));
  }
}

// ── Items (story-relevant props: handbag, gun, locket, ...) ───────────────────
let currentItemId = null;

function renderItemsList() {
  const s = S.series;
  const el = document.getElementById('items-list');
  if (!el) return;
  const items = s.items || [];
  if (!items.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет предметов</div>';
    return;
  }
  el.innerHTML = items.map(it => {
    const hasRefs = it.ref_images && it.ref_images.length > 0;
    const v = it.image_version ? `?v=${it.image_version}` : '';
    const imgUrl = hasRefs ? `/assets/${s.id}/${it.ref_images[0]}${v}` : null;
    return `
      <div class="char-item" data-autogen-kind="item" data-autogen-id="${it.id}" onclick="openItemAssets('${it.id}')"
           ondragover="_dropAssetOver(event)" ondragleave="_dropAssetLeave(event)"
           ondrop="_dropAssetUpload(event, 'item', '${it.id}')"
           title="Клик — открыть карточку. Перетащи картинку с компа — заменит фото.">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(it.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '🎒'}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(it.name)}</div>
          <div class="char-role">${esc(it.description?.slice(0,30) || '—')}</div>
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditItem('${it.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteItem('${it.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function openAddItem() {
  S.editingItemId = null;
  document.getElementById('item-modal-title').textContent = 'Новый предмет';
  clearFields(['item-name','item-description']);
  openModal('modal-item');
}

// One-click cleanup of duplicate items in the current series. Hits the
// /dedupe-items endpoint which runs fuzzy + LLM-synonym pass over series.items
// and merges synonym groups (dictaphone/voice recorder, locket/pendant). All
// episode.items_used arrays are rewritten to point at canonical ids.
async function dedupeSeriesItems() {
  if (!S.seriesId) return;
  const items = S.series?.items || [];
  if (items.length < 2) { showToast('Меньше 2 предметов — нечего объединять'); return; }
  if (!confirm(`Найти и склеить дубликаты среди ${items.length} предметов? Claude проверит синонимы (диктофон/recorder и т.п.) и объединит. Действие необратимо, но не теряет данные — берётся самое полное описание.`)) return;
  showToast('🧹 Анализирую дубликаты…');
  try {
    const r = await api.post(`/api/series/${S.seriesId}/dedupe-items`, {});
    bumpAssetVersion();
    if (r.error) throw new Error(r.error);
    if (!r.merged) {
      showToast('✓ Дубликатов не найдено');
      return;
    }
    const groups = (r.groups || []).map(g =>
      `  • ${g.canonical.name} ← ${g.merged.map(m => m.name).join(', ')}`
    ).join('\n');
    alert(`✓ Склеено ${r.merged} дубликатов в ${r.groups?.length || 0} группах:\n\n${groups}`);
    // Refresh state and re-render lists.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderItemsList === 'function') renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();
  } catch (e) {
    alert('Ошибка дедупликации: ' + (e?.message || e));
  }
}

function openEditItem(itemId) {
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  S.editingItemId = itemId;
  document.getElementById('item-modal-title').textContent = 'Редактировать предмет';
  setVal('item-name', it.name);
  setVal('item-description', it.description);
  openModal('modal-item');
}

async function saveItem() {
  const data = { name: val('item-name'), description: val('item-description') };
  if (!data.name) return alert('Введи название предмета');
  if (S.editingItemId) {
    await api.put(`/api/series/${S.seriesId}/items/${S.editingItemId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/items`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-item');
  renderItemsList();
}

async function deleteItem(itemId) {
  if (!confirm('Удалить предмет?')) return;
  await api.del(`/api/series/${S.seriesId}/items/${itemId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  renderItemsList();
}

function openItemAssets(itemId) {
  currentItemId = itemId;
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  document.getElementById('item-assets-title').textContent = `Фото: ${it.name}`;
  renderItemAssetsGrid(it);
  setVal('regen-item-wishes', it.image_constraints || '');
  const inp = document.getElementById('item-asset-file-input');
  inp.onchange = () => uploadItemRefs(itemId, inp.files);
  openModal('modal-item-assets');
}

function renderItemAssetsGrid(item) {
  const grid = document.getElementById('item-assets-grid');
  const refs = item.ref_images || [];
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}`;
    return `
      <div class="photo-thumb-wrap" onclick="openItemLightbox('${item.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteItemRef('${item.id}','${fname}')">✕</button>
      </div>`;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

async function uploadItemRefs(itemId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/item/${itemId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const it = (S.series.items || []).find(x => x.id === itemId);
  renderItemAssetsGrid(it);
  renderItemsList();
}

async function deleteItemRef(itemId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/item/${itemId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const it = (S.series.items || []).find(x => x.id === itemId);
  renderItemAssetsGrid(it);
  renderItemsList();
}

async function regenerateItem() {
  if (!currentItemId) return;
  const wishes = (val('regen-item-wishes') || '').trim();
  const btn = document.getElementById('regen-item-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ генерирую...'; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/items/${currentItemId}/regenerate`,
      { wishes }
    );
    if (!r.ready) throw new Error(r.error || 'unknown');
    bumpAssetVersion();   // refresh ALL assetUrl()-rendered images
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const it = (S.series.items || []).find(x => x.id === currentItemId);
    if (it) renderItemAssetsGrid(it);
    renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();  // episode-view sidebar
    showToast('✓ Предмет перегенерирован');
  } catch (e) {
    showToast('✗ ' + (e.message || e), 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old; }
  }
}

// Item-photo lightbox: image + regenerate panel
function openItemLightbox(itemId, url) {
  closeLightbox();
  const it = (S.series.items || []).find(x => x.id === itemId);
  if (!it) return;
  currentItemId = itemId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать предмет</h3>
        <div class="hint">
          Старое фото удалится, новое сгенерируется по описанию + пожеланиям.
          Пожелания сохранятся в предмете и будут применяться при всех будущих генерациях.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-item-wishes" rows="5"
            placeholder="Например:&#10;«потёртая кожа, не новая»&#10;«золотая фурнитура»&#10;«царапина на боку»&#10;«без бренда»">${esc(it.image_constraints || '')}</textarea>
        </div>
        <div id="lb-regen-item-status" class="lb-status"></div>
        <button id="lb-regen-item-btn" class="btn-regen" onclick="regenerateItemFromLightbox()">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

async function regenerateItemFromLightbox() {
  if (!currentItemId) return;
  const wishes = (document.getElementById('lb-regen-item-wishes').value || '').trim();
  const status = document.getElementById('lb-regen-item-status');
  const btn = document.getElementById('lb-regen-item-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  status.textContent = 'Перегенерируем фото предмета (~15-30 сек)...';
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/items/${currentItemId}/regenerate`,
      { wishes }
    );
    if (!res.ready) throw new Error(res.error || 'unknown');
    bumpAssetVersion();   // refresh ALL assetUrl()-rendered images
    status.className = 'lb-status ok';
    status.textContent = '✓ Предмет обновлён';
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const it = (S.series.items || []).find(x => x.id === currentItemId);
    if (it) renderItemAssetsGrid(it);
    renderItemsList();
    if (typeof renderEpItems === 'function') renderEpItems();  // episode-view sidebar
    setTimeout(() => closeLightbox(), 800);
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + (e.message || e);
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

// ── Character assets modal ────────────────────────────────────────────────────
let currentCharId = null;

async function openCharAssets(charId) {
  currentCharId = charId;
  // Refresh series state so any outfits added by recent script-gen / autogen
  // show up — without this, the modal renders stale `S.series.characters[i]`
  // and "no other outfits" looks like a bug even when they exist on disk.
  try {
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh && fresh.characters) S.series = fresh;
  } catch {}
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  document.getElementById('char-assets-title').textContent = `Фото: ${c.name}`;
  renderCharAssetsGrid(c);
  renderOutfitsList(c);
  renderCharCanonicalDesc(c);

  const inp = document.getElementById('char-asset-file-input');
  inp.onchange = () => uploadCharacterRefs(charId, inp.files);
  openModal('modal-char-assets');
}

// Build the canonical description shown in the card and copied into Seedance prompts.
// Mirrors backend _canonical_char_description() — appearance + base outfit description.
function _buildCanonicalCharDesc(char) {
  if (!char) return '';
  const appearance = (char.appearance || '').trim();
  const outfits = char.outfits || [];
  const base = outfits.find(o => o.is_base) || outfits[0];
  const outfitDesc = (base && (base.description || '').trim()) || '';
  return [appearance, outfitDesc].filter(Boolean).join('; ');
}

function renderCharCanonicalDesc(char) {
  const el = document.getElementById('char-canonical-desc');
  if (el) {
    const text = _buildCanonicalCharDesc(char);
    el.textContent = text || '(описание не задано — заполни appearance персонажа и/или базовый outfit)';
    el.dataset.text = text;
  }
  // Populate the editable appearance textarea + remember original for revert / dirty-check.
  const ta = document.getElementById('char-appearance-edit');
  if (ta && char) {
    const original = (char.appearance || '');
    ta.value = original;
    ta.dataset.original = original;
    ta.dataset.charId = char.id;
    const btn = document.getElementById('char-appearance-save-btn');
    if (btn) btn.disabled = true;
    const st = document.getElementById('char-appearance-save-status');
    if (st) st.textContent = '';
  }
}

// Called on every keystroke in the appearance textarea — enables/disables
// the Save button based on whether content differs from original.
function _markCharAppearanceDirty() {
  const ta = document.getElementById('char-appearance-edit');
  if (!ta) return;
  const dirty = ta.value !== (ta.dataset.original || '');
  const btn = document.getElementById('char-appearance-save-btn');
  if (btn) btn.disabled = !dirty;
  const st = document.getElementById('char-appearance-save-status');
  if (st) st.textContent = dirty ? '● несохранённые изменения' : '';
}

// Revert textarea to last saved value (from dataset.original).
function _revertCharAppearance() {
  const ta = document.getElementById('char-appearance-edit');
  if (!ta) return;
  ta.value = ta.dataset.original || '';
  _markCharAppearanceDirty();
}

// Persist appearance to backend via PUT, then refresh the canonical preview.
async function saveCharAppearance() {
  const ta = document.getElementById('char-appearance-edit');
  if (!ta) return;
  const charId = ta.dataset.charId;
  if (!charId) return;
  const newText = (ta.value || '').trim();
  const btn = document.getElementById('char-appearance-save-btn');
  const st = document.getElementById('char-appearance-save-status');
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Сохраняю...'; }
  if (st) st.textContent = '';
  try {
    const res = await api.put(
      `/api/series/${S.seriesId}/characters/${charId}`,
      { appearance: newText },
    );
    if (res && res.error) throw new Error(res.error);
    // Update in-memory state + re-render preview
    const ch = S.series.characters.find(x => x.id === charId);
    if (ch) ch.appearance = newText;
    ta.dataset.original = newText;
    renderCharCanonicalDesc(ch || res);
    // Also refresh sidebar list so any description-derived preview updates.
    renderCharactersList && renderCharactersList();
    if (st) st.textContent = '✓ сохранено';
    setTimeout(() => { if (st) st.textContent = ''; }, 2500);
  } catch (e) {
    if (st) st.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) btn.innerHTML = '💾 Сохранить';
  }
}

async function copyCanonicalDescription() {
  const el = document.getElementById('char-canonical-desc');
  const text = el?.dataset.text || el?.textContent || '';
  if (!text || text.startsWith('(')) {
    showToast('⚠ Описание пустое', 2500);
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast('✓ Скопировано', 1800);
  } catch {
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    ta.remove();
    showToast('✓ Скопировано', 1800);
  }
}

function renderCharAssetsGrid(char, bustCache) {
  const grid = document.getElementById('char-assets-grid');
  const refs = char.ref_images || [];
  const cacheSuffix = bustCache ? `?t=${Date.now()}` : '';
  grid.innerHTML = refs.map(r => {
    const fname = r.split('/').pop();
    const url = `${assetUrl(r)}${cacheSuffix}`;
    return `
      <div class="photo-thumb-wrap" onclick="openCharLightbox('${char.id}','${url}')">
        <img src="${url}" alt="">
        <button class="del-btn" onclick="event.stopPropagation();deleteCharRef('${char.id}','${fname}')">✕</button>
      </div>
    `;
  }).join('');
  if (!refs.length) grid.innerHTML = '<div style="color:var(--muted);font-size:0.85rem">Нет фото</div>';
}

// Builds an inline DOM node that replaces a broken <img>. Shows a clearly-
// broken visual + click-to-debug. Used everywhere asset paths might point at
// a file that vanished (concurrent-write JSON corruption used to do this; the
// atomic-write fix should prevent it now, but the placeholder stays as a
// safety net + diagnostic tool).
function _brokenImagePlaceholder(url) {
  const div = document.createElement('div');
  div.className = 'broken-image-placeholder';
  div.title = 'Картинка не загрузилась — кликни для дебага';
  div.dataset.assetUrl = url;
  div.innerHTML = `
    <div class="bip-icon">🚫</div>
    <div class="bip-label">Фото утеряно</div>
    <div class="bip-hint">Клик — дебаг</div>`;
  div.onclick = (e) => { e.stopPropagation(); debugAsset(url); };
  return div;
}

// Click-handler for broken-image placeholders. Hits a debug endpoint that
// reports filesystem state for the asset path so the user can paste the JSON
// blob back to support / dev. Resilient: never throws into the UI.
async function debugAsset(url) {
  try {
    // url looks like '/assets/<sid>/<rel_path>?v=<cache-buster>' — strip query
    // string before feeding rel_path to the debug API. Without this strip the
    // backend tries to open `file.jpg?v=1778...` literally, always reports
    // exists=false even when the file IS on disk. (User-reported false alarm.)
    const cleanUrl = url.split('?')[0].split('#')[0];
    const m = cleanUrl.match(/^\/assets\/([^/]+)\/(.+)$/);
    if (!m) {
      alert('Не разобрать путь к ассету: ' + url);
      return;
    }
    const [, sid, relPath] = m;
    const r = await api.get(`/api/series/${encodeURIComponent(sid)}/debug-asset?path=${encodeURIComponent(relPath)}`);
    const dump = JSON.stringify(r, null, 2);
    // Modal-ish: open a textarea-in-prompt so user can copy.
    const overlay = document.createElement('div');
    overlay.className = 'lightbox-overlay';
    overlay.style.zIndex = 99999;
    overlay.innerHTML = `
      <button class="lb-close" onclick="this.parentElement.remove()">✕</button>
      <div class="lightbox-content" onclick="event.stopPropagation()" style="max-width:720px">
        <div class="lightbox-panel" style="width:100%">
          <h3>🔍 Дебаг ассета</h3>
          <div style="font-size:0.78rem;color:var(--muted);word-break:break-all">${esc(url)}</div>
          <textarea readonly rows="18" style="width:100%;font-family:monospace;font-size:0.78rem">${esc(dump)}</textarea>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
            <button class="btn-regen" onclick="navigator.clipboard.writeText(this.parentElement.previousElementSibling.value);this.textContent='✓ Скопировано'">📋 Скопировать дебаг</button>
            <button class="btn-regen" style="background:linear-gradient(135deg,#10b981,#059669)" onclick="relinkOrphanedAssets(this)">🔗 Найти и привязать потерянные</button>
          </div>
          <div id="relink-result" style="font-size:0.82rem;color:var(--muted);margin-top:8px;white-space:pre-wrap"></div>
        </div>
      </div>`;
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
  } catch (e) {
    alert('Дебаг не удался: ' + (e?.message || e));
  }
}

// Calls /relink-assets which scans the assets/ folder and re-attaches orphan
// files to characters/locations/items whose ref_images is empty. Runs from
// the broken-image debug modal so the user can self-heal a series after a
// JSON-corruption incident wiped refs.
async function relinkOrphanedAssets(btn) {
  if (!S.seriesId) { alert('Открой сериал сначала'); return; }
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = '⏳ Сканирую...';
  const out = document.getElementById('relink-result');
  if (out) out.textContent = '';
  try {
    const r = await api.post(`/api/series/${S.seriesId}/relink-assets`, {});
    bumpAssetVersion();
    if (r?.error) throw new Error(r.error);
    const lines = (r.relinked || []).map(x =>
      `✓ ${x.kind} «${x.name}» → ${x.files.join(', ')}`
    ).join('\n');
    if (out) out.textContent = r.count
      ? `Привязано: ${r.count}\n${lines}`
      : 'Орфанов не найдено — все ассеты на местах.';
    // Refresh series state and re-render lists.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    renderCharactersList && renderCharactersList();
    renderLocationsList && renderLocationsList();
    renderItemsList && renderItemsList();
    renderEpCharacters && renderEpCharacters();
    renderEpLocations && renderEpLocations();
    renderEpItems && renderEpItems();
    btn.textContent = '✓ Готово';
  } catch (e) {
    if (out) out.textContent = '✗ ' + (e?.message || e);
    btn.disabled = false; btn.textContent = orig;
  }
}

// Plain image lightbox (used for outfit photos — no regen panel)
function openLightbox(url) {
  closeLightbox();
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

// Outfit lightbox: shows the outfit photo with prev/next nav across ALL of the
// character's photos (base portrait as item 0, then each outfit with a photo).
// Lets the user browse the wardrobe full-size without closing the lightbox
// between picks. Arrow keys + on-screen arrows + regenerate-with-wishes.
let _outfitLightboxState = null;  // {charId, items: [{kind, id, label, photoUrl}], idx}

function openOutfitLightbox(charId, outfitId) {
  closeLightbox();
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  const items = [];
  if (c.ref_images?.length) {
    items.push({
      kind: 'base', id: 'base', label: 'База',
      photoUrl: `${assetUrl(c.ref_images[0])}`,
    });
  }
  for (const o of (c.outfits || [])) {
    if (o.photo) {
      items.push({
        kind: 'outfit', id: o.id, label: o.label || '(без названия)',
        photoUrl: `${assetUrl(o.photo)}`,
      });
    }
  }
  if (!items.length) { showToast('Нет фото для просмотра'); return; }
  let idx = items.findIndex(it => it.kind === 'outfit' && it.id === outfitId);
  if (idx < 0) idx = items.findIndex(it => it.kind === 'base' && outfitId === 'base');
  if (idx < 0) idx = 0;
  _outfitLightboxState = { charId, items, idx };
  _renderOutfitLightbox();
}

function _renderOutfitLightbox() {
  const st = _outfitLightboxState;
  if (!st) return;
  const cur = st.items[st.idx];
  const c = S.series.characters.find(x => x.id === st.charId);
  const total = st.items.length;
  const prevDisabled = total < 2 ? 'disabled' : '';
  const nextDisabled = total < 2 ? 'disabled' : '';
  let div = document.getElementById('lightbox-overlay');
  if (!div) {
    div = document.createElement('div');
    div.id = 'lightbox-overlay';
    div.className = 'lightbox-overlay';
    div.onclick = (e) => { if (e.target === div) closeLightbox(); };
    document.body.appendChild(div);
  }
  // Re-render only the inner content; keep the overlay (avoids flicker on nav).
  const isOutfit = cur.kind === 'outfit';
  const outfitObj = isOutfit ? (c?.outfits || []).find(o => o.id === cur.id) : null;
  const regenLabel = isOutfit
    ? `↻ Перегенерировать образ «${esc(cur.label)}»`
    : '↻ Перегенерировать базу персонажа';
  const wishesPlaceholder = isOutfit
    ? 'Например:\n«Цвет более тёмный»\n«Без сумки»\n«Длиннее юбка»'
    : 'Например:\n«Без шрама на лице»\n«Глаза карие, не голубые»';
  const initialWishes = isOutfit ? '' : (c?.image_constraints || '');
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <button class="lb-nav lb-prev" ${prevDisabled} onclick="event.stopPropagation();outfitLightboxNav(-1)" title="Предыдущий (←)">‹</button>
    <button class="lb-nav lb-next" ${nextDisabled} onclick="event.stopPropagation();outfitLightboxNav(1)" title="Следующий (→)">›</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap">
        <img src="${cur.photoUrl}" alt="${esc(cur.label)}"
             onerror="this.replaceWith(_brokenImagePlaceholder('${cur.photoUrl}'))">
        <div class="lb-caption">
          <strong>${esc(cur.label)}</strong>
          <span style="color:var(--muted);margin-left:8px">${st.idx + 1} / ${total}</span>
        </div>
      </div>
      <div class="lightbox-panel">
        <h3>${regenLabel}</h3>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="${esc(wishesPlaceholder)}">${esc(initialWishes)}</textarea>
        </div>
        ${!isOutfit ? `
          <label class="cb">
            <input type="checkbox" id="lb-regen-outfits" checked>
            <span>Также перегенерировать все костюмы</span>
          </label>
        ` : ''}
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="${isOutfit
            ? `regenerateOutfitFromLightbox('${cur.id}')`
            : 'regenerateCharacterFromLightbox()'}">
          ↻ Перегенерировать
        </button>
      </div>
    </div>`;
}

function outfitLightboxNav(delta) {
  const st = _outfitLightboxState;
  if (!st || st.items.length < 2) return;
  st.idx = (st.idx + delta + st.items.length) % st.items.length;
  _renderOutfitLightbox();
}

async function regenerateOutfitFromLightbox(outfitId) {
  const st = _outfitLightboxState;
  if (!st) return;
  const wishes = (document.getElementById('lb-regen-wishes')?.value || '').trim();
  const btn = document.getElementById('lb-regen-btn');
  const status = document.getElementById('lb-regen-status');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую...'; }
  if (status) status.textContent = '';
  try {
    // Persist wishes onto the outfit's description so future i2i bakes them in.
    if (wishes) {
      await api.put(`/api/series/${S.seriesId}/characters/${st.charId}/outfits/${outfitId}`, {
        description: wishes,
      });
    }
    // Trigger generation (uses existing endpoint that the outfit row uses).
    const r = await api.post(`/api/series/${S.seriesId}/characters/${st.charId}/outfits/${outfitId}/generate`, {});
    if (r?.error) throw new Error(r.error);
    // Force-bump asset version so every image URL gets a fresh `?v=N` token.
    // Without this, browser HTTP cache serves the OLD outfit image even
    // after AVAI returned a new one (out_path stays the same filename).
    // User-reported 2026-05-23: «перегенерация костюмов не работает».
    bumpAssetVersion();
    // Refresh series state and re-render the lightbox with the new image.
    const fresh = await api.get(`/api/series/${S.seriesId}`);
    if (fresh) S.series = fresh;
    if (typeof renderCharactersList === 'function') renderCharactersList();
    if (typeof openCharAssets === 'function' && document.getElementById('modal-char-assets')?.classList.contains('open')) {
      const c = S.series.characters.find(x => x.id === st.charId);
      if (c) renderOutfitsList(c);
    }
    // Rebuild item list from fresh state and stay on the same outfit.
    openOutfitLightbox(st.charId, outfitId);
    if (status) status.textContent = '✓ Готово';
  } catch (e) {
    if (status) status.textContent = '✗ ' + (e?.message || e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Перегенерировать'; }
  }
}

// Character-photo lightbox: image + regenerate panel
function openCharLightbox(charId, url) {
  closeLightbox();
  const c = S.series.characters.find(x => x.id === charId);
  if (!c) return;
  currentCharId = charId;
  const div = document.createElement('div');
  div.id = 'lightbox-overlay';
  div.className = 'lightbox-overlay';
  div.innerHTML = `
    <button class="lb-close" onclick="closeLightbox()">✕</button>
    <div class="lightbox-content" onclick="event.stopPropagation()">
      <div class="lb-img-wrap"><img src="${url}" alt=""></div>
      <div class="lightbox-panel">
        <h3>↻ Перегенерировать персонажа</h3>
        <div class="hint">
          Сначала перегенерируется <strong style="color:var(--text)">основной образ</strong>,
          затем по нему как референсу — все костюмы. Старые фото удалятся.
        </div>
        <div>
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Что учесть / исправить
          </label>
          <textarea id="lb-regen-wishes" rows="5"
            placeholder="Например:&#10;«Без шрама на лице»&#10;«Глаза карие, не голубые»&#10;«Волосы чуть короче»">${esc(c.image_constraints || '')}</textarea>
          <div style="font-size:0.76rem;color:var(--muted);margin-top:4px">
            Сохранится в персонаже и будет применяться при всех будущих генерациях.
          </div>
        </div>
        <label class="cb">
          <input type="checkbox" id="lb-regen-outfits" checked>
          <span>Также перегенерировать все костюмы</span>
        </label>
        <label class="cb" title="Claude перепишет поле appearance с нуля — помогает если текущее описание содержит эмоции/действия вместо внешности">
          <input type="checkbox" id="lb-rewrite-appearance">
          <span>Переписать описание персонажа заново</span>
        </label>
        <div style="margin-top:10px">
          <label style="font-size:0.82rem;color:var(--muted);display:block;margin-bottom:4px">
            Image generator
          </label>
          <select id="lb-regen-provider" style="width:100%;padding:6px 8px;border-radius:6px;background:var(--bg-input);color:var(--text);border:1px solid var(--border)" title="Выбери движок генерации изображения. Если выбранный упадёт — автоматический fallback на банан">
            <option value="">Auto (по умолчанию серии)</option>
            <option value="banana">🍌 Banana (Gemini Image Pro)</option>
            <option value="seedream">🌱 Seedream (ByteDance)</option>
            <option value="openai">🤖 OpenAI (gpt-image-1)</option>
          </select>
        </div>
        <div id="lb-regen-status" class="lb-status"></div>
        <button id="lb-regen-btn" class="btn-regen" onclick="regenerateCharacterFromLightbox()">
          ↻ Перегенерировать
        </button>
        <button class="btn-ghost" style="color:var(--danger);margin-top:8px;width:100%" onclick="deleteCharacterFromLightbox()">
          🗑 Удалить персонажа
        </button>
      </div>
    </div>`;
  div.onclick = (e) => { if (e.target === div) closeLightbox(); };
  document.body.appendChild(div);
}

function closeLightbox() {
  const old = document.getElementById('lightbox-overlay');
  if (old) old.remove();
  _outfitLightboxState = null;
}
document.addEventListener('keydown', (e) => {
  if (!document.getElementById('lightbox-overlay')) return;
  if (e.key === 'Escape') return closeLightbox();
  if (_outfitLightboxState) {
    if (e.key === 'ArrowLeft')  { e.preventDefault(); outfitLightboxNav(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); outfitLightboxNav(1);  }
  }
});

async function deleteCharacterFromLightbox() {
  if (!currentCharId) return;
  if (!confirm('Удалить персонажа полностью? Это действие необратимо.')) return;
  closeLightbox();
  await deleteCharacter(currentCharId);
}

async function regenerateCharacterFromLightbox() {
  if (!currentCharId) return;
  const wishes = (document.getElementById('lb-regen-wishes').value || '').trim();
  const regenOutfits = document.getElementById('lb-regen-outfits').checked;
  const rewriteAppearance = document.getElementById('lb-rewrite-appearance')?.checked || false;
  // Provider override from dropdown — '' = auto (server uses series default).
  const provider = (document.getElementById('lb-regen-provider')?.value || '').trim();
  const status = document.getElementById('lb-regen-status');
  const btn = document.getElementById('lb-regen-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.className = 'lb-status';
  const providerLabel = provider ? ` через ${provider}` : '';
  status.textContent = (rewriteAppearance ? 'Переписываем описание + ' : '') +
    'Перегенерируем основной образ' + (regenOutfits ? ' + костюмы' : '') + providerLabel + ' (~15-30 сек на каждый)...';

  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/characters/${currentCharId}/regenerate`,
      { wishes, regenerate_outfits: regenOutfits, rewrite_appearance: rewriteAppearance, provider }
    );
    if (res.error) {
      status.className = 'lb-status err';
      status.textContent = 'Ошибка: ' + res.error;
      btn.disabled = false;
      btn.innerHTML = '↻ Перегенерировать';
      return;
    }
    const totalOutfits = (res.regenerated_outfits || []).length;
    const failed = res.failed_outfits || [];
    status.className = failed.length ? 'lb-status' : 'lb-status ok';
    let msg = '✓ Основной образ обновлён';
    if (regenOutfits) msg += ` · костюмов: ${totalOutfits}`;
    if (failed.length) msg += ` · ошибок: ${failed.length} (${failed.join(', ')})`;
    status.textContent = msg;

    // Bump the global asset version BEFORE re-rendering so every URL
    // built by `assetUrl()` (sidebar miniatures, outfit grid, episode
    // thumbnails) carries the new `?v=` token. Without this, the canonical
    // filename stays the same across regenerations and the browser's HTTP
    // cache serves the OLD image to every view except the lightbox (which
    // builds its own ad-hoc cache-buster below). User saw new variant in
    // lightbox, then sidebar miniature kept showing the old one and on
    // re-open the cached old image came back.
    bumpAssetVersion();
    // Refresh state
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === currentCharId);
    renderCharAssetsGrid(c, true);
    renderOutfitsList(c);
    renderCharactersList();
    if (typeof renderEpCharacters === 'function') renderEpCharacters();  // episode-view sidebar

    // Swap lightbox image to the freshly generated base (cache-bust)
    const freshUrl = res.base_url + '?t=' + Date.now();
    const lbImg = document.querySelector('#lightbox-overlay .lb-img-wrap img');
    if (lbImg) lbImg.src = freshUrl;

    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать ещё раз';
  } catch (e) {
    status.className = 'lb-status err';
    status.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

async function uploadCharacterRefs(charId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/character/${charId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderCharAssetsGrid(c);
  renderCharactersList();
}

async function deleteCharRef(charId, filename) {
  await api.del(`/api/series/${S.seriesId}/assets/character/${charId}/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderCharAssetsGrid(c);
  renderCharactersList();
}

// ── Outfit management ─────────────────────────────────────────────────────────
let editingOutfitId = null;

function renderOutfitsList(char) {
  const el = document.getElementById('outfits-list');
  if (!el) return;
  const outfits = char.outfits || [];
  if (!outfits.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">Нет костюмов. Добавь первый!</div>';
    return;
  }
  el.innerHTML = outfits.map(o => {
    const hasPhoto = !!o.photo;
    const photoUrl = hasPhoto ? `${assetUrl(o.photo)}` : '';
    const photoHtml = hasPhoto
      ? `<img src="${photoUrl}" alt="">`
      : `<div style="font-size:2.2rem">👗</div>`;
    const photoClick = hasPhoto ? `onclick="openOutfitLightbox('${char.id}','${o.id}')"` : '';
    return `
      <div class="outfit-card ${hasPhoto ? 'has-photo' : ''}" id="outfit-card-${o.id}">
        <div class="outfit-photo" ${photoClick}>${photoHtml}</div>
        <div class="outfit-info">
          <div class="outfit-label">${esc(o.label)}</div>
          ${o.description ? `<div class="outfit-desc">${esc(o.description)}</div>` : ''}
          <div class="outfit-btns">
            <button class="btn-ghost" onclick="openEditOutfit('${char.id}','${o.id}')">✎</button>
            ${hasPhoto
              ? `<button class="btn-ghost" style="color:var(--accent)" onclick="generateOutfit('${char.id}','${o.id}')">↻ Перегенерировать</button>`
              : `<button class="btn-ghost" style="color:var(--accent)" onclick="generateOutfit('${char.id}','${o.id}')">⚡ Сгенерировать</button>`}
            <button class="btn-ghost" style="color:var(--danger)" onclick="deleteOutfit('${char.id}','${o.id}')">✕</button>
          </div>
          <div class="outfit-status" id="outfit-status-${o.id}"></div>
        </div>
      </div>
    `;
  }).join('');
}

function openAddOutfit(charId) {
  editingOutfitId = null;
  currentCharId = charId;
  document.getElementById('outfit-modal-title').textContent = 'Новый костюм';
  clearFields(['outfit-label', 'outfit-description']);
  openModal('modal-outfit');
}

function openEditOutfit(charId, outfitId) {
  const c = S.series.characters.find(x => x.id === charId);
  const o = (c?.outfits || []).find(x => x.id === outfitId);
  if (!o) return;
  editingOutfitId = outfitId;
  currentCharId = charId;
  document.getElementById('outfit-modal-title').textContent = 'Редактировать костюм';
  setVal('outfit-label', o.label);
  setVal('outfit-description', o.description);
  openModal('modal-outfit');
}

async function saveOutfit() {
  const label = val('outfit-label');
  if (!label) return alert('Введи название костюма');
  const data = { label, description: val('outfit-description') };
  if (editingOutfitId) {
    await api.put(`/api/series/${S.seriesId}/characters/${currentCharId}/outfits/${editingOutfitId}`, data);
  } else {
    await api.post(`/api/series/${S.seriesId}/characters/${currentCharId}/outfits`, data);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-outfit');
  const c = S.series.characters.find(x => x.id === currentCharId);
  renderOutfitsList(c);
}

async function deleteOutfit(charId, outfitId) {
  if (!confirm('Удалить костюм?')) return;
  await api.del(`/api/series/${S.seriesId}/characters/${charId}/outfits/${outfitId}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  const c = S.series.characters.find(x => x.id === charId);
  renderOutfitsList(c);
}

async function generateOutfit(charId, outfitId) {
  const statusEl = document.getElementById(`outfit-status-${outfitId}`);
  if (!statusEl) return;

  const card = document.getElementById(`outfit-card-${outfitId}`);
  const genBtn = card?.querySelector('.outfit-btns button[style*="accent"]');
  if (genBtn) { genBtn.disabled = true; genBtn.innerHTML = '<span class="spinner"></span>'; }
  statusEl.textContent = 'Генерируем (~15 сек)...';

  try {
    const res = await api.post(`/api/series/${S.seriesId}/characters/${charId}/outfits/${outfitId}/generate`, {});
    if (res.ready) {
      statusEl.textContent = '';
      // Bump global asset cache token — browser was serving the OLD outfit
      // image from HTTP cache because filename stayed the same. Without this
      // user clicks Generate, backend returns new image, but UI shows old.
      bumpAssetVersion();
      S.series = await api.get(`/api/series/${S.seriesId}`);
      const c = S.series.characters.find(x => x.id === charId);
      renderOutfitsList(c);
      showToast('Костюм готов!');
    } else {
      statusEl.textContent = 'Ошибка: ' + (res.error || 'Неизвестная ошибка');
      if (genBtn) { genBtn.disabled = false; genBtn.textContent = '⚡ Сгенерировать'; }
    }
  } catch (e) {
    statusEl.textContent = 'Ошибка: ' + e.message;
    if (genBtn) { genBtn.disabled = false; genBtn.textContent = '⚡ Сгенерировать'; }
  }
}

// ── Regenerate character (base first, then outfits) ─────────────────────────
function openRegenChar() {
  if (!currentCharId) return;
  const c = S.series.characters.find(x => x.id === currentCharId);
  if (!c) return;
  setVal('regen-char-wishes', c.image_constraints || '');
  document.getElementById('regen-char-outfits').checked = true;
  document.getElementById('regen-char-status').textContent = '';
  const btn = document.getElementById('regen-char-confirm-btn');
  btn.disabled = false;
  btn.innerHTML = '↻ Перегенерировать';
  openModal('modal-regen-char');
}

async function regenerateCharacter() {
  if (!currentCharId) return;
  const wishes = (val('regen-char-wishes') || '').trim();
  const regenOutfits = document.getElementById('regen-char-outfits').checked;
  const status = document.getElementById('regen-char-status');
  const btn = document.getElementById('regen-char-confirm-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем основной образ...';
  status.style.color = 'var(--warning)';
  status.textContent = 'Перегенерируем основной образ (~15 сек)...';

  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/characters/${currentCharId}/regenerate`,
      { wishes, regenerate_outfits: regenOutfits }
    );
    if (res.error) {
      status.style.color = 'var(--danger)';
      status.textContent = 'Ошибка: ' + res.error;
      btn.disabled = false;
      btn.innerHTML = '↻ Перегенерировать';
      return;
    }
    const totalOutfits = (res.regenerated_outfits || []).length;
    const failedOutfits = (res.failed_outfits || []);
    status.style.color = 'var(--success)';
    let msg = '✓ Основной образ обновлён';
    if (regenOutfits) msg += ` · костюмов перегенерировано: ${totalOutfits}`;
    if (failedOutfits.length) msg += ` · ошибок: ${failedOutfits.length} (${failedOutfits.join(', ')})`;
    status.textContent = msg;
    bumpAssetVersion();   // invalidate cached photos across all views
    S.series = await api.get(`/api/series/${S.seriesId}`);
    const c = S.series.characters.find(x => x.id === currentCharId);
    renderCharAssetsGrid(c, true);
    renderOutfitsList(c);
    renderCharactersList();
    if (typeof renderEpCharacters === 'function') renderEpCharacters();  // episode-view sidebar
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать ещё раз';
    setTimeout(() => {
      if (!failedOutfits.length) closeModal('modal-regen-char');
    }, 1800);
  } catch (e) {
    status.style.color = 'var(--danger)';
    status.textContent = 'Ошибка: ' + e.message;
    btn.disabled = false;
    btn.innerHTML = '↻ Перегенерировать';
  }
}

// ── Style editor ──────────────────────────────────────────────────────────────
// Cached preset list (loaded once per session).
let _stylePresetsCache = null;

// Inline-style chips for the create-from-script modal. Renders the preset
// list as clickable pills (cinematic / pixar / anime / noir / photorealistic /
// custom). Selection writes into the hidden #import-style-type input that the
// submit handler reads. «Custom» reveals the description textarea below.
async function _hydrateImportStylePicker() {
  const container = document.getElementById('import-style-chips');
  if (!container) return;
  const customTa = document.getElementById('import-style-custom-desc');
  const hiddenInput = document.getElementById('import-style-type');
  if (!hiddenInput) return;
  const presets = await _loadStylePresets();
  // Always include 'custom' at the end of the list.
  const list = [...presets, { id: 'custom', name: 'Свой стиль', desc: '' }];
  hiddenInput.value = 'cinematic';  // default
  if (customTa) customTa.style.display = 'none';
  container.innerHTML = list.map(p => {
    const sel = (p.id === 'cinematic') ? ' selected' : '';
    return `<button type="button" class="import-style-chip${sel}" data-style="${p.id}"
      onclick="_pickImportStyle('${p.id}')"
      style="padding:5px 11px;font-size:0.82rem;border-radius:14px;border:1px solid var(--border);
             background:${p.id === 'cinematic' ? 'var(--accent)' : '#1a1a1f'};
             color:${p.id === 'cinematic' ? '#fff' : 'var(--text)'};
             cursor:pointer">${esc(p.name || p.id)}</button>`;
  }).join('');
}

function _pickImportStyle(id) {
  const hiddenInput = document.getElementById('import-style-type');
  const customTa = document.getElementById('import-style-custom-desc');
  if (hiddenInput) hiddenInput.value = id;
  document.querySelectorAll('.import-style-chip').forEach(btn => {
    const sel = (btn.dataset.style === id);
    btn.style.background = sel ? 'var(--accent)' : '#1a1a1f';
    btn.style.color = sel ? '#fff' : 'var(--text)';
  });
  if (customTa) customTa.style.display = (id === 'custom') ? '' : 'none';
}

async function _loadStylePresets() {
  if (_stylePresetsCache) return _stylePresetsCache;
  try {
    const r = await fetch('/api/style-presets').then(x => x.json());
    _stylePresetsCache = r.presets || [];
  } catch {
    _stylePresetsCache = [];
  }
  return _stylePresetsCache;
}

async function openStyleEditor() {
  const presets = await _loadStylePresets();
  const cur = (S.series && S.series.style) || {};
  const grid = document.getElementById('style-presets-grid');
  if (!grid) { openModal('modal-style'); return; }
  // Show admin-only "Регенерить сэмплы" button only to primary user.
  const adminBtn = document.getElementById('btn-regen-style-samples');
  if (adminBtn) {
    adminBtn.style.display = (window._currentUser && window._currentUser.is_primary) ? '' : 'none';
  }
  setVal('style-type', cur.type || 'cinematic');
  // Render preset cards + custom card at the end.
  grid.innerHTML = presets.map(p => `
    <div class="style-card ${cur.type === p.id ? 'selected' : ''}" data-preset="${esc(p.id)}" onclick="_styleCardSelect('${esc(p.id)}')">
      <div class="style-thumb">
        ${p.sample
          ? `<img src="${esc(p.sample)}" alt="${esc(p.label)}" onerror="this.parentElement.innerHTML='<span>нет сэмпла<br><small>(сгенерируй через 🎨 в админке)</small></span>'">`
          : `<span style="font-size:1.6rem">🎲</span>`}
      </div>
      <div class="style-info">
        <div class="name">${esc(p.label)}</div>
        ${p.desc ? `<div class="desc">${esc(p.desc.slice(0, 90))}${p.desc.length > 90 ? '…' : ''}</div>` : '<div class="desc">AI выберет стиль исходя из тона серии</div>'}
      </div>
    </div>`).join('') + `
    <div class="style-card custom-card ${cur.type === 'custom' ? 'selected' : ''}" data-preset="custom" onclick="_styleCardSelect('custom')">
      <div class="style-thumb" id="style-custom-preview">
        🎨
      </div>
      <div class="style-info">
        <div class="name">Свой стиль</div>
        <div class="desc">Опиши и сгенерь сэмпл</div>
      </div>
      <div class="custom-input">
        <textarea id="style-custom-desc" placeholder="Например: art-nouveau с сепией, гравюрная штриховка, стиль 1920-х" onclick="event.stopPropagation()" oninput="event.stopPropagation()">${esc(cur.custom_description || '')}</textarea>
        <button class="btn-sample" onclick="event.stopPropagation();_generateStyleSample()">🎨 Сгенерировать сэмпл</button>
      </div>
    </div>`;
  openModal('modal-style');
}

function _styleCardSelect(id) {
  document.querySelectorAll('.style-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.preset === id);
  });
  setVal('style-type', id);
}

async function _generateStyleSample() {
  const desc = (document.getElementById('style-custom-desc')?.value || '').trim();
  if (!desc) { alert('Опиши стиль текстом сначала'); return; }
  const btn = document.querySelector('.style-card.custom-card .btn-sample');
  const preview = document.getElementById('style-custom-preview');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую...'; }
  if (preview) preview.innerHTML = '<span class="spinner"></span>';
  try {
    const r = await api.post('/api/style-sample', { description: desc, base: 'man' });
    if (r.error) throw new Error(r.error);
    if (preview) preview.innerHTML = `<img src="${esc(r.url)}" alt="custom style sample" style="width:100%;height:100%;object-fit:cover;display:block">`;
    // Auto-select the custom card so save uses it.
    _styleCardSelect('custom');
  } catch (e) {
    if (preview) preview.innerHTML = `<span style="color:var(--danger);font-size:0.78rem">✗ ${esc(e?.message || e)}</span>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🎨 Сгенерировать сэмпл'; }
  }
}

async function saveStyle() {
  await api.put(`/api/series/${S.seriesId}`, {
    style: { type: val('style-type'), custom_description: val('style-custom-desc') }
  });
  S.series = await api.get(`/api/series/${S.seriesId}`);
  closeModal('modal-style');
  renderStyleSection();
}

// Primary-only: rebuild all baseline preset samples (cinematic, photorealistic,
// anime, pixar, noir) by calling AVAI 5 times. ~50s + ~$0.10. Saves images
// to static/img/style-samples/<id>.jpg so every user sees them in the picker.
async function regenerateAllStyleSamples() {
  if (!confirm('Сгенерировать все 5 базовых сэмплов через AVAI?\n\nЗаймёт ~50 секунд, потратит ~$0.10. После — карточки стилей у всех юзеров получат превью.')) return;
  const btn = document.getElementById('btn-regen-style-samples');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Генерирую (~50с)...'; }
  try {
    const r = await api.post('/api/admin/regenerate-style-samples', {});
    const ok = (r.results || []).filter(x => x.ok).length;
    const fail = (r.results || []).filter(x => !x.ok);
    let msg = `✓ Готово: ${ok}/${r.results?.length || 0} сэмплов сгенерировано`;
    if (fail.length) {
      msg += '\n\nОшибки:\n' + fail.map(x => `  • ${x.id}: ${x.err}`).join('\n');
    }
    alert(msg);
    // Force browser to refetch the new images by bumping cache buster
    bumpAssetVersion();
    // Refresh preset cache so new sample paths show
    _stylePresetsCache = null;
    closeModal('modal-style');
    setTimeout(() => openStyleEditor(), 200);
  } catch (e) {
    alert('Ошибка: ' + (e?.message || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Регенерить сэмплы'; }
  }
}

// Auto-prompt the user for a style choice after script-import or when a
// fresh series with no style picked yet opens. Called from importCreateSeries
// (right after navigating to the new series) and from loadSeriesView when
// style.type === 'cinematic' is the default-untouched value.
let _styleAutoPromptedFor = null;
async function maybePromptForStyle(force = false) {
  if (!S.series) return;
  const sid = S.seriesId;
  if (!force && _styleAutoPromptedFor === sid) return;
  // Skip if user already actively saved a non-default style choice.
  const cur = S.series.style || {};
  const isDefault = (cur.type === 'cinematic' && !cur.custom_description) || !cur.type;
  if (!force && !isDefault) return;
  _styleAutoPromptedFor = sid;
  setTimeout(() => openStyleEditor(), 400);
}

