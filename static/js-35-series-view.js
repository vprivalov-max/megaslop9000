// ── Series view ───────────────────────────────────────────────────────────────
async function loadSeriesView() {
  S.series = await api.get(`/api/series/${S.seriesId}`);
  S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
  renderSeriesView();
  setBreadcrumb([
    { label: 'Сериалы', action: "navigate('projects')" },
    { label: S.series.title },
  ]);
  // Resume the import-progress banner if a script-import job is still active
  // (e.g. user closed the tab and reopened mid-import).
  try {
    const st = await fetch(`/api/series/${S.seriesId}/import-status`).then(r => r.json());
    if (st && (st.running || (st.done > 0 && st.total > 0 && st.finished_at &&
        (Date.now() - new Date(st.finished_at).getTime()) < 60000))) {
      pollImportStatus(S.seriesId);
    }
  } catch {}
}

function applyVideoProviderMode() {
  const provider = (S.series && S.series.video_provider) || 'reteller';
  document.body.classList.toggle('seedance-mode', provider === 'seedance');
  const sel = document.getElementById('video-provider-select');
  if (sel) sel.value = provider;
  // Sync image-provider dropdown to persisted choice. '' = auto fallback.
  const imgSel = document.getElementById('image-provider-select');
  if (imgSel) {
    const pref = (S.series && S.series.preferred_image_provider) || '';
    imgSel.value = pref || 'auto';
  }
  // Cache provider per-series in localStorage so the next page-refresh can
  // apply body.seedance-mode IMMEDIATELY (before the /api/series fetch comes
  // back). Without this, Reteller-mode HTML defaults flash for ~100-300ms
  // every refresh and look like "режим слетел на ретеллер".
  try {
    if (S.seriesId) {
      localStorage.setItem(`series_provider:${S.seriesId}`, provider);
      localStorage.setItem('last_video_provider', provider);
    }
  } catch {}
}

// Pre-apply body class from cache so the page doesn't flash Reteller-mode
// on refresh. Called from navigate() before the fetch fires; reconciled by
// applyVideoProviderMode() once S.series arrives.
function _preApplyVideoProviderMode(seriesId) {
  try {
    let p = '';
    if (seriesId) p = localStorage.getItem(`series_provider:${seriesId}`) || '';
    if (!p) p = localStorage.getItem('last_video_provider') || '';
    if (p) document.body.classList.toggle('seedance-mode', p === 'seedance');
  } catch {}
}

// Persist per-series image-provider preference (banana / seedream / auto).
// Backend reads this via _series_image_provider() and reorders the provider
// chain in avai_generate so user's pick goes first.
async function setImageProvider(provider) {
  if (!S.series) return;
  S.series.preferred_image_provider = provider === 'auto' ? '' : provider;
  try {
    await api.put(`/api/series/${S.seriesId}`, { preferred_image_provider: S.series.preferred_image_provider });
    showToast(`✓ Image provider: ${provider}`);
  } catch (e) {
    console.error('setImageProvider failed', e);
  }
}

async function setVideoProvider(provider) {
  if (!S.series) return;
  // Switching to Reteller requires a Reteller API key. Block + prompt if missing
  // (primary user is grandfathered to global env, others must enter their own).
  if (provider === 'reteller') {
    try {
      const me = await fetch('/api/me').then(r => r.ok ? r.json() : null);
      if (me && me.authenticated && !me.has_reteller_key) {
        // Roll back the dropdown UI so it reflects reality
        const sel = document.getElementById('video-provider-select');
        if (sel) sel.value = S.series.video_provider || 'seedance';
        _showApiKeySetupModal('reteller');
        return;
      }
    } catch {}
  }
  S.series.video_provider = provider;
  applyVideoProviderMode();
  try {
    await api.put(`/api/series/${S.seriesId}`, { video_provider: provider });
  } catch (e) {
    console.error('setVideoProvider failed', e);
  }
}

function renderSeriesView() {
  const s = S.series;
  document.getElementById('series-title-display').textContent = s.title;
  applyVideoProviderMode();

  // Bible
  const autogenOn = !!s.auto_generate_assets;
  const dlHint = s.dialogue_language_hint;
  const dlLabel = dlHint ? (_LANG_LABELS[dlHint] || dlHint) : '';
  document.getElementById('bible-content').innerHTML = `
    ${dlHint ? `<div style="margin-bottom:8px;padding:6px 10px;background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.35);border-radius:6px;font-size:0.82rem;color:#fbbf24" title="При импорте сценария тулза обнаружила диалоги не на английском и юзер выбрал «оставить как есть». Видео-генерация будет на оригинальном языке.">🌐 Диалоги: <strong>${esc(dlLabel)}</strong> — не EN</div>` : ''}
    ${s.genre ? `<div><strong>Жанр:</strong> ${esc(s.genre)}</div>` : ''}
    ${s.tone ? `<div><strong>Тон:</strong> ${esc(s.tone)}</div>` : ''}
    ${s.target_audience ? `<div><strong>Аудитория:</strong> ${esc(s.target_audience)}</div>` : ''}
    ${s.world_description ? `<div style="margin-top:6px">${esc(s.world_description)}</div>` : ''}
    <label style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:8px 10px;background:rgba(132,94,247,0.06);border:1px solid rgba(132,94,247,0.2);border-radius:6px;cursor:pointer;font-size:0.85rem">
      <input type="checkbox" id="series-autogen-toggle" ${autogenOn ? 'checked' : ''} onchange="toggleSeriesAutogen()" style="width:16px;height:16px">
      <span><strong>⚡ Авто-генерация картинок</strong> — новые персонажи, костюмы и локации генерятся сами как появляются</span>
    </label>
    <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
      <button class="btn btn-sm btn-ghost" onclick="triggerAutogenSweep()" id="autogen-sweep-btn" style="font-size:0.78rem">🎨 Сгенерировать недостающее</button>
      <span id="autogen-sweep-status" style="font-size:0.78rem;color:var(--muted)"></span>
    </div>
    <div id="canon-summary" style="margin-top:10px;padding:8px 10px;background:rgba(80,180,140,0.06);border:1px solid rgba(80,180,140,0.2);border-radius:6px;font-size:0.83rem;color:var(--muted);cursor:pointer" onclick="openCanonViewer()">
      <strong style="color:var(--text)">📚 Канон сериала</strong> <span style="opacity:0.7">— автоматическая проверка логики</span>
      <div id="canon-summary-stats" style="margin-top:4px;font-size:0.78rem">загрузка…</div>
    </div>
    ${renderEraBanner(s)}
    ${renderAnthroBanner(s)}
  `;
  loadCanonSummary();
  checkAutogenOnLoad();

  // Pipeline
  renderPipeline();

  // Characters
  renderCharactersList();

  // Locations
  renderLocationsList();

  // Items
  renderItemsList();

  // Style
  renderStyleSection();

  // Episodes
  renderEpisodesList();

  if (typeof _rangeGenUI === 'function') _rangeGenUI();
  _restoreGenSelection();
  _updateRangeGenSelectionCount();
}

