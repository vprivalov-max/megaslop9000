
// ─────────────────────────────────────────────────────────────────────────────
// Montage / Timeline
// ─────────────────────────────────────────────────────────────────────────────
const MT = {
  clips: [],          // [{id, in, out, video_path, episode, chunk_idx, orig_duration, ...}]
  dragId: null,
  // Preview engine state:
  pxPerSec: 30,       // timeline scale
  globalTime: 0,      // current playhead position in TIMELINE seconds (sum of trimmed clips)
  playing: false,
  curIdx: -1,         // currently loaded clip index in <video>
  scrubbing: false,
};

function openMontage() {
  if (!S.seriesId) return;
  navigate('montage', { seriesId: S.seriesId });
}
function closeMontage() {
  navigate('series', { seriesId: S.seriesId });
}

function mtPlayheadKey() { return `mt_playhead_${S.seriesId}`; }
function mtSavePlayhead() {
  try {
    if (!S.seriesId) return;
    localStorage.setItem(mtPlayheadKey(), JSON.stringify({
      t: MT.globalTime, ts: Date.now(),
    }));
  } catch {}
}
function mtLoadPlayhead() {
  try {
    if (!S.seriesId) return 0;
    const raw = localStorage.getItem(mtPlayheadKey());
    if (!raw) return 0;
    const d = JSON.parse(raw);
    return Math.max(0, +d.t || 0);
  } catch { return 0; }
}

async function loadMontageView() {
  // Reset preview state (will be restored from localStorage below if data exists)
  MT.globalTime = 0; MT.curIdx = -1; MT.playing = false;
  const v = document.getElementById('mt-preview');
  if (v) { v.pause?.(); v.removeAttribute('src'); v.load?.(); }
  const playBtn = document.getElementById('mt-playbtn');
  if (playBtn) playBtn.textContent = '▶';
  // Title
  try {
    const s = await api.get(`/api/series/${S.seriesId}`);
    document.getElementById('montage-title').textContent = '🎬 Монтаж · ' + (s.title || '');
  } catch {}
  mtAttachPreviewListeners();
  await Promise.all([mtRefreshTimeline(), mtRefreshLibrary(), mtRefreshRenders()]);
  // Restore playhead position (per series, in localStorage)
  if (MT.clips.length) {
    const saved = mtLoadPlayhead();
    const total = mtTotalDur();
    mtSeek(Math.min(saved, Math.max(0, total - 0.1)));
  }
  // Re-position playhead on window resize / scroll
  if (!MT._respBound) {
    window.addEventListener('resize', mtUpdatePlayhead);
    document.getElementById('mt-timeline-wrap')?.addEventListener('scroll', mtUpdatePlayhead, true);
    document.getElementById('mt-timeline')?.addEventListener('scroll', mtUpdatePlayhead, true);
    // Cmd/Ctrl + wheel = zoom
    document.getElementById('mt-timeline')?.addEventListener('wheel', (e) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      e.preventDefault();
      mtZoom(e.deltaY < 0 ? 1 : -1);
    }, { passive: false });
    // Block horizontal page scroll / browser-back swipe anywhere on the
    // montage view, EXCEPT when the wheel happens over the timeline strip
    // (which has its own horizontal-scroll handler).
    document.getElementById('view-montage')?.addEventListener('wheel', (e) => {
      if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return; // only horizontal-intent
      // If event originated inside the timeline strip, let its handler do its job
      if (e.target.closest && e.target.closest('#mt-timeline')) return;
      e.preventDefault();
    }, { passive: false });
    // Cmd/Ctrl+Z while montage view is open
    document.addEventListener('keydown', (e) => {
      const view = document.getElementById('view-montage');
      if (!view || view.classList.contains('hidden')) return;
      // Ignore when typing in inputs/textareas
      const t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z' && !e.shiftKey) {
        e.preventDefault();
        mtUndo();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && (
        (e.key.toLowerCase() === 'z' && e.shiftKey) || e.key.toLowerCase() === 'y'
      )) {
        e.preventDefault();
        mtRedo();
        return;
      }
      // Plain (no-modifier) playback shortcuts
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (!MT.clips || !MT.clips.length) return;
      if (e.code === 'Space') {
        e.preventDefault();
        mtTogglePlay();
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        mtStepFrame(-1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        mtStepFrame(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        mtJumpClip(-1);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        mtJumpClip(1);
      } else if (e.key === 'c' || e.key === 'C' || e.key === 'с' || e.key === 'С') {
        e.preventDefault();
        mtCutAtPlayhead();
      } else if (e.key === 'q' || e.key === 'Q' || e.key === 'й' || e.key === 'Й') {
        e.preventDefault();
        mtTrimToPlayhead('left');
      } else if (e.key === 'w' || e.key === 'W' || e.key === 'ц' || e.key === 'Ц') {
        e.preventDefault();
        mtTrimToPlayhead('right');
      } else if (e.key === 'Home') {
        e.preventDefault();
        mtSeekToStart();
      } else if (e.key === 'End') {
        e.preventDefault();
        mtSeekToEnd();
      } else if (e.key === 'Backspace' || e.key === 'Delete') {
        e.preventDefault();
        if (MT.curIdx >= 0 && MT.clips[MT.curIdx]) {
          mtRemoveClip(MT.clips[MT.curIdx].id);
        } else {
          showToast('Поставь playhead на клип');
        }
      }
    });
    MT._respBound = true;
  }
}

async function mtRefreshTimeline() {
  try {
    const res = await api.get(`/api/series/${S.seriesId}/timeline`);
    MT.clips = res.clips || [];
    mtRenderTimeline();
    mtRefreshUndoBtn();
  } catch (e) { console.error(e); }
}

