// ── Scenario constructor: ordered hook-beats (ноды) ──────────────────────────
// An ORDERED sequence the user assembles. Catalog comes from GET /api/story-beats
// (single source of truth — the English "beat" directives live server-side).
// `_beatSeq` is the ordered selection: [{token, ru}] where token is a catalog id
// or a custom free-text string. Order is significant. Cap _BEAT_MAX.
const _BEAT_MAX = 8;
let _beatCatalog = null;
let _beatSeq = [];

async function buildBeatConstructor() {
  _beatSeq = [];   // reset on every modal open
  const palette = document.getElementById('beat-palette');
  const input = document.getElementById('beat-custom-input');
  if (input) input.value = '';
  if (!palette) return;
  if (!_beatCatalog) {
    try {
      _beatCatalog = await api.get('/api/story-beats');
    } catch (e) {
      palette.innerHTML = '<span style="font-size:0.78rem;color:var(--danger)">Не удалось загрузить каталог нод</span>';
      return;
    }
  }
  // Group by `group`, preserving first-seen order.
  const groups = [];
  const byGroup = {};
  for (const o of _beatCatalog) {
    if (!byGroup[o.group]) { byGroup[o.group] = []; groups.push(o.group); }
    byGroup[o.group].push(o);
  }
  palette.innerHTML = '';
  for (const g of groups) {
    const head = document.createElement('div');
    head.style.cssText = 'flex-basis:100%;font-size:0.72rem;color:var(--muted);margin:4px 0 2px';
    head.textContent = g;
    palette.appendChild(head);
    for (const o of byGroup[g]) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'genre-chip';
      chip.dataset.token = o.id;
      chip.textContent = '+ ' + o.ru;
      chip.style.cssText = 'cursor:pointer';
      chip.addEventListener('click', () => addBeat(o.id, o.ru));
      palette.appendChild(chip);
    }
  }
  renderBeatSequence();
}

// Resolve a catalog token to its RU label (for re-adds / persistence display).
function _beatLabel(token) {
  const hit = (_beatCatalog || []).find(o => o.id === token);
  return hit ? hit.ru : token;
}

function addBeat(token, ru) {
  token = (token || '').trim();
  if (!token) return;
  if (_beatSeq.length >= _BEAT_MAX) {
    showToast(`Максимум ${_BEAT_MAX} нод в последовательности`, 2500);
    return;
  }
  // Dedup (case-insensitive on token).
  if (_beatSeq.some(b => b.token.toLowerCase() === token.toLowerCase())) {
    showToast('Эта нода уже в последовательности', 2000);
    return;
  }
  _beatSeq.push({ token, ru: ru || _beatLabel(token) });
  renderBeatSequence();
}

function addCustomBeat() {
  const input = document.getElementById('beat-custom-input');
  if (!input) return;
  const v = (input.value || '').trim();
  if (!v) return;
  addBeat(v, v);
  input.value = '';
  input.focus();
}

function removeBeat(idx) {
  _beatSeq.splice(idx, 1);
  renderBeatSequence();
}

function moveBeat(idx, dir) {
  const j = idx + dir;
  if (j < 0 || j >= _beatSeq.length) return;
  [_beatSeq[idx], _beatSeq[j]] = [_beatSeq[j], _beatSeq[idx]];
  renderBeatSequence();
}

function renderBeatSequence() {
  const list = document.getElementById('beat-sequence-list');
  const count = document.getElementById('beat-count');
  if (count) count.textContent = _beatSeq.length ? `(${_beatSeq.length})` : '';
  if (!list) return;
  if (!_beatSeq.length) {
    list.innerHTML = '<div style="font-size:0.76rem;color:var(--muted);font-style:italic">Последовательность пуста — добавь ноды из каталога ниже или впиши свою. Порядок = порядок развития сюжета.</div>';
    return;
  }
  list.innerHTML = _beatSeq.map((b, i) => `
    <div style="display:flex;align-items:center;gap:6px;background:#1a1a1f;border:1px solid #333;border-radius:6px;padding:4px 8px">
      <span style="color:var(--accent,#845ef7);font-weight:700;min-width:18px">${i + 1}</span>
      <span style="flex:1;font-size:0.84rem">${esc(b.ru)}${b.token && _beatLabel(b.token) !== b.ru ? '' : (!_beatCatalog || !(_beatCatalog.some(o => o.id === b.token)) ? ' <span style=\"font-size:0.7rem;color:var(--muted)\">(своя)</span>' : '')}</span>
      <button type="button" title="Вверх" onclick="moveBeat(${i},-1)" ${i === 0 ? 'disabled' : ''} style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:0.9rem;padding:0 3px">↑</button>
      <button type="button" title="Вниз" onclick="moveBeat(${i},1)" ${i === _beatSeq.length - 1 ? 'disabled' : ''} style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:0.9rem;padding:0 3px">↓</button>
      <button type="button" title="Удалить" onclick="removeBeat(${i})" style="background:none;border:none;color:var(--danger,#e5484d);cursor:pointer;font-size:0.95rem;padding:0 3px">✕</button>
    </div>
  `).join('');
}

// Ordered list of tokens (catalog ids or custom strings) for the backend.
function getSelectedBeats() {
  return _beatSeq.map(b => b.token);
}

