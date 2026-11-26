"""
cfglib.py — общая база пайплайна HoweMine review.

Здесь: константы ролика, поиск ffmpeg/ffprobe, обёртки над ffmpeg-вызовами,
пути проекта, шрифты, кэш-ключи и хелперы Pillow для графики.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────── константы ────────────────────────────────
W, H = 1920, 1080
FPS = 60
SR = 48000                      # аудио 48 кГц
LUFS_TARGET = -14.0
TP_TARGET = -1.5

BRAND_RED = (0xFF, 0x7A, 0x7A)   # #FF7A7A
WHITE = (0xFF, 0xFF, 0xFF)

RARITY = {                       # цвета редкости для плашек
    "common":    (0x9E, 0x9E, 0x9E),
    "uncommon":  (0x5F, 0xD0, 0x6A),
    "rare":      (0x4F, 0xA3, 0xFF),
    "epic":      (0xB2, 0x6B, 0xFF),
    "legendary": (0xFF, 0xB3, 0x00),
    "sapphire":  (0x4F, 0xC3, 0xF7),
    "mythic":    (0xFF, 0x5C, 0x8A),
    "gold":      (0xFF, 0xC8, 0x3C),
}

ROOT = Path(__file__).resolve().parent.parent
EDIT = ROOT / "edit"
WORK = ROOT / "work"
CACHE = WORK / "cache"
OVL = WORK / "ovl"
AUD = WORK / "audio"
REPORT = WORK / "report"
QC = EDIT / "qc"
ANALYSIS = EDIT / "analysis"
RAW = Path(os.environ.get("HOWEMINE_RAW", ROOT / "raw"))

def _autodetect(pattern: str, fallback: str) -> Path:
    """Если канонического имени нет — ищем файл по маске (удобно с «сырыми» именами)."""
    if (RAW / fallback).exists():
        return RAW / fallback
    for cand in RAW.glob(pattern):
        if cand.suffix.lower() in (".mp4", ".mkv", ".mov"):
            return cand
    return RAW / fallback


# Имена исходников (см. raw/README.md): канонические A.mp4/B.mp4 либо
# «как из OBS» — Minecraft 2026.11.23*.mp4 / Minecraft 2026.11.26*.mp4
SRC_A = RAW / os.environ["HOWEMINE_SRC_A"] if os.environ.get("HOWEMINE_SRC_A") \
    else _autodetect("Minecraft 2026.11.23*.mp4", "A.mp4")
SRC_B = RAW / os.environ["HOWEMINE_SRC_B"] if os.environ.get("HOWEMINE_SRC_B") \
    else _autodetect("Minecraft 2026.11.26*.mp4", "B.mp4")

for d in (WORK, CACHE, OVL, AUD, REPORT, QC, ANALYSIS):
    d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────── ffmpeg ───────────────────────────────────
def _find(binary: str) -> str:
    env = os.environ.get(binary.upper())
    if env and Path(env).exists():
        return env
    p = shutil.which(binary)
    if p:
        return p
    try:  # запасной вариант: бинарник из imageio-ffmpeg
        import imageio_ffmpeg

        cand = imageio_ffmpeg.get_ffmpeg_exe()
        if binary == "ffmpeg" and Path(cand).exists():
            return cand
        if binary == "ffprobe":
            sib = Path(cand).with_name(Path(cand).name.replace("ffmpeg", "ffprobe"))
            if sib.exists():
                return str(sib)
    except Exception:
        pass
    raise SystemExit(
        f"[cfglib] не найден {binary}. Установите ffmpeg или задайте переменную "
        f"{binary.upper()}=/путь/к/{binary}"
    )


FFMPEG = _find("ffmpeg")
try:
    FFPROBE = _find("ffprobe")
except SystemExit:
    FFPROBE = None            # ffprobe не обязателен: измеряем через PyAV


def _probe_pyav(path: Path) -> dict:
    """Метаданные через PyAV в формате, совместимом с ffprobe -show_streams."""
    import av

    with av.open(str(path)) as c:
        streams = []
        for st in c.streams:
            base = {"codec_type": st.type, "codec_name": st.codec_context.name}
            if st.type == "video":
                base.update({
                    "width": st.codec_context.width,
                    "height": st.codec_context.height,
                    "r_frame_rate": f"{st.average_rate.numerator}/{st.average_rate.denominator}"
                    if st.average_rate else "60/1",
                    "avg_frame_rate": f"{st.average_rate.numerator}/{st.average_rate.denominator}"
                    if st.average_rate else "60/1",
                })
            if st.type == "audio":
                base.update({
                    "channels": st.codec_context.channels or 2,
                    "sample_rate": str(st.codec_context.sample_rate or 48000),
                })
            streams.append(base)
        fmt = {
            "duration": str(c.duration / 1_000_000) if c.duration else "0",
            "size": str(Path(path).stat().st_size),
            "bit_rate": str(c.bit_rate or 0),
        }
        return {"streams": streams, "format": fmt}


def run(cmd: list[str], quiet: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Запуск процесса с понятной диагностикой при падении."""
    if isinstance(cmd, (list, tuple)):
        printable = " ".join(str(c) for c in cmd)
    else:
        printable = str(cmd)
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and res.returncode != 0:
        tail = "\n".join((res.stderr or "").strip().splitlines()[-25:])
        raise RuntimeError(f"команда упала ({res.returncode}):\n{printable}\n--- stderr ---\n{tail}")
    return res