async function mtRefreshUndoBtn() {
  const undo = document.getElementById('mt-undo-btn');
  const redo = document.getElementById('mt-redo-btn');
  if (!undo) return;
  try {
    const h = await api.get(`/api/series/${S.seriesId}/timeline/history`);
    undo.disabled = !h.depth;
    undo.title = h.depth
      ? `Отменить (⌘Z) — ${h.depth} в стеке, последнее: ${h.last?.label || ''}`
      : 'Нечего отменять';
    if (redo) {
      redo.disabled = !h.redo_depth;
      redo.title = h.redo_depth
        ? `Вернуть (⌘⇧Z) — ${h.redo_depth} в стеке, следующее: ${h.next?.label || ''}`
        : 'Нечего возвращать';
    }
  } catch {}
}

async function mtUndo() {
  const btn = document.getElementById('mt-undo-btn');
  if (btn?.disabled) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/timeline/undo`, {});
    showToast(`↶ Отменено: ${r.restored || ''}`);
    await mtAfterHistoryChange();
  } catch (e) {
    showToast('✗ ' + (e.message || e));
  }
}

async function mtRedo() {
  const btn = document.getElementById('mt-redo-btn');
  if (btn?.disabled) return;
  try {
    const r = await api.post(`/api/series/${S.seriesId}/timeline/redo`, {});
    showToast(`↷ Возвращено: ${r.restored || ''}`);
    await mtAfterHistoryChange();
  } catch (e) {
    showToast('✗ ' + (e.message || e));
  }
}

async function mtAfterHistoryChange() {
  await mtRefreshTimeline();
  const total = mtTotalDur();
  if (MT.globalTime > total) MT.globalTime = Math.max(0, total - 0.5);
  if (MT.clips.length) mtSeek(MT.globalTime);
}

function mtFmtSec(s) { return (Math.round((s || 0) * 10) / 10) + 'с'; }

function mtRenderTimeline() {
  const el = document.getElementById('mt-timeline');
  const empty = document.getElementById('mt-empty');
  const summary = document.getElementById('mt-summary');
  const playhead = document.getElementById('mt-playhead');
  if (!el) return;
  const totalDur = mtTotalDur();
  summary.textContent = MT.clips.length
    ? `${MT.clips.length} клип(ов) · ~${mtFmtSec(totalDur)}` : '';
  if (!MT.clips.length) {
    el.innerHTML = '';
    empty?.classList.remove('hidden');
    if (playhead) playhead.classList.add('hidden');
    mtUpdateTimeLabel();
    return;
  }
  empty?.classList.add('hidden');
  el.innerHTML = MT.clips.map((c, i) => {
    const url = _chunkVideoUrl(c);
    const dur = Math.max(0.1, (c.out || 0) - (c.in || 0));
    const w = Math.max(70, dur * MT.pxPerSec);
    const trimMark = (c.in > 0.05 || (c.orig_duration && Math.abs(c.out - c.orig_duration) > 0.05))
      ? ' ✂' : '';
    const origDur = c.orig_duration || c.out || 0;
    return `
      <div class="mt-clip${i === MT.curIdx ? ' is-active' : ''}" draggable="true" data-id="${c.id}"
           style="width:${w}px"
           ondragstart="mtDragStart(event,'${c.id}')"
           ondragover="mtDragOver(event,'${c.id}')"
           ondragleave="mtDragLeave(event)"
           ondrop="mtDrop(event,'${c.id}')"
           ondragend="mtDragEnd(event)">
        <div class="mt-trim-handle left"  title="Тяни — обрезать слева"
             onmousedown="mtTrimStart(event,'${c.id}','in',${origDur})"></div>
        <div class="mt-trim-handle right" title="Тяни — обрезать справа"
             onmousedown="mtTrimStart(event,'${c.id}','out',${origDur})"></div>
        ${url ? `<video src="${url}#t=${c.in||0},${c.out||0}" preload="metadata" muted></video>`
              : `<div style="height:180px;background:#000;color:#888;display:flex;align-items:center;justify-content:center;font-size:11px">нет видео</div>`}
        <div class="mt-clip-meta">
          #${i+1} · <span class="ep">ep${c.episode}·#${c.chunk_idx}</span><br>
          ${mtFmtSec(dur)}${trimMark}${c.crop ? ' <span class="crop-mark">▢</span>' : ''}
        </div>
        <div class="mt-clip-actions">
          <button onclick="mtSeekToClip(${i})" title="Перейти к началу клипа">⏵</button>
          <button onclick="cropOpen('${c.id}')" title="Crop &amp; position">▢</button>
          <button class="danger" onclick="mtRemoveClip('${c.id}')" title="Убрать">✕</button>
        </div>
      </div>
    `;
  }).join('');
  mtRenderRuler();
  // Bind ruler scrubbing once
  const ruler = document.getElementById('mt-ruler');
  if (ruler && !ruler._mtBound) {
    ruler.addEventListener('mousedown', mtScrubStart);
    ruler.style.cursor = 'pointer';
    ruler._mtBound = true;
  }
  // Click on the strip to scrub
  if (!el._mtBound) {
    el.addEventListener('mousedown', mtScrubStart);
    el.addEventListener('dragover',  mtStripDragOver);
    el.addEventListener('dragleave', mtStripDragLeave);
    el.addEventListener('drop',      mtStripDrop);
    // Sync ruler scroll with timeline scroll
    el.addEventListener('scroll', () => {
      const r = document.getElementById('mt-ruler');
      if (r) r.scrollLeft = el.scrollLeft;
    });
    // Capture wheel/trackpad — scroll the timeline horizontally instead of
    // letting the page scroll. Vertical wheel translates to horizontal too.
    el.addEventListener('wheel', (ev) => {
      const dx = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      if (dx === 0) return;
      // Only consume if there is room to scroll in that direction
      const max = el.scrollWidth - el.clientWidth;
      const before = el.scrollLeft;
      el.scrollLeft = Math.max(0, Math.min(max, before + dx));
      if (el.scrollLeft !== before || max > 0) ev.preventDefault();
    }, { passive: false });
    el._mtBound = true;
  }
  mtUpdatePlayhead();
}

function mtTotalDur() {
  return MT.clips.reduce((a, c) => a + Math.max(0, (c.out || 0) - (c.in || 0)), 0);
}

// Map global timeline time → {idx, localOffset}
function mtTimeToClip(t) {
  let acc = 0;
  for (let i = 0; i < MT.clips.length; i++) {
    const c = MT.clips[i];
    const d = Math.max(0, (c.out || 0) - (c.in || 0));
    if (t < acc + d || i === MT.clips.length - 1) {
      return { idx: i, local: Math.max(0, Math.min(d, t - acc)) };
    }
    acc += d;
  }
  return { idx: -1, local: 0 };
}

// Map global time → x px on timeline strip
function mtTimeToPx(t) {
  let acc = 0, x = 0;
  for (const c of MT.clips) {
    const d = Math.max(0.1, (c.out || 0) - (c.in || 0));
    const w = Math.max(70, d * MT.pxPerSec);
    if (t <= acc + d) {
      const frac = d > 0 ? (t - acc) / d : 0;
      return x + frac * w;
    }
    acc += d; x += w + 8 /* gap */;
  }
  return x;
}

// Map x px on strip → global time
function mtPxToTime(px) {
  let acc = 0, x = 0;
  for (const c of MT.clips) {
    const d = Math.max(0.1, (c.out || 0) - (c.in || 0));
    const w = Math.max(70, d * MT.pxPerSec);
    if (px <= x + w) {
      const frac = w > 0 ? (px - x) / w : 0;
      return acc + frac * d;
    }
    acc += d; x += w + 8;
  }
  return acc;
}

function mtUpdatePlayhead() {
  const ph = document.getElementById('mt-playhead');
  const strip = document.getElementById('mt-timeline');
  if (!ph || !strip || !MT.clips.length) return;
  const px = mtTimeToPx(MT.globalTime);
  // Strip scrolls horizontally on its own (overflow-x:auto on .mt-timeline).
  // Wrap is non-scrolling parent; playhead lives inside wrap.
  // Visible left within wrap = strip-padding + px − strip.scrollLeft.
  const stripRect = strip.getBoundingClientRect();
  const wrapRect = strip.parentElement.getBoundingClientRect();
  const baseOffset = stripRect.left - wrapRect.left; // usually 0
  const left = baseOffset + 12 /* strip padding */ + px - strip.scrollLeft;
  // Hide if scrolled out of visible area
  const visible = (left >= baseOffset - 2) && (left <= baseOffset + strip.clientWidth + 2);
  ph.style.left = left + 'px';
  ph.classList.toggle('hidden', !visible);
}

function mtFmtTime(s) {
  // MM:SS.t  (one decimal)
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${String(m).padStart(2,'0')}:${sec.toFixed(1).padStart(4,'0')}`;
}
function mtUpdateTimeLabel() {
  const lbl = document.getElementById('mt-time');
  if (!lbl) return;
  lbl.textContent = `${mtFmtTime(MT.globalTime)} / ${mtFmtTime(mtTotalDur())}`;
}

