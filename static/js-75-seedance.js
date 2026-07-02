// ════════════════════════════════════════════════════════════════════════════
// SEEDANCE — video generation panel
// ════════════════════════════════════════════════════════════════════════════
const SD = { refs: [], pollTimer: null, lastStatuses: {} /* idx → status, used to detect transitions */, selected: new Set() };

// Detect status transitions (anything → completed/failed) and play sound + toast.
// Called from sdRenderList AND from poll tick.
function _sdNotifyTransitions(chunks) {
  if (!Array.isArray(chunks)) return;
  const fresh = {};
  const freshQc = {};
  let newCompleted = 0;
  let newFailed = 0;
  const newExhausted = [];   // chunks whose QC just transitioned to retry_exhausted
  for (const c of chunks) {
    const idx = c.idx;
    if (idx == null) continue;
    fresh[idx] = c.status;
    const qcStatus = (c.qc && c.qc.status) || null;
    freshQc[idx] = qcStatus;
    const was = SD.lastStatuses[idx];
    if (was != null && was !== c.status) {
      if (c.status === 'completed') newCompleted++;
      else if (c.status === 'failed') newFailed++;
    }
    // Detect transition INTO retry_exhausted — QC gave up on this chunk after
    // 2 retries. User asked for voice notification ("голосовое оповещение")
    // identifying which chunk has trouble.
    const wasQc = (SD.lastQcStatuses || {})[idx];
    if (qcStatus === 'retry_exhausted' && wasQc !== 'retry_exhausted') {
      newExhausted.push({
        idx,
        fails: (c.qc && c.qc.fails) || [],
        attempts: (c.qc && c.qc.attempts) || 0,
      });
    }
  }
  SD.lastStatuses = fresh;
  SD.lastQcStatuses = freshQc;
  if (newCompleted > 0) {
    Sounds.playSuccess();
    showToast(`✓ Готово видео: ${newCompleted} чанк${newCompleted > 1 ? 'а' : ''}`);
    // Auto-mark episode "Готово" when every script-derived segment has a
    // completed chunk. Fires only on the moment of completion (newCompleted>0)
    // and only once per episode-load (sessionAutoReadyDone) so a user who
    // manually un-checks the box doesn't get fought by the next poll tick.
    _maybeAutoMarkEpisodeReady(chunks);
  }
  if (newFailed > 0) {
    Sounds.playError();
    showToast(`✗ Ошибка генерации: ${newFailed} чанк${newFailed > 1 ? 'а' : ''}`);
  }
  // Voice + sound alert on QC give-up. Consolidated into ONE short utterance
  // per tick so multiple simultaneous fails don't queue and chain-read.
  // Sounds.speak() now cancels pending utterances by default — newest event
  // interrupts old. Combined with the consolidation here, TTS stays tight.
  if (newExhausted.length > 0) {
    try { Sounds.playError(); } catch {}
    try {
      const phrase = newExhausted.length === 1
        ? `Chunk ${newExhausted[0].idx} failed.`
        : `${newExhausted.length} chunks failed.`;
      Sounds.speak(phrase);
    } catch {}
    // Per-chunk toast remains — visual log for which chunks need review.
    for (const ex of newExhausted) {
      showToast(
        `🔇 Чанк #${ex.idx} — QC сдался (${(ex.fails || []).slice(0, 2).join(', ')})`,
        8000,
      );
    }
  }
}

let _autoReadyAppliedFor = null;  // remembers (seriesId, episodeNum) we auto-marked this session
function _maybeAutoMarkEpisodeReady(chunks) {
  if (!S.episode || S.episode.ready) return;          // already ready — nothing to do
  const key = `${S.seriesId}::${S.episode.number}`;
  if (_autoReadyAppliedFor === key) return;           // already auto-marked this session
  // Count completed chunks (any chunk that finished its render).
  const completed = chunks.filter(c => c.status === 'completed').length;
  if (!completed) return;
  // Expected segments = how many script-derived segments the current script
  // would produce. Use _autoCollectSegments which mirrors the scene-view's
  // per-segment math (handles establishing-shot toggle, scene heading split,
  // etc.). If the script is empty / not in scene mode, skip — user is still
  // editing, not done.
  let expected = 0;
  try { expected = (typeof _autoCollectSegments === 'function') ? _autoCollectSegments().length : 0; }
  catch { return; }
  if (expected <= 0) return;
  if (completed < expected) return;
  // All segments rendered — flip the toggle and persist.
  _autoReadyAppliedFor = key;
  const cb = document.getElementById('ep-ready-toggle');
  if (cb) {
    cb.checked = true;
    // Trigger the existing onchange handler so the PUT fires + toast shows.
    onReadyToggle();
  }
  showToast('🎉 Все сегменты сгенерированы — серия отмечена как готовая', 5000);
}

function sdLocDragStart(ev, locId) {
  const l = (S.series.locations || []).find(x => x.id === locId);
  if (!l) return;
  const photoUrl = l.ref_images?.[0] ? `${assetUrl(l.ref_images[0])}` : '';
  ev.dataTransfer.setData('application/json', JSON.stringify({
    kind: 'loc', id: locId, name: l.name, photoUrl
  }));
}

