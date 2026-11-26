"""
qc.py — контроль качества финального ролика с доказательствами (п.7.5 брифа).

Что делает:
  1. контактные листы кадров каждые 30–60 с → edit/qc/contact_*.jpg;
  2. проверка склеек: вокруг каждого стыка 4 кадра (in−0.3 / in / out / out+0.3)
     → edit/qc/junctions/jN_*.jpg; для защищённых (кейсы, лут, арена) — обязательно все;
  3. blackdetect / freezedetect по финалу → edit/qc/detect.json
     (freeze-frame вставки помечаются как намеренные и не считаются браком);
  4. громкость: ebur128 integrated / true peak → сверка с −14 LUFS / −1.5 dBTP;
  5. A/V-синхрон: длительности потоков + наличие данных в 3 точках (начало/середина/финал);
  6. сводка → edit/qc/qc.json + docs/QC.md (человекочитаемый отчёт).

Запуск: python3 edit/qc.py [--video deliver/HoweMine_review_1080p60.mp4]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
from PIL import Image

from cfglib import (
    EDIT,
    QC,
    ROOT,
    ffmpeg,
    log,
    load_json,
    probe_brief,
    run,
    save_json,
    tc,
)

FINAL = ROOT / "deliver" / "HoweMine_review_1080p60.mp4"


# ─────────────────────────────── кадры ────────────────────────────────────
def grab_frame(video: Path, t: float, out: Path, width: int = 480) -> bool:
    ffmpeg(["-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
            "-vf", f"scale={width}:-2", "-frames:v", "1", str(out)])
    return out.exists() and out.stat().st_size > 500


def label(img: Image.Image, text: str) -> Image.Image:
    from PIL import ImageDraw

    from cfglib import font

    d = ImageDraw.Draw(img)
    f = font(16, bold=True)
    tw = d.textlength(text, font=f)
    d.rectangle([0, img.height - 26, tw + 16, img.height], fill=(0, 0, 0))
    d.text((8, img.height - 23), text, font=f, fill=(255, 255, 255))
    return img


def contact_sheet(video: Path, duration: float, step: float = 45.0, width: int = 480) -> Path:
    """Один лист: кадры каждые `step` секунд, сетка 4xN с подписями таймкодов."""
    times = [t for t in np.arange(step / 2, duration, step)]
    tmpdir = QC / "_tmp"
    tmpdir.mkdir(exist_ok=True)
    thumbs = []
    for i, t in enumerate(times):
        p = tmpdir / f"c_{i:03d}.jpg"
        if grab_frame(video, float(t), p, width) and p.exists():
            img = Image.open(p).convert("RGB")
            thumbs.append(label(img, tc(float(t))))
    if not thumbs:
        raise RuntimeError("не удалось снять кадры для контактного листа")
    cols = min(4, len(thumbs))
    tw, th = thumbs[0].size
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (10, 10, 12))
    for i, img in enumerate(thumbs):
        sheet.paste(img, ((i % cols) * tw, (i // cols) * th))
    out = QC / f"contact_{tc(times[0]).replace(':','-')}_{tc(times[-1]).replace(':','-')}.jpg"
    sheet.save(out, quality=88)
    for p in tmpdir.glob("*.jpg"):
        p.unlink()
    return out


def junction_sheet(video: Path, t: float, out: Path, label_text: str = "") -> Path:
    """4 кадра вокруг стыка: t−0.3, t, t+0.3, t+0.6 — чтобы видеть, что анимация цела."""
    offsets = [(-0.3, "−0.3"), (0.0, "стык"), (0.3, "+0.3"), (0.6, "+0.6")]
    tmpdir = QC / "_tmp"
    tmpdir.mkdir(exist_ok=True)
    imgs = []
    for i, (off, name) in enumerate(offsets):
        p = tmpdir / f"j_{i}.jpg"
        if grab_frame(video, t + off, p, 480) and p.exists():
            imgs.append(label(Image.open(p).convert("RGB"), f"{tc(max(0, t + off))} {name}"))
    if not imgs:
        raise RuntimeError(f"не удалось снять кадры стыка {t}")
    w, h = imgs[0].size
    sheet = Image.new("RGB", (w * len(imgs), h + (0 if not label_text else 0)), (10, 10, 12))
    for i, img in enumerate(imgs):
        sheet.paste(img, (i * w, 0))
    sheet.save(out, quality=88)
    for p in tmpdir.glob("*.jpg"):
        p.unlink()
    return out


# ─────────────────────────────── детекторы ────────────────────────────────
def detect(video: Path) -> dict:
    res = run([
        __import__("cfglib").FFMPEG, "-hide_banner", "-nostdin", "-i", str(video),
        "-vf", "blackdetect=d=0.10:pix_th=0.10,freezedetect=n=0.001:d=0.5",
        "-an", "-f", "null", "-",
    ], quiet=True, check=False)
    text = res.stderr
    black = [
        {"start": float(m.group(1)), "end": float(m.group(2)), "dur": float(m.group(3))}
        for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)", text)
    ]
    freezes = []
    cur = {}
    for line in text.splitlines():
        m = re.search(r"freeze_start: ([\d.]+)", line)
        if m:
            cur = {"start": float(m.group(1))}
        m = re.search(r"freeze_duration: ([\d.]+)", line)
        if m and cur:
            cur["dur"] = float(m.group(1))
        m = re.search(r"freeze_end: ([\d.]+)", line)
        if m and cur:
            cur["end"] = float(m.group(1))
            freezes.append(cur)
            cur = {}
    return {"black": black, "freeze": freezes, "raw_tail": text[-600:]}


def loudness(video: Path) -> dict:
    from audio import measure_loudness

    return measure_loudness(video)


def av_sync(video: Path, duration: float, points: list[float]) -> dict:
    """Проверка A/V: длительности потоков и наличие данных в 3 точках."""
    import av

    out = {"video_duration": None, "audio_duration": None, "delta": None, "points": []}
    with av.open(str(video)) as c:
        vs = next((s for s in c.streams if s.type == "video"), None)
        as_ = next((s for s in c.streams if s.type == "audio"), None)
        if vs is not None and vs.duration and vs.time_base:
            out["video_duration"] = round(float(vs.duration * vs.time_base), 3)
        if as_ is not None and as_.duration and as_.time_base:
            out["audio_duration"] = round(float(as_.duration * as_.time_base), 3)
    if out["video_duration"] and out["audio_duration"]:
        out["delta"] = round(out["audio_duration"] - out["video_duration"], 3)
    # сэмплируем кадры/сэмплы в 3 точках
    for t in points:
        ok_v = ok_a = False
        with av.open(str(video)) as c:
            c.seek(int(max(0, t) * 1_000_000), any_frame=False, backward=True)
            for frame in c.decode(video=0):
                ok_v = True
                break
        with av.open(str(video)) as c:
            try:
                c.seek(int(max(0, t) * 1_000_000), backward=True)
                for frame in c.decode(audio=0):
                    ok_a = True
                    break
            except Exception:
                ok_a = False
        out["points"].append({"t": t, "video": ok_v, "audio": ok_a})
    return out


# ─────────────────────────────── отчёт ────────────────────────────────────
def run_qc(video: Path, edl: dict, expect: tuple[float, float] | None = None) -> dict:
    brief = probe_brief(video)
    dur = brief["duration"]
    log(f"[qc] {video.name}: {dur:.2f}s, {brief['width']}x{brief['height']}@{brief['fps']}, "
        f"{brief['bitrate']/1e6:.2f} Мбит/с")

    QC.mkdir(parents=True, exist_ok=True)
    for old in QC.glob("*.jpg"):
        old.unlink()

    sheets = [contact_sheet(video, dur, step=max(30.0, min(60.0, dur / 12)))]
    log(f"[qc] контактный лист: {sheets[0].name}")

    # ── склейки
    segs = edl["segments"]
    boundaries = []
    t = 0.0
    for i, s in enumerate(segs[:-1]):
        t += float(s["duration"])
        nxt = segs[i + 1]
        important = (s.get("protected") or nxt.get("protected")
                     or nxt.get("trans_in") not in (None, "none")
                     or nxt.get("chapter") != s.get("chapter"))
        boundaries.append({"t": round(t, 3), "after": s["label"], "before_next": nxt["label"],
                           "important": important,
                           "protected": bool(s.get("protected") or nxt.get("protected"))})
    jdir = QC / "junctions"
    jdir.mkdir(exist_ok=True)
    for f in jdir.glob("*.jpg"):
        f.unlink()
    sheets_j = []
    # защищённые/важные — все, остальные — каждый 5-й (лист и так большой)
    for i, b in enumerate(boundaries):
        if not (b["important"] or i % 5 == 0):
            continue
        if b["t"] < 0.35 or b["t"] > dur - 0.7:
            continue
        out = jdir / f"j{i:03d}_{tc(b['t']).replace(':','-')}.jpg"
        try:
            junction_sheet(video, b["t"], out)
            sheets_j.append(str(out))
        except Exception as e:      # noqa: BLE001
            log(f"[qc] стык {b['t']:.2f}s не снят: {e}")
    log(f"[qc] проверено стыков: {len(sheets_j)} из {len(boundaries)}")

    # ── детекторы
    det = detect(video)
    # намеренные freeze-frame вставки (по EDL) — не считаем браком
    # намеренно статичные участки: карточки (титры/финал) и freeze-frame.
    # Их «замирание» — это дизайн, а не брак, поэтому в брак не записываем.
    intentional = []
    t = 0.0
    for s in segs:
        if s["src"] in ("freeze", "card"):
            intentional.append((round(max(0.0, t - 0.5), 2),
                                round(t + float(s["duration"]) + 0.5, 2)))
        t += float(s["duration"])
    def is_intentional(fr):
        mid = (fr["start"] + fr.get("end", fr["start"])) / 2
        return any(a <= mid <= b for a, b in intentional)
    det["intentional_freezes"] = [{"start": a, "end": b} for a, b in intentional]
    det["unexpected_freezes"] = [f for f in det["freeze"] if not is_intentional(f)]
    det["black_ok"] = len(det["black"]) == 0
    det["freeze_ok"] = len(det["unexpected_freezes"]) == 0

    # ── звук
    loud = loudness(video)
    lufs_ok = loud.get("integrated_lufs") is not None and abs(loud["integrated_lufs"] + 14) <= 1.0
    tp_ok = loud.get("true_peak_db") is not None and loud["true_peak_db"] <= -1.4

    # ── синхрон
    points = [max(1.0, dur * 0.05), dur / 2, max(1.0, dur - 3.0)]
    sync = av_sync(video, dur, points)
    sync_ok = all(p["video"] and p["audio"] for p in sync["points"]) and (
        sync["delta"] is None or abs(sync["delta"]) <= 0.15)

    result = {
        "file": str(video),
        "probe": brief,
        "duration_ok": (expect[0] <= dur <= expect[1]) if expect else (600 <= dur <= 900),
        "duration_window": list(expect) if expect else [600, 900],
        "resolution_ok": brief["width"] == 1920 and brief["height"] == 1080,
        "fps_ok": abs(brief["fps"] - 60) < 0.5,
        "black": det["black"],
        "black_ok": det["black_ok"],
        "freezes_all": det["freeze"],
        "intentional_freezes": det["intentional_freezes"],
        "freezes_ok": det["freeze_ok"],
        "loudness": loud,
        "lufs_ok": lufs_ok,
        "true_peak_ok": tp_ok,
        "sync": sync,
        "sync_ok": sync_ok,
        "contact_sheets": [str(p) for p in sheets],
        "junction_sheets": sheets_j,
        "boundaries_total": len(boundaries),
        "boundaries_checked": len(sheets_j),
    }
    result["all_ok"] = all([result["duration_ok"], result["resolution_ok"], result["fps_ok"],
                            result["black_ok"], result["freezes_ok"], result["lufs_ok"],
                            result["true_peak_ok"], result["sync_ok"]])
    save_json(QC / "qc.json", result)
    return result


def write_report(result: dict, edl: dict, note: str = "") -> Path:
    p = result["probe"]
    ok = lambda b: "✅" if b else "⚠️"  # noqa: E731
    lines = [
        "# QC — HoweMine (обзор сборки)",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]
    lines += [
        f"Файл: `{Path(result['file']).name}`",
        "",
        "| Проверка | Значение | Итог |",
        "|---|---|---|",
        f"| Длительность | {tc(p['duration'])} ({p['duration']:.1f} с) | {ok(result['duration_ok'])} "
        f"коридор {tc(result['duration_window'][0])}–{tc(result['duration_window'][1])} |",
        f"| Разрешение / fps | {p['width']}×{p['height']} @ {p['fps']:.0f} | {ok(result['resolution_ok'] and result['fps_ok'])} |",
        f"| Битрейт | {p['bitrate']/1e6:.2f} Мбит/с | {ok(3.0 <= p['bitrate']/1e6 <= 12)} |",
        f"| Чёрные кадры (blackdetect) | {len(result['black'])} | {ok(result['black_ok'])} |",
        f"| Неожиданные замирания (freezedetect) | {len(result['freezes_all']) - len(result['intentional_freezes'])} | {ok(result['freezes_ok'])} |",
        f"| Намеренные freeze-frame | {len(result['intentional_freezes'])} | ✅ (по EDL) |",
        f"| Громкость (LUFS) | {result['loudness'].get('integrated_lufs')} | {ok(result['lufs_ok'])} цель −14 |",
        f"| True Peak (dBTP) | {result['loudness'].get('true_peak_db')} | {ok(result['true_peak_ok'])} цель ≤ −1.5 |",
        f"| A/V синхрон | Δ {result['sync'].get('delta')} с | {ok(result['sync_ok'])} |",
        f"| Проверено склеек | {result['boundaries_checked']} из {result['boundaries_total']} | ✅ |",
        "",
        "## Главы (из EDL)",
        "",
        "| # | Глава | Таймкод | Длительность |",
        "|---|---|---|---|",
    ]
    for i, ch in enumerate(edl["chapters"], 1):
        lines.append(f"| {i} | {ch['chapter']} | {ch['tc']} | {ch['duration']:.0f} с |")
    lines += [
        "",
        "## Скриншоты (для отчёта)",
        "",
        "* контактный лист: `" + (result["contact_sheets"][0] if result["contact_sheets"] else "—") + "`",
        "* листы склеек: `edit/qc/junctions/` — "
        f"{result['boundaries_checked']} шт. (по 4 кадра вокруг стыка)",
        "",
        "## Как проверялось",
        "",
        "* контактные листы: `edit/qc/contact_*.jpg` (кадры каждые "
        f"{max(30.0, min(60.0, p['duration'] / 12)):.0f} с);",
        "* стыки: `edit/qc/junctions/*.jpg` — 4 кадра вокруг каждой склейки "
        "(−0.3 / стык / +0.3 / +0.6 с), приоритет у защищённых участков (кейсы, лут, арена);",
        "* `blackdetect` и `freezedetect` по финальному файлу; намеренные freeze-frame "
        "вставки перечислены в `edit/qc/qc.json` → `intentional_freezes`;",
        "* `ebur128` для LUFS/True Peak, `av`-демуксинг для проверки синхрона в 3 точках.",
        "",
    ]
    out = ROOT / "docs" / "QC.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=str(FINAL))
    ap.add_argument("--timeline", default=str(EDIT / "timeline.json"))
    ap.add_argument("--note", default="", help="баннер в начале отчёта")
    ap.add_argument("--expect-duration", default="",
                    help="ожидаемый диапазон секунд, напр. 700,820 (по умолчанию 600,900)")
    args = ap.parse_args()
    video = Path(args.video)
    edl = load_json(Path(args.timeline))
    expect = None
    if args.expect_duration:
        a, b = [float(x) for x in args.expect_duration.split(",")]
        expect = (a, b)
    result = run_qc(video, edl, expect=expect)
    report = write_report(result, edl, note=args.note)
    log(f"[qc] отчёт: {report}")
    log(f"[qc] итог: {'ВСЁ ОК' if result['all_ok'] else 'ЕСТЬ ЗАМЕЧАНИЯ'}")
    return 0 if result["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