function mtRenderRuler() {
  const inner = document.getElementById('mt-ruler-inner');
  const strip = document.getElementById('mt-timeline');
  if (!inner || !strip) return;
  const total = mtTotalDur();
  const pps = MT.pxPerSec || 30;
  const padLeft = 12; // matches .mt-timeline padding
  const widthPx = Math.max(strip.scrollWidth, total * pps + padLeft * 2);
  inner.style.width = widthPx + 'px';
  if (total <= 0) { inner.innerHTML = ''; return; }
  // Pick a tick step that gives ~50-100 px between major labels
  const targetMajorPx = 80;
  const candidates = [1, 2, 5, 10, 15, 30, 60, 120, 300];
  let major = candidates[candidates.length - 1];
  for (const c of candidates) {
    if (c * pps >= targetMajorPx) { major = c; break; }
  }
  const minor = major / (major >= 5 ? 5 : (major >= 2 ? 2 : 1));
  const parts = [];
  for (let t = 0; t <= total + 0.001; t += minor) {
    const x = padLeft + t * pps;
    const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    parts.push(`<div class="mt-ruler-tick ${isMajor ? 'major' : 'minor'}" style="left:${x}px"></div>`);
    if (isMajor) {
      parts.push(`<div class="mt-ruler-label" style="left:${x}px">${mtFmtTime(t)}</div>`);
    }
  }
  inner.innerHTML = parts.join('');
}

function mtScrubStart(e) {
  if (!MT.clips.length) return;
  if (e.target.closest('button') || e.target.closest('.mt-clip-actions')) return;
  MT.scrubbing = true;
  mtScrubMove(e);
  document.addEventListener('mousemove', mtScrubMove);
  document.addEventListener('mouseup', mtScrubEnd, { once: true });
}
function mtScrubMove(e) {
  if (!MT.scrubbing) return;
  const strip = document.getElementById('mt-timeline');
  if (!strip) return;
  const rect = strip.getBoundingClientRect();
  const px = e.clientX - rect.left - 12 /* padding */ + strip.scrollLeft;
  const t = Math.max(0, Math.min(mtTotalDur(), mtPxToTime(px)));
  mtSeek(t);
}
function mtScrubEnd() {
  MT.scrubbing = false;
  document.removeEventListener('mousemove', mtScrubMove);
}

