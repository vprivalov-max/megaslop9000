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

