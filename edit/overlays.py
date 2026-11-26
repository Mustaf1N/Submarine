"""
overlays.py — рендер графики в видео-слои с альфой (ffv1/bgra), без ручной работы.

Каждый оверлей из timeline.json превращается в последовательность кадров 1920x1080:
  * кадры вне окна [local, local+dur] — полностью прозрачные;
  * анимация считается в gfx.py (пружины, появление по буквам, виньетки);
  * последовательность отдаётся в ffmpeg через stdin (rawvideo rgba) и кодируется
    в lossless ffv1 — места занимает мало, альфа сохраняется без потерь.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from cfglib import FPS, FFMPEG, H, OVL, W, log, sha
from gfx import build as build_overlay

VERSION = "ovl-3"


def overlay_path(key: str) -> Path:
    return OVL / f"{key}.mkv"


def prepare_overlay(spec: dict, seg_frames: int, force: bool = False) -> Path | None:
    """Рендер одного оверлея в .mkv (RGBA, 60 fps). Возвращает путь или None."""
    key = sha({"spec": spec, "frames": seg_frames, "v": VERSION})
    out = overlay_path(key)
    if out.exists() and not force:
        return out

    ov = build_overlay(spec)
    start = int(round(float(spec.get("local", 0.0)) * FPS))
    dur_frames = int(round(ov.dur * FPS))
    start = max(0, min(start, seg_frames - 1))
    end = min(seg_frames, start + dur_frames)
    if end <= start:
        return None

    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-framerate", str(FPS),
        "-i", "-",
        "-an", "-c:v", "ffv1", "-level", "3", "-pix_fmt", "bgra", "-g", "1",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        blank = np.zeros((H, W, 4), dtype=np.uint8)
        for _ in range(0):          # ничего до start
            pass
        for i in range(seg_frames):
            if start <= i < end:
                frame = ov.frame((i - start) / FPS)
                arr = np.asarray(frame.convert("RGBA"), dtype=np.uint8)
                if arr.shape != (H, W, 4):
                    padded = np.zeros((H, W, 4), dtype=np.uint8)
                    padded[: arr.shape[0], : arr.shape[1]] = arr
                    arr = padded
                proc.stdin.write(arr.tobytes())
            else:
                proc.stdin.write(blank.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode()[-1200:]
        if proc.wait() != 0:
            raise RuntimeError(f"оверлей {spec.get('type')} не отрендерился:\n{err}")
    except BrokenPipeError:
        raise RuntimeError(f"ffmpeg закрыл пайп на оверлее {spec.get('type')}:\n{proc.stderr.read().decode()[-1200:]}")
    log(f"[overlays] {spec.get('type'):<9} {start/FPS:6.2f}s → {end/FPS:6.2f}s  {key}")
    return out
