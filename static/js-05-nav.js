// ── Navigation ────────────────────────────────────────────────────────────────
function navigate(view, params = {}) {
  document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
  document.getElementById('view-' + view).classList.remove('hidden');
  Object.assign(S, params);
  // Apply cached video-provider mode IMMEDIATELY so refreshing on a series/
  // episode page doesn't flash Reteller-only DOM for ~300ms while waiting
  // for /api/series to return. The fetched value reconciles via
  // applyVideoProviderMode() below.
  if (view === 'series' || view === 'episode') {
    _preApplyVideoProviderMode(S.seriesId);
  } else {
    document.body.classList.remove('seedance-mode');
  }

  // Topnav "Редактор" button — visible whenever a series is open and we're
  // not already inside the montage editor.
  const navMt = document.getElementById('nav-montage-btn');
  if (navMt) {
    const showMt = !!S.seriesId && view !== 'montage' && view !== 'projects';
    navMt.classList.toggle('hidden', !showMt);
  }

  // Persist current location in URL hash so a page reload restores the view.
  try {
    let h = '';
    if (view === 'projects')      h = '';
    else if (view === 'series')   h = `#series/${encodeURIComponent(S.seriesId || '')}`;
    else if (view === 'episode')  h = `#episode/${encodeURIComponent(S.seriesId || '')}/${S.episodeNum || ''}`;
    else if (view === 'montage')  h = `#montage/${encodeURIComponent(S.seriesId || '')}`;
    if (location.hash !== h) location.hash = h;
  } catch {}

  if (view === 'projects') {
    S.seriesId = null;
    if (navMt) navMt.classList.add('hidden');
    setBreadcrumb([]);
    loadProjects();
  } else if (view === 'series') {
    loadSeriesView();
  } else if (view === 'episode') {
    loadEpisodeView();
  } else if (view === 'montage') {
    loadMontageView();
  }
  // Re-scope suspended Auto-mode placeholders to the new series (or clear them
  // entirely on /projects). Without this, refreshing inside Series A while a
  // suspended run for Series B sits in localStorage would show B's «Продолжить»
  // button — clicking it would yank the user away to Series B.
  try { if (typeof _autoRestoreSuspendedRuns === 'function') _autoRestoreSuspendedRuns(); } catch {}
}

function _navFromHash() {
  const h = (location.hash || '').replace(/^#/, '');
  if (!h) { navigate('projects'); return; }
  const parts = h.split('/').map(decodeURIComponent);
  const [view, sid, epNum] = parts;
  if (view === 'series' && sid)   { navigate('series',  { seriesId: sid }); return; }
  if (view === 'episode' && sid && epNum) {
    navigate('episode', { seriesId: sid, episodeNum: parseInt(epNum, 10) });
    return;
  }
  if (view === 'montage' && sid)  { navigate('montage', { seriesId: sid }); return; }
  navigate('projects');
}

window.addEventListener('hashchange', () => {
  // Only react to genuine outside changes (back/forward) — guard re-entry
  // by comparing to current state, otherwise navigate() already wrote it.
  const h = (location.hash || '').replace(/^#/, '');
  const currentExpected = (() => {
    if (!S.seriesId) return '';
    const view = document.querySelector('.view:not(.hidden)')?.id?.replace('view-', '');
    if (view === 'series')  return `series/${encodeURIComponent(S.seriesId)}`;
    if (view === 'episode') return `episode/${encodeURIComponent(S.seriesId)}/${S.episodeNum || ''}`;
    if (view === 'montage') return `montage/${encodeURIComponent(S.seriesId)}`;
    return '';
  })();
  if (h !== currentExpected) _navFromHash();
});

function setBreadcrumb(items) {
  const el = document.getElementById('breadcrumb');
  if (!items.length) { el.innerHTML = ''; return; }
  el.innerHTML = items.map((item, i) => {
    if (i === items.length - 1) return `<span class="crumb">${item.label}</span>`;
    return `<span class="crumb link" onclick="${item.action}" style="cursor:pointer;color:var(--muted)">${item.label}</span><span class="sep">/</span>`;
  }).join('');
}

function goBackToSeries() {
  if (S.episode) {
    // Auto-save before going back
    const changed = collectEpisodeForm();
    if (changed) saveEpisodeSilent().then(() => navigate('series', { seriesId: S.seriesId }));
    else navigate('series', { seriesId: S.seriesId });
  } else {
    navigate('series', { seriesId: S.seriesId });
  }
}

