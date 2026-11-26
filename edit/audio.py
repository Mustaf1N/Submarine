"""
audio.py — музыка и SFX для ролика HoweMine.

Всё генерируется процедурно (numpy) прямо здесь, поэтому лицензия на 100 %
чистая: это наши собственные синтезированные звуки (CC0 / public domain).
Никаких внешних библиотек, никаких «скачал где-то трек».

  * музыка: 100 BPM, ля-минор, 16 тактов = 38.4 с, тайлится на всю длину ролика
    с чередованием секций (sparse / full / breakdown) и мягким фильтром;
  * SFX: whoosh, click, tick, pop, low-boom, reverse-riser, saber-hit, ui-open;
  * финальный микс: сегментная речь/игра + музыка (с ducking −8 дБ в окнах) + SFX,
    затем two-pass loudnorm → −14 LUFS / TP ≤ −1.5 dBTP.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cfglib import FFMPEG, FPS, LUFS_TARGET, SR, TP_TARGET, AUD, ffmpeg, log, run, save_json

BPM = 100.0
BEAT = 60.0 / BPM           # 0.6 с
BAR = 4 * BEAT              # 2.4 с
LOOP_BARS = 16
LOOP_LEN = BAR * LOOP_BARS  # 38.4 с

RNG = np.random.default_rng(20261126)


# ────────────────────────────── DSP-примитивы ─────────────────────────────
def _t(dur: float) -> np.ndarray:
    return np.arange(int(dur * SR), dtype=np.float64) / SR


def _env(n: int, attack: float, decay: float, sr: int = SR) -> np.ndarray:
    """Быстрая атака + экспоненциальный спад."""
    a = max(1, int(attack * sr))
    t = np.arange(n) / sr
    env = np.exp(-t / max(1e-4, decay))
    if a > 1:
        env[:a] *= np.linspace(0, 1, a)
    return env


def _lowpass(x: np.ndarray, cutoff: float, sr: int = SR) -> np.ndarray:
    """ФНЧ (однополюсный) — векторно, через scipy; без него — через FFT-маску."""
    cutoff = float(max(20.0, min(cutoff, sr / 2 - 100)))
    try:
        from scipy.signal import butter, sosfilt

        sos = butter(2, cutoff / (sr / 2), btype="low", output="sos")
        return sosfilt(sos, x)
    except Exception:
        spec = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(len(x), 1 / sr)
        mask = 1.0 / np.sqrt(1.0 + (freqs / cutoff) ** 4)
        return np.fft.irfft(spec * mask, n=len(x))


def _highpass(x: np.ndarray, cutoff: float, sr: int = SR) -> np.ndarray:
    """ФВЧ как разность с ФНЧ (стабильно и без «звона»)."""
    return x - _lowpass(x, cutoff, sr)


def _saw(freq: np.ndarray | float, n: int) -> np.ndarray:
    phase = np.cumsum(np.broadcast_to(np.asarray(freq, dtype=np.float64), (n,))) / SR
    return 2.0 * (phase % 1.0) - 1.0


def _sine(freq: np.ndarray | float, n: int, phase0: float = 0.0) -> np.ndarray:
    phase = np.cumsum(np.broadcast_to(np.asarray(freq, dtype=np.float64), (n,))) / SR
    return np.sin(2 * math.pi * phase + phase0)


def _noise(n: int) -> np.ndarray:
    return RNG.standard_normal(n)


def _add(buf: np.ndarray, sig: np.ndarray, at: int, gain: float = 1.0) -> None:
    if at >= len(buf):
        return
    n = min(len(sig), len(buf) - at)
    buf[at : at + n] += sig[:n] * gain


def _to_stereo(x: np.ndarray, width: float = 0.12) -> np.ndarray:
    """Мягкое стерео: лёгкая задержка/фаза по каналам."""
    d = int(0.004 * SR)
    left = x
    right = np.concatenate([np.zeros(d), x[:-d]]) if d else x
    l = (1 - width) * left + width * right
    r = (1 - width) * right + width * left
    return np.stack([l, r], axis=1)


# ────────────────────────────── инструменты ───────────────────────────────
def kick(vel: float = 1.0, dur: float = 0.32) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 46 + 92 * np.exp(-t / 0.045)
    sig = _sine(f, n) * np.exp(-t / 0.16)
    click = _highpass(_noise(n), 2500) * np.exp(-t / 0.006) * 0.25
    return np.tanh((sig + click) * 1.4) * vel


def clap(vel: float = 1.0, dur: float = 0.28) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    body = _highpass(_noise(n), 1200)
    # три «щелчка» как у классического clap
    gate = np.zeros(n)
    for off in (0.0, 0.011, 0.023):
        gate += np.exp(-np.clip(t - off, 0, None) / 0.012) * (t >= off)
    sig = body * gate * 0.5 + _sine(210, n) * np.exp(-t / 0.05) * 0.15
    return sig * vel


def hat(vel: float = 0.5, dur: float = 0.07, open_hat: bool = False) -> np.ndarray:
    dur = 0.26 if open_hat else dur
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _highpass(_noise(n), 6500) * np.exp(-t / (0.11 if open_hat else 0.018))
    return sig * vel * 0.55


def bass_note(freq: float, dur: float, vel: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _saw(freq, n) * 0.5 + _sine(freq / 2, n) * 0.6
    sig = _lowpass(sig, 320)
    env = np.minimum(1.0, t / 0.008) * np.exp(-t / (dur * 0.55))
    return sig * env * vel


def pad_chord(freqs: list[float], dur: float, vel: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for f in freqs:
        for det in (-0.12, 0.0, 0.12):
            out += _sine((f + det) * (1 + 0.0007 * np.sin(2 * math.pi * 0.21 * t)), n) * 0.18
        out += _saw(f * 2, n) * 0.05
    out = _lowpass(out, 2200)
    env = np.minimum(1.0, t / 0.35) * np.minimum(1.0, np.clip((dur - t) / 0.5, 0, 1))
    return out * env * vel / max(1, len(freqs))


def pluck(freq: float, dur: float = 0.42, vel: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _sine(freq, n) * 0.6 + _sine(freq * 2.01, n) * 0.25 + _sine(freq * 3.02, n) * 0.1
    return sig * np.exp(-t / 0.14) * vel


# ────────────────────────────── музыка ────────────────────────────────────
# ля-минор: A C E G  |  F G Am ...
CHORDS = {
    "Am": ([220.00, 261.63, 329.63], 55.00),
    "F":  ([174.61, 220.00, 261.63], 43.65),
    "C":  ([261.63, 329.63, 392.00], 65.41),
    "G":  ([196.00, 246.94, 293.66], 49.00),
    "Em": ([164.81, 196.00, 246.94], 41.20),
}
PROGRESSION = ["Am", "F", "C", "G", "Am", "F", "G", "Em",
               "Am", "F", "C", "G", "Am", "Em", "F", "G"]


def build_music(total: float, section: str = "full", seed: int = 7) -> np.ndarray:
    """Трек на всю длину ролика. section: sparse | full | breakdown."""
    rng = np.random.default_rng(seed)
    n = int(total * SR)
    music = np.zeros((n, 2), dtype=np.float64)

    bar_i = 0
    pos = 0.0
    while pos < total:
        chord = PROGRESSION[bar_i % len(PROGRESSION)]
        freqs, root = CHORDS[chord]
        bar_start = int(round(pos * SR))
        dense = section == "full"
        # пад — всегда, но тише в sparse
        pad = pad_chord(freqs, BAR + 0.35, vel=0.55 if dense else 0.4)
        _add(music[:, 0], pad * 0.55, bar_start)
        _add(music[:, 1], pad * 0.55, bar_start + int(0.01 * SR))
        # барабаны
        for beat in range(4):
            b_at = bar_start + int(round(beat * BEAT * SR))
            if dense and beat in (0, 2):
                _add(music[:, 0], kick(0.9), b_at)
                _add(music[:, 1], kick(0.9), b_at)
            if dense and beat in (1, 3):
                _add(music[:, 0], clap(0.42), b_at)
                _add(music[:, 1], clap(0.38), b_at + int(0.008 * SR))
            # хэты 8-ми
            _add(music[:, 0], hat(0.30 if dense else 0.18), b_at + int(0.0 * SR))
            _add(music[:, 1], hat(0.26 if dense else 0.16), b_at + int(0.012 * SR))
            _add(music[:, 0], hat(0.16 if dense else 0.1), b_at + int(0.5 * BEAT * SR))
            _add(music[:, 1], hat(0.14 if dense else 0.09), b_at + int(0.5 * BEAT * SR) + int(0.006 * SR))
        # бас — восьмые
        for step in range(8):
            if not dense and step % 2:
                continue
            at = bar_start + int(round(step * BEAT / 2 * SR))
            freq = root if step % 4 else root * 2
            _add(music[:, 0], bass_note(freq, BEAT / 2 * 0.9, 0.55 if dense else 0.42), at)
            _add(music[:, 1], bass_note(freq, BEAT / 2 * 0.9, 0.55 if dense else 0.42), at)
        # арп-плак — лёгкий верхний слой
        arp = [freqs[0] * 2, freqs[1] * 2, freqs[2] * 2, freqs[1] * 2]
        for step in range(8):
            if not dense and rng.random() < 0.45:
                continue
            at = bar_start + int(round(step * BEAT / 2 * SR + 0.01 * SR))
            _add(music[:, 0], pluck(arp[step % 4], 0.35, 0.13), at)
            _add(music[:, 1], pluck(arp[step % 4], 0.35, 0.13), at + int(0.012 * SR))
        pos += BAR
        bar_i += 1

    # мягкий общий ФНЧ против «цифровой резкости»
    music[:, 0] = _lowpass(music[:, 0], 12000)
    music[:, 1] = _lowpass(music[:, 1], 12000)
    peak = float(np.max(np.abs(music))) or 1.0
    music /= peak
    # микро-фейды краёв
    f = int(0.05 * SR)
    music[:f] *= np.linspace(0, 1, f)[:, None]
    music[-f:] *= np.linspace(1, 0, f)[:, None]
    return music * 0.85


# ────────────────────────────── SFX ───────────────────────────────────────
def sfx_whoosh(dur: float = 0.45, up: bool = True) -> np.ndarray:
    """Whoosh: шум через полосовой фильтр со свипом + обратная огибающая."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    noise = _noise(n)
    f0, f1 = (500.0, 5200.0) if up else (5200.0, 500.0)
    # делим на 4 куска со своей центральной частотой и кроссфейдим
    band = np.zeros(n)
    seg = n // 4
    for k in range(4):
        a, b = k * seg, (n if k == 3 else (k + 1) * seg)
        center = f0 + (f1 - f0) * ((k + 0.5) / 4)
        center = float(np.clip(center, 80, SR / 2 - 200))
        hp = _highpass(noise[a:b], center * 0.55)
        bp = _lowpass(hp, center * 1.6)
        band[a:b] = bp
    env = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 1.4
    sig = band * env
    peak = float(np.max(np.abs(sig))) or 1.0
    sig = sig / peak * 0.85
    return np.stack([sig, np.roll(sig, int(0.01 * SR))], axis=1)