function randomizeGenres() {
  // Pick 2-4 random genres, uncheck everything else
  const chips = [...document.querySelectorAll('.genre-chip')];
  if (!chips.length) return;
  const count = 2 + Math.floor(Math.random() * 3); // 2, 3, or 4
  const indices = [...chips.keys()];
  // Fisher-Yates shuffle, take first `count`
  for (let i = indices.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [indices[i], indices[j]] = [indices[j], indices[i]];
  }
  const picked = new Set(indices.slice(0, count));
  chips.forEach((chip, idx) => {
    const cb = chip.querySelector('input');
    cb.checked = picked.has(idx);
    chip.classList.toggle('genre-chip-checked', cb.checked);
  });
}

// ── «🚫 Не предлагать» avoid-list persistence ────────────────────────────
// Stored in localStorage under series_ideas_avoid. Hydrated on focus AND on
// every modal-open. Auto-saved on every keystroke so user never loses input.
// First-ever load gets a sensible default (the «классические троп» the user
// is most often sick of) so the field doesn't feel empty on a fresh install.
const _SERIES_IDEAS_AVOID_DEFAULT = 'близнецы, пастор, повар, спорт, tape, livestream, fashion, gallery';

function _seriesIdeasAvoidHydrate(el) {
  if (!el) el = document.getElementById('series-ideas-avoid');
  if (!el || el.dataset.hydrated === '1') return;
  try {
    let stored = localStorage.getItem('series_ideas_avoid');
    // First-ever load (key absent) → seed with the default. After that the
    // user's edits stick — explicit empty string is respected.
    if (stored === null) {
      stored = _SERIES_IDEAS_AVOID_DEFAULT;
      localStorage.setItem('series_ideas_avoid', stored);
    }
    if (!el.value) el.value = stored;
  } catch {}
  el.dataset.hydrated = '1';
}

function _seriesIdeasAvoidSave(el) {
  if (!el) return;
  try { localStorage.setItem('series_ideas_avoid', el.value || ''); } catch {}
  el.dataset.hydrated = '1';
}

async function generateSeriesIdeas() {
  const btn = document.getElementById('btn-gen-ideas');
  const status = document.getElementById('series-gen-status');
  const list = document.getElementById('series-ideas-list');
  const genres = getSelectedGenres();
  const idea = (val('series-idea-input') || '').trim();
  // Avoid-list — user-curated tropes/words to never suggest. Stored in
  // localStorage between sessions so they don't have to retype every time.
  const avoidEl = document.getElementById('series-ideas-avoid');
  if (avoidEl) {
    _seriesIdeasAvoidHydrate(avoidEl);   // safety: if user never opened the field
    _seriesIdeasAvoidSave(avoidEl);      // safety: capture any unsaved keystrokes
  }
  const avoid = (avoidEl?.value || '').trim();
  // No genres = full creative freedom across all 6 axes (settings/twists/
  // premises/protag/antag/tones). Backend handles empty genres list fine —
  // see /api/generate-series-ideas: genre_rule is empty when genres=[].
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = '';
  list.classList.add('hidden');
  list.innerHTML = '';
  try {
    const model = _selectedWriterModel('writer-model-create');
    const ideas = await api.post('/api/generate-series-ideas', { genres, avoid, model, idea, beats: getSelectedBeats(), format_mode: getFormatMode(), ...getEraSetting() });
    list.innerHTML = ideas.map((idea, i) => `
      <div class="idea-card" onclick="pickSeriesIdea(${i})">
        <div class="idea-card-title">${esc(idea.title)}</div>
        <div class="idea-card-meta">${esc(idea.genre)} · ${esc(idea.tone)} · ${esc(idea.target_audience)}</div>
        <div class="idea-card-synopsis">${esc(idea.synopsis_ru || idea.synopsis)}</div>
      </div>
    `).join('');
    list._ideas = ideas;
    list.classList.remove('hidden');
    status.textContent = 'Выбери идею — поля заполнятся автоматически';
    status.style.color = 'var(--muted)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
    btn.innerHTML = '💡 5 идей на выбор';
  }
}

function pickSeriesIdea(index) {
  const list = document.getElementById('series-ideas-list');
  const idea = list._ideas?.[index];
  if (!idea) return;
  fillSeriesForm(idea);
  list.classList.add('hidden');
  const status = document.getElementById('series-gen-status');
  status.textContent = '✓ Поля заполнены — проверь и отредактируй если нужно';
  status.style.color = 'var(--success)';
}

function fillSeriesForm(data) {
  if (data.title)            setVal('new-series-title', data.title);
  if (data.genre)            setVal('new-series-genre', data.genre);
  if (data.tone)             setVal('new-series-tone', data.tone);
  if (data.target_audience)  setVal('new-series-audience', data.target_audience);
  if (data.world_description) setVal('new-series-world', data.world_description);
  if (data.synopsis)         setVal('new-series-synopsis', data.synopsis);
}

