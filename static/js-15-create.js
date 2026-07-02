// ── Series-create mode picker ────────────────────────────────────────────────
// Two top-level modes inside the create-series modal:
//   generate — existing AI-flow (idea → generate → fill fields)
//   import   — paste/upload an existing script, split into episodes, extract
//              chars/locs/items in the background
// Each mode shows its own block + footer button; the unused parts are hidden.
function setSeriesCreateMode(mode) {
  // Three modes: 'generate' (AI from scratch), 'import' (paste a script),
  // 'clone' (base on an existing series + revision instructions).
  if (mode !== 'import' && mode !== 'clone') mode = 'generate';
  const genBlock = document.getElementById('series-generate-block');
  const impBlock = document.getElementById('series-import-block');
  const cloBlock = document.getElementById('series-clone-block');
  if (genBlock) genBlock.style.display = mode === 'generate' ? '' : 'none';
  if (impBlock) impBlock.style.display = mode === 'import' ? '' : 'none';
  if (cloBlock) cloBlock.style.display = mode === 'clone' ? '' : 'none';
  const genBtn = document.getElementById('series-mode-generate-btn');
  const impBtn = document.getElementById('series-mode-import-btn');
  const cloBtn = document.getElementById('series-mode-clone-btn');
  if (genBtn) genBtn.classList.toggle('active', mode === 'generate');
  if (impBtn) impBtn.classList.toggle('active', mode === 'import');
  if (cloBtn) cloBtn.classList.toggle('active', mode === 'clone');
  // Footer "Создать" only drives generate mode (import & clone have own buttons).
  const footerCreateBtn = document.getElementById('series-generate-create-btn');
  if (footerCreateBtn) footerCreateBtn.style.display = mode === 'generate' ? '' : 'none';
  // Lazily populate the clone source dropdown when entering clone mode.
  if (mode === 'clone') _populateCloneSources();
}

// ── Clone-from-existing flow ─────────────────────────────────────────────────
async function _populateCloneSources() {
  const sel = document.getElementById('clone-source-sid');
  if (!sel) return;
  // Keep the user's current pick across re-entries if still valid.
  const prev = sel.value;
  try {
    const list = await api.get('/api/series');
    const series = Array.isArray(list) ? list : (list.series || list.items || []);
    if (!series.length) {
      sel.innerHTML = '<option value="">— нет сериалов для клонирования —</option>';
      return;
    }
    sel.innerHTML = '<option value="">— выбери сериал —</option>' + series.map(s => {
      const eps = (s._episode_total != null ? s._episode_total : '?');
      const title = (s.title || s.id || '').replace(/</g, '&lt;');
      return `<option value="${s.id}">${title} · ${eps} сер.</option>`;
    }).join('');
    if (prev && series.some(s => s.id === prev)) sel.value = prev;
    _cloneOnSourceChange();
  } catch (e) {
    sel.innerHTML = '<option value="">— ошибка загрузки списка —</option>';
  }
  if (!sel._cloneWired) {
    sel.addEventListener('change', _cloneOnSourceChange);
    sel._cloneWired = true;
  }
}

function _cloneOnSourceChange() {
  const sel = document.getElementById('clone-source-sid');
  const info = document.getElementById('clone-source-info');
  const titleEl = document.getElementById('clone-new-title');
  if (!sel) return;
  const opt = sel.options[sel.selectedIndex];
  if (info) info.textContent = opt && sel.value ? `Будет скопирован сюжет, библия, персонажи и серии из «${opt.text}».` : '';
  // Suggest a title if the user hasn't typed one yet.
  if (titleEl && !titleEl.value.trim() && opt && sel.value) {
    const base = opt.text.split(' · ')[0];
    titleEl.value = `${base} (вариант)`;
  }
}

async function cloneCreateSeries() {
  const source_sid = val('clone-source-sid');
  if (!source_sid) return alert('Выбери сериал-источник');
  const title = val('clone-new-title');
  if (!title) return alert('Введи название нового сериала');
  const revision_instructions = val('clone-revision-instructions');
  const _epsRaw = val('clone-episodes-count');
  const episodes_to_copy = _epsRaw ? Math.max(0, parseInt(_epsRaw, 10) || 0) : 0;
  const btn = document.getElementById('clone-create-btn');
  const statusEl = document.getElementById('clone-create-status');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Создаю и применяю правки…'; }
  if (statusEl) statusEl.textContent = 'Копирую сюжет и серии, переписываю библию и персонажей под правки — это может занять до минуты.';
  try {
    const data = await api.post('/api/series/clone-from', {
      source_sid, title, revision_instructions, episodes_to_copy,
    });
    closeModal('modal-create-series');
    const c = data._clone || {};
    let msg = `🧬 Создан «${data.title}»: скопировано серий — ${c.copied_episodes ?? 0}.`;
    if (c.pending_rewrites) msg += ` Требуют перезаписи под правки: ${c.pending_rewrites} (внутри сериала, поштучно).`;
    else if (revision_instructions) msg += ' Правки применены к библии и персонажам; сценарии серий перезаписывать не нужно.';
    showToast(msg);
    navigate('series', { seriesId: data.id });
  } catch (e) {
    if (statusEl) statusEl.textContent = '';
    showToast('Ошибка: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🧬 Создать вариант'; }
  }
}

// ── Import-from-script flow ─────────────────────────────────────────────────
function importDropFile(ev) {
  const file = ev.dataTransfer?.files?.[0];
  if (!file) return;
  _importReadFile(file);
}
function importPickFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  _importReadFile(file);
}
function _importReadFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    setVal('import-series-script', reader.result || '');
    importUpdateStats();
    // Auto-fill title from filename if empty.
    const titleEl = document.getElementById('import-series-title');
    if (titleEl && !titleEl.value.trim()) {
      titleEl.value = (file.name || 'Imported series').replace(/\.[^.]+$/, '');
    }
  };
  reader.readAsText(file);
}
function importUpdateStats() {
  const txt = (document.getElementById('import-series-script')?.value || '');
  const stats = document.getElementById('import-series-script-stats');
  if (stats) stats.textContent = `${txt.length.toLocaleString('ru-RU')} символов`;
}

// Module-level state for "user clicked «оставить как есть» on the language
// warning" — keyed by the textarea id (so import vs append flows don't bleed
// into each other). The commit function reads this to decide whether to
// send a `dialogue_language_hint` to the backend.
const _LANG_WARN_KEPT = {};  // {'import-series-script': 'ru', ...}

