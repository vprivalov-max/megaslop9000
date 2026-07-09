// ── Projects view ─────────────────────────────────────────────────────────────
let _allProjects = [];
let _filterColor = '';      // '' = any
let _filterStarred = false; // true = only starred
// Sort mode: persisted across reloads. Default keeps legacy behavior (pinned →
// alphabetic by directory order). Other modes still respect pinned-on-top.
const _SORT_MODES = new Set(['default', 'created_desc', 'created_asc', 'updated_desc', 'updated_asc']);
let _sortMode = (() => {
  try {
    const v = localStorage.getItem('projects_sort_mode') || 'default';
    return _SORT_MODES.has(v) ? v : 'default';
  } catch { return 'default'; }
})();

async function loadProjects() {
  _allProjects = await api.get('/api/series');
  applyProjectFilters();
  loadBalance();
  loadTopDramas();
  refreshGreenLightPlaybookStatus();
}

function _projectCreatedTs(s) {
  // created_at is an ISO string (UTC). Parse → epoch ms. Falsy/invalid → 0.
  const v = s.created_at;
  if (!v) return 0;
  const t = Date.parse(v);
  return Number.isFinite(t) ? t : 0;
}

function _sortProjects(list, mode) {
  const arr = list.slice();
  // Always keep pinned cards on top, regardless of sort mode — pinning would
  // be useless otherwise.
  const pinKey = s => (s.pinned ? 0 : 1);
  const pinTie = s => -(s.pinned_at || 0); // newer pin first within pinned group
  if (mode === 'created_desc') {
    arr.sort((a, b) => pinKey(a) - pinKey(b) || _projectCreatedTs(b) - _projectCreatedTs(a));
  } else if (mode === 'created_asc') {
    arr.sort((a, b) => pinKey(a) - pinKey(b) || _projectCreatedTs(a) - _projectCreatedTs(b));
  } else if (mode === 'updated_desc') {
    arr.sort((a, b) => pinKey(a) - pinKey(b) || (b._updated_at || 0) - (a._updated_at || 0));
  } else if (mode === 'updated_asc') {
    arr.sort((a, b) => pinKey(a) - pinKey(b) || (a._updated_at || 0) - (b._updated_at || 0));
  } else {
    // Default: pinned first (newer pin earlier), then keep server's alphabetic order.
    arr.sort((a, b) => pinKey(a) - pinKey(b) || pinTie(a) - pinTie(b));
  }
  return arr;
}

function applyProjectFilters() {
  let list = _allProjects;
  if (_filterColor) list = list.filter(s => (s.color || '') === _filterColor);
  if (_filterStarred) list = list.filter(s => !!s.starred);
  list = _sortProjects(list, _sortMode);
  renderProjects(list);
  // sync filter-bar visual state
  document.querySelectorAll('.color-filter-btn').forEach(b => {
    b.classList.toggle('active', (b.dataset.color || '') === _filterColor);
  });
  const starBtn = document.getElementById('star-filter-btn');
  if (starBtn) {
    starBtn.classList.toggle('active', _filterStarred);
    starBtn.innerHTML = _filterStarred ? '★ Только избранные' : '☆ Только избранные';
  }
  const sortSel = document.getElementById('sort-select');
  if (sortSel && sortSel.value !== _sortMode) sortSel.value = _sortMode;
}

function setColorFilter(color) {
  _filterColor = color || '';
  applyProjectFilters();
}

function toggleStarFilter() {
  _filterStarred = !_filterStarred;
  applyProjectFilters();
}

function setSortMode(mode) {
  _sortMode = _SORT_MODES.has(mode) ? mode : 'default';
  try { localStorage.setItem('projects_sort_mode', _sortMode); } catch {}
  applyProjectFilters();
}

// GREEN LIGHT model: a series is either flagged 'green' (GREEN LIGHT — proven
// strong statistics, fed into the writer's playbook) or has no mark. The legacy
// multi-color palette was retired.

