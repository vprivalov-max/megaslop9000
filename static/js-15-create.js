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