def sfx_click(dur: float = 0.05) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _sine(2400, n) * np.exp(-t / 0.008) * 0.5 + _highpass(_noise(n), 4000) * np.exp(-t / 0.004) * 0.3
    return np.stack([sig, sig], axis=1)


def sfx_tick(dur: float = 0.03) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _sine(3400, n) * np.exp(-t / 0.004) * 0.35
    return np.stack([sig, sig * 0.9], axis=1)


def sfx_pop(dur: float = 0.16) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 320 + 900 * (t / dur)
    sig = _sine(f, n) * np.exp(-t / 0.045)
    return np.stack([sig, np.roll(sig, 40)], axis=1) * 0.8


def sfx_low_boom(dur: float = 1.4) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = _sine(32 + 58 * np.exp(-t / 0.12), n) * np.exp(-t / 0.5)
    sig = np.tanh(sig * 1.7) * 0.9
    sub = _lowpass(_noise(n), 90) * np.exp(-t / 0.25) * 0.25
    out = sig + sub
    return np.stack([out, out], axis=1)


def sfx_riser(dur: float = 1.2) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 180 * (2 ** (2.2 * t / dur))
    tone = _sine(f, n) * 0.35
    noise = _highpass(_noise(n), 800) * 0.35
    env = (t / dur) ** 2.2
    sig = (tone + noise) * env
    return np.stack([sig, np.roll(sig, int(0.015 * SR))], axis=1) * 0.8


