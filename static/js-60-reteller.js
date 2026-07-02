// ── Reteller drawer ───────────────────────────────────────────────────────────
let rtlPollingInterval = null;

async function openAssetFolder(type) {
  try {
    await api.post(`/api/series/${S.seriesId}/open-folder`, { type });
  } catch(e) {
    showToast('Ошибка: ' + e.message);
  }
}

function openReteller() {
  // Init range from episode list
  const nums = S.episodes.map(e => e.number);
  const minN = nums.length ? Math.min(...nums) : 1;
  const maxN = nums.length ? Math.max(...nums) : 1;
  document.getElementById('range-from').value = minN;
  document.getElementById('range-to').value = maxN;
  document.getElementById('range-from').max = maxN;
  document.getElementById('range-to').max = maxN;

  // Populate settings from series defaults
  const st = S.series.settings;
  document.getElementById('rtl-duration').value = st.duration || 90;
  document.getElementById('rtl-language').value = st.language || 'Russian';
  document.getElementById('rtl-style').value = S.series.style.type || 'cinematic';
  document.getElementById('rtl-image-provider').value = st.image_provider || 'seedream';
  document.getElementById('rtl-voice').value = st.voice || 'Enceladus';
  document.getElementById('rtl-tts-provider').value = st.tts_provider || 'elevenlabs';
  document.getElementById('rtl-aspect').value = st.aspect_ratio || '9:16';
  document.getElementById('rtl-music').checked = st.enable_music !== false;
  document.getElementById('rtl-animation').checked = !!st.enable_animation;
  document.getElementById('rtl-multivoice').checked = !!st.multi_voice;

  document.getElementById('rtl-results').innerHTML = '';
  document.getElementById('reteller-overlay').classList.remove('hidden');
  document.getElementById('reteller-drawer').classList.remove('hidden');
  updateRangePreview();
}

function closeReteller() {
  document.getElementById('reteller-overlay').classList.add('hidden');
  document.getElementById('reteller-drawer').classList.add('hidden');
  if (rtlPollingInterval) { clearInterval(rtlPollingInterval); rtlPollingInterval = null; }
}

async function updateRangePreview() {
  const from = parseInt(document.getElementById('range-from').value);
  const to = parseInt(document.getElementById('range-to').value);
  if (!from || !to || from > to) return;

  const data = await api.post(`/api/series/${S.seriesId}/reteller/preview`, { from, to });
  renderRangePreview(data);
}

function renderRangePreview(data) {
  // Episode chips
  const chips = data.episodes.map(ep => {
    const hasScript = ep.script && ep.script.length > 10;
    return `<div class="range-ep-chip ${hasScript ? 'has-script' : 'no-script'}">
      Эп.${ep.number}: ${esc(ep.title)}${hasScript ? '' : ' ⚠️'}
    </div>`;
  }).join('');
  document.getElementById('range-episodes-preview').innerHTML = chips || '<span style="color:var(--muted)">Нет эпизодов в этом диапазоне</span>';

  // Character asset slots
  const charGrid = document.getElementById('asset-characters');
  if (!data.characters.length) {
    charGrid.innerHTML = '<span style="color:var(--muted);font-size:0.85rem">В этих эпизодах нет персонажей</span>';
  } else {
    charGrid.innerHTML = '';
    data.characters.forEach(c => {
      charGrid.appendChild(buildAssetCard(c, 'character'));
    });
  }

  // Location asset slots
  const locGrid = document.getElementById('asset-locations');
  if (!data.locations || !data.locations.length) {
    locGrid.innerHTML = '<span style="color:var(--muted);font-size:0.85rem">В этих эпизодах нет локаций</span>';
  } else {
    locGrid.innerHTML = '';
    data.locations.forEach(l => {
      locGrid.appendChild(buildAssetCard(l, 'location'));
    });
  }

  // Style refs
  const styleGrid = document.getElementById('asset-style');
  styleGrid.innerHTML = (data.style.ref_urls || []).map((u, i) => {
    const fname = (data.style.ref_images || [])[i]?.split('/').pop() || '';
    return `<div class="photo-thumb-wrap">
      <img src="${u}" alt="" style="width:56px;height:56px;object-fit:cover;border-radius:6px;border:1px solid var(--border)">
      <button class="del-btn" onclick="deleteStyleRefFromDrawer('${fname}')">✕</button>
    </div>`;
  }).join('');
}