function renderProjects(list) {
  const grid = document.getElementById('projects-grid');
  const empty = document.getElementById('projects-empty');
  if (!list.length) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    // Empty-state copy depends on whether filter is on
    const ep = empty.querySelector('p');
    if (ep) {
      if (_filterColor || _filterStarred) ep.innerHTML = 'Под этот фильтр ничего не подошло.<br>Сними фильтр или измени условия.';
      else ep.innerHTML = 'Ещё нет ни одного проекта.<br>Создай первый сериал!';
    }
    setupProjectDropzones();
    return;
  }
  empty.classList.add('hidden');
  grid.innerHTML = list.map(s => {
    const isGreenLight = (s.color || '') === 'green';
    const colorCls = isGreenLight ? ' color-green' : '';
    const starGlyph = s.starred ? '★' : '☆';
    const hasCover = !!s.cover_image;
    const coverUrl = hasCover
      ? `/assets/${s.id}/${s.cover_image}?v=${s.cover_image_version || 0}`
      : '';
    const coverCls = hasCover ? ' has-cover' : '';
    const coverStyle = hasCover ? ` style="background-image:url('${coverUrl}')"` : '';
    // Always-visible cover CTA: «🖼 Открыть обложку» when present (opens viewer),
    // «🎬 Сгенерить обложку» when missing (kicks off the generator). Sits at the
    // bottom of the card content so the user discovers it without hovering.
    const coverCta = hasCover
      ? `<button class="project-cover-cta has" type="button" onmousedown="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();openCoverViewer('${s.id}')" title="Открыть обложку, перегенерить или скачать">🖼 Обложка</button>`
      : `<button class="project-cover-cta" type="button" onmousedown="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();generateCoverFromCard('${s.id}', this)" title="Создать обложку через AVAI (3:4 JPEG, использует синопсис + главных героев)">🎬 Сгенерить обложку</button>`;
    return `
    <div class="project-card${s.pinned ? ' pinned' : ''}${colorCls}${coverCls}"${coverStyle} draggable="true" data-sid="${s.id}" data-title="${esc(s.title)}" onclick="if(event.target.closest('.project-delete-btn')||event.target.closest('.project-pin-btn')||event.target.closest('.project-star-btn')||event.target.closest('.project-greenlight-btn')||event.target.closest('.project-cover-btn')||event.target.closest('.project-cover-cta'))return;navigate('series',{seriesId:'${s.id}'})">
      <button class="project-pin-btn${s.pinned ? ' active' : ''}" type="button" onmousedown="event.stopPropagation()" ontouchstart="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();togglePin('${s.id}')" title="${s.pinned ? 'Открепить' : 'Закрепить вверху'}">${s.pinned ? '📌' : '📍'}</button>
      <div class="project-card-tools">
        <button class="project-star-btn${s.starred ? ' active' : ''}" type="button" onmousedown="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();toggleStar('${s.id}')" title="${s.starred ? 'Убрать из избранного' : 'В избранное'}">${starGlyph}</button>
        <button class="project-greenlight-btn${isGreenLight ? ' active' : ''}" type="button" onmousedown="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();toggleGreenLight('${s.id}')" title="${isGreenLight ? 'Снять GREEN LIGHT' : 'Пометить GREEN LIGHT — сильная статистика, идёт в обучение написателя'}">💚</button>
        <button class="project-delete-btn" type="button" onmousedown="event.stopPropagation()" ontouchstart="event.stopPropagation()" onclick="event.stopPropagation();event.preventDefault();confirmDeleteSeries('${s.id}','${esc(s.title)}')" title="Удалить">✕</button>
      </div>
      <div class="project-card-content">
        ${isGreenLight ? '<div class="greenlight-badge" title="GREEN LIGHT — подтверждённая сильная статистика; сценарии этой серии обучают написателя">GREEN LIGHT</div>' : ''}
        <h3>${esc(s.title)}</h3>
        <div class="meta">
          <span>${esc(s.genre || '—')}</span>
          <span>${esc(s.tone || '—')}</span>
        </div>
        <div class="ep-count">${s._episode_count}/${s._episode_total ?? s._episode_count} ${s.batch_mode ? `чанков × ${s.batch_size || 5}` : 'эп.'}</div>
        ${coverCta}
      </div>
      <div class="project-ep-badge" title="Готовых серий: ${s._episode_count} из ${s._episode_total ?? s._episode_count}">${s._episode_count}</div>
    </div>
  `;
  }).join('');

  // Wire drag handlers on freshly rendered cards
  grid.querySelectorAll('.project-card').forEach(card => {
    card.addEventListener('dragstart', onProjectDragStart);
    card.addEventListener('dragend',   onProjectDragEnd);
  });
  setupProjectDropzones();
}