function selectAllReadyEpisodes() {
  if (!S._genSelected) S._genSelected = new Set();
  let added = 0;
  for (const ep of (S.episodes || [])) {
    const info = _episodeReadyInfo(ep);
    const gs = ep.gen_status || '';
    if (info.ready && gs !== 'done' && gs !== 'generating') {
      if (!S._genSelected.has(ep.number)) {
        S._genSelected.add(ep.number);
        added++;
      }
    }
  }
  try { localStorage.setItem(`gen_selected:${S.seriesId}`, JSON.stringify([...S._genSelected])); } catch {}
  renderEpisodesList();
  showToast(added ? `✓ Выделено ${added} новых готовых серий` : 'Все готовые серии уже выделены', 3000);
}

function clearEpisodeSelection() {
  S._genSelected = new Set();
  try { localStorage.removeItem(`gen_selected:${S.seriesId}`); } catch {}
  renderEpisodesList();
}

// ── Logic check on selected episodes ─────────────────────────────────────
// Mirrors the «🧠 Проверить логику» button in the append-script modal but
// works on already-saved episodes selected via checkboxes in the series view.
// On apply, fixes are written DIRECTLY to ep.script on disk (with history
// snapshot for undo via the script-history endpoint).
let _logicMultiIssues = [];
let _logicMultiEpNums = [];

// Bulk re-parse [BLOCKING] outfits across episodes. Uses S._genSelected when
// non-empty, otherwise runs on ALL episodes that have a script (the legacy
// fix path — series with episodes generated before outfit-sync was working).
async function reanalyzeOutfitsForSelected() {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  if (!S._genSelected) _restoreGenSelection();
  const selectedNums = [...(S._genSelected || [])].sort((a, b) => a - b);
  const eps = (S.episodes || []);
  const scope = selectedNums.length > 0 ? 'выделенных' : 'всех серий со сценарием';
  let validNums = selectedNums.filter(n => {
    const ep = eps.find(e => e.number === n);
    return ep && (ep.script || '').trim().length > 0;
  });
  if (selectedNums.length === 0) {
    validNums = eps.filter(e => (e.script || '').trim().length > 0).map(e => e.number);
  }
  if (!validNums.length) {
    showToast('Нет эпизодов со сценарием для перепроанализирования', 4000);
    return;
  }
  const btn = event && event.target ? event.target : null;
  const orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Парсю ${validNums.length}…`; }
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/reanalyze-outfits`,
      { episode_numbers: validNums }
    );
    if (r.error) throw new Error(r.error);
    const total = r.total_new_outfits || 0;
    const weak = r.total_weak_descs_fixed || 0;
    if (total === 0 && weak === 0) {
      showToast(`✓ Перепарсил ${r.episodes_processed} эпизод${r.episodes_processed === 1 ? '' : (r.episodes_processed < 5 ? 'а' : 'ов')} — всё актуально, слабых описаний не найдено`, 5500);
    } else {
      const flat = (r.by_episode || []).flatMap(b => (b.new_outfits || []));
      if (flat.length) _notifyNewOutfits(flat);
      const bits = [];
      if (total > 0) bits.push(`+${total} нов${total === 1 ? 'ый' : 'ых'} образ${total === 1 ? '' : (total < 5 ? 'а' : 'ов')}`);
      if (weak > 0) bits.push(`усилил описания у ${weak} существующ${weak === 1 ? 'его' : 'их'} (картинки перегенерятся)`);
      showToast(`✓ Перепарсил ${r.episodes_processed} (${scope}) — ${bits.join(', ')}, генерация в фоне`, 9000);
    }
    // Refresh series + episodes so the new outfit chips appear immediately
    try {
      const fresh = await api.get(`/api/series/${S.seriesId}`);
      if (fresh && !fresh.error) {
        S.series = fresh;
        if (typeof renderCharactersList === 'function') renderCharactersList();
      }
      const epsRes = await api.get(`/api/series/${S.seriesId}/episodes`);
      if (Array.isArray(epsRes)) {
        S.episodes = epsRes;
        if (typeof renderEpisodesList === 'function') renderEpisodesList();
      }
    } catch (e) { /* best-effort refresh */ }
  } catch (e) {
    showToast('Ошибка перепроанализа: ' + (e.message || e), 6000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = orig; }
  }
}

