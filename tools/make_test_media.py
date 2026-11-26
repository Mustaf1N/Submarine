"""
tools/make_test_media.py — ГЕНЕРАТОР ТЕСТОВЫХ ДВОЙНИКОВ (не входит в монтаж).

Нужен, чтобы прогонять и проверять весь пайплайн без исходных записей на 650 МБ.
Создаёт в raw/ два файла, повторяющих по параметрам реальные записи:

    raw/A.mp4  23:19, 1920x1080, с «панелью задач Windows» и заголовком окна сверху,
               внутри — синтетическая «игра»: ходьба, инвентарь, открытие кейсов,
               намеренные замирания (lag), почти чёрный финал;
    raw/B.mp4  1:22, 1280x960 (4:3), тёмная арена (нужна gamma 1.45), окно OBS в начале.

Содержимое нарисовано процедурно, никаких чужих материалов. Используется только
для self-test пайплайна: python3 tools/make_test_media.py --minutes 23
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "edit"))
from cfglib import FFMPEG, SR  # noqa: E402

RAW = ROOT / "raw"


# ─────────────────────────────── «мир» ────────────────────────────────────
def make_world(w=2600, h=1400, block=48, seed=11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    world = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(0, h, block):
        for x in range(0, w, block):
            base = rng.integers(38, 128)
            tint = rng.normal(0, 16, 3)
            col = np.clip(np.array([base * 0.62, base * 0.95, base * 0.55]) + tint, 8, 255)
            world[y:y + block, x:x + block] = col.astype(np.uint8)
    # «деревья» и «руда» — пятна для читаемости кадра
    for _ in range(120):
        bx, by = rng.integers(0, w - block), rng.integers(0, h - block)
        s = block * rng.integers(1, 3)
        world[by:by + s, bx:bx + s] = np.clip(world[by:by + s, bx:bx + s].astype(int) + rng.integers(-40, 60), 0, 255)
    return world


def make_hud(size, gui: bool = False, dark: bool = False) -> np.ndarray:
    """Хотбар, сердца, прицел (и серые панели «инвентаря», если gui=True)."""
    w, h = size
    layer = np.zeros((h, w, 4), dtype=np.uint8)
    s = max(2, int(h / 240))          # масштаб
    bw, bh = 26 * s, 26 * s
    cnt = 9
    total = cnt * bw
    x0 = (w - total) // 2
    y0 = h - bh - 8 * s
    for i in range(cnt):
        x = x0 + i * bw
        layer[y0:y0 + bh, x + s:x + bw - s] = (36, 36, 40, 215)
        layer[y0 + s:y0 + bh - s, x + 2 * s:x + bw - 2 * s] = (96, 96, 104, 230)
        if i % 3 == 0:  # «предметы»
            layer[y0 + 6 * s:y0 + bh - 6 * s, x + 6 * s:x + bw - 6 * s] = (180, 160, 90, 235)
    # сердца
    for i in range(10):
        x = x0 + i * 8 * s
        layer[y0 - 14 * s:y0 - 6 * s, x:x + 7 * s] = (190, 30, 40, 225)
    # прицел
    cx, cy = w // 2, int(h * 0.47)
    layer[cy - 1:cy + 1, cx - 9 * s:cx + 9 * s] = (255, 255, 255, 200)
    layer[cy - 9 * s:cy + 9 * s, cx - 1:cx + 1] = (255, 255, 255, 200)
    if gui:
        gw, gh = int(w * 0.62), int(h * 0.58)
        gx, gy = (w - gw) // 2, int(h * 0.12)
        layer[gy:gy + gh, gx:gx + gw] = (198, 198, 198, 240)          # серые панели Minecraft
        for r in range(3):
            for c in range(9):
                sx = gx + int(gw * 0.06) + c * int(gw * 0.095)
                sy = gy + int(gh * 0.08) + r * int(gh * 0.22)
                layer[sy:sy + int(gh * 0.17), sx:sx + int(gw * 0.08)] = (140, 140, 140, 245)
    return layer


def blit(dst: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    h, w = src.shape[:2]
    H, W = dst.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    s = src[y0 - y:y1 - y, x0 - x:x1 - x]
    a = s[..., 3:4].astype(np.float32) / 255.0
    if s.shape[2] == 4:
        dst[y0:y1, x0:x1] = (s[..., :3] * a + dst[y0:y1, x0:x1] * (1 - a)).astype(np.uint8)
    else:
        dst[y0:y1, x0:x1] = s


def text_layer(size, lines, color=(255, 255, 255), scale=1) -> np.ndarray:
    from PIL import Image, ImageDraw

    from cfglib import font

    img = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font(max(11, int(size[1] * 0.028) * scale), bold=True)
    y = int(size[1] * 0.06)
    for ln in lines:
        d.text((int(size[0] * 0.03), y), ln, font=f, fill=(*color, 230))
        y += int(size[1] * 0.045)
    return np.asarray(img)


# ─────────────────────────────── сценарий ─────────────────────────────────
def plan_A(total: float) -> dict:
    """Ключевые события «источника A» (по карте контента брифа)."""
    return {
        "lag": [(140.0, 148.0), (310.0, 316.0), (570.0, 580.0), (830.0, 838.0), (1250.0, 1258.0)],
        "gui_static": [(480.0, 524.0), (1164.0, 1240.0)],       # инвентарь/лут — это контент
        "case_open": [(704.0, 730.0)],                          # 11:44–12:10
        "loot": [(1164.0, 1240.0)],
        "black": [(1376.0, total)],
        "notify": [(0.0, 1.6)],
    }


def active(intervals, t) -> bool:
    return any(a <= t < b for a, b in intervals)


def encode(frames_iter, out: Path, size, fps: int, extra_out: list[str] | None = None,
           audio: Path | None = None, chunk: int = 900) -> None:
    """Кодирование кадров пайпом в ffmpeg ЧАНКАМИ, затем concat -c copy.

    Большой поток кадров (5000+) через stdin у длинного процесса может потеряться,
    поэтому режем на части по chunk кадров и склеиваем без перекодирования.
    """
    import shutil

    tmpdir = out.parent / (out.stem + "_chunks")
    shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(exist_ok=True)
    parts: list[Path] = []
    it = iter(frames_iter)
    idx = 0
    done = 0
    while True:
        part = tmpdir / f"part_{idx:04d}.mp4"
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-framerate", str(fps), "-i", "-",
               "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-g", str(fps * 2), *(extra_out or []), str(part)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        n = 0
        # кадры пишем потоком (не копим в списке: 900 кадров 1080p = несколько ГБ RAM)
        for fr in it:
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            n += 1
            if n >= chunk:
                break
        proc.stdin.close()
        err = proc.stderr.read().decode()
        if proc.wait() != 0:
            raise RuntimeError(f"кодирование {part.name} упало:\n{err[-1500:]}")
        if n == 0:
            part.unlink(missing_ok=True)
            break
        parts.append(part)
        idx += 1
        done += n
        print(f"  … {done} кадров", flush=True)

    listing = tmpdir / "list.txt"
    listing.write_text("".join(f"file '{pth.name}'\n" for pth in parts), encoding="utf-8")
    ff = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-v", "error",
          "-f", "concat", "-safe", "0", "-i", str(listing)]
    if audio:
        ff += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest"]
    else:
        ff += ["-c", "copy"]
    ff += [*(extra_out or []), str(out)]
    r = subprocess.run(ff, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        raise RuntimeError(f"concat упал:\n{r.stderr.decode()[-1500:]}")
    shutil.rmtree(tmpdir, ignore_errors=True)


def make_audio(total: float, path: Path, tones: bool = True) -> None:
    """Фоновая «игра» + редкие «голосовые» всплески (для проверки ducking)."""
    rng = np.random.default_rng(5)
    n = int(total * SR)
    t = np.arange(n) / SR
    base = 0.06 * np.sin(2 * np.pi * 110 * t) + 0.03 * np.sin(2 * np.pi * 165 * t + 1.0)
    noise = 0.012 * np.convolve(rng.standard_normal(n), np.ones(12) / 12, mode="same")
    sig = base + noise
    if tones:
        # «речь»: всплески широкополосного шума раз в ~20 с
        for k in range(int(total // 20)):
            a = int((6 + k * 20) * SR)
            ln = int(3.2 * SR)
            if a + ln >= n:
                break
            seg = rng.standard_normal(ln) * 0.10
            seg = np.convolve(seg, np.ones(6) / 6, mode="same")
            env = np.hanning(ln)
            sig[a:a + ln] += seg * env * 0.9
    st = np.stack([sig, np.roll(sig, 37)], axis=1)
    st = np.clip(st, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def make_A(total: float, fps: int, out: Path) -> None:
    """Синтетический «источник 1»: контент 911x512 → 1822x1025 в окне 1920x1080."""
    plan = plan_A(total)
    cw, ch = 911, 512
    world = make_world()
    hud_norm = make_hud((cw, ch), gui=False)
    hud_gui = make_hud((cw, ch), gui=True)

    # «окно Windows»: панель задач и заголовок — считаем один раз
    chrome = np.zeros((1080, 1920, 3), dtype=np.uint8)
    chrome[:] = (18, 19, 24)
    chrome[0:48] = (30, 32, 39)
    chrome[44:48] = (58, 60, 70)
    title = text_layer((1920, 1080), ["Minecraft 2026.11.23 - 18.27.09.01.mp4  —  OBS Studio"])
    blit(chrome, title, 0, 0)

    frozen: np.ndarray | None = None

    def content(t: float) -> np.ndarray:
        """Кадр «игры» 911x512 в момент t."""
        gui = active(plan["gui_static"], t)
        speed = 0.0 if gui else 0.6
        x = int(world.shape[1] / 2 + np.sin(t * 0.6) * 700 + t * 140 * speed) % (world.shape[1] - cw - 4)
        y = int(world.shape[0] / 2 + np.cos(t * 0.9) * 260) % (world.shape[0] - ch - 4)
        img = world[y:y + ch, x:x + cw].copy()
        if gui:
            img = (img * 0.55).astype(np.uint8)
        blit(img, hud_gui if gui else hud_norm, 0, 0)
        if active(plan["case_open"], t):
            k = t - plan["case_open"][0][0]
            img = np.clip(img.astype(int) + int(120 * max(0.0, np.sin(k * 9.0))), 0, 255).astype(np.uint8)
            box = np.zeros((int(ch * 0.4), int(cw * 0.35), 4), dtype=np.uint8)
            box[..., :3] = 240
            box[..., 3] = 235
            blit(img, box, int(cw * 0.3), int(ch * 0.2 + np.sin(k * 6) * 10))
        if active(plan["loot"], t):
            for r in range(3):
                box = np.zeros((int(ch * 0.1), int(cw * 0.5), 4), dtype=np.uint8)
                box[..., :3] = 250
                box[..., 3] = 210
                blit(img, box, int(cw * 0.25), int(ch * 0.15 + r * ch * 0.13 + np.sin(t * 3 + r) * 4))
        return img

    def frames():
        nonlocal frozen
        for i in range(int(total * fps)):
            t = i / fps
            if active(plan["black"], t):
                yield np.zeros((1080, 1920, 3), dtype=np.uint8)
                continue
            if active(plan["lag"], t):
                if frozen is None:                 # вход в замирание: кадр «застывает»
                    frozen = content(t)
                img = frozen
            else:
                frozen = None
                img = content(t)
            full = chrome.copy()
            up = np.repeat(np.repeat(img, 2, axis=0), 2, axis=1)[:1025, :1822]
            full[48:48 + up.shape[0], 49:49 + up.shape[1]] = up
            if active(plan["notify"], t):
                full[56:116, 60:900] = (30, 90, 190)
            yield full

    audio = RAW / "_A_audio.wav"
    make_audio(total, audio)
    encode(frames(), out, (1920, 1080), fps, audio=audio)
    audio.unlink(missing_ok=True)


def make_B(total: float, fps: int, out: Path) -> None:
    cw, ch = 640, 480
    world = make_world(seed=3)
    world = (world * 0.30).astype(np.uint8)          # тёмная арена (gamma 1.45 её вытянет)
    hud = make_hud((cw, ch))
    arena = np.zeros((ch, cw, 4), dtype=np.uint8)
    arena[..., :3] = (70, 25, 90)
    arena[int(ch * 0.3):int(ch * 0.45), int(cw * 0.4):int(cw * 0.6), 3] = 235   # дракон

    def frames():
        # намеренные замирания (для проверки вырезки брака): 20–22 с и 60–61.5 с
        frozen = None
        for i in range(int(total * fps)):
            t = i / fps
            if 20.0 <= t < 22.0 or 60.0 <= t < 61.5:
                if frozen is not None:
                    yield frozen
                    continue
            x = int(world.shape[1] / 2 + t * 240) % (world.shape[1] - cw - 4)
            y = int(world.shape[0] / 2 + np.sin(t * 1.6) * 300) % (world.shape[0] - ch - 4)
            img = world[y:y + ch, x:x + cw].copy()
            dx = int(cw * 0.4 + np.sin(t * 0.7) * cw * 0.15)
            dy = int(ch * 0.3 + np.sin(t * 1.1) * 20)
            a = arena[dy:dy + int(ch * 0.12), dx:dx + int(cw * 0.18)]
            blit(img, a, 0, 0)
            blit(img, hud, 0, 0)
            if 30 <= t < 34:      # «удар» — вспышка
                img = np.clip(img.astype(int) + 90, 0, 255).astype(np.uint8)
            if t < 8.0:           # окно OBS поверх игры
                obs = np.zeros((int(ch * 0.55), int(cw * 0.7), 4), dtype=np.uint8)
                obs[..., :3] = (40, 42, 48)
                obs[..., 3] = 250
                blit(img, obs, int(cw * 0.15), int(ch * 0.1))
            if 20.0 <= t < 22.0 or 60.0 <= t < 61.5:
                frozen = img
            yield img

    def frames_scaled():
        # рисуем 640x480 и апскейлим x2 — так генерация в 4 раза быстрее, кадр ровно 1280x960
        for fr in frames():
            yield np.repeat(np.repeat(fr, 2, axis=0), 2, axis=1)

    audio = RAW / "_B_audio.wav"
    make_audio(total, audio, tones=False)
    encode(frames_scaled(), out, (1280, 960), fps, audio=audio)
    audio.unlink(missing_ok=True)


def main():
    global RAW
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=23.32, help="длительность источника A")
    ap.add_argument("--fps-a", type=int, default=15)
    ap.add_argument("--fps-b", type=int, default=60)
    ap.add_argument("--keep", action="store_true", help="не перезаписывать существующие файлы")
    ap.add_argument("--out-dir", default=str(RAW), help="куда писать двойники (по умолчанию raw/)")
    args = ap.parse_args()
    RAW = Path(args.out_dir)
    RAW.mkdir(parents=True, exist_ok=True)
    a, b = RAW / "A.mp4", RAW / "B.mp4"
    if not (a.exists() and args.keep):
        print(f"[test] генерирую {a} ({args.minutes:.2f} мин @ {args.fps_a} fps)…")
        make_A(args.minutes * 60, args.fps_a, a)
    if not (b.exists() and args.keep):
        print(f"[test] генерирую {b} (82 с @ {args.fps_b} fps)…")
        make_B(82.0, args.fps_b, b)
    for p in (RAW / "_A_audio.wav", RAW / "_B_audio.wav"):
        p.unlink(missing_ok=True)
    for p in (a, b):
        print(f"[test] {p.name}: {p.stat().st_size/1e6:.1f} МБ")


if __name__ == "__main__":
    main()