async function sdHandleDrop(ev) {
  ev.preventDefault();
  if (SD.refs.length >= 9) { showToast('Максимум 9 референсов'); return; }

  // 1. Internal payload (char/loc card) — CHECK FIRST. The browser may
  // also attach a "file" representation of the dragged element image
  // (the drag-preview bitmap shows up as dataTransfer.files[0]), and if
  // we look at files BEFORE JSON we'd treat an internal card drag as a
  // new external file → uploaded to AVAI as a custom ref, ignoring the
  // existing entity id. User-reported bug: "перетаскиваю локацию,
  // появляются часики, но не прикрепляется".
  try {
    const json = ev.dataTransfer.getData('application/json');
    if (json) {
      const payload = JSON.parse(json);
      if (payload && payload.kind && payload.id) {
        if (SD.refs.some(r => r.kind === payload.kind && r.id === payload.id && r.outfit === payload.outfit)) return;
        SD.refs.push({ ...payload, tag: _sdNextFreeTag() });
        sdRenderRefs();
        return;
      }
    }
  } catch {}

  // 2. File from desktop / external app
  const files = Array.from(ev.dataTransfer.files || []).filter(f => f.type.startsWith('image/'));
  if (files.length) {
    for (const file of files) {
      if (SD.refs.length >= 9) break;
      await sdUploadCustomFile(file);
    }
    return;
  }

  // 3. Image URL dragged from another browser tab
  const uri = ev.dataTransfer.getData('text/uri-list') || ev.dataTransfer.getData('text/plain');
  if (uri && /^https?:\/\//.test(uri.trim()) && /\.(png|jpe?g|webp|gif)(\?|$)/i.test(uri.trim())) {
    await sdUploadCustomUrl(uri.trim());
    return;
  }
}

// Pick the smallest 1-9 integer not currently used as a `tag` on SD.refs.
// Stable tags decouple the visible @ImageN label from the array position so
// removing a middle ref doesn't renumber the survivors (user's mental model
// breaks when @Image3 silently becomes @Image2 after deleting @Image2).
function _sdNextFreeTag() {
  const used = new Set((SD.refs || []).map(r => r && r.tag).filter(Boolean));
  for (let n = 1; n <= 9; n++) if (!used.has(n)) return n;
  return null;
}

async function sdUploadCustomFile(file) {
  const slot = document.getElementById('sd-ref-slots');
  const placeholder = document.createElement('div');
  placeholder.className = 'sd-ref-chip';
  placeholder.innerHTML = `<div class="label">⏳ ${esc(file.name.slice(0,18))}</div>`;
  slot?.appendChild(placeholder);
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await api.upload(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`, fd
    );
    if (!res.url) throw new Error(res.error || 'no url');
    SD.refs.push({
      kind: 'url', id: 'custom-' + Date.now(),
      name: res.name || file.name, photoUrl: res.url, url: res.url,
      tag: _sdNextFreeTag(),
    });
  } catch (e) {
    showToast('✗ загрузка: ' + (e.message || e));
  } finally {
    placeholder.remove();
    sdRenderRefs();
  }
}

async function sdUploadCustomUrl(srcUrl) {
  const slot = document.getElementById('sd-ref-slots');
  const placeholder = document.createElement('div');
  placeholder.className = 'sd-ref-chip';
  placeholder.innerHTML = `<div class="label">⏳ url</div>`;
  slot?.appendChild(placeholder);
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`,
      { url: srcUrl }
    );
    if (!res.url) throw new Error(res.error || 'no url');
    SD.refs.push({
      kind: 'url', id: 'custom-' + Date.now(),
      name: res.name || 'custom', photoUrl: res.url, url: res.url,
      tag: _sdNextFreeTag(),
    });
  } catch (e) {
    showToast('✗ загрузка: ' + (e.message || e));
  } finally {
    placeholder.remove();
    sdRenderRefs();
  }
}

// Toggle visual warning when "только базовые образы персов" is checked —
// makes it obvious that outfits picked in [BLOCKING] are being IGNORED
// during compose. Without this signal users frequently leave the checkbox
// on accidentally and then can't figure out why Margaret isn't in the
// prison jumpsuit they expected.
function sdUpdateBaseOnlyHint() {
  const cb = document.getElementById('sd-base-only');
  const label = document.getElementById('sd-base-only-label');
  if (!cb || !label) return;
  if (cb.checked) {
    label.style.color = '#fbbf24';
    label.style.fontWeight = '700';
    label.style.background = 'rgba(251,191,36,0.10)';
    label.style.border = '1px solid rgba(251,191,36,0.45)';
    label.style.padding = '2px 8px';
    label.style.borderRadius = '4px';
  } else {
    label.style.color = '';
    label.style.fontWeight = '';
    label.style.background = '';
    label.style.border = '';
    label.style.padding = '';
    label.style.borderRadius = '';
  }
  // Also reflect on the refs panel — if it's rendered, add/remove a banner
  const refsHost = document.getElementById('sd-ref-slots');
  if (refsHost && refsHost.parentElement) {
    const old = document.getElementById('sd-base-only-banner');
    if (cb.checked) {
      if (!old) {
        const b = document.createElement('div');
        b.id = 'sd-base-only-banner';
        b.style.cssText = 'margin:4px 0 6px;padding:6px 10px;background:rgba(251,191,36,0.12);border:1px solid rgba(251,191,36,0.45);border-radius:6px;color:#fbbf24;font-size:0.78rem;font-weight:600';
        b.textContent = '⚠ Режим «только базовые образы» — outfit-варианты из [BLOCKING] игнорируются, все персонажи используют базовый портрет. Сними галочку выше чтобы Composer подбирал костюмы по сцене.';
        refsHost.parentElement.insertBefore(b, refsHost);
      }
    } else {
      if (old) old.remove();
    }
  }
}