// ─── Trim handles ───
function mtTrimStart(e, clipId, edge /* 'in'|'out' */, origDur) {
  e.preventDefault();
  e.stopPropagation();
  const clip = MT.clips.find(c => c.id === clipId);
  if (!clip) return;
  const clipEl = document.querySelector(`.mt-clip[data-id="${clipId}"]`);
  if (!clipEl) return;
  const handleEl = e.currentTarget;
  // Disable native HTML5 drag-and-drop on the parent during trim
  clipEl.setAttribute('draggable', 'false');
  clipEl.classList.add('is-trimming');
  handleEl.classList.add('active');

  const startX  = e.clientX;
  const startIn  = clip.in  || 0;
  const startOut = clip.out || 0;
  const maxOut = origDur || startOut;

  // Tooltip
  const tip = document.createElement('div');
  tip.className = 'mt-trim-tooltip';
  document.body.appendChild(tip);

  const onMove = (ev) => {
    const dxSec = (ev.clientX - startX) / MT.pxPerSec;
    let newIn = startIn, newOut = startOut;
    if (edge === 'in')  newIn  = Math.max(0, Math.min(startOut - 0.2, startIn + dxSec));
    else                newOut = Math.max(startIn + 0.2, Math.min(maxOut, startOut + dxSec));
    clip.in = newIn; clip.out = newOut;
    // Live re-render: just update width + meta
    const dur = newOut - newIn;
    const w = Math.max(70, dur * MT.pxPerSec);
    clipEl.style.width = w + 'px';
    const meta = clipEl.querySelector('.mt-clip-meta');
    if (meta) meta.innerHTML = meta.innerHTML.replace(/^.+/, '');  // we won't bother updating text live
    tip.textContent = `${edge === 'in' ? '▶' : '◀'} ${mtFmtSec(dur)} (${mtFmtSec(newIn)}…${mtFmtSec(newOut)})`;
    tip.style.left = ev.clientX + 'px';
    tip.style.top  = ev.clientY + 'px';
    mtUpdateTimeLabel();
  };

  const onUp = async () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    handleEl.classList.remove('active');
    clipEl.classList.remove('is-trimming');
    clipEl.setAttribute('draggable', 'true');
    tip.remove();
    // PATCH if values actually changed
    if (Math.abs(clip.in - startIn) > 0.01 || Math.abs(clip.out - startOut) > 0.01) {
      try {
        const r = await fetch(
          `/api/series/${S.seriesId}/timeline/clips/${clipId}/trim`,
          {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ in: clip.in, out: clip.out }),
          }
        );
        if (!r.ok) throw new Error(await r.text());
        await mtRefreshTimeline();
        // Keep playhead within bounds
        const total = mtTotalDur();
        if (MT.globalTime > total) MT.globalTime = Math.max(0, total - 0.1);
        mtSeek(MT.globalTime);
      } catch (err) {
        showToast('✗ trim: ' + (err.message || err));
        await mtRefreshTimeline();
      }
    } else {
      // reset just in case width drifted
      mtRenderTimeline();
    }
  };

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

// Seek to a specific global time + sync video element
function mtSeek(t) {
  MT.globalTime = t;
  const { idx, local } = mtTimeToClip(t);
  mtLoadClipIntoPlayer(idx, local);
  mtUpdatePlayhead();
  mtUpdateTimeLabel();
  mtSavePlayhead();
  // Auto-scroll strip so playhead stays in view
  const strip = document.getElementById('mt-timeline');
  if (strip) {
    const px = mtTimeToPx(t) + 12 /* strip padding */;
    const margin = 40;
    if (px < strip.scrollLeft + margin) strip.scrollLeft = Math.max(0, px - margin);
    else if (px > strip.scrollLeft + strip.clientWidth - margin)
      strip.scrollLeft = px - strip.clientWidth + margin;
  }
}

function mtLoadClipIntoPlayer(idx, localOffset = 0) {
  const v = document.getElementById('mt-preview');
  if (!v) return;
  if (idx < 0 || idx >= MT.clips.length) return;
  const c = MT.clips[idx];
  if (!c.video_path) return;
  const url = _chunkVideoUrl(c);
  const targetTime = (c.in || 0) + localOffset;
  if (MT.curIdx !== idx) {
    MT.curIdx = idx;
    v.src = url;
    v.addEventListener('loadedmetadata', function once() {
      v.currentTime = targetTime;
      mtApplyPreviewCrop();
      v.removeEventListener('loadedmetadata', once);
    });
    // Mark active clip
    document.querySelectorAll('.mt-clip').forEach((el, i) => {
      el.classList.toggle('is-active', i === idx);
    });
  } else {
    if (Math.abs(v.currentTime - targetTime) > 0.05) v.currentTime = targetTime;
    mtApplyPreviewCrop();
  }
}