const _LANG_LABELS = {
  ru: 'русский', zh: 'китайский', ja: 'японский',
  ko: 'корейский', other: 'не-английский',
};

function _renderLangWarning(payload, textareaId) {
  // Returns HTML for the yellow warning plate, or '' if no warning. Skips
  // rendering if the user has already dismissed it for this textarea in
  // this session.
  if (!payload || _LANG_WARN_KEPT[textareaId]) return '';
  const pct = Math.round((payload.ratio || 0) * 100);
  const lang = _LANG_LABELS[payload.detected_lang] || payload.detected_lang || 'не-английский';
  const samples = (payload.sample_lines || []).map(s => `<div style="font-family:monospace;font-size:0.78rem;color:var(--muted);margin-left:8px">${esc(s)}</div>`).join('');
  return `
    <div class="lang-warn-plate" style="margin-bottom:10px;padding:10px 12px;background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.45);border-radius:6px;font-size:0.85rem;line-height:1.45">
      <div style="color:#fbbf24;font-weight:600;margin-bottom:4px">⚠ Диалоги не на английском (~${pct}% строк, похоже на ${esc(lang)})</div>
      <div style="color:var(--text);margin:6px 0">
        ${samples}
      </div>
      <div style="color:var(--muted);margin:6px 0 10px;font-size:0.8rem">
        По умолчанию тулза работает с английскими диалогами. Если оставить как
        есть — персонажи будут говорить на оригинальном языке в видео-генерации.
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn-accent btn-sm" onclick="langWarnAdapt('${textareaId}', this)">🔄 Адаптировать на английский (Claude, ~30с)</button>
        <button class="btn-ghost btn-sm" onclick="langWarnKeep('${textareaId}', '${esc(payload.detected_lang)}')">⏭ Оставить как есть</button>
      </div>
    </div>`;
}

// User clicked "Адаптировать" — calls translate endpoint, swaps textarea
// content with translated version, re-runs the appropriate preview which
// will now show 0% non-EN dialogue and no warning.
async function langWarnAdapt(textareaId, btn) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const script = (ta.value || '').trim();
  if (!script) return;
  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Адаптирую...';
  try {
    const r = await api.post('/api/series/import-from-script/translate-dialogues',
      { script }, { timeoutMs: 600000 });
    if (r.error) throw new Error(r.error);
    ta.value = r.translated_script || script;
    // Refresh char-count badge if present.
    if (typeof importUpdateStats === 'function') importUpdateStats();
    if (typeof appendUpdateStats === 'function') appendUpdateStats();
    // Re-run the matching preview — warning should disappear because
    // backend now sees ratio ≈ 0.
    if (textareaId === 'import-series-script' && typeof importPreviewSplit === 'function') {
      importPreviewSplit();
    } else if (textareaId === 'append-script-text' && typeof appendPreviewSplit === 'function') {
      appendPreviewSplit();
    }
    showToast(`✓ Сценарий адаптирован на английский (заменено реплик: ~${r.lines_changed_estimate || 0})`, 4000);
  } catch (e) {
    btn.disabled = false;
    btn.innerHTML = originalLabel;
    showToast('✗ Не удалось адаптировать: ' + (e?.message || e), 5000);
  }
}

// User clicked "Оставить как есть" — remember the language hint locally so
// the commit function can persist it on series.json, then hide the plate.
function langWarnKeep(textareaId, lang) {
  _LANG_WARN_KEPT[textareaId] = lang;
  const plate = document.querySelector(`.lang-warn-plate`);
  if (plate) plate.remove();
  showToast(`⚠ Сериал будет с диалогами на оригинальном языке (${_LANG_LABELS[lang] || lang})`, 4000);
}

