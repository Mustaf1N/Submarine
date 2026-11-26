"""
analyze.py — покадровый анализ исходников (шаг 0.2 с) → JSON.

Что измеряем (как требует бриф, п.7.1):
  * luma (яркость) и её дисперсия — ищем «почти чёрную ночь» и вспышки;
  * motion (средняя разница соседних кадров) — ищем замирания картинки;
  * gui_score — доля «светлых десатурированных» пикселей в центральной зоне:
    инвентарь/сундуки/меню в Minecraft — это серые панели #C6C6C6, они дают
    характерный пик. Нужен, чтобы НЕ резать статичные, но содержательные кадры;
  * chat_score — светлые пиксели в нижне-левой зоне (чат);
  * freeze-интервалы (картинка стоит > 0.8 с) и их классификация:
    content (GUI открыт → не трогаем) / lag (пустая сцена → вырезаем);
  * black-интервалы (почти чёрный экран).

Запуск:
    python3 edit/analyze.py            # оба источника
    python3 edit/analyze.py --src A
Результат: edit/analysis/analysis.json (+ короткий отчёт в stderr).
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np

from cfglib import ANALYSIS, FFMPEG, SRC_A, SRC_B, log, probe_brief, save_json

STEP = 0.2            # шаг анализа, с
SMALL_W = 480
SMALL_H = 270
FREEZE_MIN = 0.8      # замирание дольше 0.8 с считаем браком (если не GUI)
MOTION_EPS = 0.0012   # ниже — картинка «стоит»
BLACK_LUMA = 16.0
# порог «открыт GUI»: серые панели Minecraft дают ~0.5–0.95 доли светлых пикселей
# в центре; обычная сцена — меньше 0.2. Ставим с запасом, чтобы статичный
# инвентарь НЕ считался лагом (главная претензия к прошлой версии монтажа).
GUI_T = 0.25
CHAT_T = 0.25


# ─────────────────────────────── извлечение ───────────────────────────────
def extract_small(path: Path, fps: float = 1 / STEP) -> np.ndarray:
    """Мелкая копия всего источника: (N, H, W, 3) uint8."""
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
        "-vf", f"fps={fps},scale={SMALL_W}:{SMALL_H}:flags=area",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg не смог прочитать {path}:\n{proc.stderr.decode()[-1500:]}")
    buf = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(buf) // (SMALL_W * SMALL_H * 3)
    return buf[: n * SMALL_W * SMALL_H * 3].reshape(n, SMALL_H, SMALL_W, 3)


def frame_metrics(frames: np.ndarray) -> dict:
    f = frames.astype(np.float32)
    luma = 0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]      # (N,H,W)
    avg_luma = luma.reshape(len(f), -1).mean(axis=1)
    std_luma = luma.reshape(len(f), -1).std(axis=1)

    # motion: средняя абс. разница с предыдущим кадром в мелком разрешении
    d = np.abs(np.diff(luma, axis=0)).reshape(max(1, len(f) - 1), -1).mean(axis=1)
    motion = np.concatenate([[0.0], d]) / 255.0

    mx = f.max(axis=3)
    mn = f.min(axis=3)
    sat = (mx - mn) / np.maximum(1.0, mx)
    light = (mn > 118) & (sat < 0.16)          # светлые серые панели GUI

    h, w = SMALL_H, SMALL_W
    # light: (N, H, W) → берём центральную зону и нижне-левую (чат) по каждому кадру
    center = light[:, int(h * 0.10):int(h * 0.80), int(w * 0.34):int(w * 0.66)]
    chat = light[:, int(h * 0.55):int(h * 0.95), int(w * 0.01):int(w * 0.46)]
    gui_score = center.reshape(center.shape[0], -1).mean(axis=1)
    chat_score = chat.reshape(chat.shape[0], -1).mean(axis=1)
    return {
        "luma": avg_luma,
        "luma_std": std_luma,
        "motion": motion,
        "gui": gui_score,
        "chat": chat_score,
    }


# ─────────────────────────────── интервалы ────────────────────────────────
def intervals_from_mask(mask: np.ndarray, step: float, min_len: float = 0.0) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if (i - start) * step >= min_len:
                out.append((round(start * step, 2), round(i * step, 2)))
            start = None
    if start is not None and (len(mask) - start) * step >= min_len:
        out.append((round(start * step, 2), round(len(mask) * step, 2)))
    return out


def classify_freezes(metrics: dict, n: int) -> list[dict]:
    motion = metrics["motion"]
    gui = metrics["gui"]
    # «стоит картинка» = motion почти 0; GUI считаем открытым, если серых панелей много
    still = (motion < MOTION_EPS)
    gui_open = gui > GUI_T
    freezes = []
    for (a, b) in intervals_from_mask(still, STEP, min_len=FREEZE_MIN):
        i0, i1 = int(a / STEP), max(int(b / STEP), int(a / STEP) + 1)
        seg_gui = float(np.mean(gui[i0:i1]))
        where = "content" if seg_gui > GUI_T else "lag"
        freezes.append(
            {
                "start": a,
                "end": b,
                "len": round(b - a, 2),
                "gui": round(seg_gui, 4),
                "kind": where,
            }
        )
    # «почти чёрный» — отдельно (ночь в конце записи)
    blacks = intervals_from_mask(metrics["luma"] < BLACK_LUMA, STEP, min_len=1.0)
    return freezes, blacks, gui_open


def analyze_source(path: Path, key: str) -> dict:
    log(f"[analyze] {key}: {path.name} — читаю кадры…")
    brief = probe_brief(path)
    frames = extract_small(path)
    n = len(frames)
    log(f"[analyze] {key}: {n} кадров с шагом {STEP}s ({n * STEP:.1f}s)")

    m = frame_metrics(frames)
    freezes, blacks, gui_open = classify_freezes(m, n)

    # GUI-интервалы (инвентарь/сундуки открыты) — важны для правил резки
    gui_intervals = intervals_from_mask(m["gui"] > GUI_T, STEP, min_len=0.6)
    chat_intervals = intervals_from_mask(m["chat"] > CHAT_T, STEP, min_len=0.6)

    timeline = [
        {
            "t": round(i * STEP, 2),
            "luma": round(float(m["luma"][i]), 1),
            "luma_std": round(float(m["luma_std"][i]), 1),
            "motion": round(float(m["motion"][i]), 5),
            "gui": round(float(m["gui"][i]), 4),
            "chat": round(float(m["chat"][i]), 4),
        }
        for i in range(0, n, 5)          # в JSON кладём каждый 5-й отсчёт (1 c), полные данные — в npz
    ]
    np.savez_compressed(
        ANALYSIS / f"{key}_metrics.npz",
        luma=m["luma"], luma_std=m["luma_std"], motion=m["motion"], gui=m["gui"], chat=m["chat"],
    )

    lag_total = sum(f["len"] for f in freezes if f["kind"] == "lag")
    content_total = sum(f["len"] for f in freezes if f["kind"] == "content")
    log(
        f"[analyze] {key}: замираний {len(freezes)} (lag {lag_total:.1f}s, content {content_total:.1f}s), "
        f"чёрных {len(blacks)}, GUI {len(gui_intervals)}"
    )
    return {
        "file": str(path),
        "brief": brief,
        "step": STEP,
        "frames": n,
        "freezes": freezes,
        "lag_total": round(lag_total, 2),
        "content_static_total": round(content_total, 2),
        "blacks": [{"start": a, "end": b} for a, b in blacks],
        "gui_intervals": [{"start": a, "end": b} for a, b in gui_intervals],
        "chat_intervals": [{"start": a, "end": b} for a, b in chat_intervals],
        "timeline": timeline,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", choices=["A", "B", "both"], default="both")
    ap.add_argument("--a", default=str(SRC_A))
    ap.add_argument("--b", default=str(SRC_B))
    ap.add_argument("--out", default=str(ANALYSIS / "analysis.json"))
    args = ap.parse_args()

    out = {}
    if args.src in ("A", "both"):
        out["A"] = analyze_source(Path(args.a), "A")
    if args.src in ("B", "both"):
        out["B"] = analyze_source(Path(args.b), "B")
    save_json(Path(args.out), out)
    log(f"[analyze] готово → {args.out}")


if __name__ == "__main__":
    main()