// Apply crop fractions of the current clip to the preview <video> via CSS scale + translate.
// Container is .mt-preview-frame (W×H). To make crop window [fx..fx+fw]×[fy..fy+fh]
// fill the container: video CSS size = W/fw × H/fh, translate(-fx*W/fw, -fy*H/fh).
function mtApplyPreviewCrop() {
  const v = document.getElementById('mt-preview');
  const frame = v && v.parentElement;
  if (!v || !frame) return;
  const c = MT.clips[MT.curIdx];
  const crop = c && c.crop;
  // Backend stores {x,y,w,h} as fractions of source frame
  const fx = crop ? (crop.x ?? crop.fx) : null;
  const fy = crop ? (crop.y ?? crop.fy) : null;
  const fw = crop ? (crop.w ?? crop.fw) : null;
  const fh = crop ? (crop.h ?? crop.fh) : null;
  const trivial = !crop || fw == null
    || (fw >= 0.999 && fh >= 0.999 && (fx ?? 0) < 0.001 && (fy ?? 0) < 0.001);
  if (trivial) {
    v.classList.remove('cropped');
    v.style.width = '100%';
    v.style.height = '100%';
    v.style.transform = 'none';
    return;
  }
  v.classList.add('cropped');
  const W = frame.clientWidth || 240;
  const H = frame.clientHeight || 360;
  const cfx = Math.max(0, Math.min(1, fx || 0));
  const cfy = Math.max(0, Math.min(1, fy || 0));
  const cfw = Math.max(0.01, Math.min(1, fw || 1));
  const cfh = Math.max(0.01, Math.min(1, fh || 1));
  const dispW = W / cfw;
  const dispH = H / cfh;
  v.style.width = dispW + 'px';
  v.style.height = dispH + 'px';
  v.style.transform = `translate(${-cfx * dispW}px, ${-cfy * dispH}px)`;
}

// Zoom timeline (px per second). Keeps playhead anchored in viewport.
function mtZoom(dir) {
  const STEPS = [10, 15, 20, 30, 45, 60, 90, 120, 180, 240];
  const cur = MT.pxPerSec;
  let i = STEPS.findIndex(s => s >= cur);
  if (i < 0) i = STEPS.length - 1;
  if (dir > 0) i = Math.min(STEPS.length - 1, i + 1);
  else i = Math.max(0, i - 1);
  mtSetZoom(STEPS[i]);
}
function mtZoomFit() {
  const strip = document.getElementById('mt-timeline');
  const total = mtTotalDur();
  if (!strip || total <= 0) return;
  const avail = Math.max(100, strip.clientWidth - 24); // minus padding
  // ensure clips can shrink: minimum is what mtRenderTimeline enforces (Math.max(70, dur*pxPerSec))
  // so target is avail/total
  let v = Math.max(8, Math.min(240, Math.floor(avail / total)));
  mtSetZoom(v);
}
function mtSetZoom(v) {
  // Anchor: keep playhead pixel position stable in viewport
  const strip = document.getElementById('mt-timeline');
  const oldPx = mtTimeToPx(MT.globalTime);
  const visOld = strip ? (oldPx - strip.scrollLeft) : 0;
  MT.pxPerSec = v;
  const lbl = document.getElementById('mt-zoom-val');
  if (lbl) lbl.textContent = v + ' px/s';
  mtRenderTimeline();
  // Restore: scroll strip so playhead lands at same viewport offset
  if (strip) {
    const newPx = mtTimeToPx(MT.globalTime);
    strip.scrollLeft = Math.max(0, newPx - visOld);
  }
  mtUpdatePlayhead();
}

// Jump playhead to the previous/next clip boundary (cut between clips).
// dir = -1 → previous boundary, +1 → next.
// Boundaries are at the start of each clip (0, dur(0), dur(0)+dur(1), …) and
// at the end of the last clip (= total duration).
function mtJumpClip(dir) {
  if (!MT.clips || !MT.clips.length) return;
  const v = document.getElementById('mt-preview');
  if (v && !v.paused) { v.pause(); MT.playing = false;
    const btn = document.getElementById('mt-playbtn'); if (btn) btn.textContent = '▶'; }
  // Build sorted list of boundary times
  const bounds = [0];
  let acc = 0;
  for (const c of MT.clips) {
    acc += Math.max(0, (c.out || 0) - (c.in || 0));
    bounds.push(acc);
  }
  const t = MT.globalTime || 0;
  const eps = 0.01;
  let target;
  if (dir < 0) {
    // largest boundary strictly less than t (with small epsilon to avoid getting stuck)
    target = bounds.filter(b => b < t - eps).pop();
    if (target == null) target = 0;
  } else {
    target = bounds.find(b => b > t + eps);
    if (target == null) target = bounds[bounds.length - 1];
  }
  mtSeek(target);
}

// Step the playhead by ±1 frame (assume 30fps).
function mtStepFrame(dir) {
  if (!MT.clips.length) return;
  const v = document.getElementById('mt-preview');
  if (v && !v.paused) { v.pause(); MT.playing = false;
    const btn = document.getElementById('mt-playbtn'); if (btn) btn.textContent = '▶'; }
  const fps = 30;
  const total = MT.clips.reduce((a, c) => a + Math.max(0, (c.out || 0) - (c.in || 0)), 0);
  let t = (MT.globalTime || 0) + (dir > 0 ? 1 : -1) / fps;
  t = Math.max(0, Math.min(total - 0.001, t));
  mtSeek(t);
}

function mtTogglePlay() {
  const v = document.getElementById('mt-preview');
  if (!v) return;
  if (!MT.clips.length) return;
  if (MT.curIdx < 0) mtSeek(0);
  if (v.paused) {
    v.play();
    MT.playing = true;
    document.getElementById('mt-playbtn').textContent = '⏸';
  } else {
    v.pause();
    MT.playing = false;
    document.getElementById('mt-playbtn').textContent = '▶';
  }
}

function mtSeekToStart() { mtSeek(0); }
function mtSeekToEnd() {
  const total = mtTotalDur();
  if (total <= 0) return;
  // Land just-before-end so playhead is visible / video doesn't auto-stop awkwardly
  mtSeek(Math.max(0, total - 0.05));
}
function mtSeekToClip(idx) {
  // Compute global time at start of clip idx
  let t = 0;
  for (let i = 0; i < idx; i++) {
    t += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
  }
  mtSeek(t);
}