async function toggleStar(sid) {
  const s = _allProjects.find(x => x.id === sid);
  const next = !(s && s.starred);
  // Optimistic update
  if (s) s.starred = next;
  applyProjectFilters();
  try {
    await api.post(`/api/series/${sid}/meta`, { starred: next });
  } catch (e) {
    if (s) s.starred = !next;
    applyProjectFilters();
    alert('Не удалось переключить избранное: ' + e.message);
  }
}

// GREEN LIGHT is a binary flag — toggle it directly, no color picker.
async function toggleGreenLight(sid) {
  const s = _allProjects.find(x => x.id === sid);
  const next = (s && (s.color || '') === 'green') ? '' : 'green';
  await setProjectColor(sid, next);
}

async function setProjectColor(sid, color) {
  const s = _allProjects.find(x => x.id === sid);
  const prev = s?.color || '';
  if (s) s.color = color;
  applyProjectFilters();
  try {
    await api.post(`/api/series/${sid}/meta`, { color });
  } catch (e) {
    if (s) s.color = prev;
    applyProjectFilters();
    alert('Не удалось задать цвет: ' + e.message);
  }
}

// ── GREEN LIGHT playbook (the writer's "training" signal) ───────────────────
function _formatPlaybookStatus(pb) {
  if (!pb || !pb.exists) return 'playbook не собран — нажми «Обучить написателя»';
  const src = pb.sources || {};
  const nGreen = (src.green_series || []).length;
  const nDramas = src.dramas_analyzed || 0;
  let when = '';
  try { when = pb.built_at ? new Date(pb.built_at).toLocaleString('ru-RU') : ''; } catch {}
  return `playbook: ${nGreen} GREEN LIGHT-серий + ${nDramas} драм${when ? ' · ' + when : ''}`;
}

async function refreshGreenLightPlaybookStatus() {
  const el = document.getElementById('greenlight-playbook-status');
  if (!el) return;
  try {
    const pb = await api.get('/api/greenlight/playbook');
    el.textContent = _formatPlaybookStatus(pb);
  } catch {
    el.textContent = '';
  }
}

async function rebuildGreenLightPlaybook(btn) {
  const statusEl = document.getElementById('greenlight-playbook-status');
  const original = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '📗 Обучаю…'; }
  if (statusEl) statusEl.textContent = 'дистиллирую приёмы из GREEN LIGHT-серий и анализов топ-драм…';
  try {
    const r = await api.post('/api/greenlight/playbook/rebuild', {}, { timeoutMs: 300_000 });
    if (r.error) throw new Error(r.error);
    if (statusEl) statusEl.textContent = _formatPlaybookStatus({ exists: true, built_at: r.built_at, sources: r.sources });
    showToast('✓ Написатель обучен на GREEN LIGHT-приёмах', 4000);
  } catch (e) {
    if (statusEl) statusEl.textContent = 'ошибка: ' + (e.message || e);
    showToast('Не удалось пересобрать playbook: ' + (e.message || e), 7000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = original; }
  }
}

// ── Series cover art (short-drama poster on the project card) ───────────────
// Cover lives at assets/cover.jpg inside the series dir; the project-card on
// the main menu uses it as its background, and a viewer modal opens for a
// closer look with Regenerate + Download.
let _coverViewerSid = null;

