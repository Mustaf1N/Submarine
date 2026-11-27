"""
render.py — рендер ролика HoweMine по edit/timeline.json.

Пайплайн (п.7 брифа):
  1. prepare  — стоп-кадры для карточек/freeze, графические слои, музыка, SFX;
  2. segments — посгментная сборка: кроп/апскейл/спид/грейд/камера/переходы/оверлеи
                + звук сегмента, всё в 1920x1080@60, кэш по хэшу параметров;
  3. concat   — склейка видеосегментов без перекодирования;
  4. audio    — сборка звука, ducking музыки, two-pass loudnorm −14 LUFS / −1.5 dBTP;
  5. mux      — финальный H.264 High@4.2 + AAC 256k, +faststart.

Режимы:
  python3 edit/render.py --all                 # всё, если готовы raw/A.mp4 и raw/B.mp4
  python3 edit/render.py --prep
  python3 edit/render.py --segments [--limit N]
  python3 edit/render.py --concat --audio --mux
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cfglib import (
    AUD,
    CACHE,
    EDIT,
    FPS,
    LUFS_TARGET,
    OVL,
    REPORT,
    ROOT,
    SR,
    SRC_A,
    SRC_B,
    TP_TARGET,
    ffmpeg,
    log,
    load_json,
    probe_brief,
    save_json,
    sha,
)
from overlays import prepare_overlay
from timeline import parse_tc

DELIVER = ROOT / "deliver"
DELIVER.mkdir(exist_ok=True)
FINAL = DELIVER / "HoweMine_review_1080p60.mp4"
# Промежуточные сегменты: ffv1 при 1080p60 занимает гигабайты (и диск кончается),
# поэтому кодируем H.264 в near-lossless (CRF 12, preset ultrafast) — визуально
# без потерь для финального CRF 19, зато в 5–8 раз меньше и быстрее.
INTERMEDIATE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                "-pix_fmt", "yuv420p", "-g", "120", "-threads", "2"]
VERSION = "render-8"
XFADE = 0.22          # длительность растворения на стыке двух планов, с

SOURCES = {"A": SRC_A, "B": SRC_B}


# ─────────────────────────────── фильтры ─────────────────────────────────
def grade_filter(src: str) -> str:
    """Единый грейд для обоих источников (вторая запись — тёмная арена)."""
    if src == "A":
        return "eq=contrast=1.07:brightness=-0.005:saturation=1.08:gamma=1.0,unsharp=5:5:0.45:5:5:0.0"
    return "eq=contrast=1.06:brightness=0.01:saturation=1.10:gamma=1.45,unsharp=5:5:0.45:5:5:0.0"


def layout_filter(src: str) -> str:
    """Приведение кадра к 1920x1080 без искажений и без обрезки хотбара."""
    if src == "A":
        # источник 1: срезаем панель задач Windows и заголовок окна
        return "crop=1822:1025:49:55,scale=1920:1080:flags=bicubic,setsar=1"
    # источник 2: 1280x960 (4:3) → 1440x1080 по центру + размытый фон по бокам
    return (
        "split[bgsrc][fgsrc];"
        "[bgsrc]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
        "boxblur=luma_radius=42:luma_power=2,eq=brightness=-0.22:saturation=0.75[bg];"
        "[fgsrc]scale=1440:1080:flags=bicubic[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def speed_filter(speed: float) -> str:
    """Ускорение/замедление только видео (fps 60 → 60, без дропов)."""
    if abs(speed - 1.0) < 1e-4:
        return ""
    return f"setpts=PTS/{speed:.6f},fps={FPS}"


def camera_filter(cam: dict | None, frames: int) -> str:
    """push-in / drift / shake через zoompan с easing (без «цифрового» дёргания)."""
    if not cam:
        return "scale=1920:1080,setsar=1"
    kind = cam.get("type", "push")
    # zoompan требует апскейл входа, чтобы не мылить при зуме
    up = "scale=3840:2160:flags=bicubic"
    D = max(2, frames)
    if kind == "push":
        z0, z1 = float(cam.get("from", 1.0)), float(cam.get("to", 1.06))
        fx, fy = float(cam.get("fx", 0.5)), float(cam.get("fy", 0.5))
        # Линейный ход, а не ease-out: у ease-out скорость к концу плана падает
        # до нуля (производная 1-(1-p)^2 в p=1 равна 0), кадры в хвосте становятся
        # одинаковыми, и freezedetect честно видит «замирание». Ровный ход камеры
        # даёт постоянные 1–2 px за кадр — картинка живая на всём плане.
        zexpr = f"{z0}+({z1 - z0})*on/{D}"
        xexpr = f"(iw-iw/zoom)*{fx:.4f}"
        yexpr = f"(ih-ih/zoom)*{fy:.4f}"
    elif kind == "spring":
        # «живая камера» для статичных планов: дышит зумом и микро-сдвигом,
        # поэтому кадр никогда не стоит (freezedetect чистый), а глаз видит
        # не «стоп-кадр», а спокойную камеру оператора
        z0 = float(cam.get("from", 1.0))
        amp = float(cam.get("amp", 0.012))
        f = float(cam.get("freq", 0.37))
        ramp = f"min(1,on/{max(1, int(FPS * 0.6))})"      # плавный вход, без рывка
        zexpr = f"{z0}+{amp:.4f}*{ramp}*sin(2*PI*{f:.4f}*on/{FPS})"
        xexpr = f"(iw-iw/zoom)*(0.5+{amp * 0.75:.4f}*{ramp}*sin(2*PI*{f * 0.63:.4f}*on/{FPS}+2.1))"
        yexpr = f"(ih-ih/zoom)*(0.5+{amp * 0.6:.4f}*{ramp}*cos(2*PI*{f * 0.51:.4f}*on/{FPS}+0.7))"
    elif kind == "drift":
        z = float(cam.get("zoom", 1.05))
        # Нижняя граница амплитуды/частоты: слабый дрейф даёт меньше пикселя
        # смещения и превращается в «стоп-кадр» (freezedetect его ловит).
        amp = max(float(cam.get("amp", 0.012)), 0.028)
        f = max(float(cam.get("freq", 0.15)), 0.32)
        # Дрейф самого кадра — ОБЯЗАТЕЛЬНО, иначе картинка стоит: на входе
        # zoompan округляет «дыхание» зума в ту же целую позицию (0.1 px), и
        # freezedetect видит замирание. Сдвиг на 1.5 px за 2 с двигает всегда.
        swipe = 0.0035
        # «дыхание»: очень медленный дрейф + микро-покачивание
        zexpr = f"{z}+{amp * 0.25:.4f}*sin(2*PI*{f:.4f}*on/{FPS})"
        xexpr = (f"(iw-iw/zoom)*(0.5+{amp:.4f}*sin(2*PI*{f * 0.7:.4f}*on/{FPS}+1.1)"
                 f"+{swipe:.4f}*iw*on/{D})")
        yexpr = f"(ih-ih/zoom)*(0.5+{amp * 0.6:.4f}*cos(2*PI*{f * 0.9:.4f}*on/{FPS}+0.4))"
    else:  # shake
        z = float(cam.get("zoom", 1.03))
        amp = float(cam.get("amp", 0.004))
        f = float(cam.get("freq", 1.8))
        zexpr = f"{z}+{amp:.4f}*sin(2*PI*{f * 0.3:.3f}*on/{FPS})"
        xexpr = f"(iw-iw/zoom)*(0.5+{amp * 2.2:.4f}*sin(2*PI*{f:.3f}*on/{FPS}))"
        yexpr = f"(ih-ih/zoom)*(0.5+{amp * 1.6:.4f}*sin(2*PI*{f * 1.37:.3f}*on/{FPS}+2.1))"
    # d=1: один выход на вход; fps=60 — ровно 60 кадров в секунду на выходе.
    return (
        f"{up},zoompan=z='{zexpr}':x='{xexpr}':y='{yexpr}':d=1:s=1920x1080:fps={FPS},"
        f"setsar=1"
    )


def transition_filter(kind: str, dur: float) -> str:
    """Переходы в начале сегмента: whip-pan, zoom-blur, световой флеш.

    Важно: gblur.sigma не поддерживает eval=frame, поэтому размытие делаем
    через tmix (смешивание соседних кадров = motion blur) и сдвиг кадра
    выражением по t (crop поддерживает eval). Никаких дефолтных кроссфейдов.
    """
    if kind == "whip":
        return (
            "tmix=frames=3:weights='0.6 0.3 0.1',"
            f"crop=1920:1080:'(iw-1920)/2+90*exp(-t/{dur * 0.55:.3f})':'(ih-1080)/2',"
            "scale=1920:1080,setsar=1"
        )
    if kind == "zoom_blur":
        return "tmix=frames=3:weights='0.55 0.3 0.15',scale=1920:1080,setsar=1"
    if kind == "flash":
        return "eq=brightness='0.20*exp(-11*t)':saturation=1.05:eval=frame"
    return ""


# ─────────────────────────────── аудио сегмента ───────────────────────────
def segment_audio(src: str, t_in: float, t_out: float, speed: float, out_wav: Path) -> None:
    """Аудио сегмента: точная обрезка, atempo-цепочка, 48k stereo, микро-затухания."""
    dur_src = t_out - t_in
    dur_out = dur_src / speed
    chain = [f"atrim=start={t_in:.6f}:end={t_out:.6f}", "asetpts=PTS-STARTPTS"]
    sp = speed
    while sp > 2.0:
        chain.append("atempo=2.0")
        sp /= 2.0
    while sp < 0.5:
        chain.append("atempo=0.5")
        sp /= 0.5
    if abs(sp - 1.0) > 1e-3:
        chain.append(f"atempo={sp:.6f}")
    f = min(0.045, max(0.01, dur_out * 0.05))
    chain.append(f"afade=t=in:st=0:d={f:.3f}")
    chain.append(f"afade=t=out:st={max(0.0, dur_out - f):.3f}:d={f:.3f}")
    chain.append("aresample=48000")
    chain.append("aformat=channel_layouts=stereo")
    ffmpeg(["-v", "error", "-i", str(SOURCES[src]), "-vn",
            "-af", ",".join(chain), "-ar", "48000", "-ac", "2",
            "-c:a", "pcm_s16le", str(out_wav)])


# ─────────────────────────────── кадры ───────────────────────────────────
def grab_still(src: str, t: float, out_png: Path) -> Path:
    """Стоп-кадр источника с полным лэйаутом и грейдом (для карточек и freeze)."""
    dur = probe_brief(SOURCES[src])["duration"]
    if t >= dur:
        t = max(0.0, dur - 0.2)
    ffmpeg(["-v", "error", "-ss", f"{t:.3f}", "-i", str(SOURCES[src]),
            "-vf", f"{layout_filter(src)},{grade_filter(src)}", "-frames:v", "1", str(out_png)])
    if not out_png.exists() or out_png.stat().st_size < 1000:
        raise RuntimeError(f"не удалось снять кадр {src}@{t:.2f}s → {out_png} (проверьте таймкод)")
    return out_png


def resolve_stills(edl: dict) -> dict:
    """Рендерит все стоп-кадры, на которые ссылаются карточки."""
    stills = {}
    for s in edl["segments"]:
        for o in s.get("overlays", []):
            bg = o.get("bg")
            if bg and str(bg).startswith("still:"):
                spec = str(bg)[6:]
                at, t = spec.split("@", 1)
                t = parse_tc(t)
                key = sha({"still": spec, "v": VERSION})
                p = OVL / f"still_{key}.png"
                if not p.exists():
                    log(f"[render] стоп-кадр {spec} → {p.name}")
                    grab_still(at, float(t), p)
                stills[bg] = str(p)
                o["bg"] = str(p)
    return stills


# ─────────────────────────────── сегменты ────────────────────────────────
def segment_frames(s: dict) -> int:
    """Длина сегмента в кадрах. Минимум 4 кадра (0.067 с): иначе mkv/ffv1
    теряет заголовки и сегмент выпадает/ломает конкатенацию."""
    return max(4, int(round(float(s["duration"]) * FPS)))


def build_segment(s: dict, force: bool = False, prev: Path | None = None) -> tuple[Path, Path, dict]:
    """Рендер одного сегмента: видео + звук в одном ffv1/pcm-файле.

    Сегмент — самодостаточный mkv (видео ffv1 + аудио pcm_s16le), поэтому
    склейка `concat -c copy` переносит оба потока и звук не «съезжает».
    """
    frames = segment_frames(s)
    key = sha({
        "src": s["src"], "in": s["in"], "out": s["out"], "speed": s["speed"],
        "cam": s.get("camera"), "trans": s.get("trans_in"), "frames": frames,
        "ovl": [(o.get("type"), o.get("local"), o.get("dur")) for o in s.get("overlays", [])],
        "prev": prev.name if prev is not None else None,
        "v": VERSION,
    })
    vid = CACHE / f"seg_{key}.mp4"
    aud = CACHE / f"seg_{key}.wav"
    meta = CACHE / f"seg_{key}.json"
    # кэш валиден только если файл реально записан: прерванный рендер оставлял
    # пустой mp4, и следующий запуск «успешно» брал его из кэша
    if vid.exists() and vid.stat().st_size > 20_000 and meta.exists() and not force:
        return vid, aud, load_json(meta)

    ovl_paths = []
    for o in s.get("overlays", []):
        pth = prepare_overlay(o, frames, force=force)
        if pth:
            ovl_paths.append(pth)

    if s["src"] in SOURCES:
        # 1) звук сегмента — отдельным файлом (точная обрезка + atempo + микро-фейды)
        segment_audio(s["src"], s["in"], s["out"], s["speed"], aud)
        inputs = ["-ss", f"{s['in']:.6f}", "-t", f"{s['out'] - s['in']:.6f}", "-i", str(SOURCES[s["src"]])]
        # 2) видео: лэйаут → грейд → спид → переход → камера → оверлеи
        fc = []
        chain = [f"[0:v]{layout_filter(s['src'])}", grade_filter(s["src"]),
                 # приводим любую частоту кадров источника к 60 ДО камеры:
                 # zoompan(d=1) на входе 15/30 fps ломает длительность сегмента
                 f"fps={FPS}"]
        # первый план ролика всегда проявляется из чёрного
        # растворение из чёрного короче чёрного кадра: blackdetect не должен
        # ловить «брак» на первом кадре ролика
        if prev is None and not s.get("no_fade_in"):
            chain.append("fade=t=in:st=0:d=0.12")
        sp = speed_filter(s["speed"])
        if sp:
            chain.append(sp)
        fc.append(",".join(chain) + "[v0]")
        cur, vn = "[v0]", 0
        kind = s.get("trans_in", "none")
        use_xfade = kind in ("fade", "whip", "zoom_blur") and prev is not None
        tr = "" if use_xfade else transition_filter(kind, 0.28)
        if tr:
            vn += 1
            fc.append(f"{cur}{tr}[v{vn}]")
            cur = f"[v{vn}]"
        vn += 1
        fc.append(f"{cur}{camera_filter(s.get('camera'), frames)}[v{vn}]")
        cur = f"[v{vn}]"
        in_idx = 1
        for pth in ovl_paths:
            inputs += ["-i", str(pth)]
            vn += 1
            nxt = f"[v{vn}]"
            fc.append(f"{cur}[{in_idx}:v]overlay=0:0:format=auto:eof_action=pass{nxt}")
            cur, in_idx = nxt, in_idx + 1

        # Мягкий стык: первые XFADE секунд плана проявляются из последней картинки
        # предыдущего. Так склейка не читается как «вспышка»: кадр непрерывен,
        # но новый план уже идёт своим ходом (время сегмента не меняется).
        if use_xfade:
            inputs += ["-sseof", f"-{XFADE + 0.4:.3f}", "-i", str(prev)]
            fc.append(f"[{in_idx}:v]trim=0:{XFADE:.3f},setpts=PTS-STARTPTS,"
                      f"scale=1920:1080,setsar=1,format=yuva420p,"
                      f"fade=t=out:st=0:d={XFADE:.3f}:alpha=1[xf]")
            vn += 1
            fc.append(f"{cur}[xf]overlay=0:0:eof_action=pass:format=auto[v{vn}]")
            cur, in_idx = f"[v{vn}]", in_idx + 1
        fc.append(f"{cur}format=yuv420p[vout]")
        inputs += ["-i", str(aud)]
        a_idx = in_idx
        ffmpeg(["-v", "error", *inputs, "-filter_complex", ";".join(fc),
                "-map", "[vout]", "-map", f"{a_idx}:a",
                "-r", str(FPS), "-frames:v", str(frames), *INTERMEDIATE,
                "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2", str(vid)])
    else:
        # карточка / freeze-frame: статичная подложка + оверлеи + тишина
        if s["src"] == "card":
            base = str(s["overlays"][0].get("bg"))
        else:
            key_f = sha({"s": s.get("frame_src"), "t": s.get("frame_t"), "v": VERSION})
            base = str(OVL / f"freeze_{key_f}.png")
            if not Path(base).exists():
                grab_still(s.get("frame_src", "A"), float(s.get("frame_t", 0.0)), Path(base))
        inputs = ["-loop", "1", "-framerate", str(FPS), "-t", f"{s['duration']:.3f}", "-i", base]
        fc = ["[0:v]scale=1920:1080:flags=lanczos,format=rgba[b0]"]
        cur, vn, in_idx = "[b0]", 0, 1
        for pth in ovl_paths:
            inputs += ["-i", str(pth)]
            vn += 1
            nxt = f"[b{vn}]"
            fc.append(f"{cur}[{in_idx}:v]overlay=0:0{nxt}")
            cur, in_idx = nxt, in_idx + 1
        fc.append(f"{cur}format=yuv420p[vout]")
        inputs += ["-f", "lavfi", "-t", f"{s['duration']:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        a_idx = in_idx
        ffmpeg(["-v", "error", *inputs, "-filter_complex", ";".join(fc),
                "-map", "[vout]", "-map", f"{a_idx}:a",
                "-r", str(FPS), "-frames:v", str(frames), *INTERMEDIATE,
                "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "2", str(vid)])
        ffmpeg(["-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", f"{s['duration']:.3f}", "-c:a", "pcm_s16le", str(aud)])

    info = {"id": s.get("id") or s["label"], "label": s["label"], "frames": frames,
            "duration": round(frames / FPS, 3), "video": str(vid), "audio": str(aud),
            "src": s["src"], "in": s["in"], "out": s["out"], "speed": s["speed"]}
    save_json(meta, info)
    return vid, aud, info


# ─────────────────────────────── сборка ──────────────────────────────────
def concat_video(paths: list[Path], out: Path, force: bool = False) -> Path:
    """Склейка без перекодирования (нужна только для отладки/QC длины)."""
    # имя файла зависит от состава сегментов: правка EDL не оставит устаревшую склейку
    out = out.with_name(f"{out.stem}_{sha([p.name for p in paths])}{out.suffix}")
    if out.exists() and not force:
        return out
    listing = CACHE / "concat_list.txt"
    listing.write_text("".join(f"file '{p.resolve()}'\n" for p in paths), encoding="utf-8")
    # каждый сегмент содержит и видео, и звук: concat -c copy переносит оба потока,
    # поэтому итоговый mkv уже имеет дорожку (звук берём прямо из него)
    ffmpeg(["-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-fflags", "+genpts", str(out)])
    return out


def build_audio(edl: dict, seg_audios: list[Path], force: bool = False,
                video: Path | None = None) -> Path:
    """Игра → музыка с ducking → SFX → two-pass loudnorm."""
    from audio import build_mix, concat_segment_audio, master, measure_loudness, write_sfx_bank

    write_sfx_bank()
    game = AUD / "game_raw.wav"
    has_audio = bool(video is not None and video.exists() and probe_brief(video)["acodec"])
    if has_audio:
        # звук берём прямо из склеенного видео — гарантированно совпадает по длине
        ffmpeg(["-v", "error", "-i", str(video), "-vn", "-ac", "2", "-ar", str(SR),
                "-c:a", "pcm_s16le", str(game)])
        total = probe_brief(video)["duration"]
    else:
        # сегментная склейка аудио (точная длина по кадрам + микро-фейды)
        segments = [{"file": str(p), "frames": segment_frames(s), "gain": float(s.get("gain", 1.0))}
                    for s, p in zip(edl["segments"], seg_audios)]
        concat_segment_audio(segments, game)
        total = sum(x["frames"] for x in segments) / FPS
    mix = AUD / "mix_raw.wav"
    build_mix(total, game, edl.get("sfx", []), edl.get("duck_windows", []), mix,
              mute_windows=edl.get("music_mute_windows", []))

    # AAC на энкоде добавляет ~0.4–0.9 дБ интерсэмпловых пиков, поэтому пик
    # контролируется по факту «пробного» AAC (см. audio.master(..., aac_tp=...))
    # Цель по громкости для wav берём на +0.5 LUFS выше: AAC-энкод «съедает»
    # ~0.5 LU (интерсэмпловые пики), и в готовом файле выходит ровно −14 LUFS.
    # aac_tp: пик подгоняем по факту пробного AAC, чтобы итог был ≤ −1.5 dBTP.
    norm = master(mix, AUD / "mix_norm.wav", target_lufs=LUFS_TARGET + 0.5,
                  tp=TP_TARGET - 0.15, aac_tp=TP_TARGET)
    meas = measure_loudness(AUD / "mix_norm.wav")
    save_json(REPORT / "loudness.json", {"normalize": norm, "measured_after": meas})
    log(f"[render] звук: {meas}")
    return AUD / "mix_norm.wav"


def mux(video, audio_path: Path, out: Path = FINAL) -> Path:
    """Финальный мукс + контроль True Peak по факту (после AAC).

    `video` — путь к склейке или список сегментов: тогда сегменты склеиваются
    демуксером concat в один проход (без промежуточного файла на диске).
    """
    from audio import true_peak_dbtp

    gain = 0.0
    tp = None
    for attempt in range(4):
        _mux_once(video, audio_path, out, gain)
        tp = true_peak_dbtp(out)
        log(f"[render] мукс #{attempt + 1}: гейн {gain:+.2f} дБ → TP {tp:+.2f} dBTP")
        if tp <= TP_TARGET:
            break
        gain -= tp - TP_TARGET + 0.15
    save_json(REPORT / "mux_peak.json", {"gain_db": round(gain, 2), "true_peak_dbtp": tp})
    return out


def _mux_once(video, audio_path: Path, out: Path, gain_db: float = 0.0) -> Path:
    if isinstance(video, (list, tuple)):
        listing = CACHE / f"final_list_{sha([Path(p).name for p in video])}.txt"
        listing.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in video), encoding="utf-8")
        vin = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    else:
        vin = ["-i", str(video)]
    ffmpeg([
        "-v", "error", *vin, "-i", str(audio_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-x264-params", "keyint=120:min-keyint=60:scenecut=0",
        "-r", str(FPS), "-g", "120",
        "-af", f"volume={gain_db:+.2f}dB",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-shortest", str(out),
    ])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--prep", action="store_true")
    ap.add_argument("--segments", action="store_true")
    ap.add_argument("--concat", action="store_true")
    ap.add_argument("--audio", action="store_true")
    ap.add_argument("--mux", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--timeline", default=str(EDIT / "timeline.json"))
    args = ap.parse_args()
    if not any([args.all, args.prep, args.segments, args.concat, args.audio, args.mux]):
        args.all = True

    edl = load_json(Path(args.timeline))
    missing = [k for k, p in SOURCES.items() if not Path(p).exists()]
    if missing and not args.prep:
        log(f"[render] НЕТ ИСХОДНИКОВ: {', '.join(str(SOURCES[k]) for k in missing)}")
        log("[render] положите записи в raw/ (см. raw/README.md) — без них рендер невозможен")
        return 2

    # стоп-кадры для карточек/freeze нужны всегда (дёшево и идемпотентно);
    # исходный timeline.json не перезаписываем — разрешённая копия уходит в work/
    resolve_stills(edl)
    save_json(Path(ROOT) / "work" / "timeline_resolved.json", edl)

    if args.all or args.segments or args.concat or args.audio or args.mux:
        log(f"[render] сегментов в EDL: {len(edl['segments'])}")
        vids, auds, infos = [], [], []
        total = len(edl["segments"])
        for i, s in enumerate(edl["segments"], 1):
            if args.limit and i > args.limit:
                break
            log(f"[render] {i:>3}/{total}  {s['src']:<6} {s['in']:>7.2f}–{s['out']:>7.2f} "
                f"×{s['speed']:<5} {s['label'][:46]}")
            prev = vids[-1] if vids else None
            v, a, info = build_segment(s, force=args.force, prev=prev)
            vids.append(v)
            auds.append(a)
            infos.append(info)
        save_json(REPORT / "segments.json", infos)

        if args.limit and args.limit < total:
            log("[render] частичный прогон (--limit) — сборку и микс не делаю")
            return 0

        joined = concat_video(vids, CACHE / "video_joined.mkv", force=args.force)
        log(f"[render] видео склеено (проверка длины): {probe_brief(joined)['duration']:.2f}s")
        mix = build_audio(edl, auds, force=args.force, video=joined)
        # финальный мукс — сразу из сегментов: не нужен ни лишний файл, ни лишний проход
        out = mux(vids, mix)
        b = probe_brief(out)
        log(f"[render] готово: {out}\n          {b['duration']:.2f}s, {b['bitrate']/1e6:.2f} Мбит/с, "
            f"{b['width']}x{b['height']}@{b['fps']}")
        save_json(REPORT / "final_probe.json", b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