function mtAttachPreviewListeners() {
  const v = document.getElementById('mt-preview');
  if (!v || v._mtBound) return;
  v.addEventListener('timeupdate', () => {
    if (MT.curIdx < 0 || MT.scrubbing) return;
    const c = MT.clips[MT.curIdx];
    if (!c) return;
    // If we ran past the trim out → switch to next clip
    if (v.currentTime >= (c.out || 0) - 0.02) {
      const nextIdx = MT.curIdx + 1;
      if (nextIdx < MT.clips.length) {
        // Recompute global time for next clip start
        let t = 0;
        for (let i = 0; i < nextIdx; i++) t += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
        MT.globalTime = t;
        mtLoadClipIntoPlayer(nextIdx, 0);
        if (MT.playing) v.play();
      } else {
        v.pause();
        MT.playing = false;
        document.getElementById('mt-playbtn').textContent = '▶';
      }
      mtUpdateTimeLabel();
      mtUpdatePlayhead();
      return;
    }
    // Translate local video time → global timeline time
    let t = 0;
    for (let i = 0; i < MT.curIdx; i++) t += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
    MT.globalTime = t + Math.max(0, v.currentTime - (c.in || 0));
    mtUpdatePlayhead();
    mtUpdateTimeLabel();
  });
  v._mtBound = true;
}

async function mtTrimToPlayhead(side /* 'left' | 'right' */) {
  if (MT.curIdx < 0 || !MT.clips[MT.curIdx]) { showToast('Поставь playhead на клип'); return; }
  const c = MT.clips[MT.curIdx];
  let pre = 0;
  for (let i = 0; i < MT.curIdx; i++) pre += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
  const at = MT.globalTime - pre;                 // offset inside trimmed clip
  const dur = (c.out || 0) - (c.in || 0);
  if (at < 0.1 || at > dur - 0.1) { showToast('Слишком близко к краю'); return; }
  const splitAbs = (c.in || 0) + at;              // absolute seconds in original media
  const newIn  = side === 'left'  ? splitAbs : (c.in  || 0);
  const newOut = side === 'right' ? splitAbs : (c.out || 0);
  try {
    const r = await fetch(
      `/api/series/${S.seriesId}/timeline/clips/${c.id}/trim`,
      { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ in: newIn, out: newOut }) }
    );
    if (!r.ok) throw new Error(await r.text());
    showToast(side === 'left' ? '✂ обрезано слева' : '✂ обрезано справа');
    await mtRefreshTimeline();
    const total = mtTotalDur();
    if (MT.globalTime > total) MT.globalTime = Math.max(0, total - 0.1);
    mtSeek(MT.globalTime);
  } catch (e) { showToast('✗ trim: ' + (e.message || e)); }
}