async function generateCoverFromCard(sid, btn) {
  // Card-level «🎬» button when no cover exists yet. Spinner on the button,
  // then refresh the project list so the new cover paints itself in.
  if (!sid) return;
  const original = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }
  showToast('🎨 Генерю обложку…', 4000);
  try {
    const r = await api.post(`/api/series/${sid}/cover/generate`, {}, { timeoutMs: 240_000 });
    if (r.error) throw new Error(r.error);
    // Sync local state so the next render picks up the new cover without a
    // full round-trip.
    const s = _allProjects.find(x => x.id === sid);
    if (s) {
      s.cover_image = r.url.replace(`/assets/${sid}/`, '').split('?')[0];
      s.cover_image_url = r.image_url || '';
      s.cover_image_version = r.image_version || Date.now();
    }
    applyProjectFilters();
    showToast('✓ Обложка готова', 3000);
  } catch (e) {
    showToast('Ошибка генерации обложки: ' + (e.message || e), 7000);
    if (btn) { btn.disabled = false; btn.innerHTML = original; }
  }
}

function openCoverViewer(sid) {
  const s = _allProjects.find(x => x.id === sid);
  if (!s || !s.cover_image) return;
  _coverViewerSid = sid;
  const url = `/assets/${sid}/${s.cover_image}?v=${s.cover_image_version || 0}`;
  document.getElementById('cover-viewer-title').textContent = s.title || 'Обложка';
  const img = document.getElementById('cover-viewer-img');
  img.src = url;
  img.alt = s.title || '';
  // Meta chips + synopsis are shown in English. We render whatever cached
  // English we have right now and, in parallel, fire one request that
  // translates whatever is missing and caches it on series.json. When the
  // promise resolves we just re-render with the fresh values.
  const metaEl = document.getElementById('cover-viewer-meta');
  const renderMeta = () => {
    // Genre only — tone & target audience are NOT shown here per user request.
    const genre = (s.genre_en || s.genre || '').trim();
    metaEl.innerHTML = genre ? `<span class="cv-meta-chip">${esc(genre)}</span>` : '';
  };
  renderMeta();
  // Synopsis is shown in English. If we already have a cached translation on
  // the series (synopsis_en / world_description_en), paint it immediately;
  // otherwise show the source while Claude Haiku translates in the
  // background, then swap in the translation.
  const synEl = document.getElementById('cover-viewer-synopsis');
  const renderSyn = (synText, worldText, isTranslating) => {
    let html = '';
    const block = (label, body, isTr) => `
      <div class="cv-syn-block">
        <div class="cv-syn-label">
          ${label}${isTr ? ' <span class="cv-translating">переводится…</span>' : ''}
          <button class="cv-copy-btn" type="button" onclick="copyCoverSynopsis(this)" title="Скопировать">📋</button>
        </div>
        <div class="cv-syn-body">${esc(body)}</div>
      </div>`;
    if (synText) html += block('Synopsis', synText, isTranslating);
    if (worldText) html += block('World', worldText, false);
    if (!html) html = '<div class="cv-syn-empty">No synopsis yet — add one in the series bible.</div>';
    synEl.innerHTML = html;
  };
  const synSrc = (s.synopsis || '').trim();
  const worldSrc = (s.world_description || '').trim();
  const synEnCached = (s.synopsis_en || '').trim();
  const worldEnCached = (s.world_description_en || '').trim();
  // Prefer cached English; fall back to source while waiting for translation.
  const initialSyn = synEnCached || synSrc;
  const initialWorld = worldEnCached || worldSrc;
  // Hit the endpoint whenever (a) any source field needs translating, or
  // (b) genre is still blank — the endpoint also infers a genre label from
  // the synopsis when it's missing, so we want to fetch even when nothing
  // needs translation.
  const needsTranslate =
    ['synopsis', 'world_description', 'genre']
      .some(f => (s[f] || '').trim() && !(s[f + '_en'] || '').trim())
    || (!(s.genre || '').trim() && (s.synopsis || '').trim());
  renderSyn(initialSyn, initialWorld, needsTranslate);
  if (needsTranslate) {
    api.get(`/api/series/${sid}/cover/synopsis-en`)
      .then(r => {
        if (_coverViewerSid !== sid) return;  // user closed / opened another
        // Cache every returned EN field back onto our in-memory series so
        // the next viewer-open hits the cache without a round-trip.
        if (s && r) {
          for (const k of Object.keys(r)) {
            if (r[k]) s[k] = r[k];
          }
        }
        renderSyn(r.synopsis_en || synSrc, r.world_description_en || worldSrc, false);
        renderMeta();
      })
      .catch(e => {
        if (_coverViewerSid !== sid) return;
        // Translation failed — leave source text in place, drop the spinner.
        renderSyn(initialSyn, initialWorld, false);
        console.warn('[cover viewer] synopsis translation failed:', e);
      });
  }
  document.getElementById('cover-viewer-status').textContent = '';
  // Wire the download button to point at the live URL.
  const dl = document.getElementById('cover-viewer-download');
  dl.href = url;
  // Force-download with a friendly filename instead of cover.jpg.
  const safe = (s.title || 'cover').replace(/[^\wЀ-ӿ -]+/g, '').replace(/\s+/g, '_') || 'cover';
  dl.download = `${safe}_cover.jpg`;
  openModal('modal-cover-viewer');
}

