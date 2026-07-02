// ─────────────────────────────────────────────────────────────────────────────
// Crop & Position modal
// ─────────────────────────────────────────────────────────────────────────────
const CROP = {
  clipId: null,
  videoNatW: 0, videoNatH: 0,
  // crop fractions [0..1] of source frame
  fx: 0, fy: 0, fw: 1, fh: 1,
  // locked aspect ratio = source ratio (so output frame keeps same shape)
  ar: 9/16,
};

function cropOpen(clipId) {
  const clip = MT.clips.find(c => c.id === clipId);
  if (!clip || !clip.video_path) { showToast('Нет видео'); return; }
  CROP.clipId = clipId;
  // Initial fractions from saved crop or default to full frame
  if (clip.crop) {
    CROP.fx = clip.crop.x; CROP.fy = clip.crop.y;
    CROP.fw = clip.crop.w; CROP.fh = clip.crop.h;
  } else {
    CROP.fx = 0; CROP.fy = 0; CROP.fw = 1; CROP.fh = 1;
  }
  const v = document.getElementById('crop-source');
  v.src = `${assetUrl(clip.video_path)}#t=${(clip.in||0)+0.1}`;
  v.onloadedmetadata = () => {
    CROP.videoNatW = v.videoWidth;
    CROP.videoNatH = v.videoHeight;
    CROP.ar = CROP.videoNatW / CROP.videoNatH;
    cropRender();
  };
  document.getElementById('crop-info').textContent = `ep${clip.episode}·#${clip.chunk_idx}`;
  document.getElementById('crop-modal').classList.remove('hidden');
  // Attach drag listeners once
  if (!CROP._bound) {
    cropAttachDrag();
    CROP._bound = true;
  }
}

function cropClose() {
  document.getElementById('crop-modal').classList.add('hidden');
  const v = document.getElementById('crop-source');
  v.pause?.(); v.removeAttribute('src'); v.load?.();
  CROP.clipId = null;
}

function cropRender() {
  const v = document.getElementById('crop-source');
  const rect = document.getElementById('crop-rect');
  if (!v || !rect) return;
  const w = v.clientWidth, h = v.clientHeight;
  if (!w || !h) return;
  rect.style.left   = (CROP.fx * w) + 'px';
  rect.style.top    = (CROP.fy * h) + 'px';
  rect.style.width  = (CROP.fw * w) + 'px';
  rect.style.height = (CROP.fh * h) + 'px';
  // Readout
  const px = Math.round(CROP.fw * CROP.videoNatW);
  const py = Math.round(CROP.fh * CROP.videoNatH);
  document.getElementById('crop-readout').innerHTML = `
    Source: ${CROP.videoNatW}×${CROP.videoNatH}<br>
    Crop:   ${px}×${py} px<br>
    Pos:    (${(CROP.fx*100).toFixed(0)}%, ${(CROP.fy*100).toFixed(0)}%)<br>
    Size:   ${(CROP.fw*100).toFixed(0)}% × ${(CROP.fh*100).toFixed(0)}%`;
}