function sdRenderRefs() {
  const slot = document.getElementById('sd-ref-slots');
  if (!slot) return;
  // Keep the base-only warning in sync every time refs render
  try { sdUpdateBaseOnlyHint(); } catch {}
  // Backfill `tag` for refs loaded from older state (chunks reused via "Reuse"
  // or sdComposeFill paths that don't go through sdSlotDrop). Use array
  // position +1 only when no stable tag exists yet — preserves backwards-compat.
  SD.refs.forEach((r, i) => { if (r && !r.tag) r.tag = i + 1; });
  slot.innerHTML = SD.refs.map((r, i) => {
    const tag = r.tag || (i + 1);
    // Always render an explicit outfit badge for character refs so the user
    // can SEE at a glance whether a costume is attached or it's the base
    // portrait. For loc/lastframe/cutframe — no outfit concept, skip badge.
    let outfitBadge = '';
    if (r.kind === 'char') {
      if (r.outfit) {
        outfitBadge = `<div class="ref-outfit named" title="Используется образ: ${esc(r.outfit)}">👗 ${esc(r.outfit)}</div>`;
      } else {
        outfitBadge = `<div class="ref-outfit base" title="Используется базовый портрет персонажа">⊙ BASE</div>`;
      }
    }
    const kindIcon = r.kind === 'loc' ? '🏛'
                  : r.kind === 'lastframe' ? '🎞'
                  : r.kind === 'cutframe' ? '✂'
                  : r.kind === 'char' ? '👤' : '🖼';
    return `
    <div class="sd-ref-chip" data-i="${i}"
         ondragover="sdSlotDragOver(event)"
         ondragleave="sdSlotDragLeave(event)"
         ondrop="sdSlotDrop(event,${i})"
         title="@Image${tag}: ${esc(r.name)}${r.outfit ? ' / '+esc(r.outfit) : (r.kind==='char' ? ' / base portrait' : '')} — перетащи сюда другую карточку чтобы заменить">
      <div class="ref-top">@Image${tag}</div>
      ${r.photoUrl
        ? `<img src="${r.photoUrl}" alt="">`
        : '<div class="ref-noimg">no photo</div>'}
      ${outfitBadge}
      <div class="ref-bottom">
        <span class="ref-kind">${kindIcon}</span>
        <span class="ref-name">${esc(r.name || '—')}</span>
      </div>
      <button class="rm" onclick="sdRemoveRef(${i})" title="Убрать">×</button>
    </div>`;
  }).join('');
}