// Build a character or location asset card (filled or placeholder with drag&drop)
function buildAssetCard(item, type) {
  const uploadFn = type === 'character'
    ? (files) => uploadCharRefFromDrawer(item.id, files)
    : (files) => uploadLocRefFromDrawer(item.id, files);

  if (item.has_refs) {
    // Filled card
    const card = document.createElement('div');
    card.className = 'asset-char-card ready';
    const outfits = (item.outfits_in_range || []);
    const outfitsHtml = type === 'character' && outfits.length ? `
      <div class="asset-char-outfits" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;padding-top:6px;border-top:1px dashed var(--border)">
        ${outfits.map(o => {
          const epList = o.episodes.map(n => `Эп.${n}`).join(', ');
          const labelTxt = `${o.label || '—'} · ${epList}`;
          if (o.has_photo) {
            return `<div class="outfit-mini" title="${esc(o.label)} — ${epList}\n${esc(o.description||'')}" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:60px">
              <img src="${o.photo_url}" alt="" style="width:54px;height:54px;object-fit:cover;border-radius:6px;border:1px solid var(--border)">
              <div style="font-size:0.65rem;color:var(--muted);text-align:center;line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%">${esc(o.label)}</div>
              <div style="font-size:0.6rem;color:var(--accent)">${esc(epList)}</div>
            </div>`;
          }
          return `<div class="outfit-mini missing" title="${esc(o.label)} — нет фото\n${esc(o.description||'')}" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:60px">
            <div style="width:54px;height:54px;border-radius:6px;border:1px dashed var(--warn,#f5a623);display:flex;align-items:center;justify-content:center;background:rgba(245,166,35,0.08);font-size:1.2rem">⚡</div>
            <div style="font-size:0.65rem;color:var(--muted);text-align:center;line-height:1.1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%">${esc(o.label)}</div>
            <div style="font-size:0.6rem;color:var(--accent)">${esc(epList)}</div>
          </div>`;
        }).join('')}
      </div>` : '';
    card.innerHTML = `
      <div class="asset-char-name">${esc(item.name)}</div>
      <div class="asset-char-refs">
        ${(item.ref_urls || []).map(u => `<img class="asset-ref-thumb" src="${u}" alt="">`).join('')}
      </div>
      ${outfitsHtml}
      <label class="asset-upload-btn">
        <input type="file" accept="image/*" multiple hidden>
        + Добавить
      </label>`;
    card.querySelector('input[type=file]').addEventListener('change', e => uploadFn(e.target.files));
    return card;
  }

  // Placeholder with drag & drop
  const placeholder = document.createElement('div');
  placeholder.className = 'asset-placeholder';
  placeholder.innerHTML = `
    <div class="placeholder-icon">${type === 'character' ? '👤' : '📍'}</div>
    <div class="placeholder-label">${esc(item.name)}</div>
    <div class="placeholder-hint">Перетащи фото сюда или нажми загрузить</div>
    <div class="placeholder-actions">
      <label class="btn-upload">
        <input type="file" accept="image/*" multiple hidden>
        ⬆ Загрузить
      </label>
      <button class="btn-gen-prompt" data-id="${item.id}" data-type="${type}">
        ✨ Промпт для Banana 2
      </button>
    </div>`;

  // File input
  placeholder.querySelector('input[type=file]').addEventListener('change', e => uploadFn(e.target.files));

  // Drag & drop
  placeholder.addEventListener('dragover', e => { e.preventDefault(); placeholder.classList.add('drag-over'); });
  placeholder.addEventListener('dragleave', () => placeholder.classList.remove('drag-over'));
  placeholder.addEventListener('drop', e => {
    e.preventDefault();
    placeholder.classList.remove('drag-over');
    if (e.dataTransfer.files.length) uploadFn(e.dataTransfer.files);
  });

  // Generate prompt button
  placeholder.querySelector('.btn-gen-prompt').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const origText = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>';
    btn.disabled = true;
    try {
      const result = await api.post(`/api/series/${S.seriesId}/generate-prompt`, {
        type: btn.dataset.type,
        id: btn.dataset.id,
      });
      showPromptModal(result.prompt);
    } catch (err) {
      alert('Ошибка: ' + err.message);
    } finally {
      btn.innerHTML = origText;
      btn.disabled = false;
    }
  });

  return placeholder;
}