function cropAttachDrag() {
  const stage = document.getElementById('crop-stage');
  const rect = document.getElementById('crop-rect');
  if (!stage || !rect) return;

  // Drag the whole rect (move)
  rect.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('crop-handle')) return;
    e.preventDefault();
    const v = document.getElementById('crop-source');
    const W = v.clientWidth, H = v.clientHeight;
    const startX = e.clientX, startY = e.clientY;
    const sfx = CROP.fx, sfy = CROP.fy;
    const onMove = (ev) => {
      const dx = (ev.clientX - startX) / W;
      const dy = (ev.clientY - startY) / H;
      CROP.fx = Math.max(0, Math.min(1 - CROP.fw, sfx + dx));
      CROP.fy = Math.max(0, Math.min(1 - CROP.fh, sfy + dy));
      cropRender();
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // Resize via corner handles (preserves source aspect)
  rect.querySelectorAll('.crop-handle').forEach(handle => {
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      const corner = handle.classList.contains('nw') ? 'nw'
                   : handle.classList.contains('ne') ? 'ne'
                   : handle.classList.contains('sw') ? 'sw' : 'se';
      const v = document.getElementById('crop-source');
      const W = v.clientWidth, H = v.clientHeight;
      const startX = e.clientX, startY = e.clientY;
      const s = { fx: CROP.fx, fy: CROP.fy, fw: CROP.fw, fh: CROP.fh };
      const ar = CROP.ar; // preserve source AR

      const onMove = (ev) => {
        const dxFrac = (ev.clientX - startX) / W;
        const dyFrac = (ev.clientY - startY) / H;
        // For corner-relative resizing, we change fw based on dx, then fh = fw / ar (in fractions)
        // ar is W/H of source; same applies to fractions because both sides scale equally.
        let newFw = s.fw, newFh = s.fh, newFx = s.fx, newFy = s.fy;
        if (corner === 'se') {
          newFw = Math.max(0.05, Math.min(1 - s.fx, s.fw + dxFrac));
          newFh = newFw * (CROP.videoNatH / CROP.videoNatW) * (W / H * (CROP.videoNatW / CROP.videoNatH));
          // Simpler: keep crop AR == source AR (so final stays uniform).
          // Both natural and on-screen use same AR, so newFh = newFw  in source-space
          // = newFw * (Wnat/Hnat) / (Wnat/Hnat) = newFw. But we operate in *fractions* of W and H,
          // and W/H aspect on stage already matches source AR.
          newFh = Math.max(0.05, Math.min(1 - s.fy, newFw));
        } else if (corner === 'sw') {
          let nW = Math.max(0.05, Math.min(s.fx + s.fw, s.fw - dxFrac));
          newFx = s.fx + (s.fw - nW);
          newFw = nW;
          newFh = Math.max(0.05, Math.min(1 - s.fy, newFw));
        } else if (corner === 'ne') {
          newFw = Math.max(0.05, Math.min(1 - s.fx, s.fw + dxFrac));
          let nH = Math.max(0.05, Math.min(s.fy + s.fh, newFw));
          newFy = s.fy + (s.fh - nH);
          newFh = nH;
        } else { // nw
          let nW = Math.max(0.05, Math.min(s.fx + s.fw, s.fw - dxFrac));
          let nH = Math.max(0.05, Math.min(s.fy + s.fh, nW));
          newFx = s.fx + (s.fw - nW);
          newFy = s.fy + (s.fh - nH);
          newFw = nW; newFh = nH;
        }
        // Final clamp
        newFw = Math.min(newFw, 1 - newFx);
        newFh = Math.min(newFh, 1 - newFy);
        CROP.fx = newFx; CROP.fy = newFy; CROP.fw = newFw; CROP.fh = newFh;
        cropRender();
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });

  // Re-render on window resize so on-screen rect stays in sync with video size
  window.addEventListener('resize', cropRender);
}

function cropReset() {
  CROP.fx = 0; CROP.fy = 0; CROP.fw = 1; CROP.fh = 1;
  cropRender();
}

async function cropClear() {
  if (!CROP.clipId) return;
  try {
    await fetch(`/api/series/${S.seriesId}/timeline/clips/${CROP.clipId}/crop`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true }),
    }).then(r => { if (!r.ok) throw new Error('PATCH failed'); });
    showToast('Crop убран');
    cropClose();
    await mtRefreshTimeline();
    mtApplyPreviewCrop();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

async function cropSave() {
  if (!CROP.clipId) return;
  // No-op if it's effectively the full frame
  if (CROP.fw > 0.999 && CROP.fh > 0.999 && CROP.fx < 0.001 && CROP.fy < 0.001) {
    return cropClear();
  }
  try {
    const r = await fetch(`/api/series/${S.seriesId}/timeline/clips/${CROP.clipId}/crop`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ x: CROP.fx, y: CROP.fy, w: CROP.fw, h: CROP.fh }),
    });
    if (!r.ok) throw new Error(await r.text());
    showToast('✓ Crop сохранён');
    cropClose();
    await mtRefreshTimeline();
    mtApplyPreviewCrop();
  } catch (e) { showToast('✗ ' + (e.message || e)); }
}