function sdSlotDragOver(e) {
  e.preventDefault();
  e.currentTarget.classList.add('drop-target');
}
function sdSlotDragLeave(e) {
  e.currentTarget.classList.remove('drop-target');
}
async function sdSlotDrop(e, idx) {
  e.preventDefault();
  e.stopPropagation();
  e.currentTarget.classList.remove('drop-target');

  // 1. File from desktop
  const files = Array.from(e.dataTransfer.files || []).filter(f => f.type.startsWith('image/'));
  if (files.length) {
    // Replace this slot with custom-uploaded file
    const fd = new FormData();
    fd.append('file', files[0]);
    try {
      const res = await api.upload(
        `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`, fd
      );
      if (res.url) {
        SD.refs[idx] = {
          kind: 'url', id: 'custom-' + Date.now(),
          name: res.name || files[0].name, photoUrl: res.url, url: res.url,
        };
        sdRenderRefs();
      }
    } catch (err) { showToast('✗ ' + (err.message || err)); }
    return;
  }

  // 2. URL from external tab
  const uri = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain');
  if (uri && /^https?:\/\//.test(uri.trim()) && /\.(png|jpe?g|webp|gif)(\?|$)/i.test(uri.trim())) {
    try {
      const res = await api.post(
        `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/upload-ref`,
        { url: uri.trim() }
      );
      if (res.url) {
        SD.refs[idx] = {
          kind: 'url', id: 'custom-' + Date.now(),
          name: res.name || 'custom', photoUrl: res.url, url: res.url,
        };
        sdRenderRefs();
      }
    } catch (err) { showToast('✗ ' + (err.message || err)); }
    return;
  }

  // 3. Internal char/loc card payload
  let payload;
  try { payload = JSON.parse(e.dataTransfer.getData('application/json') || '{}'); }
  catch { return; }
  if (!payload.kind || !payload.id) return;
  // Resolve name + photoUrl from series data so chip renders correctly
  let name = payload.name || '', photoUrl = payload.photoUrl || '';
  if (!name || !photoUrl) {
    if (payload.kind === 'char') {
      const c = (S.series.characters || []).find(x => x.id === payload.id);
      if (c) {
        name = c.name;
        if (payload.outfit) {
          const o = (c.outfits || []).find(o => o.label === payload.outfit);
          if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
        }
        if (!photoUrl && c.ref_images?.[0]) photoUrl = `${assetUrl(c.ref_images[0])}`;
      }
    } else if (payload.kind === 'loc') {
      const l = (S.series.locations || []).find(x => x.id === payload.id);
      if (l) { name = l.name; if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`; }
    }
  }
  SD.refs[idx] = { ...payload, name, photoUrl };
  sdRenderRefs();
}

function sdRemoveRef(i) {
  SD.refs.splice(i, 1);
  sdRenderRefs();
}

// Remap @ImageN tags in `promptText` so they reference refs by their CURRENT
// positional order (Seedance API is positional), while the UI shows STABLE
// tags. Removes any @ImageN entries whose tag has no surviving ref.
//
// e.g. user has refs [A(tag=1), C(tag=3)] (after deleting tag=2) and prompt:
//   "@Image1=A, @Image2=B, @Image3=C. A talks to C."
// Returns:
//   "@Image1=A, @Image2=C. A talks to C."  (B's BINDING entry dropped, C
//   renumbered from @Image3 to @Image2 to match positional order in refs[])
function _sdRemapPromptForSubmit(promptText, refs) {
  if (!promptText) return promptText;
  // tag → newPos (1-based)
  const tagToPos = new Map();
  refs.forEach((r, i) => {
    if (r && r.tag) tagToPos.set(r.tag, i + 1);
  });
  // Sentinel-based two-pass to avoid double-rewriting (e.g. 1→2 then 2→3).
  let out = promptText;
  // Pass 1: original @ImageN → \x01IMG\x01N
  out = out.replace(/@Image(\d+)/g, (m, n) => `\x01IMG\x01${n}`);
  // Pass 2: \x01IMG\x01N → @ImageK (remap or drop)
  // For each placeholder, look up tag → new pos. If tag has no pos (ref deleted),
  // we want to strip the WHOLE BINDING entry like `@Image2=Name (description),`
  // — handle that with a separate pass first.
  // First strip BINDING entries for orphan tags:
  for (const [origTag] of [...new Set([...promptText.matchAll(/@Image(\d+)/g)].map(m => parseInt(m[1])))].entries()) {
    // unused — replaced by simpler strip below
  }
  // Simpler: scan all @ImageN occurrences in original prompt; for each unique N
  // that is NOT in tagToPos, strip its binding entry from the post-sentinel text.
  const allTags = [...new Set([...promptText.matchAll(/@Image(\d+)/g)].map(m => parseInt(m[1])))];
  for (const t of allTags) {
    if (!tagToPos.has(t)) {
      // Drop binding-style entry: optional leading comma/space, @ImageT=Name (...) up to next comma/period/newline
      const bindingRe = new RegExp(`,?\\s*\\x01IMG\\x01${t}\\s*[=\\-—]\\s*[^,.\\n]*`, 'g');
      out = out.replace(bindingRe, '');
      // Drop standalone @ImageT mentions
      const standaloneRe = new RegExp(`\\s*\\x01IMG\\x01${t}\\b`, 'g');
      out = out.replace(standaloneRe, '');
    }
  }
  // Now remap surviving \x01IMG\x01N → @ImageK
  out = out.replace(/\x01IMG\x01(\d+)/g, (m, n) => {
    const pos = tagToPos.get(parseInt(n));
    return pos != null ? `@Image${pos}` : '';
  });
  // Tidy: empty parens left from inline tag removal "Kyle (@Image2) listens" → "Kyle  listens",
  // double commas, leading commas after BINDING strips.
  out = out.replace(/\(\s*\)/g, '');
  out = out.replace(/[ \t]+/g, ' ');
  out = out.replace(/,\s*,/g, ',').replace(/(:\s*),/g, '$1').replace(/\(\s*,/g, '(');
  return out;
}

// Recompose the prompt via Claude USING ONLY the refs currently in the SD panel.
// Use case: user manually removed redundant char refs (e.g. extras hanging in
// the corner) and wants the prompt rewritten to focus on who's left, instead
// of editing tokens by hand. Server-side `locked_refs` instructs Claude to use
// EXACTLY these — no auto-detection of missing chars from chunk text.
async function sdRecomposeWithCurrentRefs() {
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  if (!chunk) { showToast('Вставь кусок сценария'); return; }
  if (!SD.refs.length) { showToast('Нет рефов — сначала добавь хотя бы один'); return; }
  const st = document.getElementById('sd-compose-status');
  if (st) st.textContent = '🔄 перекомпоную с текущими рефами...';
  const useLastframe = !!document.getElementById('sd-use-lastframe')?.checked;
  const useCutframes = !!document.getElementById('sd-use-cutframes')?.checked;
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  const closeUpOnly  = !!document.getElementById('sd-close-up-only')?.checked;
  // Snapshot tags so we restore stable labels after the recompose returns
  // (server returns refs without `tag`, so we re-attach them by id+kind+outfit).
  const tagSnapshot = SD.refs.map(r => ({ kind: r.kind, id: r.id, outfit: r.outfit || null, tag: r.tag }));
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/compose`,
      {
        chunk_text: chunk,
        use_prev_lastframe: useLastframe,
        use_prev_cutframes: useCutframes,
        style: useStyle ? styleVal : '',
        base_outfits_only: baseOnly,
        close_up_only: closeUpOnly,
        locked_refs: SD.refs.map(r => ({ kind: r.kind, id: r.id, outfit: r.outfit || null })),
      }
    );
    document.getElementById('sd-prompt').value = res.prompt || '';
    SD.refs = (res.refs || []).map(r => {
      let name = '', photoUrl = '';
      if (r.kind === 'char') {
        const c = (S.series.characters || []).find(x => x.id === r.id);
        if (c) {
          name = c.name;
          if (r.outfit) {
            const o = (c.outfits || []).find(o => o.label === r.outfit);
            if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
          }
          if (!photoUrl && c.ref_images?.[0]) photoUrl = `${assetUrl(c.ref_images[0])}`;
        }
      } else if (r.kind === 'loc') {
        const l = (S.series.locations || []).find(x => x.id === r.id);
        if (l) {
          name = l.name;
          if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`;
        }
      } else if (r.kind === 'item') {
        const it = (S.series.items || []).find(x => x.id === r.id);
        if (it) {
          name = it.name;
          if (it.ref_images?.[0]) photoUrl = `${assetUrl(it.ref_images[0])}`;
        }
      } else if (r.kind === 'lastframe' || r.kind === 'cutframe') {
        name = r.name || (r.kind === 'lastframe' ? 'last frame' : 'pre-cut frame');
        photoUrl = r.url || '';
      }
      if (!photoUrl && r.url) photoUrl = r.url;
      if (!name && r.name) name = r.name;
      // Restore stable tag from pre-recompose snapshot
      const snap = tagSnapshot.find(s => s.kind === r.kind && s.id === r.id && (s.outfit || null) === (r.outfit || null));
      return { ...r, name, photoUrl, tag: snap?.tag };
    });
    sdRenderRefs();
    if (st) st.textContent = '✓ перекомпоновано с пинн-рефами';
  } catch (e) {
    if (st) st.textContent = '✗ ' + (e.message || e);
  }
}

async function sdCompose() {
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  if (!chunk) { showToast('Вставь кусок сценария'); return; }
  const st = document.getElementById('sd-compose-status');
  st.textContent = '⚙ компоную через Claude...';
  const useLastframe = !!document.getElementById('sd-use-lastframe')?.checked;
  const useCutframes = !!document.getElementById('sd-use-cutframes')?.checked;
  const useStyle     = !!document.getElementById('sd-use-style')?.checked;
  const styleVal     = (document.getElementById('sd-style')?.value || '').trim();
  const baseOnly     = !!document.getElementById('sd-base-only')?.checked;
  const closeUpOnly  = !!document.getElementById('sd-close-up-only')?.checked;
  try {
    const res = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/compose`,
      {
        chunk_text: chunk,
        use_prev_lastframe: useLastframe,
        use_prev_cutframes: useCutframes,
        style: useStyle ? styleVal : '',
        base_outfits_only: baseOnly,
        close_up_only: closeUpOnly,
      }
    );
    document.getElementById('sd-prompt').value = res.prompt || '';
    SD.refs = (res.refs || []).map(r => {
      // Re-pull name + photoUrl from the loaded series
      let name = '', photoUrl = '';
      if (r.kind === 'char') {
        const c = (S.series.characters || []).find(x => x.id === r.id);
        if (c) {
          name = c.name;
          if (r.outfit) {
            const o = (c.outfits || []).find(o => o.label === r.outfit);
            if (o?.photo) photoUrl = `${assetUrl(o.photo)}`;
          }
          if (!photoUrl && c.ref_images?.[0]) photoUrl = `${assetUrl(c.ref_images[0])}`;
        }
      } else if (r.kind === 'loc') {
        const l = (S.series.locations || []).find(x => x.id === r.id);
        if (l) {
          name = l.name;
          if (l.ref_images?.[0]) photoUrl = `${assetUrl(l.ref_images[0])}`;
        }
      } else if (r.kind === 'item') {
        const it = (S.series.items || []).find(x => x.id === r.id);
        if (it) {
          name = it.name;
          if (it.ref_images?.[0]) photoUrl = `${assetUrl(it.ref_images[0])}`;
        }
      } else if (r.kind === 'lastframe') {
        // Server-attached continuity frame from previous chunk
        name = r.name || 'last frame';
        photoUrl = r.url || '';
      } else if (r.kind === 'cutframe') {
        // Pre-cut keyframe extracted from inside the previous chunk
        name = r.name || 'pre-cut frame';
        photoUrl = r.url || '';
      }
      if (!photoUrl && r.url) photoUrl = r.url;
      if (!name && r.name) name = r.name;
      return { ...r, name, photoUrl };
    });
    sdRenderRefs();
    // B1: if composer explicitly reset scene_continuity AND prev_neighbour
    // exists — capture the reason so /seedance/start can persist it on the
    // chunk for the yellow «⚠ континьюити сброшен» badge.
    if (res.scene_continuity === false && res.prev_neighbour) {
      SD._continuityResetReason = (res.reasoning || '').trim() ||
        'composer explicitly reset scene continuity';
    } else {
      delete SD._continuityResetReason;
    }
    let msg = res.scene_continuity ? '✓ продолжение прошлой сцены'
      : (res.prev_neighbour ? '⚠ континьюити сброшен composer\'ом' : '✓ скомпоновано');
    if (res.lastframe_attached) msg += ' · 🎞 last frame прицеплен';
    if (res.cutframes_attached) msg += ` · ✂ ${res.cutframes_attached} кадр(ов) перед склейками`;
    if (res.state_analysis_attached) msg += ' · 🧠 состояние персов проанализировано';
    if (res.framing) msg += ` · 🎥 ${res.framing}`;
    if ((res.anti_background_scrub_hits || []).length) {
      msg += ` · 🧹 background-scrub: ${res.anti_background_scrub_hits.length}`;
    }
    if (res.pose_lock_fallback && res.pose_lock_fallback !== 'vision') {
      msg += ` · 🦴 pose-lock: ${res.pose_lock_fallback}`;
    }
    if ((res.compose_warnings || []).length) {
      const kinds = res.compose_warnings.map(w => w.kind).join(', ');
      msg += ` · ⚠ ${kinds}`;
    }
    if (!res.cur_pos_found) {
      msg += ' · ⚠ позицию в сценарии не нашёл (continuity без соседа)';
    } else if (res.prev_neighbour) {
      const pn = res.prev_neighbour;
      const epPart = pn.episode && pn.episode !== S.episode.number ? ` (эп ${pn.episode})` : '';
      const vid = pn.has_video ? '' : ' [нет видео]';
      msg += ` · prev: #${pn.idx}${epPart}${vid}`;
    } else {
      msg += ' · prev: нет (первый в эп.)';
    }
    if ((res.unresolved_refs || []).length) {
      const names = res.unresolved_refs.map(r => `${r.kind}:${r.id}`).join(', ');
      msg += ` · ⚠ не подгрузились: ${names}`;
      showToast('⚠ Часть рефов не удалось подгрузить (' + names + ') — проверь, есть ли у локации/перса фото');
    }
    if ((res.outfit_fallbacks || []).length) {
      const fbs = res.outfit_fallbacks;
      msg += ` · ⚠ outfit-fallback × ${fbs.length}`;
      const lines = fbs.map(f => {
        const avail = (f.available_labels || []).length
          ? `имеются: ${f.available_labels.join(', ')}`
          : 'у перса нет outfits — только база';
        return `• ${f.char_name}: запрошен "${f.requested_outfit}", откатились на base (${avail})`;
      }).join('\n');
      showToast('⚠ Composer выбрал несуществующий outfit-label — откат на базу:\n' + lines, 9000);
    }
    if ((res.duplicate_chars_dropped || []).length) {
      const dups = res.duplicate_chars_dropped;
      msg += ` · 🚫 удалено дублей × ${dups.length}`;
      showToast(
        '🚫 Composer пытался добавить персонажа дважды — продублированные ref\'ы удалены автоматически (защита от двойников в кадре). ' +
        'Если такое повторяется часто — пришли промпт, докрутим правила.',
        7000
      );
    }
    if ((res.closeup_dropped || []).length) {
      const dropped = res.closeup_dropped;
      msg += ` · 🎯 close-up: дроп ${dropped.length} перс`;
      const names = dropped.map(d => d.char_name).join(', ');
      showToast(`🎯 Close-up only: оставлен один перс + локация. Дропнуты из refs: ${names}.`, 5000);
    }
    if (res.auto_close_up_detected) {
      msg += ' · 🎯 auto-close-up hint';
    }
    // Auto-fit the duration slider to the composed chunk — runs `chunk_text`
    // through the same estimator used for badges + segmentation. Respects
    // manual overrides (won't fight a user who dragged the slider away from
    // the last auto-set value).
    _sdAutoSetDurationIfManual();
    const recDur = _sdRecommendedDurationFromChunkText(chunk);
    if (recDur != null) msg += ` · ⏱ ${recDur}с`;
    st.textContent = msg;
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
  }
}

function sdToggleStyleField() {
  const cb = document.getElementById('sd-use-style');
  const inp = document.getElementById('sd-style');
  if (!inp) return;
  inp.classList.toggle('hidden', !cb?.checked);
  if (cb?.checked) inp.focus();
  sdSavePrefs();
}

// Per-series localStorage key for moderation_bypass — keeps grid/cartoon/etc
// scoped to one show. Switching series should NOT carry over the mod-bypass
// setting (different shows have different moderation profiles, e.g. romantic
// drama works fine with `grid` while a violent thriller needs `cartoon`).
function _sdModKey(sid) { return `sd_mod_bypass_${sid || ''}`; }

// True when project's visual style is animated/cartoonish (Pixar, anime, etc).
// Used to auto-default mod-bypass to `cartoon` for new series with no per-series
// override yet. Mirrors the backend `stylised` check in _series_style_clause.
function _seriesIsCartoonish(s) {
  if (!s) return false;
  const type = (s.style && s.style.type || '').toLowerCase();
  if (type === 'pixar' || type === 'anime') return true;
  const v = (s.visual_style || '').toLowerCase();
  if (!v) return false;
  return ['pixar','anime','manga','cartoon','claymation','studio ghibli','arcane','comic','graphic novel']
    .some(k => v.includes(k));
}

function sdSavePrefs() {
  try {
    const modBypass = document.getElementById('sd-mod-bypass').value;
    localStorage.setItem('sd_prefs', JSON.stringify({
      duration: document.getElementById('sd-duration').value,
      resolution: document.getElementById('sd-resolution').value,
      moderation_bypass: modBypass,    // also kept globally as fallback default for new series
      model_tier: document.getElementById('sd-model-tier')?.value || 'reference-fast',
      use_prev_lastframe: !!document.getElementById('sd-use-lastframe')?.checked,
      use_prev_cutframes: !!document.getElementById('sd-use-cutframes')?.checked,
      use_style: !!document.getElementById('sd-use-style')?.checked,
      style: (document.getElementById('sd-style')?.value || '').trim(),
      base_outfits_only: !!document.getElementById('sd-base-only')?.checked,
      close_up_only: !!document.getElementById('sd-close-up-only')?.checked,
    }));
    // Per-series override
    if (S.seriesId) {
      localStorage.setItem(_sdModKey(S.seriesId), modBypass);
    }
  } catch (e) {}
}

function sdLoadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem('sd_prefs') || '{}');
    if (p.duration) document.getElementById('sd-duration').value = p.duration;
    if (p.resolution) document.getElementById('sd-resolution').value = p.resolution;
    // moderation_bypass default: Seedance no longer enforces grid-mode
    // moderation, so every NEW series starts with bypass OFF. Only an explicit
    // per-series override (user manually picked grid/collage_grid/cartoon for a
    // risky show) survives — it does NOT carry over to other shows, and there is
    // no cartoon-style or global last-used auto-default anymore.
    const seriesMod = S.seriesId ? localStorage.getItem(_sdModKey(S.seriesId)) : null;
    const finalMod = seriesMod || 'off';
    if (finalMod) document.getElementById('sd-mod-bypass').value = finalMod;
    const cb = document.getElementById('sd-use-lastframe');
    if (cb && typeof p.use_prev_lastframe === 'boolean') cb.checked = p.use_prev_lastframe;
    const cf = document.getElementById('sd-use-cutframes');
    if (cf && typeof p.use_prev_cutframes === 'boolean') cf.checked = p.use_prev_cutframes;
    const sc = document.getElementById('sd-use-style');
    const si = document.getElementById('sd-style');
    if (sc && typeof p.use_style === 'boolean') sc.checked = p.use_style;
    if (si && typeof p.style === 'string') si.value = p.style;
    if (si && sc) si.classList.toggle('hidden', !sc.checked);
    const bo = document.getElementById('sd-base-only');
    if (bo && typeof p.base_outfits_only === 'boolean') bo.checked = p.base_outfits_only;
    const cu = document.getElementById('sd-close-up-only');
    if (cu && typeof p.close_up_only === 'boolean') cu.checked = p.close_up_only;
    const mt = document.getElementById('sd-model-tier');
    if (mt && typeof p.model_tier === 'string'
        && (p.model_tier === 'reference-pro' || p.model_tier === 'reference-fast')) {
      mt.value = p.model_tier;
    }
  } catch (e) {}
}

async function sdGenerate() {
  console.log('[sdGenerate] click');
  // Pre-flight: warn if any active char/loc lacks a generated ref. Without
  // this the user gets a video with random face/location for the missing one.
  if (!await _confirmMissingAssetsBeforeGen('генерацию видео')) return;
  const promptEl = document.getElementById('sd-prompt');
  const prompt = (promptEl?.value || '').trim();
  if (!prompt) {
    showToast('⚠ Промпт пустой — заполни поле "Prompt"', 4000);
    promptEl?.focus();
    promptEl?.classList.add('input-error');
    setTimeout(() => promptEl?.classList.remove('input-error'), 2000);
    return;
  }
  const chunk = document.getElementById('sd-chunk-text').value.trim();
  const duration = parseInt(document.getElementById('sd-duration').value) || 15;
  const resolution = document.getElementById('sd-resolution').value;
  const moderation_bypass = document.getElementById('sd-mod-bypass').value;
  const model_tier = document.getElementById('sd-model-tier')?.value || 'reference-fast';
  sdSavePrefs();

  const btn = document.querySelector('#seedance-panel button[onclick="sdGenerate()"]');
  const oldHtml = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ ...'; }

  // Optimistic placeholder so the user sees a card immediately
  try {
    const optimistic = {
      idx: '…', status: 'submitting', prompt, duration, resolution,
      moderation_bypass, progress: null, _optimistic: true,
    };
    sdRenderList([...(SD._lastChunks || []), optimistic]);
    document.getElementById('sd-gen-list')?.scrollIntoView({behavior:'smooth', block:'nearest'});
  } catch (err) {
    console.warn('[sdGenerate] optimistic render failed (continuing anyway):', err);
  }

  try {
    // Remap @ImageN tags in prompt to match positional order of refs[] before
    // sending to API. UI keeps stable tags (so removing @Image2 leaves @Image3
    // visible as @Image3), but Seedance is positional — we must renumber on
    // submit. Also strips BINDING entries for tags whose ref was deleted.
    const submitPrompt = _sdRemapPromptForSubmit(prompt, SD.refs);
    const startBody = {
      prompt: submitPrompt, chunk_text: chunk, duration, resolution, moderation_bypass,
      model: model_tier,
      refs: SD.refs.map(r => ({ kind: r.kind, id: r.id, outfit: r.outfit || null, url: r.url || null })),
    };
    // Infer script_order from current episode script so a manual regenerate
    // lands in the correct timeline slot during auto-assemble. Without this
    // the new chunk has script_order=null → backend treats it as an orphan
    // and dumps it at the tail of the final cut (out of chronological order).
    try {
      if (chunk && typeof _autoCollectSegments === 'function') {
        const segs = _autoCollectSegments();
        const _norm = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 200).toLowerCase();
        const ck = _norm(chunk);
        if (ck) {
          let bestIdx = -1;
          for (let i = 0; i < segs.length; i++) {
            const sk = _norm(segs[i].text);
            if (sk && (sk === ck || sk.startsWith(ck.slice(0, 80)) || ck.startsWith(sk.slice(0, 80)))) {
              bestIdx = i; break;
            }
          }
          if (bestIdx >= 0) {
            startBody.script_order = bestIdx;
            const seg = segs[bestIdx];
            if (typeof seg.sceneIdx === 'number') startBody.sceneIdx = seg.sceneIdx;
            if (typeof seg.segIdx === 'number')   startBody.segIdx   = seg.segIdx;
          }
        }
      }
    } catch (err) {
      console.warn('[sdGenerate] script_order inference failed (continuing without):', err);
    }
    // B1: carry the «continuity reset by composer» reason from the last
    // sdCompose response so it lands on the chunk record for the UI badge.
    if (SD._continuityResetReason) {
      startBody.continuity_reset_reason = SD._continuityResetReason;
    }
    const startUrl = `/api/series/${S.seriesId}/episodes/${S.episode.number}/seedance/start`;
    let res;
    try {
      res = await api.post(startUrl, startBody);
    } catch (e) {
      // Precheck false-positive escape hatch: backend's Haiku precheck is
      // conservative on self-harm-adjacent scenes (e.g. character holding a
      // pill bottle). Show reasoning + suggestion and let the user override
      // via skip_precheck. Without this the toast only shows the bare code.
      if (e?.payload?.error === 'moderation_precheck_reject') {
        const pc = e.payload.precheck || {};
        const ok = window.confirm(
          '⚠ Pre-flight модерация Seedance отклонила промпт.\n\n' +
          'Причина: ' + (pc.reasoning || '—') + '\n' +
          (pc.categories?.length ? 'Категории: ' + pc.categories.join(', ') + '\n' : '') +
          'Подсказка: ' + (pc.suggestion || '—') + '\n\n' +
          'Отправить всё равно? (Seedance может всё же отклонить — но если это false positive, прорвётся.)'
        );
        if (!ok) throw e;
        res = await api.post(startUrl, { ...startBody, skip_precheck: true });
        showToast(`▶ Чанк #${res.chunk?.idx ?? '?'} в очереди (precheck bypassed)`);
        await sdRefreshList();
        sdEnsurePoll();
        return;
      }
      throw e;
    }
    showToast(`▶ Чанк #${res.chunk?.idx ?? '?'} в очереди — можно листать дальше, генерация 1-15 мин`);
    // Не очищаем prompt/chunk_text/refs — часто хочется доработать тот же промпт
    // и сгенерировать вариацию. Хочешь чистый лист — кнопка ↻ Reuse / руками.
    await sdRefreshList();
    sdEnsurePoll();
  } catch (e) {
    showToast('✗ Ошибка запуска: ' + (e.message || e), 8000);
    await sdRefreshList();
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml || '▶ Сгенерировать'; }
  }
}

