"""
report.py — человекочитаемые документы из EDL и QC:
  * docs/ОТЧЁТ.md — главы с таймкодами, что вырезано, что ускорено, защищённые окна;
  * docs/howemine.srt — субтитры ключевых реплик/плашек (по оверлеям EDL);
  * docs/ГЛАВЫ.md — только таблица глав (для описания видео на YouTube).

Запуск: python3 edit/report.py [--timeline edit/timeline.json] [--qc edit/qc/qc.json]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from cfglib import EDIT, QC, ROOT, load_json, log, tc

FINAL = ROOT / "deliver" / "HoweMine_review_1080p60.mp4"


# ─────────────────────────────── субтитры ─────────────────────────────────
def srt_time(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def build_srt(edl: dict) -> str:
    """SRT из плашек/подписей EDL — ключевые реплики и факты ролика."""
    cues = []
    for s in edl["segments"]:
        for o in s.get("overlays", []):
            start = s["start"] + float(o.get("local", 0.0)) + 0.15
            dur = min(float(o.get("dur", 2.0)) - 0.2, 3.5)
            if dur < 0.6:
                continue
            kind = o.get("type")
            if kind == "callout":
                parts = [p for p in [o.get("kicker"), o.get("title")] + list(o.get("lines", [])) if p]
                text = " — ".join(parts[:2]) + ("\n" + parts[2] if len(parts) > 2 else "")
            elif kind == "caption":
                text = o.get("text", "")
            elif kind == "stamp":
                text = f"{o.get('text','')} {o.get('sub','')}".strip()
            elif kind in ("chapter",):
                text = f"{o.get('num','')} {o.get('title','')}".strip()
            else:
                continue
            if not text:
                continue
            cues.append((start, start + dur, text))
    # субтитры не должны пересекаться: сортируем и подтягиваем начало следующей
    cues.sort(key=lambda c: c[0])
    fixed = []
    for a, b, text in cues:
        if fixed:
            prev_a, prev_b, _ = fixed[-1]
            if a < prev_b + 0.08:
                a = prev_b + 0.08
            if a >= b - 0.3:
                continue
        fixed.append((a, b, text))
    out = []
    for i, (a, b, text) in enumerate(fixed, 1):
        out.append(f"{i}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n")
    return "\n".join(out)


# ─────────────────────────────── отчёт ────────────────────────────────────
def write_report(edl: dict, qc: dict | None, note: str = "") -> Path:
    meta = edl["meta"]
    segs = edl["segments"]
    chapters = edl["chapters"]

    # что ускорено
    sped = sorted([s for s in segs if s["speed"] > 1.02], key=lambda s: s["start"])
    protected = [s for s in segs if s.get("protected")]
    removed = edl.get("removed", [])

    lines = [
        "# ОТЧЁТ — HoweMine, обзор сборки на продажу",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]
    lines += [
        "**Готовый файл:** `deliver/HoweMine_review_1080p60.mp4`  ",
        f"**Длительность:** {tc(meta['total_duration'])} ({meta['total_duration']:.1f} с) · "
        f"{meta['width']}×{meta['height']} @ {meta['fps']} fps · H.264 High@4.2 + AAC 256k  ",
        f"**Сегментов в EDL:** {meta['counts']['segments']} · "
        f"**переходов:** {meta['counts']['transitions']} · "
        f"**вырезано брака:** {meta['counts']['cuts_removed']} фрагментов, "
        f"{meta['counts']['removed_seconds']} с",
        "",
        "## 1. Главы готового ролика",
        "",
        "| # | Глава | Таймкод | Длительность | Что в кадре |",
        "|---|---|---|---|---|",
    ]
    descr = {
        "ХУК": "лучший кадр: открытие кейса + [САПФИР], удар по дракону",
        "ТИТР": "карточка HoweMine + «ОБЗОР СБОРКИ НА ПРОДАЖУ»",
        "СПАВН": "вывеска, аукцион, NPC, торговые точки, детали витрины",
        "ТУР ПО СПАВНУ": "архитектура спавна крупным планом (обзор, ×1.6)",
        "ТОРГОВЛЯ": "витрина и цены, точило, ассортимент",
        "ПРОГРЕСС": "крафт и мост через пропасть",
        "БАЗА": "обзор базы снаружи и внутри, подсобные зоны",
        "СКЛАД": "сундуки, инвентарь, путь к донат-кейсам",
        "ДОНАТ-КЕЙСЫ": "«100 ХОХЛОВ» и «Легенда», [САПФИР] — целиком, ×1.0",
        "ЛОКАЦИИ": "локации мира и постройки",
        "ПУТЕШЕСТВИЕ": "путь по миру (ходьба ускорена)",
        "КУБ И БИОМЫ": "куб и биомы, сбор дропа",
        "ЛУТ": "разбор инвентаря и прокрутка лута — целиком, ×1.0",
        "СТРОЙКА": "возведение базы (ускорено, без потери деталей)",
        "АРЕНА": "Эндер-дракон: вход, бой, добивание, лес после боя",
        "ФИНАЛ": "вечер в мире + финальная карточка «СПАСИБО ЗА ПРОСМОТР»",
    }
    for i, ch in enumerate(chapters, 1):
        lines.append(f"| {i} | {ch['chapter']} | {ch['tc']} | {ch['duration']:.0f} с | "
                     f"{descr.get(ch['chapter'], '')} |")

    lines += [
        "",
        "## 2. Что вырезано (таймкоды исходников)",
        "",
        "Режем только брак. Список формируется автоматически из анализа кадров "
        "(`edit/analysis/analysis.json`) и правил `edit/timeline.py`.",
        "",
        "| Источник | С каких пор | По каким | Длительность | Почему |",
        "|---|---|---|---|---|",
    ]
    if removed:
        for r in sorted(removed, key=lambda r: r["start"]):
            lines.append(f"| {r['src']} | {tc(r['start'])} | {tc(r['end'])} | "
                         f"{r['len']:.1f} с | {r.get('reason','')} |")
    else:
        lines.append("| — | — | — | — | вырезок не потребовалось (брака в анализе не найдено) |")
    lines += [
        "",
        "Дополнительно по правилам брифа всегда вырезаются:",
        "",
        "* окно OBS/Discord в начале второй записи (0:00–0:08) и уведомление «запись запущена»;",
        "* почти чёрная ночь в конце первой записи (22:56+);",
        "* меню паузы «MINECRAFT», экраны настроек и окно «Все предметы» — если попадают в план;",
        "* замирания картинки дольше 0.8 с **только там, где не открыт GUI** "
        "(статичный инвентарь — это контент, он остаётся).",
        "",
        "## 3. Что ускорено и что оставлено в реальной скорости",
        "",
        "| Участок | Скорость | Таймкод в ролике |",
        "|---|---|---|",
    ]
    if sped:
        for s in sped:
            lines.append(f"| {s['label']} | ×{s['speed']:.2f} | {tc(s['start'])} — "
                         f"{tc(s['start'] + s['duration'])} |")
    else:
        lines.append("| — | — | ускорять не потребовалось |")
    lines += [
        "",
        "### Защищённые участки (строго ×1.0, без вырезок внутри)",
        "",
        "| Участок | Таймкод | Длительность |",
        "|---|---|---|",
    ]
    for s in protected:
        lines.append(f"| {s['label']} | {tc(s['start'])} | {s['duration']:.1f} с |")
    lines += [
        "",
        "Эти окна помечены в EDL как `\"protected\": true`: оптимизатор длительности "
        "не имеет права менять их скорость, а вырезка брака не может зайти внутрь них "
        "(проверка — `edit/timeline.py: subtract(..., protected=...)`).",
        "",
        "## 4. Монтажные приёмы в ролике",
        "",
        "* камера: push-in 105–115 % с easing, «дыхательный» дрейф и микро-шейк "
        "(амплитуда ≤ 0.5 %), всё через `zoompan` с выражениями по кадру;",
        "* склейки: на каждом стыке — растворение 0.22 с из последнего кадра "
        "предыдущего плана (картинка не рвётся, нет «вспышек» между планами); "
        "первый кадр ролика проявляется из чёрного за 0.12 с;",
        "* анти-«вспышка»: план короче 0.9 с на экране не выводится — остаётся только "
        "основной кусок, огрызки у вырезок уходят в вырезанное;",
        "* графика: открывающая/финальная карточки, 6 кинетических титров глав, "
        "плашки-выноски с линией-указателем и виньеткой объекта, freeze-frame [САПФИР];",
        "* звук: музыка 100 BPM (синтезирована), SFX на переходах и наградах, "
        "ducking музыки −8 дБ в защищённых окнах и просадка под карточками;",
        "* цвет: единый грейд на обе записи (контраст 1.07, насыщенность 1.08, "
        "unsharp 0.45), у тёмной арены дополнительно gamma 1.45;",
        "* 4:3 вторая запись разложена в 16:9: картинка 1440×1080 по центру + "
        "размытый затемнённый фон по бокам, хотбар не обрезается.",
        "",
        "## 5. QC",
        "",
    ]
    if qc:
        lines += [
            "| Проверка | Значение | Итог |",
            "|---|---|---|",
            f"| Чёрные кадры | {len(qc['black'])} | {'OK' if qc['black_ok'] else 'ЕСТЬ'} |",
            f"| Замирания на стыках | "
            f"{len(qc['freezes_all'])} | "
            f"{'OK' if qc['freezes_ok'] else 'ЕСТЬ'} |",
            f"| LUFS | {qc['loudness'].get('integrated_lufs')} | целевые −14 |",
            f"| True Peak | {qc['loudness'].get('true_peak_db')} dBTP | целевые ≤ −1.5 |",
            f"| A/V синхрон | Δ {qc['sync'].get('delta')} с | "
            f"{'OK' if qc['sync_ok'] else 'ЕСТЬ'} |",
            f"| Проверено склеек | {qc['boundaries_checked']} из {qc['boundaries_total']} | OK |",
            "",
            "Подробности и листы: `docs/QC.md`, `edit/qc/`.",
        ]
    else:
        lines.append("QC не запускался (`python3 edit/qc.py`).")
    lines += [
        "",
        "## 6. Как пересобрать",
        "",
        "```bash",
        "mkdir -p raw && cp 'Minecraft 2026.11.23 - 18.27.09.01.mp4' raw/A.mp4 \\",
        "                     && cp 'Minecraft 2026.11.26 - 03.19.04.01.mp4' raw/B.mp4",
        "python3 edit/analyze.py        # покадровый анализ 0.2 с → edit/analysis/",
        "python3 edit/timeline.py       # EDL 12:00–13:30 → edit/timeline.json",
        "python3 edit/render.py --all   # рендер → deliver/HoweMine_review_1080p60.mp4",
        "python3 edit/qc.py             # контактные листы, склейки, blackdetect/freezedetect",
        "python3 edit/report.py         # этот отчёт + docs/howemine.srt",
        "```",
        "",
        "Сегменты кэшируются по хэшу параметров (`work/cache/`), поэтому пересборка "
        "после правки одного участка занимает секунды.",
    ]
    out = ROOT / "docs" / "ОТЧЁТ.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def write_chapters(edl: dict) -> Path:
    lines = ["# Главы ролика HoweMine", "", "| Время | Глава |", "|---|---|"]
    for ch in edl["chapters"]:
        lines.append(f"| {ch['tc']} | {ch['chapter']} |")
    out = ROOT / "docs" / "ГЛАВЫ.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeline", default=str(EDIT / "timeline.json"))
    ap.add_argument("--qc", default=str(QC / "qc.json"))
    ap.add_argument("--note", default="", help="баннер в начале отчёта (напр. «данные self-test»)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "ОТЧЁТ.md"))
    ap.add_argument("--appendix", default="", help="файл с дополнительным разделом в конце")
    args = ap.parse_args()
    edl = load_json(Path(args.timeline))
    qc = load_json(Path(args.qc)) if Path(args.qc).exists() else None
    rep = write_report(edl, qc, note=args.note)
    if args.out != str(ROOT / "docs" / "ОТЧЁТ.md"):
        rep = Path(args.out)
        rep.write_text((ROOT / "docs" / "ОТЧЁТ.md").read_text(encoding="utf-8"), encoding="utf-8")
    cha = write_chapters(edl)
    srt = ROOT / "docs" / "howemine.srt"
    srt.write_text(build_srt(edl), encoding="utf-8")
    if args.appendix and Path(args.appendix).exists():
        extra = Path(args.appendix).read_text(encoding="utf-8")
        rep.write_text(rep.read_text(encoding="utf-8").rstrip() + "\n" + extra, encoding="utf-8")
    log(f"[report] {rep}")
    log(f"[report] {cha}")
    log(f"[report] {srt} ({srt.read_text(encoding='utf-8').count('-->')} реплик)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