function closeCoverViewer() {
  closeModal('modal-cover-viewer');
  _coverViewerSid = null;
}

function copyCoverSynopsis(btn) {
  const block = btn.closest('.cv-syn-block');
  if (!block) return;
  const text = (block.querySelector('.cv-syn-body')?.textContent || '').trim();
  if (!text) return;
  const ok = (label) => {
    const orig = btn.innerHTML;
    btn.innerHTML = '✓';
    btn.classList.add('cv-copy-done');
    setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('cv-copy-done'); }, 1400);
    if (typeof showToast === 'function') showToast(label, 2000);
  };
  const fail = (e) => {
    if (typeof showToast === 'function') showToast('Не удалось скопировать: ' + (e?.message || e), 4000);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => ok('Скопировано в буфер'), fail);
  } else {
    // Fallback for older browsers / non-https contexts.
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      ok('Скопировано в буфер');
    } catch (e) { fail(e); }
  }
}

async function regenerateCoverFromViewer() {
  const sid = _coverViewerSid;
  if (!sid) return;
  const statusEl = document.getElementById('cover-viewer-status');
  const regenBtn = document.getElementById('cover-viewer-regen');
  const img = document.getElementById('cover-viewer-img');
  const original = regenBtn.innerHTML;
  regenBtn.disabled = true;
  regenBtn.innerHTML = '<span class="spinner"></span> Перегенерирую…';
  statusEl.textContent = 'AVAI рисует новую версию (10-40с)…';
  img.style.opacity = '0.45';
  try {
    // Explicit «Regenerate» = user wants a genuinely different look, so force
    // a fresh per-series art-direction brief (new palette/composition/font),
    // not just a reroll of the same recipe.
    const r = await api.post(`/api/series/${sid}/cover/generate`, { new_art_direction: true }, { timeoutMs: 240_000 });
    if (r.error) throw new Error(r.error);
    const s = _allProjects.find(x => x.id === sid);
    if (s) {
      s.cover_image = r.url.replace(`/assets/${sid}/`, '').split('?')[0];
      s.cover_image_url = r.image_url || '';
      s.cover_image_version = r.image_version || Date.now();
    }
    const url = `/assets/${sid}/${s.cover_image}?v=${s.cover_image_version}`;
    img.src = url;
    img.style.opacity = '1';
    const dl = document.getElementById('cover-viewer-download');
    dl.href = url;
    statusEl.innerHTML = '<span style="color:#4ade80">✓ Готово</span>';
    applyProjectFilters();
  } catch (e) {
    img.style.opacity = '1';
    statusEl.innerHTML = `<span style="color:#f87171">Ошибка: ${esc(e.message || e)}</span>`;
  } finally {
    regenBtn.disabled = false;
    regenBtn.innerHTML = original;
  }
}