// Show «🎬 Собрать серию» button when there's at least one completed chunk,
// and the «⬇ Скачать финал» link when an assembled mp4 already exists.
function _sdUpdateAssembleUI(chunks) {
  const btn = document.getElementById('sd-assemble-btn');
  const link = document.getElementById('sd-assembled-link');
  if (!btn || !link) return;
  const completed = (chunks || []).filter(c => c.status === 'completed' && c.video_path);
  btn.style.display = completed.length > 0 ? '' : 'none';
  if (completed.length > 0) {
    btn.innerHTML = `🎬 Собрать серию · ${completed.length} чанк${completed.length === 1 ? '' : (completed.length < 5 ? 'а' : 'ов')}`;
  }
  const assembledRel = S.episode?.assembled_path;
  if (assembledRel && S.seriesId) {
    link.style.display = '';
    // href is set for accessibility but actual download triggered via onclick
    // (downloadAssembledWithMusic) so we can also pull MUS_*.wav alongside.
    link.href = `/assets/${S.seriesId}/${assembledRel}?v=${S.episode.assembled_at || ''}`;
    link.title = `Скачать ${assembledRel.split('/').pop()} (+ музыка)`;
  } else {
    link.style.display = 'none';
  }
  // Refresh music UI on every chunk update — cheap (uses cached poll data).
  try { _sdUpdateMusicUI(chunks, MUSIC.last[_musicKey()] || null); } catch {}
  try { _sdRefreshMusic(); } catch {}
}