def ffmpeg(args: list[str], quiet: bool = True) -> None:
    run([FFMPEG, "-hide_banner", "-nostdin", "-y", *args], quiet=quiet)


def probe(path: Path) -> dict:
    if FFPROBE:
        res = run([FFPROBE, "-v", "error", "-print_format", "json",
                   "-show_format", "-show_streams", str(path)])
        return json.loads(res.stdout)
    return _probe_pyav(path)


def probe_brief(path: Path) -> dict:
    """Короткая сводка: разрешение, fps, длительность, кодеки."""
    meta = probe(path)
    v = next((s for s in meta["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in meta["streams"] if s["codec_type"] == "audio"), None)
    num, den = (v["r_frame_rate"].split("/") + ["1"])[:2] if v else ("0", "1")
    fps = float(num) / float(den or 1)
    return {
        "path": str(path),
        "duration": float(meta["format"].get("duration", 0.0)),
        "size": int(meta["format"].get("size", 0)),
        "bitrate": int(meta["format"].get("bit_rate", 0) or 0),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": round(fps, 3),
        "vcodec": v["codec_name"] if v else None,
        "acodec": a["codec_name"] if a else None,
        "achannels": int(a.get("channels", 0)) if a else 0,
    }


# ─────────────────────────────── утилиты ──────────────────────────────────
def tc(seconds: float, ms: bool = False) -> str:
    """Секунды → 0:00 / 00:00:00.000."""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if ms:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{int(round((s - int(s)) * 1000)):03d}"
    if h >= 1:
        return f"{int(h)}:{int(m):02d}:{int(s):02d}"
    return f"{int(m)}:{int(s):02d}"


def frames_for(duration: float) -> int:
    """Сколько кадров занимает сегмент (округление до кадра)."""
    return max(1, int(round(duration * FPS)))


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, obj) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ─────────────────────────────── шрифты / Pillow ──────────────────────────
FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
FONT_CANDIDATES_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def font(size: int, bold: bool = True):
    from PIL import ImageFont

    for p in (FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REG):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def text_size(draw, text: str, f) -> tuple[int, int]:
    l, t, r, b = draw.textbbox((0, 0), text, font=f)
    return r - l, b - t


def draw_text_tracked(draw, xy, text, f, fill, tracking=0):
    """Текст с трекингом (как в титрах). Возвращает ширину."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill)
        x += draw.textlength(ch, font=f) + tracking
    return x - xy[0]


def tracked_width(draw, text, f, tracking=0) -> float:
    return sum(draw.textlength(ch, font=f) for ch in text) + tracking * max(0, len(text) - 1)


def rounded_rect(img, box, radius, fill=None, outline=None, width=2):
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    return img


def radial_glow(size, color, power=2.2, peak=200):
    """Радиальная виньетка-подсветка (RGBA), для акцента на предмете."""
    import numpy as np
    from PIL import Image

    w, h = size
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    r = np.sqrt(((xx - cx) / (w / 2.0)) ** 2 + ((yy - cy) / (h / 2.0)) ** 2)
    a = np.clip(1.0 - r, 0.0, 1.0) ** power * peak
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = color
    rgba[..., 3] = a.astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def bottom_gradient(size=(W, H), strength=110, start=0.55):
    """Мягкий градиент затемнения снизу (для читаемости хотбара/титров)."""
    import numpy as np
    from PIL import Image

    w, h = size
    y = np.linspace(0, 1, h)[:, None]
    a = np.clip((y - start) / (1 - start), 0, 1) ** 1.6 * strength
    alpha = np.repeat(a, w, axis=1).astype(np.uint8)
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 3] = alpha
    return Image.fromarray(rgba, "RGBA")


def ensure_png(img, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path