// ── Drag & drop: archive / trash ─────────────────────────────────────────────
let _draggingProject = null;

function onProjectDragStart(e) {
  const card = e.currentTarget;
  _draggingProject = { sid: card.dataset.sid, title: card.dataset.title };
  card.classList.add('dragging');
  try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', card.dataset.sid); } catch (_) {}
  document.body.classList.add('drag-active');
}

function onProjectDragEnd(e) {
  e.currentTarget.classList.remove('dragging');
  document.body.classList.remove('drag-active');
  document.querySelectorAll('.dropzone-btn').forEach(z => z.classList.remove('drop-hover'));
  _draggingProject = null;
}

let _dropzonesWired = false;
function setupProjectDropzones() {
  if (_dropzonesWired) return;
  _dropzonesWired = true;
  const archiveZone = document.getElementById('dz-archive');
  const trashZone   = document.getElementById('dz-trash');
  [archiveZone, trashZone].forEach(zone => {
    if (!zone) return;
    zone.addEventListener('dragover',  e => { if (_draggingProject) { e.preventDefault(); zone.classList.add('drop-hover'); } });
    zone.addEventListener('dragleave', () => zone.classList.remove('drop-hover'));
    zone.addEventListener('drop',      e => {
      if (!_draggingProject) return;
      e.preventDefault();
      zone.classList.remove('drop-hover');
      const item = _draggingProject;
      if (zone.id === 'dz-archive') archiveProject(item.sid, item.title);
      else trashProject(item.sid, item.title);
    });
  });
}

async function archiveProject(sid, title) {
  try {
    await api.post(`/api/series/${sid}/archive`, { archived: true });
    showToast(`«${title}» отправлен в архив`);
    loadProjects();
  } catch (e) { alert('Не удалось архивировать: ' + e.message); }
}

async function trashProject(sid, title) {
  const ok = confirm(
    `⚠️ УДАЛИТЬ НАВСЕГДА «${title}»?\n\n` +
    `Будут стёрты с диска ВСЕ файлы сериала: серии, скрипты, референсы, локации, видео.\n\n` +
    `Это действие НЕЛЬЗЯ отменить. Продолжить?`
  );
  if (!ok) return;
  try {
    await api.del(`/api/series/${sid}`);
    showToast(`«${title}» удалён навсегда`);
    loadProjects();
  } catch (e) { alert('Не удалось удалить: ' + e.message); }
}

// ── Archive view ─────────────────────────────────────────────────────────────
async function openArchiveModal() {
  openModal('modal-archive');
  await renderArchiveList();
}