async function importPreviewSplit() {
  const script = (document.getElementById('import-series-script')?.value || '').trim();
  const previewEl = document.getElementById('import-series-preview');
  if (!script) { previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  // Reset kept-decision when user re-pastes / re-previews so the warning
  // can show again if it's still relevant.
  delete _LANG_WARN_KEPT['import-series-script'];
  previewEl.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Анализирую разбивку...</div>';
  try {
    const r = await api.post('/api/series/import-from-script/preview', { script });
    if (r.error) throw new Error(r.error);
    const eps = r.episodes || [];
    if (!eps.length) {
      previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Не удалось разбить — будет создан 1 эпизод со всем текстом</div>';
      return;
    }
    const warnHtml = _renderLangWarning(r.dialogue_lang_warning, 'import-series-script');
    previewEl.innerHTML = `
      ${warnHtml}
      <div style="font-size:0.85rem;color:var(--success);margin-bottom:6px">
        ✓ Найдено эпизодов: <strong>${eps.length}</strong>
      </div>
      <div style="max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:var(--surface2)">
        ${eps.map(e => `
          <div style="padding:5px 4px;border-bottom:1px solid var(--border);font-size:0.82rem">
            <strong>Эп. ${e.number}</strong>
            ${e.title ? `<span style="color:var(--muted)"> · ${esc(e.title)}</span>` : ''}
            <span style="color:var(--muted);margin-left:8px">(${e.length.toLocaleString('ru-RU')} симв.)</span>
            <div style="color:var(--muted);font-size:0.75rem;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.preview)}</div>
          </div>`).join('')}
      </div>`;
  } catch (e) {
    previewEl.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  }
}

// ── Pre-upload state for the create-from-script flow ───────────────────────
// Two arrays of File objects + their filename-derived display names. Built up
// as the user picks files in the modal, replayed when they hit «Создать».
const IMPORT_PICKED = { chars: [], locs: [] };
function _importStemToName(filename) {
  let stem = (filename || '').replace(/\.[^.]+$/, '').trim();
  stem = stem.replace(/[._\-]+/g, ' ').trim();
  stem = stem.replace(/\s+/g, ' ');
  if (!stem) return '';
  if (stem === stem.toUpperCase()) {
    // ALL CAPS → Title Case for readability.
    stem = stem.replace(/\w\S*/g, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase());
  }
  return stem;
}
function _renderImportPickedList(kind) {
  const arr = IMPORT_PICKED[kind] || [];
  const host = document.getElementById(kind === 'chars' ? 'import-char-files-preview' : 'import-loc-files-preview');
  if (!host) return;
  if (!arr.length) { host.innerHTML = ''; return; }
  host.innerHTML = arr.map((entry, i) => `
    <div class="import-file-chip" title="${esc(entry.file.name)}">
      <img src="${esc(entry.previewUrl)}" alt="">
      <div class="import-file-chip-meta">
        <input type="text" value="${esc(entry.name)}" oninput="importRenameFile('${kind}', ${i}, this.value)">
        <div class="import-file-chip-fname">${esc(entry.file.name)}</div>
      </div>
      <button type="button" class="import-file-chip-x" onclick="importRemoveFile('${kind}', ${i})" title="Убрать">×</button>
    </div>`).join('');
}
function importRenameFile(kind, idx, value) {
  const arr = IMPORT_PICKED[kind] || [];
  if (arr[idx]) arr[idx].name = (value || '').trim();
}
function importRemoveFile(kind, idx) {
  const arr = IMPORT_PICKED[kind] || [];
  const entry = arr[idx];
  if (entry && entry.previewUrl) { try { URL.revokeObjectURL(entry.previewUrl); } catch (_) {} }
  arr.splice(idx, 1);
  _renderImportPickedList(kind);
}
function _importPickedFiles(kind, inputEl) {
  const files = Array.from(inputEl.files || []);
  for (const f of files) {
    if (!f.type || !f.type.startsWith('image/')) continue;
    const name = _importStemToName(f.name);
    if (!name) continue;
    // Dedup by name within the picker.
    if (IMPORT_PICKED[kind].some(e => e.name.toLowerCase() === name.toLowerCase())) continue;
    IMPORT_PICKED[kind].push({ file: f, name, previewUrl: URL.createObjectURL(f) });
  }
  inputEl.value = '';  // allow re-picking the same file after removal
  _renderImportPickedList(kind);
}
function importPickedCharFiles(el) { _importPickedFiles('chars', el); }
function importPickedLocFiles(el)  { _importPickedFiles('locs',  el); }
function _importResetPickedFiles() {
  for (const kind of ['chars', 'locs']) {
    for (const e of IMPORT_PICKED[kind]) { try { URL.revokeObjectURL(e.previewUrl); } catch (_) {} }
    IMPORT_PICKED[kind] = [];
    _renderImportPickedList(kind);
  }
}

async function importCreateSeries() {
  const title = (document.getElementById('import-series-title')?.value || '').trim();
  const script = (document.getElementById('import-series-script')?.value || '').trim();
  const extractChars = !!document.getElementById('import-extract-characters')?.checked;
  const extractLocs  = !!document.getElementById('import-extract-locations')?.checked;
  const extractItems = !!document.getElementById('import-extract-items')?.checked;
  if (!title) { alert('Введи название сериала'); return; }
  if (!script) { alert('Сценарий пустой — вставь текст или подгрузи файл'); return; }
  // Sanity: if user disabled char extraction AND didn't upload any → warn
  // (the series will have zero characters until they add them manually).
  if (!extractChars && !IMPORT_PICKED.chars.length) {
    if (!confirm('Извлечение персонажей выключено и ни одного файла не подгружено — серия будет без персонажей. Продолжить?')) return;
  }
  const btn = document.getElementById('import-create-btn');
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Создаю сериал...';
  try {
    const hasFiles = IMPORT_PICKED.chars.length + IMPORT_PICKED.locs.length > 0;
    const langHint = _LANG_WARN_KEPT['import-series-script'] || '';
    // Style choice — read BEFORE submit so generation uses it from the start
    // instead of defaulting to 'cinematic' then prompting the user 900ms
    // after the worker already kicked off (and locked some chars to the
    // wrong look). User-reported real bug: «The Fox CEO's Trap» — picked
    // PIXAR in the post-create prompt, but worker had already started
    // generating realistic portraits before that prompt opened.
    const styleType = (document.getElementById('import-style-type')?.value || 'cinematic').trim();
    const styleCustomDesc = (document.getElementById('import-style-custom-desc')?.value || '').trim();
    const writerModel = _selectedWriterModel('import-writer-model');
    let r;
    if (hasFiles) {
      // Multipart path — pre-upload files alongside the script.
      const fd = new FormData();
      fd.append('title', title);
      fd.append('script', script);
      fd.append('extract_characters', extractChars ? '1' : '0');
      fd.append('extract_locations',  extractLocs  ? '1' : '0');
      fd.append('extract_items',      extractItems ? '1' : '0');
      if (langHint) fd.append('dialogue_language_hint', langHint);
      fd.append('style_type', styleType);
      if (styleCustomDesc) fd.append('style_custom_description', styleCustomDesc);
      fd.append('writer_model', writerModel);
      // Rename file to the user-edited name (preserving extension) so the
      // backend stem→name converter picks up edits made in the chip UI.
      const _renamed = (entry) => {
        const ext = (entry.file.name.match(/\.[^.]+$/) || [''])[0];
        const safe = entry.name.replace(/[\/\\<>:"|?*]/g, '_');
        return new File([entry.file], `${safe}${ext}`, { type: entry.file.type });
      };
      for (const e of IMPORT_PICKED.chars) fd.append('character_files', _renamed(e));
      for (const e of IMPORT_PICKED.locs)  fd.append('location_files',  _renamed(e));
      r = await api.postForm('/api/series/import-from-script', fd, { timeoutMs: 180000 });
    } else {
      const body = {
        title, script,
        extract_characters: extractChars,
        extract_locations:  extractLocs,
        extract_items:      extractItems,
        style_type: styleType,
        style_custom_description: styleCustomDesc,
        writer_model: writerModel,
      };
      if (langHint) body.dialogue_language_hint = langHint;
      r = await api.post('/api/series/import-from-script', body);
    }
    if (r.error) throw new Error(r.error);
    closeModal('modal-create-series');
    _createSeriesDraftClear();
    const summaryBits = [`${r.episodes_created} эпизодов`];
    if (r.characters_uploaded) summaryBits.push(`${r.characters_uploaded} персов`);
    if (r.locations_uploaded)  summaryBits.push(`${r.locations_uploaded} локаций`);
    showToast(`✓ Создано: ${summaryBits.join(' + ')}${r.extraction_started ? ' · извлечение в фоне' : ''}`, 5000);
    _importResetPickedFiles();
    navigate('series', { seriesId: r.sid });
    if (r.extraction_started) setTimeout(() => pollImportStatus(r.sid), 600);
    // No longer auto-prompting for style here — user picks it in the import
    // modal BEFORE submit so the worker uses the right style from the very
    // first character/location generation.
  } catch (e) {
    alert('Ошибка импорта: ' + (e?.message || e));
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Wire stats update on textarea typing.
document.addEventListener('DOMContentLoaded', () => {
  const ta = document.getElementById('import-series-script');
  if (ta) ta.addEventListener('input', importUpdateStats);
});

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
// ── Auto-pipeline after «✨ Сгенерировать новые» ──────────────────────────────
// After batch-generation fills the textarea, optionally run the exact chain a
// user does by hand: 🔧 под стандарт → 🧠 проверка логики → 🩹 лечение всех
// проблем → 📜 добавить серии. Each stage is independently toggleable in
// Настройки and defaults ON. While the chain runs, a full-screen overlay blocks
// every click/keypress so nothing can be touched mid-process, and shows live
// per-stage status.
const AUTO_PIPE_KEYS = { adapt: 'auto_pipe_adapt', logic: 'auto_pipe_logic', add: 'auto_pipe_add' };
function autoPipeEnabled(key) { return localStorage.getItem(AUTO_PIPE_KEYS[key]) !== 'false'; }
function setAutoPipeEnabled(key, on) { try { localStorage.setItem(AUTO_PIPE_KEYS[key], on ? 'true' : 'false'); } catch {} }

// Full-screen blocking overlay with a live checklist of pipeline stages.
const AppendPipelineOverlay = (() => {
  let _el = null, _timer = null, _start = 0, _running = false, _keyBlock = null, _steps = [];
  const ICON = {
    pending: '<span style="color:var(--muted);font-size:1.05rem">○</span>',
    run:     '<span class="spinner" style="width:15px;height:15px;border-width:2px;display:inline-block;vertical-align:middle"></span>',
    done:    '<span style="color:#4ade80;font-weight:700">✓</span>',
    skip:    '<span style="color:var(--muted)">–</span>',
    error:   '<span style="color:#f87171;font-weight:700">✗</span>',
  };
  function rowsHtml() {
    return _steps.map(s => {
      const dim = (s.status === 'pending' || s.status === 'skip') ? 'opacity:0.55' : '';
      const note = s.note ? `<span style="color:var(--muted);font-size:0.8rem;margin-left:auto">${esc(s.note)}</span>` : '';
      return `<div style="display:flex;align-items:center;gap:12px;padding:9px 4px;border-bottom:1px solid var(--border);${dim}">
        <span style="flex:0 0 20px;text-align:center">${ICON[s.status] || ICON.pending}</span>
        <span style="font-size:0.92rem">${s.label}</span>${note}
      </div>`;
    }).join('');
  }
  function render() {
    if (!_el) return;
    const body = _el.querySelector('[data-role="rows"]');
    if (body) body.innerHTML = rowsHtml();
  }
  function show(steps) {
    close();
    _start = Date.now(); _running = true;
    _steps = steps.map(s => ({ ...s, status: 'pending', note: '' }));
    _el = document.createElement('div');
    Object.assign(_el.style, {
      position: 'fixed', inset: '0', zIndex: '50000', padding: '20px',
      background: 'rgba(8,10,18,0.82)', backdropFilter: 'blur(3px)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
    });
    _el.innerHTML = `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);width:480px;max-width:92vw;padding:22px 24px">
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
          <span style="font-size:1.3rem">🤖</span>
          <div style="flex:1">
            <div style="font-weight:700;font-size:1.02rem">Автоматическая обработка сценария</div>
            <div data-role="sub" style="font-size:0.8rem;color:var(--muted)">Не трогай ничего — идёт процесс…</div>
          </div>
          <span data-role="elapsed" style="font-family:ui-monospace,monospace;font-size:0.85rem;background:var(--surface2);border:1px solid var(--border);padding:3px 8px;border-radius:6px">0:00</span>
        </div>
        <div data-role="rows"></div>
        <div data-role="foot" style="margin-top:16px;display:none;justify-content:flex-end">
          <button class="btn-primary" data-role="close-btn">Закрыть</button>
        </div>
      </div>`;
    document.body.appendChild(_el);
    render();
    // Swallow every keystroke (esp. Escape, which would close the modal) while
    // the pipeline is running.
    _keyBlock = (e) => { if (_running) { e.stopPropagation(); if (e.key === 'Escape') e.preventDefault(); } };
    document.addEventListener('keydown', _keyBlock, true);
    _timer = setInterval(() => {
      if (!_el) return;
      const sec = Math.floor((Date.now() - _start) / 1000);
      const el = _el.querySelector('[data-role="elapsed"]');
      if (el) el.textContent = `${Math.floor(sec / 60)}:${(sec % 60).toString().padStart(2, '0')}`;
    }, 1000);
    return { set, finish, fail, close };
  }
  function set(key, status, note) {
    const s = _steps.find(x => x.key === key);
    if (s) { s.status = status; if (note != null) s.note = note; }
    render();
  }
  function _stopRunning() {
    _running = false;
    if (_timer) { clearInterval(_timer); _timer = null; }
  }
  function _showFoot(label) {
    if (!_el) return;
    const foot = _el.querySelector('[data-role="foot"]');
    const btn = _el.querySelector('[data-role="close-btn"]');
    if (btn) { btn.textContent = label || 'Закрыть'; btn.onclick = close; }
    if (foot) foot.style.display = 'flex';
  }
  // Pipeline ran to the end. autoClose=true means episodes were already added
  // and the modal closed — fade the overlay out on its own.
  function finish(autoClose, msg) {
    _stopRunning();
    if (_el) {
      const sub = _el.querySelector('[data-role="sub"]');
      if (sub) sub.textContent = msg || '✅ Готово';
    }
    if (autoClose) { setTimeout(close, 1100); }
    else { _showFoot('Закрыть'); }
  }
  function fail(msg) {
    _stopRunning();
    if (_el) {
      const sub = _el.querySelector('[data-role="sub"]');
      if (sub) { sub.innerHTML = `<span style="color:#f87171">✗ ${esc(msg || 'Ошибка')}</span>`; }
    }
    _showFoot('Закрыть');
  }
  function close() {
    _stopRunning();
    if (_keyBlock) { document.removeEventListener('keydown', _keyBlock, true); _keyBlock = null; }
    if (_el) { _el.remove(); _el = null; }
  }
  return { show };
})();

// Runs adapt → logic → heal → add against the textarea, driving the overlay.
// Throws on the first failing stage (caller marks the overlay failed).
async function _runAppendPipeline(overlay) {
  const ta = document.getElementById('append-script-text');
  const getScript = () => (ta?.value || '').trim();
  // 1) ADAPT TO STANDARD
  if (autoPipeEnabled('adapt')) {
    overlay.set('adapt', 'run');
    const r = await api.post('/api/adapt-script-to-standard', { script: getScript() }, { timeoutMs: 180000 });
    if (r.error) throw new Error('Подгонка под стандарт: ' + r.error);
    if (r.script) { ta.dataset.preAdaptSnapshot = ta.value; ta.value = r.script; appendUpdateStats(); }
    overlay.set('adapt', 'done');
  } else { overlay.set('adapt', 'skip', 'выключено'); }
  // 2) LOGIC CHECK + 3) HEAL ALL FOUND ISSUES
  if (autoPipeEnabled('logic')) {
    overlay.set('logic', 'run');
    const lc = await api.post('/api/series/import-from-script/logic-check', { script: getScript() }, { timeoutMs: 120000 });
    if (lc.error) throw new Error('Проверка логики: ' + lc.error);
    const issues = lc.issues || [];
    if (!issues.length) {
      overlay.set('logic', 'done', 'чисто');
      overlay.set('heal', 'skip', 'нет проблем');
    } else {
      overlay.set('logic', 'done', `${issues.length} замеч.`);
      overlay.set('heal', 'run');
      const fx = await api.post('/api/series/import-from-script/apply-fixes', { script: getScript(), issues }, { timeoutMs: 180000 });
      if (fx.error) throw new Error('Лечение проблем: ' + fx.error);
      if (fx.script) { ta.dataset.preFixSnapshot = ta.value; ta.value = fx.script; appendUpdateStats(); }
      overlay.set('heal', 'done', `${fx.applied_count ?? issues.length} правок`);
    }
  } else {
    overlay.set('logic', 'skip', 'выключено');
    overlay.set('heal', 'skip', 'выключено');
  }
  // 4) ADD EPISODES
  if (autoPipeEnabled('add')) {
    overlay.set('add', 'run');
    await _appendCommitScript();   // closes the modal + refreshes series on success
    overlay.set('add', 'done');
    overlay.finish(true);
  } else {
    overlay.set('add', 'skip', 'выключено');
    overlay.finish(false, 'Готово — проверь сценарий и нажми «📜 Добавить серии».');
  }
}

async function appendGenerateScript(btn) {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  const count = parseInt(document.getElementById('append-gen-count')?.value, 10) || 5;
  if (count < 1 || count > 20) { showToast('Количество серий: 1-20', 4000); return; }
  const direction = (document.getElementById('append-gen-direction')?.value || '').trim();
  // Optional advanced parameters — empty = let Claude decide.
  const durationSecRaw = (document.getElementById('append-gen-duration-sec')?.value || '').trim();
  const linesCountRaw  = (document.getElementById('append-gen-lines-count')?.value || '').trim();
  const maxCharsRaw    = (document.getElementById('append-gen-max-chars')?.value || '').trim();
  const styleVal = (document.getElementById('append-gen-style')?.value || '').trim();
  const durationSec = durationSecRaw ? Math.max(30, Math.min(240, parseInt(durationSecRaw, 10))) : null;
  const linesCount  = linesCountRaw  ? Math.max(3, Math.min(40, parseInt(linesCountRaw, 10)))   : null;
  const maxChars    = maxCharsRaw    ? Math.max(1, Math.min(6, parseInt(maxCharsRaw, 10)))      : null;
  const noInterruptions = document.getElementById('append-gen-no-interruptions')?.checked !== false;
  const ta = document.getElementById('append-script-text');
  const statusEl = document.getElementById('append-gen-status');
  if (ta && ta.value.trim() && !await appConfirm({
    title: 'Перезаписать содержимое textarea?',
    message: `В textarea ниже уже есть текст (${ta.value.length} символов). Сгенерированный сценарий заменит его. Продолжить?`,
    okText: 'Да, перезаписать',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;

  // Decide whether the post-generation chain runs. If every stage is off in
  // Настройки, we keep the old manual behavior (preview + manual buttons).
  const usePipeline = autoPipeEnabled('adapt') || autoPipeEnabled('logic') || autoPipeEnabled('add');
  const overlay = usePipeline ? AppendPipelineOverlay.show([
    { key: 'generate', label: '✨ Генерация сценария' },
    { key: 'adapt',    label: '🔧 Подгонка под стандарт' },
    { key: 'logic',    label: '🧠 Проверка логики' },
    { key: 'heal',     label: '🩹 Лечение проблем' },
    { key: 'add',      label: '📜 Добавление серий' },
  ]) : null;

  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude пишет…';
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span> Генерирую ${count} серий… ~${30 * count}-${90 * count}с`;

  try {
    if (overlay) overlay.set('generate', 'run');
    const r = await api.post(
      `/api/series/${S.seriesId}/generate-script-batch`,
      { count, direction, duration_sec: durationSec, lines_count: linesCount, style: styleVal, max_main_chars_per_scene: maxChars, no_interruptions: noInterruptions },
      { timeoutMs: 600_000 },
    );
    if (r.error) throw new Error(r.error);
    if (!r.script) throw new Error('пустой ответ');
    if (ta) {
      ta.value = r.script;
      appendUpdateStats();
    }
    if (statusEl) {
      statusEl.innerHTML = `<span style="color:#4ade80">✓ Сгенерировано ${r.count} серий (№${r.first_episode}–${r.last_episode}). ` +
                          `Можно: 🧠 Проверить логику · 👁 Превью разбивки · 📜 Добавить серии</span>`;
    }
    // Auto-switch to paste mode so user sees the textarea + preview/logic buttons.
    setAppendMode('paste');
    if (overlay) {
      overlay.set('generate', 'done', `№${r.first_episode}–${r.last_episode}`);
      // Re-enable the generate button now — the overlay blocks input anyway, and
      // the chain may close the whole modal on success.
      btn.disabled = false;
      btn.innerHTML = orig;
      await _runAppendPipeline(overlay);
      return;
    }
    showToast(`✓ Сгенерировано ${r.count} серий — проверяй и добавляй`, 6000);
    // Auto-fire preview so the user immediately sees the episode list AND
    // any non-English-dialogue warning (Claude occasionally drifts to RU
    // when the bible/direction are in Russian). The preview re-runs lang
    // detection — it's regex-cheap (~10ms) so the extra call is fine.
    if (typeof appendPreviewSplit === 'function') {
      setTimeout(() => appendPreviewSplit(), 50);
    }
  } catch (e) {
    if (overlay) overlay.fail(e?.message || e);
    if (statusEl) statusEl.innerHTML = `<span style="color:#f87171">✗ Ошибка: ${esc(e?.message || e)}</span>`;
    showToast('Ошибка: ' + (e?.message || e), 6000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
function appendDropFile(ev) {
  const file = ev.dataTransfer?.files?.[0];
  if (!file) return;
  _appendReadFile(file);
}
function appendPickFile(input) {
  const file = input.files?.[0];
  if (!file) return;
  _appendReadFile(file);
}
function _appendReadFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    setVal('append-script-text', reader.result || '');
    appendUpdateStats();
  };
  reader.readAsText(file);
}
function appendUpdateStats() {
  const txt = (document.getElementById('append-script-text')?.value || '');
  const stats = document.getElementById('append-script-stats');
  if (stats) stats.textContent = `${txt.length.toLocaleString('ru-RU')} символов`;
}
async function appendPreviewSplit() {
  const script = (document.getElementById('append-script-text')?.value || '').trim();
  const previewEl = document.getElementById('append-script-preview');
  if (!script) { previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  delete _LANG_WARN_KEPT['append-script-text'];
  previewEl.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Анализирую разбивку...</div>';
  try {
    const r = await api.post('/api/series/import-from-script/preview', { script });
    if (r.error) throw new Error(r.error);
    const eps = r.episodes || [];
    const startNum = ((S.episodes || []).reduce((m, e) => Math.max(m, e.number || 0), 0)) + 1;
    if (!eps.length) {
      previewEl.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Не удалось разбить — будет добавлена 1 серия со всем текстом</div>';
      return;
    }
    const warnHtml = _renderLangWarning(r.dialogue_lang_warning, 'append-script-text');
    previewEl.innerHTML = `
      ${warnHtml}
      <div style="font-size:0.85rem;color:var(--success);margin-bottom:6px">
        ✓ Найдено эпизодов: <strong>${eps.length}</strong> · станут сериями <strong>№${startNum}–${startNum + eps.length - 1}</strong>
      </div>
      <div style="max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px;background:var(--surface2)">
        ${eps.map((e, i) => `
          <div style="padding:5px 4px;border-bottom:1px solid var(--border);font-size:0.82rem">
            <strong>№${startNum + i}</strong>
            ${e.title ? `<span style="color:var(--muted)"> · ${esc(e.title)}</span>` : ''}
            <span style="color:var(--muted);margin-left:8px">(${e.length.toLocaleString('ru-RU')} симв.)</span>
            <div style="color:var(--muted);font-size:0.75rem;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.preview)}</div>
          </div>`).join('')}
      </div>`;
  } catch (e) {
    previewEl.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  }
}
// Last logic-check result, held in module scope so the «Применить» button can
// look up which issues are ticked. Cleared every time check re-runs.
let _appendLogicIssues = [];

async function appendLogicCheck() {
  const script = (document.getElementById('append-script-text')?.value || '').trim();
  const out = document.getElementById('append-script-logic');
  if (!script) { out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Claude читает все серии и ищет противоречия… ~15-40 сек</div>';
  _appendLogicIssues = [];
  try {
    const r = await api.post('/api/series/import-from-script/logic-check', { script }, { timeoutMs: 120000 });
    if (r.error) throw new Error(r.error);
    const issues = r.issues || [];
    _appendLogicIssues = issues;
    if (!issues.length) {
      out.innerHTML = `<div style="padding:10px;background:rgba(74,222,128,0.12);border:1px solid rgba(74,222,128,0.35);border-radius:6px;color:#4ade80;font-size:0.85rem">✅ Логика чистая — проанализировано серий: <strong>${r.episodes_analyzed}</strong>. Противоречий не найдено.</div>`;
      return;
    }
    const sevColor = { critical: '#f87171', high: '#fbbf24', medium: '#a78bfa', low: '#9ca3af' };
    const sevLabel = { critical: 'CRIT', high: 'HIGH', medium: 'MED', low: 'LOW' };
    const typeLabel = {
      contradiction:    '⚡ Противоречие',
      plot_hole:        '🕳 Плот-хол',
      forgotten_thread: '🧵 Забытая линия',
      continuity:       '🔗 Continuity',
      timeline:         '⏱ Таймлайн',
    };
    out.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;font-size:0.85rem">
        <span style="color:var(--muted)">🧠 Найдено проблем:</span>
        <strong style="color:#fbbf24">${issues.length}</strong>
        <span style="color:var(--muted)">(серий: ${r.episodes_analyzed})</span>
        <button class="btn-ghost btn-sm" onclick="appendLogicSelectAll(true)" style="margin-left:auto">✓ Все</button>
        <button class="btn-ghost btn-sm" onclick="appendLogicSelectAll(false)">✕ Снять</button>
        <button class="btn-accent btn-sm" onclick="appendLogicApply(this)" title="Claude перепишет сценарий минимально, только исправив выделенные проблемы. Результат подставится в textarea — после этого можно снова «Проверить логику».">🩹 Полечить выбранные</button>
      </div>
      <div style="max-height:340px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--surface2)">
        ${issues.map((it, i) => {
          const presetChecked = (it.severity === 'critical' || it.severity === 'high') ? 'checked' : '';
          return `
          <label style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);font-size:0.82rem;cursor:pointer" data-logic-issue="${i}">
            <input type="checkbox" class="logic-issue-cb" data-idx="${i}" ${presetChecked} style="margin-top:3px;width:16px;height:16px;flex:0 0 16px;accent-color:#10b981">
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
                <span style="background:${sevColor[it.severity] || '#9ca3af'};color:#000;padding:2px 6px;border-radius:4px;font-weight:700;font-size:0.7rem">${sevLabel[it.severity] || it.severity || '?'}</span>
                <span style="color:var(--muted)">${esc(typeLabel[it.type] || it.type || '')}</span>
                <span style="color:var(--accent);font-weight:600;margin-left:auto">Эп. ${(it.episodes || []).join(', ')}</span>
              </div>
              <div style="color:var(--text);font-weight:600;margin-bottom:3px">${esc(it.summary || '')}</div>
              ${it.evidence ? `<div style="color:var(--muted);font-style:italic;font-size:0.78rem;margin-bottom:3px">«${esc(it.evidence)}»</div>` : ''}
              ${it.fix ? `<div style="color:#4ade80;font-size:0.78rem">→ ${esc(it.fix)}</div>` : ''}
            </div>
          </label>`;
        }).join('')}
      </div>
      <div style="margin-top:6px;font-size:0.78rem;color:var(--muted)">
        💡 Поставь галочки на тех проблемах что хочешь починить → «🩹 Полечить выбранные». Critical/High по умолчанию уже отмечены. Можно править textarea вручную и снова жать «Проверить логику».
      </div>`;
  } catch (e) {
    out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  }
}

function appendLogicSelectAll(val) {
  document.querySelectorAll('.logic-issue-cb').forEach(cb => { cb.checked = val; });
}

async function appendLogicApply(btn) {
  const ta = document.getElementById('append-script-text');
  const script = (ta?.value || '').trim();
  if (!script) { showToast('Сценарий пустой', 3000); return; }
  const selected = [...document.querySelectorAll('.logic-issue-cb:checked')]
    .map(cb => _appendLogicIssues[parseInt(cb.dataset.idx, 10)])
    .filter(Boolean);
  if (!selected.length) {
    showToast('Не выделено ни одной проблемы для лечения', 3000);
    return;
  }
  if (!await appConfirm({
    title: '🩹 Полечить выделенные проблемы?',
    message: `Будет переписано: ${selected.length} проблем(ы).\n\n` +
             `Claude сделает минимальные правки — оставит всё остальное как есть. ` +
             `Получившийся сценарий заменит текущий в textarea (но в любой момент можно нажать Cmd+Z).`,
    okText: '🩹 Полечить',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude переписывает… ~30-60 сек';
  const banner = LongOpBanner.show(
    `🩹 Лечим ${selected.length} проблем(ы) логики`,
    [
      'Читаем сценарий и список проблем…',
      'Анализируем затронутые сцены…',
      'Переписываем минимальными правками…',
      'Сохраняем сценарий…',
    ],
  );
  try {
    const r = await api.post('/api/series/import-from-script/apply-fixes',
      { script, issues: selected },
      { timeoutMs: 180000 });
    if (r.error) throw new Error(r.error);
    if (!r.script) throw new Error('пустой ответ');
    // Save undo snapshot (Cmd+Z would only undo characters typed by user; this
    // is a programmatic replace and bypasses native undo). Stash in dataset.
    ta.dataset.preFixSnapshot = script;
    ta.value = r.script;
    appendUpdateStats();
    const changes = r.changes || [];
    const changesHtml = changes.length
      ? changes.map(c => `<li><strong>#${c.issue_index || '?'}:</strong> ${esc(c.summary || '')}</li>`).join('')
      : '<li>(no per-issue summary returned)</li>';
    const out = document.getElementById('append-script-logic');
    if (out) {
      out.innerHTML = `
        <div style="padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:6px;font-size:0.82rem">
          <div style="color:#10b981;font-weight:700;margin-bottom:6px">✓ Применено ${r.applied_count} правок. Что изменилось:</div>
          <ul style="margin:6px 0 6px 18px;color:var(--text)">${changesHtml}</ul>
          <div style="display:flex;gap:8px;margin-top:8px">
            <button class="btn-ghost btn-sm" onclick="appendLogicUndo()" title="Вернуть текст до правок">↶ Отменить</button>
            <button class="btn-accent btn-sm" onclick="appendLogicCheck()" title="Перепроверить новый текст на оставшиеся проблемы">🧠 Проверить ещё раз</button>
          </div>
        </div>`;
    }
    showToast(`✓ Применено ${r.applied_count} правок`, 5000);
  } catch (e) {
    showToast('Ошибка лечения: ' + (e?.message || e), 6000);
  } finally {
    banner.close();
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

function appendLogicUndo() {
  const ta = document.getElementById('append-script-text');
  if (!ta || !ta.dataset.preFixSnapshot) {
    showToast('Нет снапшота для отката', 3000);
    return;
  }
  ta.value = ta.dataset.preFixSnapshot;
  delete ta.dataset.preFixSnapshot;
  appendUpdateStats();
  showToast('↶ Откачено к версии до правок', 3000);
  const out = document.getElementById('append-script-logic');
  if (out) out.innerHTML = '';
}

// ── Logic-check for the "New Series → Import" modal ────────────────────────
// Mirror of appendLogicCheck / appendLogicApply / appendLogicUndo but wired
// to import-series-script + import-series-logic instead of append-* elements.

let _importLogicIssues = [];

async function importLogicCheck(btn) {
  const script = (document.getElementById('import-series-script')?.value || '').trim();
  const out = document.getElementById('import-series-logic');
  if (!script) { if (out) out.innerHTML = '<div style="color:var(--warning);font-size:0.85rem">Сценарий пустой</div>'; return; }
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Читаю…'; }
  if (out) out.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)"><span class="spinner"></span> Claude читает все серии и ищет противоречия… ~15-40 сек</div>';
  _importLogicIssues = [];
  try {
    const r = await api.post('/api/series/import-from-script/logic-check', { script }, { timeoutMs: 120000 });
    if (r.error) throw new Error(r.error);
    const issues = r.issues || [];
    _importLogicIssues = issues;
    if (!issues.length) {
      if (out) out.innerHTML = `<div style="padding:10px;background:rgba(74,222,128,0.12);border:1px solid rgba(74,222,128,0.35);border-radius:6px;color:#4ade80;font-size:0.85rem">✅ Логика чистая — проанализировано серий: <strong>${r.episodes_analyzed}</strong>. Противоречий не найдено.</div>`;
      return;
    }
    const sevColor = { critical: '#f87171', high: '#fbbf24', medium: '#a78bfa', low: '#9ca3af' };
    const sevLabel = { critical: 'CRIT', high: 'HIGH', medium: 'MED', low: 'LOW' };
    const typeLabel = {
      contradiction:    '⚡ Противоречие',
      plot_hole:        '🕳 Плот-хол',
      forgotten_thread: '🧵 Забытая линия',
      continuity:       '🔗 Continuity',
      timeline:         '⏱ Таймлайн',
    };
    if (out) out.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px;font-size:0.85rem">
        <span style="color:var(--muted)">🧠 Найдено проблем:</span>
        <strong style="color:#fbbf24">${issues.length}</strong>
        <span style="color:var(--muted)">(серий: ${r.episodes_analyzed})</span>
        <button class="btn-ghost btn-sm" onclick="importLogicSelectAll(true)" style="margin-left:auto">✓ Все</button>
        <button class="btn-ghost btn-sm" onclick="importLogicSelectAll(false)">✕ Снять</button>
        <button class="btn-accent btn-sm" onclick="importLogicApply(this)" title="Claude перепишет сценарий минимально, только исправив выделенные проблемы">🩹 Полечить выбранные</button>
      </div>
      <div style="max-height:300px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--surface2)">
        ${issues.map((it, i) => {
          const presetChecked = (it.severity === 'critical' || it.severity === 'high') ? 'checked' : '';
          return `
          <label style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);font-size:0.82rem;cursor:pointer">
            <input type="checkbox" class="import-logic-cb" data-idx="${i}" ${presetChecked} style="margin-top:3px;width:16px;height:16px;flex:0 0 16px;accent-color:#10b981">
            <div style="flex:1;min-width:0">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap">
                <span style="background:${sevColor[it.severity] || '#9ca3af'};color:#000;padding:2px 6px;border-radius:4px;font-weight:700;font-size:0.7rem">${sevLabel[it.severity] || it.severity || '?'}</span>
                <span style="color:var(--muted)">${esc(typeLabel[it.type] || it.type || '')}</span>
                <span style="color:var(--accent);font-weight:600;margin-left:auto">Эп. ${(it.episodes || []).join(', ')}</span>
              </div>
              <div style="color:var(--text);font-weight:600;margin-bottom:3px">${esc(it.summary || '')}</div>
              ${it.evidence ? `<div style="color:var(--muted);font-style:italic;font-size:0.78rem;margin-bottom:3px">«${esc(it.evidence)}»</div>` : ''}
              ${it.fix ? `<div style="color:#4ade80;font-size:0.78rem">→ ${esc(it.fix)}</div>` : ''}
            </div>
          </label>`;
        }).join('')}
      </div>
      <div style="margin-top:6px;font-size:0.78rem;color:var(--muted)">
        💡 Поставь галочки на тех проблемах что хочешь починить → «🩹 Полечить выбранные». Critical/High по умолчанию уже отмечены.
      </div>`;
  } catch (e) {
    if (out) out.innerHTML = `<div style="color:var(--danger);font-size:0.85rem">Ошибка: ${esc(e?.message || e)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '🧠 Проверить логику'; }
  }
}

function importLogicSelectAll(val) {
  document.querySelectorAll('.import-logic-cb').forEach(cb => { cb.checked = val; });
}

async function importLogicApply(btn) {
  const ta = document.getElementById('import-series-script');
  const script = (ta?.value || '').trim();
  if (!script) { showToast('Сценарий пустой', 3000); return; }
  const selected = [...document.querySelectorAll('.import-logic-cb:checked')]
    .map(cb => _importLogicIssues[parseInt(cb.dataset.idx, 10)])
    .filter(Boolean);
  if (!selected.length) { showToast('Не выделено ни одной проблемы для лечения', 3000); return; }
  if (!await appConfirm({
    title: '🩹 Полечить выделенные проблемы?',
    message: `Будет переписано: ${selected.length} проблем(ы).\n\nClaude сделает минимальные правки — оставит всё остальное как есть.`,
    okText: '🩹 Полечить', cancelText: 'Отмена', okStyle: 'accent',
  })) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude переписывает…';
  const banner = LongOpBanner.show(
    `🩹 Лечим ${selected.length} проблем(ы) логики`,
    [
      'Читаем сценарий и список проблем…',
      'Анализируем затронутые сцены…',
      'Переписываем минимальными правками…',
      'Сохраняем сценарий…',
    ],
  );
  try {
    const r = await api.post('/api/series/import-from-script/apply-fixes',
      { script, issues: selected }, { timeoutMs: 180000 });
    if (r.error) throw new Error(r.error);
    if (!r.script) throw new Error('пустой ответ');
    ta.dataset.preFixSnapshot = script;
    ta.value = r.script;
    // Update char counter
    const stats = document.getElementById('import-series-script-stats');
    if (stats) stats.textContent = r.script.length.toLocaleString('ru') + ' символов';
    const changes = r.changes || [];
    const changesHtml = changes.length
      ? changes.map(c => `<li><strong>#${c.issue_index || '?'}:</strong> ${esc(c.summary || '')}</li>`).join('')
      : '<li>(no per-issue summary returned)</li>';
    const out = document.getElementById('import-series-logic');
    if (out) out.innerHTML = `
      <div style="padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:6px;font-size:0.82rem">
        <div style="color:#10b981;font-weight:700;margin-bottom:6px">✓ Применено ${r.applied_count} правок. Что изменилось:</div>
        <ul style="margin:6px 0 6px 18px;color:var(--text)">${changesHtml}</ul>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn-ghost btn-sm" onclick="importLogicUndo()" title="Вернуть текст до правок">↶ Отменить</button>
          <button class="btn-accent btn-sm" onclick="importLogicCheck(this)">🧠 Проверить ещё раз</button>
        </div>
      </div>`;
    showToast(`✓ Применено ${r.applied_count} правок`, 5000);
  } catch (e) {
    showToast('Ошибка лечения: ' + (e?.message || e), 6000);
  } finally {
    banner.close();
    btn.disabled = false; btn.innerHTML = orig;
  }
}

function importLogicUndo() {
  const ta = document.getElementById('import-series-script');
  if (!ta || !ta.dataset.preFixSnapshot) { showToast('Нет снапшота для отката', 3000); return; }
  ta.value = ta.dataset.preFixSnapshot;
  delete ta.dataset.preFixSnapshot;
  const stats = document.getElementById('import-series-script-stats');
  if (stats) stats.textContent = ta.value.length.toLocaleString('ru') + ' символов';
  showToast('↶ Откачено к версии до правок', 3000);
  const out = document.getElementById('import-series-logic');
  if (out) out.innerHTML = '';
}