async function checkLogicOnSelected() {
  if (!S.seriesId) { showToast('Открой сериал'); return; }
  if (!S._genSelected) _restoreGenSelection();
  const nums = [...(S._genSelected || [])].sort((a, b) => a - b);
  if (!nums.length) {
    showToast('Сначала отметь галочками серии для проверки', 4000);
    return;
  }
  // Filter: only those that have a script (cast_extracted not required —
  // we work on raw script text).
  const eps = (S.episodes || []);
  const validNums = nums.filter(n => {
    const ep = eps.find(e => e.number === n);
    return ep && (ep.script || '').trim().length > 0;
  });
  if (!validNums.length) {
    showToast('У выделенных серий нет сценариев для проверки', 4000);
    return;
  }
  _logicMultiEpNums = validNums;
  _logicMultiIssues = [];
  const statusEl = document.getElementById('logic-multi-status');
  const issuesEl = document.getElementById('logic-multi-issues');
  if (statusEl) statusEl.innerHTML = `<span class="spinner"></span> Claude читает ${validNums.length} серий: ${validNums.map(n => '№' + n).join(', ')}…  ~${Math.max(15, validNums.length * 6)}-${validNums.length * 12}с`;
  if (issuesEl) issuesEl.innerHTML = '';
  openModal('modal-logic-multi');
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/episodes/logic-check-multi`,
      { episode_numbers: validNums },
      { timeoutMs: 600_000 }
    );
    if (r.error) throw new Error(r.error);
    const issues = r.issues || [];
    _logicMultiIssues = issues;
    _logicMultiRenderIssues(issues, r.episodes_analyzed || validNums.length);
  } catch (e) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#f87171">Ошибка: ${esc(e?.message || e)}</span>`;
  }
}