async function mtCutAtPlayhead() {
  if (MT.curIdx < 0 || !MT.clips[MT.curIdx]) { showToast('Поставь playhead на клип'); return; }
  const c = MT.clips[MT.curIdx];
  // Local offset (within trimmed clip): globalTime - sum of preceding durations
  let pre = 0;
  for (let i = 0; i < MT.curIdx; i++) pre += Math.max(0, (MT.clips[i].out || 0) - (MT.clips[i].in || 0));
  const at = MT.globalTime - pre;
  const dur = (c.out || 0) - (c.in || 0);
  if (at < 0.1 || at > dur - 0.1) { showToast('Слишком близко к краю'); return; }
  try {
    await api.post(`/api/series/${S.seriesId}/timeline/clips/${c.id}/split`, { at });
    showToast('✂ разрезано');
    await mtRefreshTimeline();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

function mtDragStart(e, id) {
  MT.dragId = id;
  e.dataTransfer.effectAllowed = 'move';
  e.currentTarget.classList.add('dragging');
}
function mtDragOver(e, id) {
  // Allow drop if reordering an existing clip OR dropping a library item
  if (MT.libDragRef) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    e.currentTarget.classList.add('drop-target');
    return;
  }
  if (!MT.dragId || MT.dragId === id) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drop-target');
}
function mtDragLeave(e) { e.currentTarget.classList.remove('drop-target'); }
function mtDragEnd(e) {
  document.querySelectorAll('.mt-clip').forEach(el => {
    el.classList.remove('dragging','drop-target');
  });
  const strip = document.getElementById('mt-timeline');
  if (strip) strip.classList.remove('drop-target');
  MT.dragId = null;
}
async function mtDrop(e, targetId) {
  e.preventDefault();
  e.currentTarget.classList.remove('drop-target');
  // Library drop → insert before target clip
  if (MT.libDragRef) {
    const ref = MT.libDragRef;
    MT.libDragRef = null;
    await mtInsertFromLib(ref.ep, ref.idx, targetId);
    return;
  }
  if (!MT.dragId || MT.dragId === targetId) return;
  const order = MT.clips.map(c => c.id);
  const fromIdx = order.indexOf(MT.dragId);
  const toIdx = order.indexOf(targetId);
  if (fromIdx < 0 || toIdx < 0) return;
  order.splice(toIdx, 0, order.splice(fromIdx, 1)[0]);
  // optimistic reorder
  const byId = Object.fromEntries(MT.clips.map(c => [c.id, c]));
  MT.clips = order.map(id => byId[id]);
  mtRenderTimeline();
  try {
    await api.post(`/api/series/${S.seriesId}/timeline/reorder`, { order });
  } catch (err) {
    showToast('✗ reorder: ' + (err.message || err));
    await mtRefreshTimeline();
  }
}

// ─── Library → timeline drag ──────────────────────────────────────────────
function mtLibDragStart(e, ep, idx) {
  MT.libDragRef = { ep, idx };
  e.dataTransfer.effectAllowed = 'copy';
  try { e.dataTransfer.setData('text/plain', `lib:${ep}:${idx}`); } catch {}
  e.currentTarget.classList.add('dragging');
}
function mtLibDragEnd(e) {
  document.querySelectorAll('.mt-lib-item').forEach(el => el.classList.remove('dragging'));
  document.querySelectorAll('.mt-clip').forEach(el => el.classList.remove('drop-target'));
  const strip = document.getElementById('mt-timeline');
  if (strip) strip.classList.remove('drop-target');
  MT.libDragRef = null;
}
function mtStripDragOver(e) {
  if (!MT.libDragRef) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  document.getElementById('mt-timeline')?.classList.add('drop-target');
}
function mtStripDragLeave(e) {
  document.getElementById('mt-timeline')?.classList.remove('drop-target');
}
async function mtStripDrop(e) {
  if (!MT.libDragRef) return;
  e.preventDefault();
  document.getElementById('mt-timeline')?.classList.remove('drop-target');
  const ref = MT.libDragRef;
  MT.libDragRef = null;
  // If drop landed on a child .mt-clip, the clip's own drop fired already; skip.
  if (e.target.closest && e.target.closest('.mt-clip')) return;
  await mtInsertFromLib(ref.ep, ref.idx, null); // append
}

async function mtInsertFromLib(ep, idx, targetClipId /* null = append */) {
  try {
    const r = await api.post(`/api/series/${S.seriesId}/timeline/clips/add`, {
      episode: ep, chunk_idx: idx,
    });
    const newId = r.clip?.id;
    if (newId && targetClipId) {
      const order = MT.clips.map(c => c.id);
      const tIdx = order.indexOf(targetClipId);
      // newly added clip is appended on the server; build new order:
      const newOrder = order.slice();
      if (tIdx >= 0) newOrder.splice(tIdx, 0, newId);
      else newOrder.push(newId);
      await api.post(`/api/series/${S.seriesId}/timeline/reorder`, { order: newOrder });
    }
    showToast('✓ Добавлено');
    await mtRefreshTimeline();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

async function mtRemoveClip(id) {
  await api.del(`/api/series/${S.seriesId}/timeline/clips/${id}`);
  await mtRefreshTimeline();
}


// Library: aggregate all completed seedance chunks across episodes
async function mtRefreshLibrary() {
  const lib = document.getElementById('mt-library');
  if (!lib) return;
  const filterSel = document.getElementById('mt-lib-filter');
  const countEl = document.getElementById('mt-lib-count');
  lib.innerHTML = '<div class="muted" style="font-size:12px">Загружаю...</div>';
  try {
    const eps = await api.get(`/api/series/${S.seriesId}/episodes`);
    const epList = (eps.episodes || eps || []);
    const allItems = [];
    for (const ep of epList) {
      try {
        const r = await api.get(`/api/series/${S.seriesId}/episodes/${ep.number}/seedance/list`);
        for (const c of (r.chunks || [])) {
          if (c.status === 'completed' && c.video_path) {
            allItems.push({ ep: ep.number, ep_title: ep.title || '', ...c });
          }
        }
      } catch {}
    }
    // Populate filter dropdown (preserve current selection)
    if (filterSel) {
      const prev = filterSel.value || 'all';
      const epsWithClips = [...new Set(allItems.map(c => c.ep))].sort((a,b) => a-b);
      const opts = ['<option value="all">Все эпизоды</option>']
        .concat(epsWithClips.map(n => {
          const t = (epList.find(e => e.number === n)?.title || '').slice(0, 24);
          return `<option value="${n}">Эпизод ${n}${t ? ' · ' + esc(t) : ''}</option>`;
        }));
      filterSel.innerHTML = opts.join('');
      // restore previous filter if still valid
      if ([...filterSel.options].some(o => o.value === prev)) filterSel.value = prev;
    }
    const filterVal = filterSel?.value || 'all';
    const items = filterVal === 'all'
      ? allItems
      : allItems.filter(c => String(c.ep) === String(filterVal));
    if (countEl) countEl.textContent = `${items.length} из ${allItems.length}`;
    if (!items.length) {
      lib.innerHTML = allItems.length
        ? '<div class="muted" style="font-size:12px">В этом эпизоде нет готовых клипов.</div>'
        : '<div class="muted" style="font-size:12px">Нет готовых видео. Сгенерируй что-нибудь в эпизодах через Seedance.</div>';
      return;
    }
    lib.innerHTML = items.map(c => {
      const url = _chunkVideoUrl(c);
      return `
        <div class="mt-lib-item" draggable="true"
             ondragstart="mtLibDragStart(event,${c.ep},${c.idx})"
             ondragend="mtLibDragEnd(event)"
             title="Клик — фуллскрин · перетащи на таймлайн">
          <video data-src="${url}" preload="none" muted onclick="mtLibFullscreen(event,'${url}')" style="cursor:zoom-in"></video>
          <div class="mt-lib-meta">
            <div class="ep">ep${c.ep}·#${c.idx} · ${c.duration}с</div>
            <div class="preview">${esc((c.prompt||'').slice(0,80))}</div>
            <button class="mt-lib-add" onclick="mtAddFromLib(${c.ep},${c.idx},this)">➕ На таймлайн</button>
          </div>
        </div>`;
    }).join('');
    // Hover-scrub: водишь мышью по превью — кадры пролистываются.
    // Lazy-load (preload=none → src on first hover) + throttle seeks to ~30 fps
    // so браузер не захлёбывался range-запросами при большой библиотеке.
    if (!lib._mtHoverBound) {
      let lastMove = 0;
      let pendingFrac = 0;
      let pendingVid  = null;
      const flush = () => {
        if (!pendingVid) return;
        const v = pendingVid;
        const dur = v.duration;
        if (isFinite(dur) && dur > 0) {
          try { v.currentTime = pendingFrac * dur; } catch {}
        }
        pendingVid = null;
      };
      const onMove = (e) => {
        const v = e.target.closest('.mt-lib-item video');
        if (!v) return;
        // Lazy-attach src on first hover so we don't fan out 20+ metadata requests
        if (!v.src && v.dataset.src) {
          v.src = v.dataset.src;
          v.preload = 'metadata';
        }
        const rect = v.getBoundingClientRect();
        const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        pendingFrac = frac;
        pendingVid  = v;
        const now = performance.now();
        if (now - lastMove >= 33) {       // ~30 fps cap
          lastMove = now;
          flush();
        }
      };
      const resetVid = (v) => { try { v.currentTime = 0; } catch {} };
      lib.addEventListener('mousemove', onMove);
      lib.addEventListener('mouseout', (e) => {
        const v = e.target.closest && e.target.closest('.mt-lib-item video');
        if (!v) return;
        const item = v.closest('.mt-lib-item');
        if (!item.contains(e.relatedTarget)) {
          if (pendingVid === v) pendingVid = null;
          resetVid(v);
        }
      });
      lib._mtHoverBound = true;
    }
  } catch (e) {
    lib.innerHTML = '<div class="muted">Ошибка загрузки</div>';
  }
}

function mtLibFullscreen(e, url) {
  // Stop the click from bubbling (e.g. starting a drag-related side-effect)
  e.stopPropagation();
  e.preventDefault();
  // Build a one-shot overlay with a big video player
  const old = document.getElementById('mt-lib-fs');
  if (old) old.remove();
  const overlay = document.createElement('div');
  overlay.id = 'mt-lib-fs';
  overlay.className = 'mt-lib-fs-overlay';
  overlay.innerHTML = `
    <button class="mt-lib-fs-close" title="Закрыть (Esc)">✕</button>
    <video src="${url}" controls autoplay playsinline></video>
  `;
  const close = () => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (ev) => { if (ev.key === 'Escape') close(); };
  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay || ev.target.classList.contains('mt-lib-fs-close')) close();
  });
  document.addEventListener('keydown', onKey);
  document.body.appendChild(overlay);
}