async function uploadLocRefFromDrawer(locId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/location/${locId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function uploadCharRefFromDrawer(charId, files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/character/${charId}`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function uploadStyleRefs(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    await api.upload(`/api/series/${S.seriesId}/assets/style`, fd);
  }
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function deleteStyleRefFromDrawer(filename) {
  await api.del(`/api/series/${S.seriesId}/assets/style/${filename}`);
  S.series = await api.get(`/api/series/${S.seriesId}`);
  updateRangePreview();
}

async function generateRangePrompt() {
  const from = parseInt(document.getElementById('range-from').value);
  const to   = parseInt(document.getElementById('range-to').value);
  if (!from || !to || from > to) return alert('Укажи корректный диапазон');
  const btn    = document.getElementById('rtl-range-prompt-btn');
  const status = document.getElementById('rtl-range-prompt-status');
  const result = document.getElementById('rtl-range-prompt-result');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Генерируем...';
  status.textContent = ''; result.classList.add('hidden');
  try {
    const ctx = { seriesId: S.seriesId, seriesTitle: S.series?.title };
    const res = await trackTask(`Промпт диапазона ${from}–${to}`, ctx, () =>
      api.post(`/api/series/${S.seriesId}/reteller/range-prompt`, { from, to })
    );
    document.getElementById('rtl-range-prompt-text').value = res.prompt;
    const chars = (res.characters || []).map(c => c.name).join(', ');
    const locs  = (res.locations  || []).map(l => l.name).join(', ');
    document.getElementById('rtl-range-cast').innerHTML =
      (chars ? `<div><strong>Персонажи:</strong> ${esc(chars)}</div>` : '') +
      (locs  ? `<div><strong>Локации:</strong> ${esc(locs)}</div>`   : '');
    result.classList.remove('hidden');
  } catch(e) {
    status.textContent = 'Ошибка: ' + e.message;
    status.className = 'pipeline-status err';
  } finally {
    btn.disabled = false; btn.innerHTML = '📋 Сгенерировать промпт для Reteller';
  }
}

function copyRangePrompt() {
  const ta = document.getElementById('rtl-range-prompt-text');
  navigator.clipboard.writeText(ta.value).then(() => showToast('Скопировано!')).catch(() => {
    ta.select(); document.execCommand('copy'); showToast('Скопировано!');
  });
}

async function submitEpisodeToReteller() {
  if (!S.episodeNum) return alert('Открой эпизод');
  const btn = document.getElementById('ep-rtl-submit-btn');
  const statusEl = document.getElementById('ep-rtl-submit-status');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Отправляем...';
  statusEl.textContent = '';

  // Use series defaults; user can tweak in the drawer for batch mode
  const st = S.series.settings || {};
  const settings = {
    duration:             st.duration || 90,
    language:             st.language || 'Russian',
    style:                (S.series.style && S.series.style.type) || 'cinematic',
    image_provider:       st.image_provider || 'banana',
    voice:                st.voice || 'Enceladus',
    tts_provider:         st.tts_provider || 'elevenlabs',
    aspect_ratio:         st.aspect_ratio || '9:16',
    enable_music:         st.enable_music === true,
    enable_animation:     st.enable_animation !== false,
    animation_speed:      st.animation_speed || 'fast',
    animation_resolution: st.animation_resolution || '480p',
    animation_model:      st.animation_model || 'seedance-2-ref',
    cinema:               !!st.cinema,
    trim:                 st.trim !== false,
    no_fades:             st.no_fades !== false,
    multi_voice:          !!st.multi_voice,
  };

  try {
    const num = S.episodeNum;
    const results = await api.post(`/api/series/${S.seriesId}/reteller/submit`, { from: num, to: num, settings });
    const r = results[0] || {};
    if (r.error) {
      statusEl.innerHTML = `<span style="color:var(--danger)">${esc(r.error.slice(0,200))}</span>`;
    } else {
      const link = r.project_url
        ? `<a href="${r.project_url}" target="_blank" class="btn-accent btn-sm" style="margin-top:6px;display:inline-block">Открыть драфт →</a>`
        : '';
      statusEl.innerHTML = `✓ Драфт создан в Reteller. ${link}`;
      // refresh episode state
      S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
      S.episode = await api.get(`/api/series/${S.seriesId}/episodes/${num}`);
      renderEpReteller();
      renderEpisodesList();
    }
  } catch (e) {
    statusEl.innerHTML = `<span style="color:var(--danger)">Ошибка: ${esc(e.message || e)}</span>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📝 Создать драфт этой серии';
  }
}

async function submitToReteller() {
  const from = parseInt(document.getElementById('range-from').value);
  const to = parseInt(document.getElementById('range-to').value);
  if (!from || !to || from > to) return alert('Укажи корректный диапазон');

  const btn = document.getElementById('rtl-submit-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Отправляем...';

  const st = S.series.settings || {};
  const settings = {
    duration:             parseInt(document.getElementById('rtl-duration').value),
    language:             document.getElementById('rtl-language').value,
    style:                document.getElementById('rtl-style').value,
    image_provider:       document.getElementById('rtl-image-provider').value,
    voice:                document.getElementById('rtl-voice').value,
    tts_provider:         document.getElementById('rtl-tts-provider').value,
    aspect_ratio:         document.getElementById('rtl-aspect').value,
    enable_music:         document.getElementById('rtl-music').checked,
    enable_animation:     document.getElementById('rtl-animation').checked,
    multi_voice:          document.getElementById('rtl-multivoice').checked,
    // Reteller animation settings — read from series (no UI fields yet, defaults from screenshot)
    animation_speed:      st.animation_speed || 'fast',
    animation_resolution: st.animation_resolution || '480p',
    animation_model:      st.animation_model || 'seedance-2-ref',
    cinema:               !!st.cinema,
    trim:                 st.trim !== false,
    no_fades:             st.no_fades !== false,
  };

  try {
    const results = await api.post(`/api/series/${S.seriesId}/reteller/submit`, { from, to, settings });
    renderSubmitResults(results);
    S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
    renderEpisodesList();
    // No polling — drafts wait for the user to start them on Reteller's page
    showToast('Драфты созданы в Reteller — открой ссылки и запусти вручную');
  } catch (e) {
    alert('Ошибка: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📝 Создать драфты в Reteller';
  }
}

function renderSubmitResults(results) {
  const el = document.getElementById('rtl-results');
  el.innerHTML = results.map(r => `
    <div class="rtl-result-row" id="rtl-row-${r.episode}">
      <div class="rtl-result-ep">Эп. ${r.episode}</div>
      <div class="rtl-result-status">
        ${r.error
          ? `<span style="color:var(--danger)">${esc(r.error.slice(0,80))}</span>`
          : `<span class="status-badge status-${r.reteller_status||'draft'}">${statusLabel(r.reteller_status||'draft')}</span>`}
      </div>
      ${r.project_url
        ? `<a class="btn-ghost btn-sm" href="${r.project_url}" target="_blank" onclick="event.stopPropagation()">Открыть в Reteller →</a>`
        : (r.project_id ? `<div class="rtl-result-link" id="rtl-link-${r.episode}"></div>` : '')}
    </div>
  `).join('');
}

function startPolling(results) {
  const pending = results.filter(r => r.project_id && r.reteller_status !== 'completed' && r.reteller_status !== 'error');
  if (!pending.length) return;

  rtlPollingInterval = setInterval(async () => {
    let allDone = true;
    for (const r of pending) {
      try {
        const data = await api.get(`/api/series/${S.seriesId}/reteller/status/${r.project_id}`);
        updateResultRow(r.episode, data);
        if (data.status !== 'completed' && data.status !== 'error') allDone = false;
        else {
          S.episodes = await api.get(`/api/series/${S.seriesId}/episodes`);
          renderEpisodesList();
        }
      } catch (_) { allDone = false; }
    }
    if (allDone) { clearInterval(rtlPollingInterval); rtlPollingInterval = null; }
  }, 8000);
}

function updateResultRow(epNum, data) {
  const statusEl = document.querySelector(`#rtl-row-${epNum} .rtl-result-status`);
  if (statusEl) statusEl.innerHTML = `<span class="status-badge status-${data.status}">${statusLabel(data.status)}</span>`;
  if (data.videoUrl) {
    const linkEl = document.getElementById(`rtl-link-${epNum}`);
    if (linkEl) linkEl.innerHTML = `<a href="${data.videoUrl}" target="_blank">▶ Видео</a>`;
  }
}