async function renderArchiveList() {
  const list = document.getElementById('archive-list');
  const empty = document.getElementById('archive-empty');
  list.innerHTML = '<p class="muted">Загружаем…</p>';
  empty.classList.add('hidden');
  try {
    const items = await api.get('/api/series?archived=1');
    if (!items.length) {
      list.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    list.innerHTML = items.map(s => `
      <div class="archive-item">
        <div class="archive-info">
          <div class="archive-title">${esc(s.title)}</div>
          <div class="archive-meta">${esc(s.genre || '—')} · ${esc(s.tone || '—')} · ${s._episode_count} эп.</div>
        </div>
        <div class="archive-actions">
          <button class="btn-ghost btn-sm" onclick="restoreFromArchive('${s.id}','${esc(s.title)}')">↩ Вернуть</button>
          <button class="btn-ghost btn-sm danger" onclick="trashFromArchive('${s.id}','${esc(s.title)}')">🗑 Удалить навсегда</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = `<p class="muted">Ошибка: ${esc(e.message)}</p>`;
  }
}

async function restoreFromArchive(sid, title) {
  try {
    await api.post(`/api/series/${sid}/archive`, { archived: false });
    showToast(`«${title}» возвращён из архива`);
    await renderArchiveList();
    loadProjects();
  } catch (e) { alert('Не удалось вернуть: ' + e.message); }
}

async function trashFromArchive(sid, title) {
  const ok = confirm(`⚠️ УДАЛИТЬ НАВСЕГДА «${title}»?\n\nВсе файлы сериала будут стёрты с диска. Это действие нельзя отменить.`);
  if (!ok) return;
  try {
    await api.del(`/api/series/${sid}`);
    showToast(`«${title}» удалён`);
    await renderArchiveList();
    loadProjects();
  } catch (e) { alert('Не удалось удалить: ' + e.message); }
}

async function togglePin(sid) {
  try {
    await api.post(`/api/series/${sid}/pin`, {});
    loadProjects();
  } catch (e) { alert('Не удалось переключить закрепление: ' + e.message); }
}

async function confirmDeleteSeries(sid, title) {
  if (!confirm(`Удалить сериал «${title}»? Это действие нельзя отменить.`)) return;
  await api.del(`/api/series/${sid}`);
  loadProjects();
}

function openCreateSeries() {
  window._pendingSeriesOutline = null;
  window._pendingAsianRecast = false;
  clearFields(['series-idea-input','new-series-title','new-series-genre','new-series-tone','new-series-audience','new-series-world','new-series-synopsis']);
  document.getElementById('series-ideas-list').classList.add('hidden');
  document.getElementById('series-ideas-list').innerHTML = '';
  document.getElementById('series-gen-status').textContent = '';
  const autogen = document.getElementById('new-series-autogen');
  if (autogen) autogen.checked = true;
  const singleMode = document.querySelector('input[name="new-series-mode"][value="single"]');
  if (singleMode) singleMode.checked = true;
  buildGenreFilters();
  buildBeatConstructor();   // async — renders the ordered scenario-beat constructor
  // Clear UI-only state for import mode; content restored from draft below
  document.getElementById('import-series-preview').innerHTML = '';
  // Reset per-type extraction toggles + picked-file chips when modal reopens
  // so the previous session's state doesn't leak into the next series.
  for (const id of ['import-extract-characters','import-extract-locations','import-extract-items']) {
    const el = document.getElementById(id); if (el) el.checked = true;
  }
  try { _importResetPickedFiles(); } catch (_) {}
  try { _hydrateImportStylePicker(); } catch (_) {}
  // Reset clone-mode fields so a previous session's edits don't leak in.
  for (const id of ['clone-new-title','clone-revision-instructions']) {
    const el = document.getElementById(id); if (el) el.value = '';
  }
  const cloneCount = document.getElementById('clone-episodes-count'); if (cloneCount) cloneCount.value = '0';
  const cloneInfo = document.getElementById('clone-source-info'); if (cloneInfo) cloneInfo.textContent = '';
  const cloneStatus = document.getElementById('clone-create-status'); if (cloneStatus) cloneStatus.textContent = '';
  setSeriesCreateMode('generate');
  // Hydrate the «🚫 Не предлагать» field from localStorage so user sees their
  // persisted blocked-tropes list immediately on modal open (no need to
  // expand <details> first, no need to click into the textarea first).
  // Reset .hydrated flag so the helper re-runs even if it ran in a prior session.
  const avoidEl = document.getElementById('series-ideas-avoid');
  if (avoidEl) {
    delete avoidEl.dataset.hydrated;
    avoidEl.value = '';
    _seriesIdeasAvoidHydrate(avoidEl);
  }
  openModal('modal-create-series');
  // Restore import-mode draft and wire autosave
  _createSeriesDraftRestore();
  _createSeriesDraftWire();
  // Update char counter if script was restored
  const importScriptEl = document.getElementById('import-series-script');
  const importStatsEl  = document.getElementById('import-series-script-stats');
  if (importScriptEl && importStatsEl)
    importStatsEl.textContent = (importScriptEl.value.length || 0).toLocaleString('ru') + ' символов';
}