async function mtAddFromLib(ep, idx, btn) {
  if (btn) btn.disabled = true;
  try {
    await api.post(`/api/series/${S.seriesId}/timeline/clips/add`, {
      episode: ep, chunk_idx: idx,
    });
    showToast('✓ Добавлено');
    await mtRefreshTimeline();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
  if (btn) btn.disabled = false;
}

async function mtRender() {
  if (!MT.clips.length) { showToast('Таймлайн пустой'); return; }
  const btn = document.getElementById('mt-render-btn');
  const old = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Рендерю...'; }
  try {
    const res = await api.post(`/api/series/${S.seriesId}/timeline/render`, {});
    showToast(`✓ Готово · ${res.size_mb} МБ · ${res.mode}`);
    await mtRefreshRenders();
    // Trigger immediate browser download of the freshly-rendered file.
    // User asked for this — previously they had to scroll down to the
    // renders list to grab the file. Now a download starts automatically
    // the moment the render completes.
    if (res.url) _mtTriggerDownload(res.url, res.path);
  } catch (e) {
    showToast('✗ ' + (e.message || e), 7000);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old || '▶ Собрать MP4'; }
  }
}

// Triggers a browser download for an asset URL. Uses a hidden <a download>
// because window.open() / location.href would navigate away from the page.
// The `path` arg is the relative path inside the series — its basename
// becomes the suggested filename so the user gets a sensible name in
// their Downloads folder rather than the random "render-xxxxx.mp4" UUID.
function _mtTriggerDownload(url, path) {
  try {
    const a = document.createElement('a');
    a.href = url;
    if (path) {
      const fname = String(path).split('/').pop() || 'render.mp4';
      a.download = fname;
    } else {
      a.download = '';
    }
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 100);
  } catch (e) {
    console.warn('[mtRender] auto-download failed', e);
  }
}

async function mtRefreshRenders() {
  const el = document.getElementById('mt-renders');
  if (!el) return;
  try {
    const res = await api.get(`/api/series/${S.seriesId}/timeline/renders`);
    const items = res.renders || [];
    if (!items.length) { el.innerHTML = '<div class="muted" style="font-size:12px">Пока нет сборок.</div>'; return; }
    el.innerHTML = items.map(r => {
      const d = new Date(r.created_at*1000).toLocaleString();
      return `
        <div class="mt-render-row">
          <video src="${r.url}" controls preload="metadata"></video>
          <div class="grow">
            <div class="name">${r.name}</div>
            <div class="meta">${d} · ${r.size_mb} МБ</div>
          </div>
          <a class="btn-ghost btn-sm" href="${r.url}" download>⬇ Скачать</a>
          <button class="btn-ghost btn-sm" onclick="mtDeleteRender('${r.name}')">🗑</button>
        </div>`;
    }).join('');
  } catch {}
}

async function mtDeleteRender(name) {
  if (!confirm('Удалить эту сборку?')) return;
  await api.del(`/api/series/${S.seriesId}/timeline/renders/${name}`);
  await mtRefreshRenders();
}