function _logicMultiRenderIssues(issues, analyzedCount) {
  const statusEl = document.getElementById('logic-multi-status');
  const issuesEl = document.getElementById('logic-multi-issues');
  if (!issues.length) {
    if (statusEl) statusEl.innerHTML = `<span style="color:#4ade80">✅ Логика чистая — проанализировано серий: <strong>${analyzedCount}</strong>. Противоречий не найдено.</span>`;
    if (issuesEl) issuesEl.innerHTML = '';
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
  if (statusEl) {
    statusEl.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="color:var(--muted)">🧠 Найдено:</span>
        <strong style="color:#fbbf24">${issues.length} проблем</strong>
        <span style="color:var(--muted)">(серий: ${analyzedCount})</span>
        <button class="btn-ghost btn-sm" onclick="_logicMultiSelectAll(true)" style="margin-left:auto">✓ Все</button>
        <button class="btn-ghost btn-sm" onclick="_logicMultiSelectAll(false)">✕ Снять</button>
        <button class="btn-accent btn-sm" onclick="_logicMultiApply(this)" title="Claude перепишет соответствующие серии минимально, только исправив выделенные проблемы. Старые версии сохранятся в истории сценариев — можно откатить.">🩹 Полечить выбранные</button>
      </div>
    `;
  }
  if (issuesEl) {
    issuesEl.innerHTML = `
      <div style="max-height:380px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;background:var(--surface2);margin-top:8px">
        ${issues.map((it, i) => {
          const preset = (it.severity === 'critical' || it.severity === 'high') ? 'checked' : '';
          return `
            <label style="display:flex;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);font-size:0.82rem;cursor:pointer">
              <input type="checkbox" class="logic-multi-cb" data-idx="${i}" ${preset} style="margin-top:3px;width:16px;height:16px;flex:0 0 16px;accent-color:#10b981">
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
      <div style="margin-top:8px;font-size:0.78rem;color:var(--muted)">
        💡 CRIT и HIGH предчекнуты автоматически. После «🩹 Полечить» Claude перепишет соответствующие серии и сохранит старые версии в истории.
      </div>
    `;
  }
}

function _logicMultiSelectAll(val) {
  document.querySelectorAll('.logic-multi-cb').forEach(cb => { cb.checked = val; });
}

async function _logicMultiApply(btn) {
  const selected = [...document.querySelectorAll('.logic-multi-cb:checked')]
    .map(cb => _logicMultiIssues[parseInt(cb.dataset.idx, 10)])
    .filter(Boolean);
  if (!selected.length) { showToast('Не отмечено ни одной проблемы', 3000); return; }
  if (!await appConfirm({
    title: '🩹 Полечить выделенные проблемы?',
    message: `Будет переписано: ${selected.length} проблем(ы) в ${_logicMultiEpNums.length} сериях.\n\n` +
             `Claude перепишет затронутые серии минимально — остальные строки сохранятся дословно. ` +
             `Старые версии сценариев попадут в историю — откатить можно через «📜 История сценария» на странице эпизода.`,
    okText: '🩹 Полечить',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Claude переписывает…';
  const banner = LongOpBanner.show(
    `🩹 Лечим ${selected.length} проблем(ы) в ${_logicMultiEpNums.length} сериях`,
    [
      `Загружаем сценарии серий: ${_logicMultiEpNums.map(n => '№' + n).join(', ')}…`,
      'Claude читает контекст и список проблем…',
      'Анализирует затронутые сцены и диалоги…',
      'Переписывает минимально (~30-90 сек на партию)…',
      'Сохраняет новые версии и старые в историю…',
    ],
  );
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/episodes/logic-apply-multi`,
      { episode_numbers: _logicMultiEpNums, issues: selected },
      { timeoutMs: 300_000 }
    );
    if (r.error) throw new Error(r.error);
    const updated = r.updated || [];
    const skipped = r.skipped || [];
    const changes = r.changes || [];
    const statusEl = document.getElementById('logic-multi-status');
    const issuesEl = document.getElementById('logic-multi-issues');
    if (statusEl) {
      statusEl.innerHTML = `<span style="color:#4ade80">✓ Переписано ${updated.length} серий${skipped.length ? ` (пропущено ${skipped.length})` : ''}. Применено ${r.applied_count} правок.</span>`;
    }
    if (issuesEl) {
      const updRows = updated.map(u =>
        `<li><strong>Эп. ${u.number}:</strong> ${u.before_len} → ${u.after_len} символов</li>`
      ).join('');
      const skipRows = skipped.map(s => `<li>Эп. ${s.number}: ${esc(s.reason)}</li>`).join('');
      const chgRows = changes.map(c => `<li><strong>#${c.issue_index || '?'}:</strong> ${esc(c.summary || '')}</li>`).join('');
      issuesEl.innerHTML = `
        <div style="padding:10px 12px;background:rgba(16,185,129,0.10);border:1px solid rgba(16,185,129,0.35);border-radius:6px;font-size:0.82rem;margin-top:8px">
          <div style="color:#10b981;font-weight:700;margin-bottom:6px">✓ Что переписано:</div>
          <ul style="margin:4px 0 8px 18px;color:var(--text)">${updRows || '<li>(ничего)</li>'}</ul>
          ${skipped.length ? `<div style="color:#fbbf24;margin-top:6px">⚠ Пропущено:</div><ul style="margin:4px 0 0 18px">${skipRows}</ul>` : ''}
          ${chgRows ? `<div style="color:var(--muted);margin-top:8px;font-size:0.78rem">Правки по проблемам:</div><ul style="margin:4px 0 0 18px;color:var(--muted);font-size:0.78rem">${chgRows}</ul>` : ''}
          <div style="display:flex;gap:8px;margin-top:10px">
            <button class="btn-accent btn-sm" onclick="checkLogicOnSelected()">🧠 Проверить ещё раз</button>
            <button class="btn-ghost btn-sm" onclick="closeModal('modal-logic-multi')">Закрыть</button>
          </div>
        </div>`;
    }
    // Reload episodes so UI sees the rewritten scripts.
    try {
      S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
      if (typeof renderEpisodesList === 'function') renderEpisodesList();
    } catch {}
    showToast(`✓ Переписано ${updated.length} серий`, 6000);
  } catch (e) {
    showToast('Ошибка: ' + (e?.message || e), 6000);
  } finally {
    banner.close();
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

// Force-reset gen_status on episodes that are stuck in 'generating' but have
// no actual generation in progress (no in-flight chunks). Useful after a
// browser crash or range-gen crash that left orphan «generating» markers.
async function resetStuckGenStatuses() {
  if (!S.seriesId) return;
  const stuck = (S.episodes || []).filter(e => e.gen_status === 'generating');
  if (!stuck.length) {
    showToast('Нет серий со статусом «генерится»', 3000);
    return;
  }
  if (!await appConfirm({
    title: '🔄 Сбросить статус генерации',
    message: `Найдено ${stuck.length} серий со статусом «🎬 Генерится…»: ${stuck.map(e => '№' + e.number).join(', ')}.\n\n` +
             `Сбросить им статус? Это нужно делать если генерация на самом деле НЕ идёт (упал range-gen или закрылась вкладка). ` +
             `Если генерация ИДЁТ в данный момент — нажми «Остановить» в виджете внизу справа, а не эту кнопку.`,
    okText: '🔄 Сбросить',
    cancelText: 'Отмена',
    okStyle: 'accent',
  })) return;
  let reset = 0, failed = 0;
  for (const ep of stuck) {
    try {
      await api.put(`/api/series/${S.seriesId}/episodes/${ep.number}`, { gen_status: null });
      ep.gen_status = null;
      reset++;
    } catch (e) { failed++; }
  }
  renderEpisodesList();
  showToast(`✓ Сброшено ${reset} серий${failed ? ` (${failed} не удалось)` : ''}`, 5000);
}

function renderEraBanner(s) {
  // Surface auto-detected historical/genre era so the user can accept,
  // pick another, or refuse before any non-modern look is baked into
  // character portraits. The server only applies the era to image-gen
  // when era_confirmed === true; until then assets stay modern.
  const detected = s._era_detected || '';
  const label = s._era_detected_label || '';
  const confirmed = !!s._era_confirmed;
  const choice = s._era_choice || 'auto';
  // Two distinct UI states:
  //   1) Detected but not confirmed → SUGGESTION banner (yellow).
  //   2) Confirmed with a specific era → settled INFO chip (subtle).
  if (detected && !confirmed) {
    return `
      <div id="era-banner-suggest" style="margin-top:10px;padding:10px 12px;background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.45);border-radius:6px;font-size:0.85rem">
        <div style="margin-bottom:8px;line-height:1.4">
          <strong style="color:#fbbf24">🏛 Эпоха сериала</strong> —
          судя по описанию, вашему сериалу подходит эра <strong>«${esc(label)}»</strong>.
          Это правильно? Если да, портреты персонажей будут сгенерированы в этом стиле.
          Если нет — поставьте «Современность» или выберите другую эпоху.
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-sm btn-primary" onclick="confirmEra('${detected}')">✓ Принять «${esc(label)}»</button>
          <button class="btn btn-sm btn-ghost" onclick="confirmEra('modern')">Современность</button>
          <select id="era-other-pick" style="font-size:0.82rem;padding:5px 8px;background:var(--bg-elev,#1a1a24);color:var(--text);border:1px solid var(--border,#333);border-radius:4px" onchange="if(this.value)confirmEra(this.value)">
            <option value="">Выбрать другую эпоху…</option>
            ${(s._era_options || []).map(o => `<option value="${o.key}" ${o.key===detected?'disabled':''}>${esc(o.label)}</option>`).join('')}
          </select>
        </div>
      </div>
    `;
  }
  // Confirmed-state chip — only show when user has explicitly picked something
  // non-modern (modern is the silent default so no chip needed).
  if (confirmed && choice && choice !== 'modern' && choice !== 'auto' && choice !== 'none') {
    const chipLabel = (s._era_options || []).find(o => o.key === choice)?.label || choice;
    return `
      <div style="margin-top:10px;padding:6px 10px;background:rgba(132,94,247,0.06);border:1px solid rgba(132,94,247,0.25);border-radius:6px;font-size:0.82rem;display:flex;align-items:center;gap:8px">
        <span>🏛 Эпоха: <strong>${esc(chipLabel)}</strong></span>
        <button class="btn btn-sm btn-ghost" style="margin-left:auto;font-size:0.75rem" onclick="confirmEra('modern')">Сменить на современность</button>
      </div>
    `;
  }
  return '';
}

function renderAnthroBanner(s) {
  // Surface auto-detected anthropomorphic (furry) world so the user can
  // accept or refuse BEFORE any species features are propagated to side
  // characters. Same safe-default-modern pattern as the era banner: until
  // the user explicitly confirms, asset generation stays human-world.
  const detected = !!s._anthro_detected;
  const confirmed = !!s._anthro_confirmed;
  const choice = s._anthro_choice || 'auto';
  const evidence = s._anthro_evidence || [];
  // SUGGEST: detector says yes, user hasn't picked → ask.
  if (detected && !confirmed) {
    return `
      <div id="anthro-banner-suggest" style="margin-top:10px;padding:10px 12px;background:rgba(251,191,36,0.10);border:1px solid rgba(251,191,36,0.45);border-radius:6px;font-size:0.85rem">
        <div style="margin-bottom:6px;line-height:1.4">
          <strong style="color:#fbbf24">🐺 Анимало-мир (furry)?</strong> —
          в сериале обнаружены признаки антропоморфного мира.
        </div>
        ${evidence.length ? `<div style="margin-bottom:8px;font-size:0.8rem;color:var(--muted)">Что увидел детектор: ${evidence.map(esc).join('; ')}.</div>` : ''}
        <div style="margin-bottom:8px;line-height:1.4">
          Если это сериал про <strong>фурри / зверолюдей</strong> — нажми «Да, фурри-мир».
          Если про <strong>обычных людей</strong> — нажми «Нет, человеческий мир» (опционально сбросит звериные черты у уже созданных персонажей и портретов).
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-sm btn-primary" onclick="confirmAnthro('anthro')">✓ Да, фурри-мир</button>
          <button class="btn btn-sm btn-ghost" onclick="confirmAnthro('human')">Нет, человеческий мир</button>
        </div>
      </div>
    `;
  }
  // INFO chip — anthro confirmed.
  if (confirmed && choice === 'anthro') {
    return `
      <div style="margin-top:10px;padding:6px 10px;background:rgba(132,94,247,0.06);border:1px solid rgba(132,94,247,0.25);border-radius:6px;font-size:0.82rem;display:flex;align-items:center;gap:8px">
        <span>🐺 Мир: <strong>антропоморфные животные</strong></span>
        <button class="btn btn-sm btn-ghost" style="margin-left:auto;font-size:0.75rem" onclick="confirmAnthro('human')">Сделать человеческим</button>
      </div>
    `;
  }
  return '';
}

// Hard-pause anthro gate: invoked when a generation request is blocked with
// 409 needs_anthro_decision. Asks the user once whether this series contains
// NON-humans, persists the choice, and returns true if generation may proceed.
async function resolveAnthroGate(url, data) {
  const m = (url || '').match(/\/api\/series\/([^\/]+)/);
  const sid = (m && m[1]) || (typeof S !== 'undefined' && S.seriesId);
  if (!sid) return false;
  const names = (data.flagged_chars || []).join(', ');
  const yes = confirm(
    `Стоп — генерация НЕ-людей запрещена без подтверждения.\n\n` +
    `Похоже, в этом сериале есть НЕ-люди: ${names || '—'}.\n\n` +
    `Это мир НЕ-людей (звери / фурри / мифические существа)?\n\n` +
    `OK — ДА, разрешить генерацию НЕ-людей в этом сериале (спросим только один раз).\n` +
    `Отмена — НЕТ, это люди (звериные черты будут убраны, персонажи сгенерятся людьми).`
  );
  const choice = yes ? 'anthro' : 'human';
  try {
    await api.post(`/api/series/${sid}/anthro`,
      { choice, strip_species: (choice === 'human'), clear_portraits: false },
      { _anthroRetried: true });
    if (typeof S !== 'undefined' && S.seriesId === sid) {
      try { S.series = await api.get(`/api/series/${sid}`); renderSeriesView && renderSeriesView(); } catch {}
    }
    return true;
  } catch (e) {
    alert('Не удалось сохранить выбор мира: ' + (e?.message || e));
    return false;
  }
}

async function confirmAnthro(choice) {
  const sid = S.seriesId;
  if (!sid) return;
  const hasPortraits = (S.series.characters || []).some(c => (c.ref_images || []).length > 0);
  let stripSpecies = true;
  let clearPortraits = false;
  if (choice === 'human') {
    // For a switch to human world — offer to strip species markers AND
    // optionally wipe portraits. Default: strip yes, wipe portraits only
    // if user wants to regenerate.
    if (hasPortraits) {
      clearPortraits = confirm(
        'Сбросить уже сгенерированные звериные портреты, чтобы перегенерировать персонажей как людей?\n\n' +
        'OK — удалить и перегенерировать.\n' +
        'Cancel — оставить старые портреты (только описания будут очищены).'
      );
    }
  }
  try {
    const r = await api.post(`/api/series/${sid}/anthro`, {
      choice, strip_species: stripSpecies, clear_portraits: clearPortraits,
    });
    if (r && r.ok) {
      const fresh = await api.get(`/api/series/${sid}`);
      S.series = fresh;
      renderSeriesView();
      if ((r.cleared_portraits || []).length) {
        try { triggerAutogenSweep && triggerAutogenSweep(); } catch {}
      }
    }
  } catch (e) {
    alert('Не удалось сохранить выбор мира: ' + (e?.message || e));
  }
}

async function confirmEra(choice) {
  const sid = S.seriesId;
  if (!sid) return;
  // When switching AWAY from a confirmed non-modern era OR accepting a
  // detected one, ask whether to wipe existing portraits — they were
  // generated under the wrong era and would otherwise stay stale.
  const prevChoice = S.series._era_choice || 'auto';
  const prevConfirmed = !!S.series._era_confirmed;
  const hasPortraits = (S.series.characters || []).some(c => (c.ref_images || []).length > 0);
  let clearPortraits = false;
  if (hasPortraits && (choice !== prevChoice) && (prevConfirmed || S.series._era_detected)) {
    clearPortraits = confirm(
      'Удалить уже сгенерированные портреты персонажей, чтобы перегенерировать их под новую эпоху?\n\n' +
      'OK — удалить и перегенерировать с новым стилем.\n' +
      'Cancel — оставить как есть (старые фото останутся, новый стиль применится только к новым).'
    );
  }
  try {
    const r = await api.post(`/api/series/${sid}/era`, { choice, clear_portraits: clearPortraits });
    if (r && r.ok) {
      // Refresh series so banner state + portrait list reflect the new choice.
      const fresh = await api.get(`/api/series/${sid}`);
      S.series = fresh;
      renderSeriesView();
      if ((r.cleared_portraits || []).length) {
        // Optional: kick autogen so user doesn't have to click manually.
        try { triggerAutogenSweep && triggerAutogenSweep(); } catch {}
      }
    }
  } catch (e) {
    alert('Не удалось сохранить выбор эпохи: ' + (e?.message || e));
  }
}

function renderCharactersList() {
  const s = S.series;
  const el = document.getElementById('characters-list');
  if (!s.characters.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет персонажей</div>';
    return;
  }
  el.innerHTML = s.characters.map(c => {
    const hasRefs = c.ref_images && c.ref_images.length > 0;
    const v = c.image_version ? `?v=${c.image_version}` : '';
    const imgUrl = hasRefs ? `/assets/${s.id}/${c.ref_images[0]}${v}` : null;
    // Outfit chips — exclude base (it's the ref portrait) and show generation state.
    const outfits = (c.outfits || []).filter(o => !o.is_base);
    const pendingCount = outfits.filter(o => !o.photo).length;
    const readyCount = outfits.length - pendingCount;
    const chips = outfits.slice(0, 4).map(o => {
      const ready = !!o.photo;
      const label = esc(o.label || '?');
      return `<span class="outfit-chip ${ready ? 'ready' : 'pending'}" title="${ready ? 'Фото готово' : 'Генерируется...'}">${ready ? '👗' : '⚙'} ${label}</span>`;
    }).join('');
    const moreChip = outfits.length > 4 ? `<span class="outfit-chip more">+${outfits.length - 4}</span>` : '';
    const outfitRow = outfits.length
      ? `<div class="outfit-chips-row">${chips}${moreChip}</div>`
      : '';
    return `
      <div class="char-item" data-autogen-kind="char" data-autogen-id="${c.id}" onclick="openCharAssets('${c.id}')"
           ondragover="_dropAssetOver(event)" ondragleave="_dropAssetLeave(event)"
           ondrop="_dropAssetUpload(event, 'char', '${c.id}')"
           title="Клик — открыть карточку. Перетащи картинку с компа — заменит фото.">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(c.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : esc(c.name[0])}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(c.name)}${outfits.length ? ` <span class="outfit-count-badge" title="${readyCount}/${outfits.length} образов готово">👗${readyCount}/${outfits.length}</span>` : ''}</div>
          <div class="char-role">${esc(c.gender === 'male' ? 'М' : 'Ж')} · ${esc(c.description?.slice(0,30) || '—')}</div>
          ${outfitRow}
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditCharacter('${c.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteCharacter('${c.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function renderLocationsList() {
  const s = S.series;
  const el = document.getElementById('locations-list');
  const locs = s.locations || [];
  if (!locs.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:0.83rem">Нет локаций</div>';
    return;
  }
  el.innerHTML = locs.map(l => {
    const hasRefs = l.ref_images && l.ref_images.length > 0;
    const v = l.image_version ? `?v=${l.image_version}` : '';
    const imgUrl = hasRefs ? `/assets/${s.id}/${l.ref_images[0]}${v}` : null;
    return `
      <div class="char-item" data-autogen-kind="loc" data-autogen-id="${l.id}" onclick="openLocAssets('${l.id}')"
           ondragover="_dropAssetOver(event)" ondragleave="_dropAssetLeave(event)"
           ondrop="_dropAssetUpload(event, 'loc', '${l.id}')"
           title="Клик — открыть карточку. Перетащи картинку с компа — заменит фото.">
        <div class="char-avatar">
          ${imgUrl ? `<img src="${imgUrl}" alt="${esc(l.name)}" onerror="this.replaceWith(_brokenImagePlaceholder('${imgUrl}'))">` : '📍'}
          <div class="autogen-overlay" hidden><span class="spinner"></span></div>
        </div>
        <div class="char-info">
          <div class="char-name">${esc(l.name)}</div>
          <div class="char-role">${esc(l.description?.slice(0,30) || '—')}</div>
        </div>
        <div class="char-ref-dot ${hasRefs ? 'has-refs' : 'no-refs'}" title="${hasRefs ? 'Есть фото' : 'Нет фото'}"></div>
        <button class="btn-icon" onclick="event.stopPropagation();openEditLocation('${l.id}')" title="Редактировать">✎</button>
        <button class="btn-icon" onclick="event.stopPropagation();deleteLocation('${l.id}')" title="Удалить" style="color:var(--danger)">✕</button>
      </div>
    `;
  }).join('');
}

function renderStyleSection() {
  const s = S.series;
  const refs = (s.style.ref_images || []);
  document.getElementById('style-section').innerHTML = `
    <div style="font-size:0.85rem;color:var(--muted);margin-bottom:6px">
      <strong style="color:var(--text)">${esc(styleLabel(s.style.type))}</strong>
      ${s.style.custom_description ? `<br><span>${esc(s.style.custom_description.slice(0,60))}</span>` : ''}
    </div>
    <div class="style-refs-row">
      ${refs.map(r => `<img class="style-thumb" src="/assets/${s.id}/${r}" alt="style ref">`).join('')}
    </div>
  `;
}

function styleLabel(t) {
  const map = {cinematic:'Кинематограф',photorealistic:'Фотореализм',anime:'Аниме',auto:'Авто',custom:'Свой'};
  return map[t] || t;
}

// Compute whether an episode is ready for video generation:
//   - script accepted (has used entities OR cast_extracted=true)
//   - every used char has a portrait (ref_images)
//   - every requested outfit has a photo
//   - every used location has a photo
//   - every used item has a photo
// Returns { ready: bool, missing: {chars, outfits, locs, items} } so the UI
// can show what's blocking a particular episode without opening it. Replaces
// the old manual gen_ready toggle: status now derives automatically from the
// actual asset state, no human bookkeeping required.
function _episodeReadyInfo(ep) {
  const out = { ready: false, missing: { chars: [], outfits: [], locs: [], items: [] }, hasScript: false };
  if (!ep || !S.series) return out;
  const scriptOk = !!(ep.script || '').trim();
  if (!scriptOk) return out;
  const inferAccepted = !!ep.cast_extracted ||
    ((ep.characters_used || []).length + (ep.locations_used || []).length + (ep.items_used || []).length) > 0;
  if (!inferAccepted) return out;
  out.hasScript = true;
  const charById = Object.fromEntries((S.series.characters || []).map(c => [c.id, c]));
  const locById  = Object.fromEntries((S.series.locations  || []).map(l => [l.id, l]));
  const itemById = Object.fromEntries((S.series.items      || []).map(i => [i.id, i]));
  for (const cid of (ep.characters_used || [])) {
    const c = charById[cid];
    if (!c || c._skip_autogen) continue;
    if (!c.ref_images || !c.ref_images.length) {
      out.missing.chars.push({ id: cid, name: c.name });
    }
    const reqOutfits = (ep.character_outfits && ep.character_outfits[cid]) || [];
    const labels = Array.isArray(reqOutfits) ? reqOutfits : (reqOutfits ? [reqOutfits] : []);
    for (const ref of labels) {
      if (!ref || ref === 'base') continue;
      const o = (c.outfits || []).find(o => o.label === ref) || (c.outfits || []).find(o => o.id === ref);
      if (!o || o._skip_autogen) continue;
      if (!o.photo) out.missing.outfits.push({ char: c.name, label: o.label });
    }
  }
  for (const lid of (ep.locations_used || [])) {
    const l = locById[lid];
    if (!l || l._skip_autogen) continue;
    if (!l.ref_images || !l.ref_images.length) {
      out.missing.locs.push({ id: lid, name: l.name });
    }
  }
  for (const iid of (ep.items_used || [])) {
    const it = itemById[iid];
    if (!it || it._skip_autogen) continue;
    if (!it.ref_images || !it.ref_images.length) {
      out.missing.items.push({ id: iid, name: it.name });
    }
  }
  const totalMissing = out.missing.chars.length + out.missing.outfits.length + out.missing.locs.length + out.missing.items.length;
  out.ready = totalMissing === 0;
  return out;
}

// Toggle whether an episode is selected for the next range-gen kickoff. The
// selection lives on `S._genSelected` (Set of episode numbers) and the
// «Сгенерировать выделенные» button reads from it. Persisted to localStorage
// per-series so a refresh doesn't lose the selection mid-curating.
function toggleEpisodeSelected(num, ev) {
  if (ev) ev.stopPropagation();
  if (!S._genSelected) S._genSelected = new Set();
  if (S._genSelected.has(num)) S._genSelected.delete(num);
  else S._genSelected.add(num);
  try { localStorage.setItem(`gen_selected:${S.seriesId}`, JSON.stringify([...S._genSelected])); } catch {}
  _updateRangeGenSelectionCount();
}
function _restoreGenSelection() {
  try {
    const raw = localStorage.getItem(`gen_selected:${S.seriesId}`);
    S._genSelected = new Set(raw ? JSON.parse(raw) : []);
  } catch { S._genSelected = new Set(); }
}
function _updateRangeGenSelectionCount() {
  const el = document.getElementById('range-gen-selected-count');
  if (!el) return;
  const n = S._genSelected ? S._genSelected.size : 0;
  el.textContent = n ? `выделено: ${n}` : '';
}

function renderEpisodesList() {
  try { _initSeriesMusicCheckbox(); } catch {}
  const el = document.getElementById('episodes-list');
  const empty = document.getElementById('episodes-empty');
  if (!S.episodes.length) {
    el.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  if (!S._genSelected) _restoreGenSelection();
  el.innerHTML = S.episodes.map(ep => {

    const rtlStatus = ep.reteller?.status;
    const hasVideo = ep.reteller?.video_url;
    const ar = ep.audit_report;
    let auditBadge = '';
    if (ar) {
      const critCount = (ar.violations || []).filter(v => v.severity === 'critical').length;
      if (ar.passes && critCount === 0) {
        auditBadge = `<span class="status-badge" style="background:rgba(74,222,128,0.15);color:#4ade80" title="Логика проверена · попыток: ${ar.retries}">✓ logic</span>`;
      } else {
        auditBadge = `<span class="status-badge" style="background:rgba(248,113,113,0.15);color:#f87171" title="Критических проблем: ${critCount}">⚠ ${critCount}</span>`;
      }
    }
    const dsp = ep.days_since_previous;
    const dspBadge = (dsp !== undefined && dsp !== null && ep.number > 1)
      ? `<span class="status-badge" style="background:rgba(132,94,247,0.12);color:#a78bfa" title="Дней с прошлой серии">+${dsp}д</span>` : '';
    // Gen-status badge: surfaces episode's place in the multi-episode queue
    // so the user sees at a glance which episodes are ready / queued / running
    // / done / failed without opening each one. «Готова» is now derived from
    // the actual asset state — no manual flag.
    const gs = ep.gen_status || '';
    const readyInfo = _episodeReadyInfo(ep);
    const isReady = readyInfo.ready;
    const totalMissing = readyInfo.missing.chars.length + readyInfo.missing.outfits.length + readyInfo.missing.locs.length + readyInfo.missing.items.length;
    let genBadge = '';
    if (gs === 'done') {
      genBadge = `<span class="status-badge" style="background:rgba(74,222,128,0.20);color:#4ade80;font-weight:700" title="Все чанки сгенерированы и собраны">✅ Готово</span>`;
    } else if (gs === 'generating') {
      genBadge = `<span class="status-badge" style="background:rgba(132,94,247,0.20);color:#a78bfa;font-weight:700" title="Серия генерится прямо сейчас">🎬 Генерится…</span>`;
    } else if (gs === 'failed') {
      genBadge = `<span class="status-badge" style="background:rgba(248,113,113,0.20);color:#f87171;font-weight:700" title="Генерация упала — открой серию для деталей">✗ Сбой</span>`;
    } else if (gs === 'queued') {
      genBadge = `<span class="status-badge" style="background:rgba(251,191,36,0.20);color:#fbbf24" title="Стоит в очереди range-gen">⏳ В очереди</span>`;
    } else if (isReady) {
      genBadge = `<span class="status-badge" style="background:rgba(16,185,129,0.18);color:#10b981;font-weight:700" title="Сценарий принят, все ассеты на месте — можно запускать видео-генерацию">▶ Готова</span>`;
    } else if (readyInfo.hasScript) {
      const tip = totalMissing
        ? `Сценарий принят, но не хватает: ` +
          [
            readyInfo.missing.chars.length ? `персы (${readyInfo.missing.chars.length})` : '',
            readyInfo.missing.outfits.length ? `аутфиты (${readyInfo.missing.outfits.length})` : '',
            readyInfo.missing.locs.length ? `локации (${readyInfo.missing.locs.length})` : '',
            readyInfo.missing.items.length ? `предметы (${readyInfo.missing.items.length})` : '',
          ].filter(Boolean).join(', ')
        : 'Сценарий принят, без ассетов';
      genBadge = `<span class="status-badge" style="background:rgba(251,191,36,0.18);color:#fbbf24" title="${esc(tip)}">⚠ ${totalMissing} ассет${totalMissing === 1 ? '' : 'ов'}</span>`;
    }
    // Selection checkbox — only meaningful for episodes that are READY for
    // generation. For episodes that aren't ready, hide the checkbox so the
    // user can't queue something that would just stall.
    const checked = (S._genSelected && S._genSelected.has(ep.number)) ? 'checked' : '';
    const isQueueable = isReady && gs !== 'done' && gs !== 'generating';
    const checkboxHtml = isQueueable
      ? `<label class="ep-row-check" onclick="event.stopPropagation()" title="Выделить для пакетной видео-генерации">
           <input type="checkbox" ${checked} onchange="toggleEpisodeSelected(${ep.number}, event)">
         </label>`
      : `<span class="ep-row-check ep-row-check-disabled" title="${gs === 'done' ? 'Уже сгенерирована' : (gs === 'generating' ? 'Сейчас генерится' : 'Не готова — добей ассеты сначала')}"></span>`;
    return `
      <div class="episode-row" onclick="navigate('episode',{seriesId:'${S.seriesId}',episodeNum:${ep.number}})">
        ${checkboxHtml}
        <div class="ep-num" title="${esc(chunkLabel(S.series, ep.number))}">${isBatchMode(S.series) ? chunkRange(S.series, ep.number).join('–') : ep.number}</div>
        <div class="ep-info">
          <div class="ep-title">${esc(ep.title)}</div>
          <div class="ep-synopsis">${esc(ep.synopsis || (ep.script ? ep.script.slice(0,80) : '—'))}</div>
        </div>
        <div class="ep-badges">
          <span class="status-badge status-${ep.status}">${statusLabel(ep.status)}</span>
          ${genBadge}
          ${dspBadge}
          ${auditBadge}
          ${rtlStatus ? `<span class="status-badge status-${rtlStatus}">${statusLabel(rtlStatus)}</span>` : ''}
          ${hasVideo ? `<a href="${ep.reteller.video_url}" target="_blank" class="btn-ghost btn-sm" onclick="event.stopPropagation()">▶ Видео</a>` : ''}
        </div>
        <div class="ep-actions">
          <button class="btn-icon" onclick="event.stopPropagation();deleteEpisodeConfirm(${ep.number})" title="Удалить эпизод" style="color:var(--danger)">✕</button>
        </div>
      </div>
    `;
  }).join('');
  _updateRangeGenSelectionCount();
}

function statusLabel(s) {
  const m = {draft:'Черновик',sent:'Отправлено',processing:'Генерация...',completed:'Готово',error:'Ошибка',ready:'Готов'};
  return m[s] || s;
}