async function sdAssembleEpisode(btn) {
  if (!S.seriesId || !S.episode) { showToast('Открой серию'); return; }
  const chunks = (SD._lastChunks || []).filter(c => c.status === 'completed' && c.video_path);
  if (!chunks.length) { showToast('Нет готовых чанков для сборки', 3000); return; }
  // Warn if some segments are missing (not all rendered yet).
  const segmentCount = _autoCollectSegments().length || chunks.length;
  if (chunks.length < segmentCount) {
    const proceed = await appConfirm({
      title: '⚠ Серия собрана не полностью',
      message: `Готовых чанков: ${chunks.length}, а сегментов в сценарии: ${segmentCount}.\n\n` +
               `Можно собрать всё равно — получится короче чем полный эпизод. Или сначала ` +
               `догенерировать недостающие чанки.`,
      okText: 'Всё равно собрать',
      cancelText: 'Отмена',
      okStyle: 'accent',
    });
    if (!proceed) return;
  }
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Склейка ffmpeg…';
  try {
    const r = await api.post(
      `/api/series/${S.seriesId}/episodes/${S.episode.number}/auto-assemble`,
      { require_all: false, expected_segments: segmentCount },
      { timeoutMs: 900_000 }
    );
    if (r.error) {
      const detail = r.stderr ? '\n' + r.stderr.slice(-600) : '';
      console.error('[assemble] ffmpeg stderr:', r.stderr);
      throw new Error(r.error + detail);
    }
    // Refresh episode so we get the new assembled_path.
    try {
      S.episode = await api.get(`/api/series/${S.seriesId}/episodes/${S.episode.number}`);
    } catch {}
    _sdUpdateAssembleUI(SD._lastChunks || []);
    showToast(`✓ Собрано: ${r.filename} (${r.size_mb}MB, ${r.chunks} чанков)`, 8000);
    // Auto-trigger download.
    const link = document.getElementById('sd-assembled-link');
    if (link && link.style.display !== 'none') link.click();
  } catch (e) {
    showToast('Ошибка сборки: ' + (e?.message || e), 6000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}