async function findTopDramas() {
  const btn = document.getElementById('btn-find-top-dramas');
  const status = document.getElementById('series-gen-status');
  const wrap = document.getElementById('top-dramas-list');
  const ideasList = document.getElementById('series-ideas-list');
  const genres = getSelectedGenres();
  const idea = (val('series-idea-input') || '').trim();
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Ищем топ...';
  status.textContent = '';
  if (ideasList) ideasList.classList.add('hidden');
  wrap.classList.add('hidden');
  wrap.innerHTML = '';
  try {
    const res = await api.post('/api/research-top-dramas', { genres, idea, ...getEraSetting() });
    const dramas = (res && res.dramas) || [];
    if (!dramas.length) {
      status.textContent = 'Ничего не нашлось — попробуй ещё раз';
      status.style.color = 'var(--danger)';
      return;
    }
    wrap.innerHTML = dramas.map((d, i) => `
      <div class="idea-card">
        <div class="idea-card-title">${esc(d.title)}${d.popularity ? ` <span style="font-size:0.72rem;color:#ff922b;font-weight:600">${esc(d.popularity)}</span>` : ''}</div>
        <div class="idea-card-meta">${esc(d.genre)}${d.why_hook ? ' · ' + esc(d.why_hook) : ''}</div>
        <div class="idea-card-synopsis">${esc(d.premise_ru || d.premise)}</div>
        <button onclick="makeSimilarFromDrama(${i})" style="margin-top:10px;width:100%;padding:8px;border:none;border-radius:8px;background:linear-gradient(135deg,#845ef7,#5c7cfa);color:#fff;font-weight:600;cursor:pointer">🎬 Сделать подобный сериал</button>
      </div>
    `).join('');
    wrap._dramas = dramas;
    wrap.classList.remove('hidden');
    status.textContent = 'Выбери драму — сгенерируем 5 идей в её духе';
    status.style.color = 'var(--muted)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Reads the "Азиатская адаптация" toggle (either the board checkbox or the
// picker checkbox). When on: the source plot is kept 1-to-1 but every character
// is Asian and the world is re-set to an East-Asian setting.
function _dramaAsianRecast() {
  return !!(document.getElementById('drama-asian-recast')?.checked
         || document.getElementById('drama-asian-recast-picker')?.checked);
}

async function makeSimilarFromDrama(index) {
  const wrap = document.getElementById('top-dramas-list');
  const d = wrap._dramas?.[index];
  if (!d) return;
  const status = document.getElementById('series-gen-status');
  const list = document.getElementById('series-ideas-list');
  wrap.classList.add('hidden');
  list.classList.add('hidden');
  list.innerHTML = '';
  const asian = _dramaAsianRecast();
  // 1-TO-1: take the chosen hit's premise verbatim, retitle it, fill the form.
  status.innerHTML = '<span class="spinner"></span> Готовим идею «' + esc(d.title) + '»' + (asian ? ' (азиатская адаптация)' : ' 1-в-1') + '...';
  status.style.color = 'var(--muted)';
  try {
    const genres = getSelectedGenres();
    const model = _selectedWriterModel('writer-model-create');
    const ideas = await api.post('/api/ideas-from-drama', { drama: d, genres, model, asian_recast: asian, ...getEraSetting() });
    const idea = Array.isArray(ideas) ? ideas[0] : ideas;
    if (!idea) throw new Error('пустой ответ');
    fillSeriesForm(idea);
    window._pendingAsianRecast = asian;
    status.textContent = '✓ Идея «' + d.title + '» загружена' + (asian ? ' (азиатская адаптация)' : ' 1-в-1') + ' — проверь поля и жми «Создать сериал»';
    status.style.color = 'var(--success)';
  } catch (e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.style.color = 'var(--danger)';
  }
}

// ── Persistent top-dramas board (main page) ───────────────────────────────
async function loadTopDramas() {
  const board = document.getElementById('top-dramas-board');
  if (!board) return;
  try {
    const store = await api.get('/api/top-dramas');
    renderTopDramasBoard(store);
  } catch (e) { /* silent on load */ }
}

function renderTopDramasBoard(store) {
  const board = document.getElementById('top-dramas-board');
  const meta = document.getElementById('top-dramas-meta');
  if (!board) return;
  const dramas = (store && store.dramas) || [];
  window._topDramas = dramas;
  if (meta) {
    if (store && store.scanned_at) {
      const dt = new Date(store.scanned_at);
      meta.textContent = isNaN(dt) ? '' : '· обновлено ' + dt.toLocaleString('ru-RU');
    } else meta.textContent = '';
  }
  if (!dramas.length) {
    board.innerHTML = '<div style="color:var(--muted);font-size:0.85rem;grid-column:1/-1">Список пуст — нажми «Обновить список», чтобы найти топовые шортдраммы.</div>';
    return;
  }
  board.innerHTML = dramas.map((d, i) => `
    <div class="idea-card" style="display:flex;flex-direction:column;gap:6px">
      <div class="idea-card-title">${esc(d.title)}${d.popularity ? ` <span style="font-size:0.7rem;color:#ff922b;font-weight:600">${esc(d.popularity)}</span>` : ''}</div>
      <div class="idea-card-meta">${esc(d.genre)}${d.why_hook ? ' · ' + esc(d.why_hook) : ''}</div>
      <div class="idea-card-synopsis">${esc(d.premise_ru || d.premise)}</div>
      <div id="topdrama-analysis-${i}"></div>
      <div style="display:flex;gap:8px;margin-top:6px">
        <button onclick="analyzeTopDrama(${i})" style="flex:1;padding:7px;border:1px solid #845ef7;border-radius:8px;background:transparent;color:#b39df5;font-weight:600;cursor:pointer;font-size:0.8rem">🔍 Изучить подробнее</button>
        <button onclick="makeSeriesFromTopDrama(${i})" style="flex:1;padding:7px;border:none;border-radius:8px;background:linear-gradient(135deg,#845ef7,#5c7cfa);color:#fff;font-weight:600;cursor:pointer;font-size:0.8rem">🎬 Сделать сериал</button>
      </div>
    </div>
  `).join('');
  dramas.forEach((d, i) => { if (d.analysis) _renderDramaAnalysis(i, d.analysis); });
}

async function refreshTopDramas() {
  const btn = document.getElementById('btn-refresh-top-dramas');
  const status = document.getElementById('top-dramas-status');
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Ищем...'; }
  if (status) { status.textContent = 'Сканируем рынок топовых шортдрамм (~1 мин)...'; status.style.color = 'var(--muted)'; }
  try {
    const store = await api.post('/api/top-dramas/scan', {});
    renderTopDramasBoard(store);
    if (status) status.textContent = 'Готово — найдено ' + ((store.dramas || []).length) + ' шортдрамм.';
  } catch (e) {
    if (status) { status.textContent = 'Ошибка: ' + e.message; status.style.color = 'var(--danger)'; }
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

async function analyzeTopDrama(index) {
  const d = (window._topDramas || [])[index];
  if (!d) return;
  const panel = document.getElementById('topdrama-analysis-' + index);
  if (d.analysis) { _renderDramaAnalysis(index, d.analysis); return; }
  if (panel) panel.innerHTML = '<div style="color:var(--muted);font-size:0.8rem;padding:6px 0">🔍 Изучаем драму подробно (~1 мин)...</div>';
  try {
    const res = await api.post('/api/top-dramas/analyze', { id: d.id, drama: d });
    d.analysis = res.analysis;
    _renderDramaAnalysis(index, res.analysis);
  } catch (e) {
    if (panel) panel.innerHTML = '<div style="color:var(--danger);font-size:0.8rem">Ошибка: ' + esc(e.message) + '</div>';
  }
}

function _renderDramaAnalysis(index, a) {
  const panel = document.getElementById('topdrama-analysis-' + index);
  if (!panel || !a) return;
  const eps = (a.first_5_episodes || []).map(e => `<li>${esc(e)}</li>`).join('');
  const chars = (a.main_characters || []).map(c => `<li>${esc(c)}</li>`).join('');
  panel.innerHTML = `
    <div style="margin-top:8px;padding:10px;background:rgba(132,94,247,0.06);border-radius:8px;font-size:0.82rem;line-height:1.5">
      <div style="font-weight:600;color:#b39df5;margin-bottom:4px">Детальный синопсис</div>
      <div style="margin-bottom:8px">${esc(a.detailed_synopsis_ru || a.detailed_synopsis)}</div>
      ${a.central_conflict ? `<div><b>Конфликт:</b> ${esc(a.central_conflict)}</div>` : ''}
      ${chars ? `<div style="margin-top:6px"><b>Персонажи:</b><ul style="margin:4px 0 0 16px;padding:0">${chars}</ul></div>` : ''}
      ${eps ? `<div style="margin-top:6px"><b>Завязка (первые 5 серий):</b><ol style="margin:4px 0 0 16px;padding:0">${eps}</ol></div>` : ''}
    </div>`;
}

async function makeSeriesFromTopDrama(index) {
  const d = (window._topDramas || [])[index];
  if (!d) return;
  const a = d.analysis;
  const premise = a ? (a.detailed_synopsis || d.premise || '') : (d.premise || '');
  const premise_ru = a ? (a.detailed_synopsis_ru || d.premise_ru || '') : (d.premise_ru || '');
  const status = document.getElementById('top-dramas-status');
  const asian = _dramaAsianRecast();
  if (status) { status.textContent = 'Готовим сериал на основе «' + d.title + '»' + (asian ? ' (азиатская адаптация)' : '') + '...'; status.style.color = 'var(--muted)'; }
  try {
    const ideas = await api.post('/api/ideas-from-drama', { drama: { title: d.title, genre: d.genre, premise, premise_ru }, asian_recast: asian });
    const idea = Array.isArray(ideas) ? ideas[0] : ideas;
    if (!idea) throw new Error('пустой ответ');
    openCreateSeries();
    fillSeriesForm(idea);
    const outline = (a && Array.isArray(a.first_5_episodes) && a.first_5_episodes.length) ? a.first_5_episodes : null;
    window._pendingSeriesOutline = outline;
    window._pendingAsianRecast = asian;
    // Remember which drama this series is adapted from, so the created series can
    // later be continued by re-analyzing the drama's next episodes (6-10, …).
    window._pendingSourceDrama = { id: d.id, title: d.title, genre: d.genre, premise: (premise_ru || premise || ''), attribution: 'exact' };
    if (status) status.textContent = '';
    const gs = document.getElementById('series-gen-status');
    if (gs) {
      const asianNote = asian ? ' Все персонажи — азиаты, азиатская тематика (сюжет сохранён).' : '';
      gs.textContent = outline
        ? '✓ Идея «' + d.title + '» загружена.' + asianNote + ' Первые ' + outline.length + ' серий напишутся по разбору (с новыми именами) — жми «Создать сериал»'
        : '✓ Идея «' + d.title + '» загружена' + (asian ? ' (азиатская адаптация).' : '') + ' — проверь поля и жми «Создать сериал»';
      gs.style.color = 'var(--success)';
    }
  } catch (e) {
    if (status) { status.textContent = 'Ошибка: ' + e.message; status.style.color = 'var(--danger)'; }
  }
}

// Format mode picker — short_drama (TikTok serial) vs instagram_series (sitcom-style).
// Sets the hidden input + visually toggles the two pill buttons. Read by createSeries,
// generateFromIdea, generateSeriesIdeas so the chosen mode flows into all backend generators.
function setFormatMode(mode) {
  if (mode !== 'short_drama' && mode !== 'instagram_series') mode = 'short_drama';
  const hidden = document.getElementById('new-series-format-mode');
  if (hidden) hidden.value = mode;
  ['short_drama', 'instagram_series'].forEach(m => {
    const btn = document.getElementById(`format-mode-${m}-btn`);
    if (btn) btn.classList.toggle('active', m === mode);
  });
}

function getFormatMode() {
  return document.getElementById('new-series-format-mode')?.value || 'short_drama';
}

// ── Era + world setting (create-series modal) ────────────────────────────
// Show/hide the free-text input when «Своя…» is picked.
function _toggleCustomEra() {
  const sel = document.getElementById('series-era');
  const inp = document.getElementById('series-era-custom');
  if (!sel || !inp) return;
  inp.classList.toggle('hidden', sel.value !== '__custom__');
  if (sel.value === '__custom__') inp.focus();
}
function _toggleCustomWorld() {
  const sel = document.getElementById('series-world');
  const inp = document.getElementById('series-world-custom');
  if (!sel || !inp) return;
  inp.classList.toggle('hidden', sel.value !== '__custom__');
  if (sel.value === '__custom__') inp.focus();
}
// Returns the era/setting payload for the idea generators. Defaults to
// modern + realistic (backend treats that as «no directive»).
function getEraSetting() {
  return {
    era:          document.getElementById('series-era')?.value || 'modern',
    era_custom:   (document.getElementById('series-era-custom')?.value || '').trim(),
    world_setting: document.getElementById('series-world')?.value || 'realistic',
    world_custom: (document.getElementById('series-world-custom')?.value || '').trim(),
  };
}

function _autoWriteFirstEpisodes(n, tries) {
  tries = tries || 0;
  const btn = document.getElementById('append-gen-start-btn');
  const cnt = document.getElementById('append-gen-count');
  // Series view loads async — poll until the append panel exists (max ~12s).
  if (!btn || !cnt) {
    if (tries < 40) { setTimeout(() => _autoWriteFirstEpisodes(n, tries + 1), 300); }
    return;
  }
  cnt.value = String(n);
  try { btn.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch (_) {}
  // appendGenerateScript no-confirms on an empty textarea (brand-new series).
  appendGenerateScript(btn);
}

async function createSeries() {
  const title = val('new-series-title');
  if (!title) return alert('Введи название');
  const autogen = document.getElementById('new-series-autogen')?.checked ?? true;
  // Episode duration target — clamp to writer-safe range [30, 240] to match
  // the bible modal validation. Empty/blank → fall back to server default (60s).
  const _durRaw = val('new-series-target-duration');
  const target_duration_sec = _durRaw ? Math.max(30, Math.min(240, parseInt(_durRaw, 10))) : null;
  try {
    const data = await api.post('/api/series', {
      title, genre: val('new-series-genre'), tone: val('new-series-tone'),
      target_audience: val('new-series-audience'), world_description: val('new-series-world'),
      synopsis: val('new-series-synopsis'),
      format_mode: getFormatMode(),
      // Scenario constructor: ordered hook-beats (ноды) assembled in the modal —
      // persisted as beat_sequence and replayed in order by episode generation.
      beats: getSelectedBeats(),
      // Era/world pick — lets the backend pre-confirm the asset-generation era
      // so character portraits render in-period without the «Ваш сериал в
      // сеттинге X?» banner re-asking (and defaulting to modern if ignored).
      ...getEraSetting(),
      auto_generate_assets: autogen,
      batch_mode: false,
      batch_size: 1,
      writer_model: _selectedWriterModel('writer-model-create'),
      target_duration_sec,
      source_episode_outline: window._pendingSeriesOutline || null,
      asian_recast: !!window._pendingAsianRecast,
      source_drama: window._pendingSourceDrama || null,
    });
    const _autoOutlineN = (window._pendingSeriesOutline || []).length;
    window._pendingSeriesOutline = null;
    window._pendingAsianRecast = false;
    window._pendingSourceDrama = null;
    closeModal('modal-create-series');
    _createSeriesDraftClear();
    if (data?._scaffold?.prproj_warning) {
      showToast('⚠ ' + data._scaffold.prproj_warning + ' (templates/empty.prproj)');
    }
    navigate('series', { seriesId: data.id });
    // Created from a deep-analyzed drama → auto-write the first episodes to its outline.
    if (_autoOutlineN) _autoWriteFirstEpisodes(_autoOutlineN);
    // Same first-time style prompt as the import flow.
    setTimeout(() => maybePromptForStyle(true), 900);
  } catch(e) {
    showToast('Ошибка: ' + e.message);
  }
}

// Toggle "ready" on the current episode — instant save so the projects-grid badge updates.
async function onReadyToggle() {
  const el = document.getElementById('ep-ready-toggle');
  if (!el || !S.seriesId || !S.episodeNum) return;
  const ready = !!el.checked;
  try {
    await api.put(`/api/series/${S.seriesId}/episodes/${S.episodeNum}`, { ready });
    if (S.episode) S.episode.ready = ready;
    showToast(ready ? '✓ Серия отмечена как готовая' : 'Серия снова в работе');
  } catch(e) {
    el.checked = !ready;
    showToast('Ошибка: ' + e.message);
  }
}

// Rename files in <series>/OUT/ to studio delivery convention.
async function renameOutFiles() {
  if (!S.seriesId) return;
  if (!confirm('Переименовать все файлы в папке OUT по студийным требованиям?\n\nВидео → Series_Name_E1.mp4\nАудио → VO_/MUS_/SFX_Series_Name_N.wav\n\nФайлы которые не получится опознать — будут пропущены.')) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/rename-out`, {});
    const lines = [];
    lines.push(`Переименовано: ${r.renamed.length} из ${r.total_files}`);
    if (r.skipped.length) lines.push(`Пропущено: ${r.skipped.length}`);
    if (r.errors.length) lines.push(`Ошибок: ${r.errors.length}`);
    showToast(lines.join(' · '));
    // Detailed report in console for debugging / spot-check
    console.group('[rename-out] ' + r.series_safe_name);
    if (r.renamed.length) { console.log('renamed:'); r.renamed.forEach(x => console.log('  ', x.from, '→', x.to)); }
    if (r.skipped.length) { console.warn('skipped:'); r.skipped.forEach(x => console.warn('  ', x.name, '—', x.reason)); }
    if (r.errors.length) { console.error('errors:'); r.errors.forEach(x => console.error('  ', x.name, '—', x.error)); }
    console.groupEnd();
    if (r.skipped.length || r.errors.length) {
      const detail = [
        r.skipped.length ? 'ПРОПУЩЕНЫ:\n' + r.skipped.map(x=>`• ${x.name} — ${x.reason}`).join('\n') : '',
        r.errors.length  ? 'ОШИБКИ:\n'   + r.errors.map(x=>`• ${x.name} — ${x.error}`).join('\n') : '',
      ].filter(Boolean).join('\n\n');
      alert(detail);
    }
  } catch(e) {
    showToast('Ошибка: ' + e.message);
  }
}

// Toggle auto-generate-assets on an existing series + trigger an immediate sweep
async function toggleSeriesAutogen() {
  const cb = document.getElementById('series-autogen-toggle');
  if (!cb) return;
  const enabled = cb.checked;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/auto-generate`, { enabled });
    S.series.auto_generate_assets = r.enabled;
    if (r.enabled && r.sweep_started) {
      showToast('Авто-генерация включена — недостающие картинки уже генерятся');
    } else if (r.enabled) {
      showToast('Авто-генерация включена');
    } else {
      showToast('Авто-генерация выключена');
    }
  } catch (e) {
    cb.checked = !enabled; // revert
    showToast('Ошибка: ' + e.message);
  }
}

let _autogenPollTimer = null;

async function triggerAutogenSweep() {
  const btns = [
    document.getElementById('autogen-sweep-btn'),
    document.getElementById('autogen-sweep-btn-ep'),
  ].filter(Boolean);
  const statuses = [
    document.getElementById('autogen-sweep-status'),
    document.getElementById('autogen-sweep-status-ep'),
  ].filter(Boolean);
  if (!btns.length) return;
  btns.forEach(b => { b.disabled = true; b.innerHTML = '<span class="spinner"></span> запускаю…'; });
  try {
    await fetch(`/api/series/${S.seriesId}/auto-generate/sweep`, {method: 'POST'});
    pollAutogenStatus();
  } catch (e) {
    btns.forEach(b => { b.disabled = false; b.innerHTML = '🎨 Сгенерировать недостающее'; });
    statuses.forEach(s => { s.textContent = 'Ошибка: ' + e.message; });
  }
}

function _autogenApplyInProgress(inProgress) {
  // Reset all overlays
  document.querySelectorAll('[data-autogen-kind] .autogen-overlay').forEach(el => {
    el.hidden = true;
  });
  document.querySelectorAll('[data-autogen-kind].is-generating').forEach(el => {
    el.classList.remove('is-generating');
  });
  // Apply current in-progress entries (char + loc + item — outfits live in modal)
  for (const entry of (inProgress || [])) {
    if (!['char', 'loc', 'item'].includes(entry.kind)) continue;
    const sel = `[data-autogen-kind="${entry.kind}"][data-autogen-id="${entry.parent_id}"]`;
    const card = document.querySelector(sel);
    if (!card) continue;
    card.classList.add('is-generating');
    const ov = card.querySelector('.autogen-overlay');
    if (ov) ov.hidden = false;
  }
}

async function pollAutogenStatus() {
  // Two button/status pairs: one in series sidebar, one in episode sidebar.
  // Helpers below mutate BOTH so the user sees identical state regardless
  // of which view they're on.
  const _allBtns = () => [
    document.getElementById('autogen-sweep-btn'),
    document.getElementById('autogen-sweep-btn-ep'),
  ].filter(Boolean);
  const _allStatuses = () => [
    document.getElementById('autogen-sweep-status'),
    document.getElementById('autogen-sweep-status-ep'),
  ].filter(Boolean);
  const _setBtnHtml = (html) => _allBtns().forEach(b => { b.innerHTML = html; });
  const _setBtnDisabled = (d) => _allBtns().forEach(b => { b.disabled = d; });
  const _setStatusText = (t) => _allStatuses().forEach(s => { s.textContent = t; });
  const _setStatusHtml = (h) => _allStatuses().forEach(s => { s.innerHTML = h; });
  const status = document.getElementById('autogen-sweep-status') || document.getElementById('autogen-sweep-status-ep');
  const btn = document.getElementById('autogen-sweep-btn') || document.getElementById('autogen-sweep-btn-ep');
  if (!status && !btn) return;
  if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }

  // Track previous done count so we know when to re-fetch+re-render lists
  let prevDone = -1;
  // Track whether we've ever observed a running sweep this session — used by
  // the not-running branch to decide whether to render a final "done"/"errors"/
  // "ничего не нужно" line vs. just stay quiet (which would otherwise flicker
  // every 2.5s as the heartbeat re-arms the poller from cold state).
  let sawRunning = false;
  const tick = async () => {
    try {
      const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
      const st = await r.json();
      // Mark in-progress items on DOM (spinner overlay) and clear stale marks
      _autogenApplyInProgress(st.in_progress || []);
      // Phantom-running guard: a stale running=true with empty queue and
      // no in_progress entries means the worker thread died (server reload,
      // KeyboardInterrupt, etc.) without reaching the finally{} that flips
      // running=false. Don't spin the UI forever — treat it as done.
      const ipList = st.in_progress || [];
      const isPhantom = st.running && (st.queue || 0) === 0 && ipList.length === 0;
      if (st.running && !isPhantom) {
        sawRunning = true;
        const ipBits = ipList.map(x => x.name).filter(Boolean).slice(0, 3).join(', ');
        const ipSuffix = ipBits ? ` · сейчас: ${ipBits}` : '';
        _setStatusText(`генерация… ${st.done}/${st.queue}` + (st.errors.length ? ` · ошибок: ${st.errors.length}` : '') + ipSuffix);
        _setBtnHtml('<span class="spinner"></span> ' + st.done + '/' + st.queue);
        // Re-fetch + re-render every time `done` increments — lets assets pop in live
        if (prevDone !== -1 && st.done > prevDone) {
          try {
            const fresh = await fetch(`/api/series/${S.seriesId}`).then(r => r.json());
            S.series = fresh;
            // Asset on disk changed — invalidate the cache-buster so next render
            // fetches fresh image bytes instead of showing the previous version.
            bumpAssetVersion();
            renderCharactersList();
            renderLocationsList();
            renderItemsList();
            // Episode-side panels also have <img> for the same chars/locs/items —
            // they'd otherwise keep showing stale until user navigates away.
            if (typeof renderEpCharacters === 'function') renderEpCharacters();
            if (typeof renderEpLocations === 'function') renderEpLocations();
            if (typeof renderEpItems === 'function') renderEpItems();
            _autogenApplyInProgress(st.in_progress || []);  // re-apply spinners after re-render wipes them
          } catch {}
        }
        prevDone = st.done;
      } else {
        if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }
        _setBtnDisabled(false);
        _setBtnHtml('🎨 Сгенерировать недостающее');
        // Status text rules — pick ONE branch and don't re-render it on every
        // poll tick (otherwise the line keeps appearing/disappearing every 2.5s
        // and the button visually shakes around it):
        //   • sawRunning true on a previous tick → show finished result message
        //   • sawRunning false (we never observed a running sweep this session)
        //     → idle state; just clear the status so it doesn't flicker
        // The "— ничего не нужно генерить" message used to be the idle state too
        // and triggered the bug; now it only shows on the freshly-finished tick
        // when queue and done are both zero (means user clicked button + nothing
        // was needed).
        if (sawRunning) {
          // Status-line policy: ONLY show errors. Success states ("✓ готово
          // N/M" and "ничего не нужно генерить") used to render here on every
          // sweep-finish — but the autogen system can re-trigger sweeps in the
          // background (script accept, get_series self-heal, etc.), and each
          // brief running=true blip would re-paint the line, schedule a 6s
          // clear, then paint again on the next blip. Net effect: text
          // appearing and disappearing under the button, shaking the layout.
          // User just wants "ошибки если есть, иначе тишина".
          if (st.errors && st.errors.length) {
            _setStatusHtml(`<span style="color:var(--danger,#f87171)">готово ${st.done}/${st.queue} · ошибок ${st.errors.length}</span>`);
            showToast('Авто-генерация: ошибок — ' + st.errors.length + '. Подробности в консоли сервера.');
            console.warn('[autogen errors]', st.errors);
            setTimeout(() => _setStatusText(''), 6000);
          } else {
            _setStatusText('');
          }
          // Refresh series state to show new images
          try {
            const fresh = await fetch(`/api/series/${S.seriesId}`).then(r => r.json());
            S.series = fresh;
            renderCharactersList();
            renderLocationsList();
            renderItemsList();
          } catch {}
        } else {
          // Idle background poll — keep status empty. Don't fight with whatever
          // text might have been written by triggerAutogenSweep / acceptScript.
          // Bail out completely — no need to keep ticking when nothing is
          // happening (the heartbeat will re-attach if a sweep starts).
        }
        return 'done';   // signal to outer loop: stop ticking
      }
    } catch (e) {
      if (_autogenPollTimer) { clearInterval(_autogenPollTimer); _autogenPollTimer = null; }
      _setBtnDisabled(false);
      _setBtnHtml('🎨 Сгенерировать недостающее');
      _setStatusText('Ошибка опроса: ' + e.message);
    }
  };
  // Run first tick. If it returned 'done' (sweep is not running) — don't arm
  // the recurring poller. The heartbeat will re-spawn pollAutogenStatus when
  // it sees running=true again. Without this guard the poller kept ticking
  // every 2.5s with the same "ничего не нужно" message, scheduling overlapping
  // 6s clears, which made the status line shake around the button.
  const firstResult = await tick();
  if (firstResult !== 'done') {
    _autogenPollTimer = setInterval(tick, 2500);
  }
}

// Pre-flight check before any video generation. Returns list of missing assets:
//   { chars: [{name, missing}], outfits: [{char, label}], locs: [{name}] }
// where `missing` is 'base' or 'outfit_label'. Empty arrays = all good.
function _checkMissingAssets(scope = 'episode') {
  const out = { chars: [], outfits: [], locs: [], running: false };
  if (!S.series || !S.episode) return out;
  const charById = Object.fromEntries((S.series.characters || []).map(c => [c.id, c]));
  const locById = Object.fromEntries((S.series.locations || []).map(l => [l.id, l]));
  const usedCharIds = new Set(S.episode.characters_used || []);
  const usedLocIds  = new Set(S.episode.locations_used  || []);
  const charOutfits = S.episode.character_outfits || {};
  for (const cid of usedCharIds) {
    const c = charById[cid];
    if (!c) continue;
    if (!c.ref_images || !c.ref_images.length) {
      out.chars.push({ name: c.name, id: cid });
    }
    // Check requested outfits for this char. The episode stores values that
    // may be either outfit LABELS or outfit IDS depending on when the entry
    // was written (legacy episodes mix both). Try both to find the outfit.
    // If the value matches NEITHER an existing label NOR an existing id —
    // it's an orphan ref pointing at a deleted/renamed outfit. We silently
    // ignore it instead of reporting "missing" (the outfit literally doesn't
    // exist anymore so there's nothing to generate). This is what was
    // happening in the user's screenshot: SARAH/49d33f6b, MARCUS/d22d7423,
    // ELENA/c5d28813 were stale 8-char outfit ids; backend autogen saw all
    // real outfits already had photos and reported "ничего не нужно генерить",
    // while this preflight kept screaming about orphans.
    const requested = charOutfits[cid] || [];
    const labels = Array.isArray(requested) ? requested : (requested ? [requested] : []);
    for (const ref of labels) {
      if (!ref || ref === 'base') continue;
      const outfits = c.outfits || [];
      const outfit = outfits.find(o => o.label === ref) || outfits.find(o => o.id === ref);
      if (!outfit) continue;  // orphan — drop silently
      if (!outfit.photo && !outfit.is_base) {
        out.outfits.push({ char: c.name, label: outfit.label });
      }
    }
  }
  for (const lid of usedLocIds) {
    const l = locById[lid];
    if (!l) continue;
    if (!l.ref_images || !l.ref_images.length) {
      out.locs.push({ name: l.name, id: lid });
    }
  }
  return out;
}

// Confirm dialog returning a Promise<bool>. Returns false if user cancels.
// Empty missing-list returns true without prompting.
async function _confirmMissingAssetsBeforeGen(label = 'генерации') {
  const missing = _checkMissingAssets();
  const total = missing.chars.length + missing.outfits.length + missing.locs.length;
  if (total === 0) return true;
  // Check if autogen is currently running — friendlier message
  let runningSuffix = '';
  try {
    const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
    const st = await r.json();
    if (st.running) {
      runningSuffix = `\n\n⏳ Авто-генерация СЕЙЧАС идёт: ${st.done}/${st.queue}. Можешь подождать ~30-60с — оставшиеся ассеты доделаются.`;
    } else {
      runningSuffix = `\n\n⚠ Auto-generate не запущен. Нажми "🎨 Сгенерировать недостающее" в панели персонажей/локаций, или запусти видео-генерацию всё равно — но сцена будет с placeholder/неправильным видом для перечисленных выше.`;
    }
  } catch {}
  const lines = [];
  if (missing.chars.length) lines.push(`👤 Персонажи без портрета: ${missing.chars.map(c => c.name).join(', ')}`);
  if (missing.outfits.length) lines.push(`👕 Аутфиты не сгенерены: ${missing.outfits.map(o => `${o.char}/${o.label}`).join(', ')}`);
  if (missing.locs.length) lines.push(`🏛 Локации без фото: ${missing.locs.map(l => l.name).join(', ')}`);
  const msg = `Не все ассеты этой сцены готовы:\n\n${lines.join('\n')}${runningSuffix}\n\nЗапустить ${label} всё равно?`;
  return appConfirm({
    title: '⚠ Не все ассеты готовы',
    message: msg,
    okText: 'Запустить всё равно',
    cancelText: 'Отмена',
    okStyle: 'accent',
  });
}

// Auto-resume polling if a sweep is running when the user opens the page
async function checkAutogenOnLoad() {
  try {
    const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
    const st = await r.json();
    if (st.running) pollAutogenStatus();
  } catch {}
  // Always start the heartbeat — it watches for autogen runs that kick off
  // AFTER the page loaded (e.g. server-side trigger from /extract-characters
  // or get_series self-heal). Without this, the spinner overlay only appears
  // for sweeps that were already running at page-load time.
  _startAutogenHeartbeat();
}

// Lightweight heartbeat that polls autogen status every 4s while user is on
// a series/episode view. When it sees `running=true` and no UI poller is
// active, it attaches the live UI poller. When sweeps kick off in the
// background (autogen sometimes triggers from the server side after script-gen
// or get_series self-heal), this catches them automatically — without needing
// a page refresh.
let _autogenHeartbeatTimer = null;
function _startAutogenHeartbeat() {
  if (_autogenHeartbeatTimer) return;
  _autogenHeartbeatTimer = setInterval(async () => {
    if (!S.seriesId) return;
    try {
      const r = await fetch(`/api/series/${S.seriesId}/auto-generate/status`);
      const st = await r.json();
      // If sweep is running AND we don't have an active UI poller — attach.
      if (st.running && !_autogenPollTimer) {
        pollAutogenStatus();
      }
      // If sweep just finished (no longer running) AND we still see in_progress
      // markers from a previous render — clear them.
      if (!st.running && (!st.in_progress || !st.in_progress.length)) {
        _autogenApplyInProgress([]);
      }
    } catch {}
  }, 4000);
}