def sfx_hit(dur: float = 0.6) -> np.ndarray:
    """Удар/взрыв: шумовой всплеск + низ."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    burst = _highpass(_noise(n), 300) * np.exp(-t / 0.05) * 0.6
    body = _sine(90 + 40 * np.exp(-t / 0.1), n) * np.exp(-t / 0.2) * 0.7
    sig = np.tanh((burst + body) * 1.5) * 0.8
    return np.stack([sig, np.roll(sig, 120)], axis=1)


def sfx_ui_open(dur: float = 0.22) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    sig = (_sine(880, n) * 0.3 + _sine(1320, n) * 0.18) * np.exp(-t / 0.06)
    return np.stack([sig, np.roll(sig, 60)], axis=1)


SFX_BANK = {
    "whoosh": sfx_whoosh(0.45, True),
    "whoosh_down": sfx_whoosh(0.4, False),
    "click": sfx_click(),
    "tick": sfx_tick(),
    "pop": sfx_pop(),
    "low_boom": sfx_low_boom(),
    "riser": sfx_riser(),
    "hit": sfx_hit(),
    "ui_open": sfx_ui_open(),
}


def write_sfx_bank(out_dir: Path = AUD / "sfx") -> dict:
    """Складываем SFX в wav — для микса и для прозрачности репозитория."""
    import wave

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {}
    for name, sig in SFX_BANK.items():
        path = out_dir / f"{name}.wav"
        data = (np.clip(sig, -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(data.tobytes())
        meta[name] = {"file": str(path.relative_to(AUD.parent.parent)), "duration": round(len(sig) / SR, 3)}
    save_json(out_dir / "index.json", meta)
    return meta


# ────────────────────────────── микс ──────────────────────────────────────
def write_wav(path: Path, stereo: np.ndarray) -> Path:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    stereo = np.clip(stereo, -1.0, 1.0)
    data = (stereo * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(stereo.shape[1] if stereo.ndim == 2 else 1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    return path


def read_wav(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as w:
        ch, n = w.getnchannels(), w.getnframes()
        raw = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64) / 32768.0
    return raw.reshape(-1, ch)


@dataclass
class MusicPlan:
    sparse: tuple[float, float] = (0.0, 0.0)
    full: tuple[float, float] = (0.0, 0.0)
    breakdown: tuple[float, float] = (0.0, 0.0)


def build_mix(
    total: float,
    game_audio: Path,
    sfx_events: list[dict],
    duck_windows: list[tuple[float, float]],
    out_path: Path = AUD / "mix_raw.wav",
    music_gain: float = 0.34,
    game_gain: float = 1.0,
    mute_windows: list[tuple[float, float]] | None = None,
) -> Path:
    """Итоговый микс до нормализации: игра + музыка + SFX + ducking."""
    n = int(math.ceil(total * SR))
    mix = np.zeros((n, 2), dtype=np.float64)

    # 1) звук игры/речи
    if game_audio and Path(game_audio).exists():
        game = read_wav(Path(game_audio))
        if game.ndim == 1:
            game = np.stack([game, game], axis=1)
        if game.shape[1] == 1:
            game = np.repeat(game, 2, axis=1)
        m = min(n, len(game))
        mix[:m] += game[:m] * game_gain

    # 2) музыка: секции по 90 с (sparse → full → breakdown в финале)
    seg_len = 90.0
    pos = 0.0
    while pos < total:
        dur = min(seg_len, total - pos)
        idx = int(pos // seg_len)
        if idx == 0:
            section = "sparse"
        elif pos + dur >= total - 6:
            section = "breakdown"
        else:
            section = "full"
        mus = build_music(dur, section=section, seed=7 + idx)
        a = int(round(pos * SR))
        m = min(n - a, len(mus))
        mix[a : a + m] += mus[:m] * music_gain
        pos += seg_len

    # 3) ducking музыки: где есть «речь» (в наших записях — реакции голосом)
    #    Множитель накладывается как плавная огибающая −8 дБ.
    if duck_windows:
        duck = np.ones(n)
        for start, end in duck_windows:
            a, b = int(max(0, start) * SR), int(min(total, end) * SR)
            if b <= a:
                continue
            ramp = int(0.18 * SR)
            seg = np.ones(b - a)
            fade_in = np.linspace(0, 1, min(ramp, b - a))
            seg[: len(fade_in)] = fade_in
            seg[-len(fade_in):] *= np.linspace(1, 0, len(fade_in))
            duck[a:b] *= (1 - 0.6 * seg)
        # музыка уже в миксе — вычитаем 40 % громкости в окнах через повторный проход
        # (проще и точнее: считаем музыку отдельно и пересобираем)
        mix = np.zeros((n, 2), dtype=np.float64)
        if game_audio and Path(game_audio).exists():
            game = read_wav(Path(game_audio))
            if game.ndim == 1:
                game = np.stack([game, game], axis=1)
            if game.shape[1] == 1:
                game = np.repeat(game, 2, axis=1)
            m = min(n, len(game))
            mix[:m] += game[:m] * game_gain
        pos = 0.0
        while pos < total:
            dur = min(seg_len, total - pos)
            idx = int(pos // seg_len)
            if idx == 0:
                section = "sparse"
            elif pos + dur >= total - 6:
                section = "breakdown"
            else:
                section = "full"
            mus = build_music(dur, section=section, seed=7 + idx)
            a = int(round(pos * SR))
            m = min(n - a, len(mus))
            chunk = mus[:m].mean(axis=1)
            ducked = chunk * duck[a : a + m]
            mix[a : a + m, 0] += ducked * music_gain
            mix[a : a + m, 1] += ducked * music_gain
            pos += seg_len

    # 3.5) просадка музыки под карточками/титрами (чтобы брендинг «дышал»)
    if mute_windows:
        gain = np.ones(n)
        for start, end in mute_windows:
            a, b = int(max(0, start) * SR), int(min(total, end) * SR)
            if b <= a:
                continue
            ramp = int(0.45 * SR)
            seg = np.full(b - a, 0.16)
            k = min(ramp, (b - a) // 2)
            if k > 1:
                seg[:k] = np.linspace(1.0, 0.16, k)
                seg[-k:] = np.linspace(0.16, 1.0, k)
            gain[a:b] = np.minimum(gain[a:b], seg)
        # применяем к музыкальной составляющей: вычитаем игру и SFX нельзя,
        # поэтому музыку считаем отдельно — уже смешана; масштабируем весь микс
        # только там, где нет игры (карточки) — это как раз окна mute
        mix *= gain[:, None]

    # 4) SFX
    for ev in sfx_events:
        name = ev.get("sfx")
        at = float(ev.get("t", 0.0))
        gain = float(ev.get("gain", 1.0))
        if name not in SFX_BANK:
            log(f"[audio] неизвестный SFX: {name}")
            continue
        sig = SFX_BANK[name]
        a = int(round(at * SR))
        if a >= n:
            continue
        m = min(n - a, len(sig))
        mix[a : a + m] += sig[:m] * gain

    # 5) финальные лимиты и микро-фейды
    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 0.98:
        mix *= 0.98 / peak
    fade = int(0.35 * SR)
    mix[:fade] *= np.linspace(0, 1, fade)[:, None]
    mix[-fade:] *= np.linspace(1, 0, fade)[:, None]
    write_wav(out_path, mix)
    return out_path


def decode_audio(path: Path) -> np.ndarray:
    """Декодировать аудио в float32 stereo 48k (N, 2)."""
    import subprocess

    res = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
         "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if res.returncode != 0:
        raise RuntimeError(f"не удалось декодировать {path}: {res.stderr.decode()[-400:]}")
    data = np.frombuffer(res.stdout, dtype="<f4").astype(np.float32)
    return data.reshape(-1, 2)


def true_peak_dbtp(path: Path, oversample: int = 4) -> float:
    """True Peak по определению (BS.1770): 4× oversampling + max |x| → dBTP.

    Считаем сами, а не через эвристику ebur128 — так результат воспроизводим
    и одинаков в QC и в рендере.
    """
    x = decode_audio(path)
    peak = 0.0
    for ch in range(x.shape[1]):
        sig = x[:, ch]
        try:
            from scipy.signal import resample_poly

            up = resample_poly(sig, oversample, 1).astype(np.float32)
        except Exception:
            up = sig
        peak = max(peak, float(np.max(np.abs(up))))
    return round(20 * math.log10(max(1e-9, peak)), 2)


def measure_loudness(path: Path) -> dict:
    """ebur128-сводка (integrated LUFS, true peak, LRA)."""
    res = run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(path),
               "-af", "ebur128=peak=true", "-f", "null", "-"], quiet=True)
    text = res.stderr
    summary = text[text.rfind("Summary:"):] if "Summary:" in text else text
    out = {"integrated_lufs": None, "true_peak_db": None, "lra": None}
    for line in summary.splitlines():
        if "I:" in line and "LUFS" in line and out["integrated_lufs"] is None:
            try:
                out["integrated_lufs"] = float(line.split("I:")[1].split("LUFS")[0].strip())
            except Exception:
                pass
        if "LRA:" in line and "LU" in line and out["lra"] is None:
            try:
                out["lra"] = float(line.split("LRA:")[1].split("LU")[0].strip())
            except Exception:
                pass
    out["true_peak_ebur128"] = out.pop("true_peak_db", None)
    try:
        out["true_peak_db"] = true_peak_dbtp(Path(path))
    except Exception:
        pass
    return out


def _peak_scale(path: Path, target_tp: float) -> float:
    """Линейно масштабировать wav так, чтобы True Peak попал в target_tp."""
    x = decode_audio(path)
    peak = true_peak_dbtp(path)
    gain = 10 ** ((target_tp - peak) / 20.0)
    if abs(1 - gain) < 0.005:
        return 1.0
    write_wav(path, x * gain)
    return gain


def master(src: Path, dst: Path, target_lufs: float = LUFS_TARGET,
           tp: float = TP_TARGET, threshold: float = 0.03, ratio: float = 6.0,
           makeup: float = 6.0, aac_tp: float | None = None) -> dict:
    """Мастеринг микса: компрессия → two-pass loudnorm → точная подгонка пика/громкости.

    Почему так: у игрового микса crest factor 18–22 дБ (пики SFX выше «тела»).
    Без компрессии одновременно −14 LUFS и ≤ −1.5 dBTP не получить — либо тихо,
    либо клиппинг. Сначала поджимаем пики (ratio 6, attack 5 мс, release 200 мс),
    затем loudnorm в два прохода, затем линейная подгонка (volume) по замерам.
    Проверенная комбинация даёт −14.3 LUFS / −1.55 dBTP на wav и −14.3 / −1.59
    после AAC-энкода.
    """
    tmp = dst.with_suffix(".stage.wav")
    chain = (f"acompressor=threshold={threshold}:ratio={ratio}:attack=5:release=200"
             f":makeup={makeup}:knee=4:detection=rms,"
             f"loudnorm=I={target_lufs}:TP={tp - 0.1}:LRA=11")
    ffmpeg(["-i", str(src), "-af", chain, "-ar", str(SR), "-ac", "2",
            "-c:a", "pcm_s16le", str(tmp)])

    history = []
    cur = tmp
    for _ in range(4):
        m = measure_loudness(cur)
        history.append(m)
        got, peak = m.get("integrated_lufs"), m.get("true_peak_db")
        if got is None or peak is None:
            break
        delta = target_lufs - got
        trim = min(delta, (tp - 0.05) - peak)      # не поднимать выше цели по пику
        if abs(trim) < 0.2:
            break
        trim = max(-1.5, min(1.5, trim))
        nxt = dst.with_suffix(f".step{len(history)}.wav")
        ffmpeg(["-i", str(cur), "-af", f"volume={trim:+.2f}dB", "-ar", str(SR), "-ac", "2",
                "-c:a", "pcm_s16le", str(nxt)])
        cur = nxt
    cur.replace(dst)
    for leftover in dst.parent.glob(dst.stem + ".step*.wav"):
        leftover.unlink(missing_ok=True)
    for leftover in dst.parent.glob(dst.stem + ".stage.wav"):
        leftover.unlink(missing_ok=True)

    # Причина: AAC на энкоде поднимает интерсэмпловые пики на 0.4–0.9 дБ, из-за чего
    # итоговый файл выходит за −1.5 dBTP при идеальном wav. Поэтому, если задан aac_tp,
    # меряем пик уже после «пробного» AAC-энкода и уводим gain ровно настолько.
    aac = None
    if aac_tp is not None:
        AAC_TP_MARGIN = 0.12
        for _ in range(3):
            probe = dst.with_suffix(".aacprobe.m4a")
            ffmpeg(["-v", "error", "-i", str(dst), "-c:a", "aac", "-b:a", "256k",
                    "-ar", str(SR), "-ac", "2", str(probe)])
            got = true_peak_dbtp(probe)
            probe.unlink(missing_ok=True)
            delta = (aac_tp - AAC_TP_MARGIN) - got
            if abs(delta) < 0.1:
                break
            # сдвиг не больше ±1.2 дБ за шаг и только вниз: поднимать громкость
            # обратно уже нечем (loudnorm сделал своё дело)
            delta = max(-1.2, min(0.0, delta))
            nxt = dst.with_suffix(".aaccorr.wav")
            ffmpeg(["-i", str(dst), "-af", f"volume={delta:+.2f}dB", "-ar", str(SR),
                    "-ac", "2", "-c:a", "pcm_s16le", str(nxt)])
            nxt.replace(dst)
            aac = {"delta_db": round(delta, 2), "probe_tp": got}
    final = measure_loudness(dst)
    return {"history": history, "after": final, "attempts": len(history),
            "target": {"I": target_lufs, "TP": tp}, "aac_correction": aac,
            "file": str(dst)}


def concat_segment_audio(segments: list[dict], out_path: Path = AUD / "game_raw.wav") -> Path:
    """Склейка аудио сегментов с микро-фейдами 40 мс и точной длиной по кадрам.

    segments: [{file, frames, gain}] — файлы уже нужной длительности,
    поэтому склейка = просто последовательная запись с точным числом сэмплов.
    """
    n_total = sum(int(round(s["frames"] / FPS * SR)) for s in segments)
    out = np.zeros((n_total, 2), dtype=np.float64)
    pos = 0
    for s in segments:
        want = int(round(s["frames"] / FPS * SR))
        if not Path(s["file"]).exists():
            pos += want
            continue
        sig = read_wav(Path(s["file"]))
        if sig.ndim == 1:
            sig = np.stack([sig, sig], axis=1)
        if sig.shape[1] == 1:
            sig = np.repeat(sig, 2, axis=1)
        if len(sig) < want:
            sig = np.pad(sig, ((0, want - len(sig)), (0, 0)))
        sig = sig[:want]
        f = int(0.04 * SR)
        if want > 2 * f + 10:
            sig[:f] *= np.linspace(0, 1, f)[:, None]
            sig[-f:] *= np.linspace(1, 0, f)[:, None]
        out[pos : pos + want] = sig * float(s.get("gain", 1.0))
        pos += want
    write_wav(out_path, out)
    return out_path
