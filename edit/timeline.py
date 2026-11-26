"""
timeline.py — единственный источник правды монтажа: edit/timeline.json.

Строит полный EDL ролика HoweMine (10–15 мин, цель 12:00–13:30):
  * режет только брак из analyze.json (замирания без GUI, меню, OBS-окно);
  * НИКОГДА не трогает окна с анимациями кейсов, выдачей награды и прокруткой лута
    (они помечены protected и идут строго ×1.0 с запасом 0.3 с);
  * задаёт темп (speed), движение камеры (zoompan), переходы, оверлеи, SFX;
  * подгоняет итоговую длительность в коридор 10:00–15:00, изменяя скорость только
    у «гибких» (flex) сегментов — защищённые не трогает никогда;
  * считает абсолютные таймкоды глав, список SFX, окна ducking для музыки.

Запуск:  python3 edit/timeline.py [--target 750] [--no-fit]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from cfglib import (
    ANALYSIS,
    FPS,
    EDIT,
    SRC_A,
    SRC_B,
    log,
    load_json,
    save_json,
    tc,
)

def parse_tc(value) -> float:
    """'11:45.8' / '705.8' / 705.8 → секунды."""
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip()
    if ":" in txt:
        parts = [float(p) for p in txt.split(":")]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    return float(txt)


# ─────────────────────────── соответствие координат ───────────────────────
# Источник A: crop=1822:1025:49:55 → scale 1920x1080.
CROP_A = (1822, 1025, 49, 55)
# Источник B: картинка 1280x960 → 1440x1080 по центру (240 px полей с боков).
SCALE_B = 1440 / 1280


def ax(sx: float, sy: float) -> tuple[int, int]:
    """Координаты источника A → координаты кадра 1920x1080."""
    cw, ch, cx, cy = CROP_A
    return (int((sx - cx) * 1920 / cw), int((sy - cy) * 1080 / ch))


def bx(sx: float, sy: float) -> tuple[int, int]:
    """Координаты источника B → координаты кадра 1920x1080."""
    return (int(sx * SCALE_B + (1920 - 1280 * SCALE_B) / 2), int(sy * SCALE_B))


# ─────────────────────────── пресеты камеры ───────────────────────────────
def cam_push(z0=1.0, z1=1.07, fx=0.5, fy=0.5):
    return {"type": "push", "from": z0, "to": z1, "fx": fx, "fy": fy}


def cam_drift(z=1.045, amp=0.012, freq=0.18):
    return {"type": "drift", "zoom": z, "amp": amp, "freq": freq}


def cam_shake(z=1.03, amp=0.004, freq=1.7):
    return {"type": "shake", "zoom": z, "amp": amp, "freq": freq}


def cam_focus(fx, fy, z0=1.02, z1=1.16):
    return {"type": "push", "from": z0, "to": z1, "fx": fx, "fy": fy}


CAM_DYNAMIC = cam_push(1.0, 1.06, 0.5, 0.55)
CAM_OVERVIEW = cam_drift(1.05, 0.014, 0.16)
CAM_ACTION = cam_shake(1.035, 0.0045, 1.9)


# ─────────────────────────── плашки и подписи ─────────────────────────────
def callout(kicker, title, lines, rarity="epic", at="A", sx=960, sy=560, side="left",
            dur=3.2, local=0.35, big=False, rect_pad=120):
    """Плашка-выноска с указателем на предмет. Координаты — в пикселях источника."""
    px, py = (ax(sx, sy) if at == "A" else bx(sx, sy))
    half = rect_pad
    return {
        "type": "callout",
        "kicker": kicker,
        "title": title,
        "lines": lines,
        "rarity": rarity,
        "local": local,
        "dur": dur,
        "big": big,
        "px": px,
        "py": py,
        "rect": [px - half, py - half, px + half, py + half],
        "x": 120 if side == "left" else 1160,
        "y": 210 if side == "left" else 250,
    }


def caption(text, dur=2.6, local=0.4, color="white", align="center", y=940, size=40):
    return {"type": "caption", "text": text, "local": local, "dur": dur,
            "color": color, "align": align, "y": y, "size": size}


def chapter(num, title, sub="", dur=1.1, local=0.15):
    return {"type": "chapter", "num": num, "title": title, "sub": sub,
            "local": local, "dur": dur, "x": 110, "y": 700}


# ─────────────────────────── сегменты ─────────────────────────────────────
def seg(src, t0, t1, speed=1.0, label="", chapter="", flex=True, camera=None,
        trans_in="none", overlays=None, sfx=None, gain=1.0, note="", protected=False,
        id=None):
    return {
        "id": id,
        "src": src,
        "in": round(float(t0), 3),
        "out": round(float(t1), 3),
        "speed": float(speed),
        "label": label,
        "chapter": chapter,
        "flex": bool(flex),
        "camera": camera or CAM_DYNAMIC,
        "trans_in": trans_in,
        "overlays": overlays or [],
        "sfx": sfx or [],
        "gain": gain,
        "note": note,
        "protected": bool(protected),
    }


def sfx(name, local, gain=1.0):
    return {"sfx": name, "local": round(float(local), 3), "gain": gain}


# ─────────────────────────── карта ролика ─────────────────────────────────
def raw_plan() -> dict:
    """Полный план до вырезки брака и подгонки длительности."""
    S: list[dict] = []
    chapters: list[dict] = []

    # ── 0. ХУК: лучший кадр ролика + резкий титр (без «здравствуйте»)
    S.append(seg("A", 11 * 60 + 45.4, 11 * 60 + 46.8, 1.0, "хук · открытие кейса",
                 "ХУК", flex=False, camera=cam_focus(0.5, 0.45, 1.0, 1.12),
                 trans_in="flash", overlays=[
                     {"type": "bars", "local": 0.0, "dur": 1.4},
                     {"type": "caption", "text": "СМОТРИ ДО КОНЦА", "local": 0.15, "dur": 1.2,
                      "color": "brand", "y": 760, "size": 54},
                 ], sfx=[sfx("low_boom", 0.0, 0.9), sfx("riser", 0.0, 0.35)],
                 note="вау-кадр на первом кадре"))
    S.append(seg("A", 12 * 60 + 1.2, 12 * 60 + 2.4, 1.0, "хук · [САПФИР]",
                 "ХУК", flex=False, camera=cam_focus(0.5, 0.5, 1.02, 1.14),
                 trans_in="whip", overlays=[
                     {"type": "stamp", "text": "[САПФИР]", "sub": "на 7 дней", "local": 0.05, "dur": 1.15},
                 ], sfx=[sfx("pop", 0.1, 0.8)]))
    S.append(seg("B", 50.0, 56.5, 1.0, "хук · арена, удар по дракону",
                 "ХУК", flex=False, camera=cam_shake(1.05, 0.006, 2.2),
                 trans_in="zoom_blur", overlays=[
                     {"type": "caption", "text": "ОБЗОР СБОРКИ HOWEMINE", "local": 0.5, "dur": 3.4,
                      "color": "white", "y": 930, "size": 46},
                 ], sfx=[sfx("hit", 0.0, 0.85), sfx("whoosh", 0.0, 0.5)]))

    # ── 1. Открывающая карточка
    S.append({
        "id": "card_open", "src": "card", "in": 0.0, "out": 2.8, "speed": 1.0,
        "label": "открывающая карточка", "chapter": "ТИТР", "flex": False, "camera": None,
        "trans_in": "none", "sfx": [sfx("whoosh", 0.05, 0.7), sfx("click", 0.55, 0.5)],
        "overlays": [{"type": "open_card", "local": 0.0, "dur": 2.8,
                      "title": "HoweMine", "sub": "ОБЗОР СБОРКИ НА ПРОДАЖУ",
                      "bg": "still:A@11:45.8"}],
        "gain": 1.0, "note": "брендинг: HoweMine #FF7A7A + линия + подзаголовок", "protected": False,
    })

    # ── 2. СПАВН: площадка, аукцион, NPC
    chapters.append({"num": "01", "title": "СПАВН", "start_label": "спавн: вывеска и аукцион"})
    S.append(seg("A", 2.0, 30.0, 1.15, "спавн · вывеска HoweMine, аукцион", "СПАВН",
                 camera=cam_drift(1.05, 0.012, 0.14), trans_in="whip", overlays=[
                     chapter("01", "СПАВН", "витрина · NPC · торговые точки"),
                     callout("ЛОКАЦИЯ", "Спавн HoweMine", ["Аукцион и торговые точки", "Витрина сборки"],
                             "rare", "A", 700, 480, "left", 3.4, 0.95),
                 ], sfx=[sfx("whoosh", 0.0, 0.55), sfx("click", 0.95, 0.4)]))
    S.append(seg("A", 32.0, 45.0, 1.4, "спавн · NPC и первые цены", "СПАВН",
                 camera=cam_push(1.0, 1.05, 0.55, 0.5), overlays=[
                     caption("спавн: аукцион, NPC, торговые точки", 2.4, 0.5),
                 ], sfx=[sfx("click", 0.5, 0.35)]))
    S.append(seg("A", 45.0, 2 * 60 + 30.0, 1.9, "спавн · детали витрины и NPC", "СПАВН",
                 camera=cam_drift(1.06, 0.015, 0.12), trans_in="whip",
                 overlays=[caption("витрина сборки: детали и оформление", 3.0, 1.0)],
                 sfx=[sfx("whoosh", 0.0, 0.45)]))

    # ── 3. Тур по спавну (обзор)
    S.append(seg("A", 10 * 60 + 0.0, 10 * 60 + 38.0, 1.6, "тур по спавну · часть 1", "ТУР ПО СПАВНУ",
                 camera=cam_drift(1.06, 0.016, 0.13), trans_in="none"))
    S.append(seg("A", 10 * 60 + 44.0, 11 * 60 + 12.0, 1.6, "тур по спавну · часть 2", "ТУР ПО СПАВНУ",
                 camera=cam_drift(1.06, 0.016, 0.11), trans_in="whip",
                 sfx=[sfx("whoosh", 0.0, 0.5)]))
    S.append(seg("A", 11 * 60 + 18.0, 11 * 60 + 36.0, 1.5, "тур по спавну · часть 3", "ТУР ПО СПАВНУ",
                 camera=cam_push(1.02, 1.08, 0.45, 0.5),
                 overlays=[caption("архитектура спавна крупным планом", 2.6, 0.6)]))

    # ── 4. ТОРГОВЛЯ: цены, точило
    chapters.append({"num": "02", "title": "ТОРГОВЛЯ", "start_label": "торговля: цены и точило"})
    S.append(seg("A", 2 * 60 + 30.0, 2 * 60 + 57.0, 1.0, "торговля · витрина и цены", "ТОРГОВЛЯ",
                 camera=cam_push(1.0, 1.07, 0.5, 0.45), trans_in="zoom_blur", overlays=[
                     chapter("02", "ТОРГОВЛЯ", "цены · витрина · точило"),
                     callout("ЭКОНОМИКА", "Витрина торговца", ["Цены на ресурсы и снаряжение", "Покупка/продажа"],
                             "gold", "A", 780, 500, "left", 3.4, 1.0),
                 ], sfx=[sfx("whoosh", 0.0, 0.55), sfx("click", 1.0, 0.45)]))
    S.append(seg("A", 2 * 60 + 57.0, 3 * 60 + 17.0, 1.0, "торговля · точило", "ТОРГОВЛЯ",
                 camera=cam_push(1.0, 1.09, 0.55, 0.5), overlays=[
                     callout("ИНСТРУМЕНТ", "Точило", ["Зачарования и ремонт", "Ключевая механика прогрессии"],
                             "uncommon", "A", 960, 620, "right", 3.0, 0.4),
                 ], sfx=[sfx("click", 0.4, 0.4)]))
    S.append(seg("A", 3 * 60 + 17.0, 4 * 60 + 30.0, 1.9, "торговля · ассортимент и цены", "ТОРГОВЛЯ",
                 camera=cam_drift(1.05, 0.014, 0.14), trans_in="none",
                 overlays=[caption("ассортимент: что и за сколько продаётся", 3.0, 1.0)]))

    # ── 5. КРАФТ + МОСТ
    S.append(seg("A", 6 * 60 + 0.0, 6 * 60 + 12.0, 1.0, "крафт", "ПРОГРЕСС",
                 camera=cam_push(1.0, 1.08, 0.5, 0.5), trans_in="none", overlays=[
                     caption("крафт · только реальные механики", 2.4, 0.3),
                 ], sfx=[sfx("ui_open", 0.15, 0.4)]))
    S.append(seg("A", 6 * 60 + 12.0, 6 * 60 + 40.0, 1.15, "мост через пропасть", "ПРОГРЕСС",
                 camera=cam_drift(1.05, 0.013, 0.15), trans_in="whip",
                 sfx=[sfx("whoosh", 0.0, 0.5)]))

    # ── 6. БАЗА (обзор)

    S.append(seg("A", 6 * 60 + 40.0, 7 * 60 + 23.0, 1.55, "база · обзор снаружи и внутри", "БАЗА",
                 camera=cam_drift(1.06, 0.015, 0.12), overlays=[
                     callout("ПОСТРОЙКА", "База игрока", ["Обзор снаружи и внутри", "Склад, фермы, крафт-зона"],
                             "rare", "A", 900, 520, "left", 3.4, 1.5),
                 ], sfx=[sfx("click", 1.5, 0.4)]))
    S.append(seg("A", 7 * 60 + 23.0, 8 * 60 + 0.0, 1.9, "база · детали и подсобные зоны", "БАЗА",
                 camera=cam_push(1.0, 1.06, 0.5, 0.5), trans_in="none"))

    # ── 7. СУНДУКИ И ИНВЕНТАРЬ
    S.append(seg("A", 8 * 60 + 0.0, 8 * 60 + 44.0, 1.6, "сундуки · инвентарь", "СКЛАД",
                 camera=cam_push(1.0, 1.05, 0.5, 0.5), trans_in="zoom_blur", overlays=[
                     caption("статика инвентаря — это контент, а не брак", 2.8, 1.2),
                 ], sfx=[sfx("whoosh", 0.0, 0.45), sfx("ui_open", 1.2, 0.5)]))

    S.append(seg("A", 8 * 60 + 44.0, 11 * 60 + 44.0, 2.1, "склад · путь к донат-кейсам", "СКЛАД",
                 camera=cam_drift(1.05, 0.013, 0.15), trans_in="none",
                 overlays=[caption("подготовка к открытию донат-кейсов", 3.0, 1.2)]))

    # ── 8. ДОНАТ-КЕЙСЫ (ключевой вау-момент, строго ×1.0, защищено)
    chapters.append({"num": "03", "title": "ДОНАТ-КЕЙСЫ", "start_label": "донат-кейсы"})
    S.append(seg("A", 11 * 60 + 44.0, 12 * 60 + 10.0, 1.0, "донат-кейсы · «100 ХОХЛОВ» + «Легенда»",
                 "ДОНАТ-КЕЙСЫ", flex=False, protected=True, camera=cam_push(1.0, 1.05, 0.5, 0.5),
                 trans_in="flash", overlays=[
                     chapter("03", "ДОНАТ-КЕЙСЫ", "открытие целиком · ×1.0"),
                     callout("ДОНАТ-КЕЙС", "«100 ХОХЛОВ»", ["Открытие показано целиком", "Реальная скорость, без ускорения"],
                             "legendary", "A", 960, 560, "left", 3.2, 1.4, big=True),
                     callout("НАГРАДА", "[САПФИР] на 7 дней", ["Ключевой дроп ролика", "С запасом 0.3 с до и после"],
                             "sapphire", "A", 960, 520, "right", 3.0, 14.5, big=True),
                 ], sfx=[sfx("riser", 0.0, 0.7), sfx("low_boom", 1.5, 0.9),
                         sfx("pop", 14.6, 0.8), sfx("low_boom", 15.2, 0.7)],
                 note="ГЛАВНОЕ: ни одного кадра не вырезано; анимация кейса + выдача награды целиком"))
    # freeze-frame с подписью сразу ПОСЛЕ анимации (вставка, не замена)
    S.append({
        "id": "freeze_sapphire", "src": "freeze", "in": 0.0, "out": 1.0, "speed": 1.0,
        "label": "freeze-frame · [САПФИР]", "chapter": "ДОНАТ-КЕЙСЫ", "flex": False,
        "camera": None, "trans_in": "flash", "gain": 1.0, "protected": False,
        "frame_src": "A", "frame_t": 12 * 60 + 9.4,
        "overlays": [{"type": "stamp", "text": "[САПФИР]", "sub": "на 7 дней · легендарный дроп",
                      "local": 0.0, "dur": 1.0}],
        "sfx": [sfx("pop", 0.0, 0.7), sfx("click", 0.4, 0.4)],
        "note": "freeze-frame вставлен после анимации, чтобы её не рвать",
    })

    # ── 9. ЛОКАЦИИ
    S.append(seg("A", 12 * 60 + 30.0, 14 * 60 + 0.0, 1.0, "локации · часть 1", "ЛОКАЦИИ",
                 camera=cam_drift(1.05, 0.013, 0.14), trans_in="whip",
                 overlays=[callout("МИР", "Локации сборки", ["Биомы и постройки мира", "Единый грейд с обеими записями"],
                                   "uncommon", "A", 860, 500, "left", 3.0, 1.2)],
                 sfx=[sfx("whoosh", 0.0, 0.45)]))
    S.append(seg("A", 14 * 60 + 0.0, 14 * 60 + 28.0, 1.0, "локации · часть 2", "ЛОКАЦИИ",
                 camera=cam_push(1.0, 1.06, 0.5, 0.5)))

    # ── 10. ПУТЕШЕСТВИЕ
    S.append(seg("A", 14 * 60 + 28.0, 15 * 60 + 30.0, 1.7, "путешествие · часть 1", "ПУТЕШЕСТВИЕ",
                 camera=cam_drift(1.05, 0.014, 0.12), trans_in="none",
                 overlays=[caption("путешествие по миру · ходьба ускорена ×1.5", 3.0, 1.0)]))
    S.append(seg("A", 15 * 60 + 30.0, 16 * 60 + 10.0, 1.7, "путешествие · часть 2", "ПУТЕШЕСТВИЕ",
                 camera=cam_push(1.0, 1.06, 0.45, 0.55), trans_in="zoom_blur",
                 sfx=[sfx("whoosh", 0.0, 0.5)]))
    S.append(seg("A", 16 * 60 + 10.0, 17 * 60 + 12.0, 1.85, "путешествие · часть 3", "ПУТЕШЕСТВИЕ",
                 camera=cam_drift(1.05, 0.012, 0.16)))

    # ── 11. КУБ И БИОМЫ
    S.append(seg("A", 17 * 60 + 12.0, 17 * 60 + 50.0, 1.6, "куб · часть 1", "КУБ И БИОМЫ",
                 camera=cam_drift(1.05, 0.014, 0.13), trans_in="whip",
                 overlays=[caption("куб и биомы — витрина мира", 2.6, 0.8)],
                 sfx=[sfx("whoosh", 0.0, 0.45)]))
    S.append(seg("A", 17 * 60 + 50.0, 18 * 60 + 20.0, 1.9, "куб · часть 2", "КУБ И БИОМЫ",
                 camera=cam_push(1.0, 1.07, 0.55, 0.45)))

    S.append(seg("A", 18 * 60 + 20.0, 19 * 60 + 24.0, 2.3, "переход к луту · сбор дропа", "КУБ И БИОМЫ",
                 camera=cam_drift(1.05, 0.014, 0.16), trans_in="none",
                 overlays=[caption("собираем лут — дальше главное", 2.8, 1.0)]))

    # ── 12. ЛУТ (главное: реальная скорость, без ускорения, защищено)
    chapters.append({"num": "04", "title": "ЛУТ", "start_label": "лут и прокрутка дропа"})
    S.append(seg("A", 19 * 60 + 24.0, 20 * 60 + 40.0, 1.0, "лут · разбор инвентаря и прокрутка",
                 "ЛУТ", flex=False, protected=True, camera=cam_push(1.0, 1.04, 0.5, 0.5),
                 trans_in="zoom_blur", overlays=[
                     chapter("04", "ЛУТ", "реальная скорость · без ускорения"),
                     callout("ЛУТ", "Разбор дропа", ["Прокрутка лута — целиком", "×1.0, с запасом 0.3 с"],
                             "legendary", "A", 960, 560, "left", 3.6, 1.2, big=True),
                     callout("ИНВЕНТАРЬ", "Что выпало", ["Каждый предмет в кадре", "Без ускорения и сокращений"],
                             "rare", "A", 960, 600, "right", 3.2, 26.0),
                     callout("ДРОП", "Появление предмета", ["Момент выдачи — полностью", "0.3 с запаса до и после"],
                             "sapphire", "A", 960, 540, "left", 3.0, 48.0),
                 ], sfx=[sfx("whoosh", 0.0, 0.5), sfx("pop", 12.0, 0.6), sfx("click", 26.0, 0.4),
                         sfx("pop", 48.0, 0.6)],
                 note="ГЛАВНОЕ: прокрутка лута и появление предметов показаны целиком в реальном темпе"))

    # ── 13. СТРОЙКА
    chapters.append({"num": "05", "title": "СТРОЙКА", "start_label": "стройка"})
    S.append(seg("A", 20 * 60 + 46.0, 21 * 60 + 20.0, 1.7, "стройка · часть 1", "СТРОЙКА",
                 camera=cam_drift(1.05, 0.013, 0.14), trans_in="whip", overlays=[
                     chapter("05", "СТРОЙКА", "ускорение ×1.3 — без потери деталей"),
                     callout("СТРОЙКА", "Возведение базы", ["Блок за блоком, без мусорных кадров", "Медленные участки ускорены"],
                             "uncommon", "A", 900, 520, "left", 3.2, 1.4),
                 ], sfx=[sfx("whoosh", 0.0, 0.5), sfx("tick", 1.4, 0.35)]))
    S.append(seg("A", 21 * 60 + 20.0, 22 * 60 + 6.0, 1.75, "стройка · часть 2", "СТРОЙКА",
                 camera=cam_push(1.0, 1.06, 0.5, 0.5)))

    # ── 14. АРЕНА (источник B)
    chapters.append({"num": "06", "title": "АРЕНА", "start_label": "арена и Эндер-дракон"})
    S.append(seg("B", 9.0, 32.0, 1.0, "арена · вход и первый бой", "АРЕНА", flex=False,
                 camera=cam_shake(1.045, 0.005, 2.0), trans_in="flash", overlays=[
                     chapter("06", "АРЕНА", "Эндер-дракон · 4:3 → 16:9"),
                     callout("БОСС", "Эндер-дракон", ["Арена из второй записи", "Картинка 4:3 вписана в 16:9"],
                             "mythic", "B", 640, 480, "left", 3.2, 1.2),
                 ], sfx=[sfx("hit", 0.0, 0.8), sfx("low_boom", 1.2, 0.8)]))
    S.append(seg("B", 35.0, 69.0, 1.0, "арена · добивание дракона", "АРЕНА", flex=False,
                 camera=cam_shake(1.05, 0.006, 2.4), trans_in="none",
                 sfx=[sfx("hit", 4.0, 0.7), sfx("low_boom", 12.0, 0.85)]))
    S.append(seg("B", 71.0, 82.0, 1.0, "финал · лес после боя", "АРЕНА", flex=False,
                 camera=cam_drift(1.05, 0.015, 0.12),
                 overlays=[caption("после боя: мир и биомы", 2.4, 1.2)]))

    # ── 15. Финал: ночь не используем (почти чёрный экран), берём вечер
    S.append(seg("A", 22 * 60 + 6.0, 22 * 60 + 56.0, 1.7, "финал · вечер в мире", "ФИНАЛ",
                 camera=cam_drift(1.07, 0.018, 0.1), trans_in="whip",
                 overlays=[caption("спасибо за просмотр", 2.6, 2.0, "brand", y=900, size=44)],
                 sfx=[sfx("whoosh", 0.0, 0.5)]))
    S.append({
        "id": "card_end", "src": "card", "in": 0.0, "out": 2.6, "speed": 1.0,
        "label": "финальная карточка", "chapter": "ФИНАЛ", "flex": False, "camera": None,
        "trans_in": "none", "gain": 1.0, "protected": False,
        "overlays": [{"type": "end_card", "local": 0.0, "dur": 2.6,
                      "title": "HoweMine", "sub": "СПАСИБО ЗА ПРОСМОТР",
                      "foot": "сборка на продажу · обзор", "bg": "still:A@22:30.0"}],
        "sfx": [sfx("whoosh", 0.0, 0.6), sfx("low_boom", 0.1, 0.5)],
        "note": "финальная карточка 2.6 с",
    })
    return {"segments": S, "chapters": chapters}


# ─────────────────────────── вырезка брака ────────────────────────────────
def lag_cuts(analysis: dict, src: str, margin: float = 0.12) -> list[tuple[float, float]]:
    """Интервалы, которые режем: только замирания БЕЗ открытого GUI + меню/OBS."""
    if not analysis or src not in analysis:
        return []
    data = analysis[src]
    cuts: list[tuple[float, float]] = []
    for fz in data.get("freezes", []):
        if fz.get("kind") != "lag":
            continue
        if fz.get("len", 0) < 0.8:
            continue
        cuts.append((max(0.0, fz["start"] + margin), max(0.0, fz["end"] - margin)))
    for bl in data.get("blacks", []):
        if bl["end"] - bl["start"] >= 1.0:
            cuts.append((bl["start"], bl["end"]))
    # окно OBS поверх игры в начале источника B (по брифу) + уведомление «запись запущена»
    if src == "B":
        cuts.append((0.0, 8.0))
    if src == "A":
        cuts.append((0.0, 1.6))
    return merge_int(cuts)


def merge_int(items: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not items:
        return []
    items = sorted(items)
    out = [list(items[0])]
    for a, b in items[1:]:
        if a <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def subtract(seg_in: float, seg_out: float, cuts: list[tuple[float, float]],
             protected: list[tuple[float, float]], min_piece: float = 0.4):
    """Возвращает (живые куски, вырезанные куски) внутри [in, out]."""
    clipped = []
    for a, b in cuts:
        a2, b2 = max(a, seg_in), min(b, seg_out)
        if b2 - a2 <= 0.02:
            continue
        # защищённые окна не режем вообще
        for pa, pb in protected:
            if a2 < pb and b2 > pa:
                return [(seg_in, seg_out)], []
        clipped.append((a2, b2))
    clipped = merge_int(clipped)
    pieces, removed = [], []
    cur = seg_in
    for a, b in clipped:
        if a - cur >= min_piece:
            pieces.append((round(cur, 3), round(a, 3)))
        elif a > cur:
            removed.append((round(cur, 3), round(a, 3)))   # огрызок — в брак
        removed.append((round(a, 3), round(b, 3)))
        cur = b
    if seg_out - cur >= min_piece:
        pieces.append((round(cur, 3), round(seg_out, 3)))
    elif cur < seg_out:
        removed.append((round(cur, 3), round(seg_out, 3)))
    if not pieces:
        return [(seg_in, seg_out)], []
    return pieces, removed


# ─────────────────────────── сборка EDL ───────────────────────────────────
def build(analysis: dict | None = None, target: float = 750.0, fit: bool = True,
          time_scale: float = 1.0) -> dict:
    plan = raw_plan()
    segments = plan["segments"]

    # масштаб таймкодов источника A — только для self-test на коротких синтетических дублях
    if abs(time_scale - 1.0) > 1e-9:
        for s_ in segments:
            if s_["src"] == "A":
                s_["in"] = round(s_["in"] * time_scale, 3)
                s_["out"] = round(s_["out"] * time_scale, 3)
            if "frame_t" in s_ and s_.get("frame_src") == "A":
                s_["frame_t"] = round(s_["frame_t"] * time_scale, 3)
            for o in s_.get("overlays", []):
                bg = str(o.get("bg", ""))
                if bg.startswith("still:") and "@" in bg:
                    src_k, t_str = bg[6:].split("@", 1)
                    t_sec = parse_tc(t_str)
                    o["bg"] = f"still:{src_k}@{t_sec * time_scale:.3f}"

    protected_windows = {
        "A": [(11 * 60 + 44.0, 12 * 60 + 10.0), (19 * 60 + 24.0, 20 * 60 + 40.0)],
        "B": [(29.5, 36.5)],      # момент удара и смерти дракона — не рвать
    }
    if abs(time_scale - 1.0) > 1e-9:
        protected_windows["A"] = [(a * time_scale, b * time_scale) for a, b in protected_windows["A"]]
        if analysis and "A" in analysis:
            analysis = dict(analysis)
            a_copy = json.loads(json.dumps(analysis["A"]))
            for key in ("freezes", "blacks", "gui_intervals", "chat_intervals"):
                for it in a_copy.get(key, []):
                    for k2 in ("start", "end"):
                        if k2 in it:
                            it[k2] = round(it[k2] * time_scale, 3)
            analysis["A"] = a_copy
    cuts_by_src = {s: lag_cuts(analysis, s) for s in ("A", "B")}

    # 1) вырезка брака + разбиение на живые куски
    out_segments: list[dict] = []
    removed_log: list[dict] = []
    for s in segments:
        if s["src"] in ("card", "freeze"):
            out_segments.append(s)
            continue
        prot = protected_windows.get(s["src"], [])
        pieces, removed = subtract(s["in"], s["out"], cuts_by_src[s["src"]], prot)
        for a, b in removed:
            removed_log.append({"src": s["src"], "start": a, "end": b,
                                "len": round(b - a, 2), "label": s["label"],
                                "reason": "замирание без GUI / меню / OBS / чёрный кадр"})
        for i, (a, b) in enumerate(pieces):
            p = dict(s)
            p["in"], p["out"] = a, b
            if i > 0:
                p["trans_in"] = "none"
                p["overlays"] = [dict(o, local=max(0.0, o["local"] - (a - s["in"]))) for o in s["overlays"]]
                p["sfx"] = []
                p["label"] = s["label"] + " (продолжение)"
            out_segments.append(p)

    # 2) подгонка длительности: скорость меняем только у flex-сегментов
    def total(segs):
        return sum(duration(x) for x in segs)

    def duration(x):
        return max(0.0, (x["out"] - x["in"]) / max(0.05, x["speed"]))

    if fit:
        # равномерно масштабируем скорости ТОЛЬКО у flex-сегментов, с клампом 0.95…3.0
        flex = [x for x in out_segments if x.get("flex") and not x.get("protected")]
        for _ in range(60):
            cur = total(out_segments)
            if abs(cur - target) < 1.5:
                break
            base_sum = sum((x["out"] - x["in"]) / x["speed"] for x in flex)
            if base_sum <= 1e-6:
                break
            remaining = target - (cur - base_sum)          # сколько времени должно дать flex
            factor = base_sum / max(1.0, remaining)         # >1 → ускорить
            changed = False
            for x in flex:
                new_speed = max(0.95, min(3.0, x["speed"] * factor))
                if abs(new_speed - x["speed"]) > 1e-4:
                    x["speed"] = round(new_speed, 3)
                    changed = True
            if not changed:
                break

    # 3) абсолютные таймкоды + SFX + ducking
    t = 0.0
    chapter_starts: dict[str, float] = {}
    all_sfx: list[dict] = []
    duck: list[tuple[float, float]] = []
    for s in out_segments:
        s["start"] = round(t, 3)
        s["duration"] = round(duration(s), 3)
        if s["chapter"] not in chapter_starts:
            chapter_starts[s["chapter"]] = round(t, 3)
        for e in s.get("sfx", []):
            all_sfx.append({"t": round(t + e["local"], 3), "sfx": e["sfx"], "gain": e["gain"]})
        if s.get("protected"):
            duck.append((round(t, 3), round(t + s["duration"], 3)))
        t += s["duration"]

    total_dur = round(t, 3)

    # 4) окна музыки: под титрами/лутом приглушаем, на динамике — полная
    fade_zones = [(x["start"], x["start"] + x["duration"])
                  for x in out_segments if x["src"] in ("card", "freeze")]

    edl = {
        "meta": {
            "title": "HoweMine — обзор сборки на продажу",
            "fps": FPS,
            "width": 1920,
            "height": 1080,
            "total_duration": total_dur,
            "total_tc": tc(total_dur),
            "target": target,
            "corridor": [600, 900],
            "sources": {"A": str(SRC_A), "B": str(SRC_B)},
            "style": {
                "brand": "#FF7A7A",
                "grade": {"contrast": 1.07, "brightness": -0.005, "saturation": 1.08,
                          "gamma": 1.02, "sharpen": 0.45},
                "grade_B": {"contrast": 1.06, "saturation": 1.10, "gamma": 1.45},
            },
            "protected_windows": {k: v for k, v in protected_windows.items()},
            "counts": {
                "segments": len(out_segments),
                "cuts_removed": len(removed_log),
                "removed_seconds": round(sum(r["len"] for r in removed_log), 2),
                "transitions": sum(1 for x in out_segments if x.get("trans_in", "none") != "none"),
            },
        },
        "chapters": [],
        "segments": out_segments,
        "sfx": all_sfx,
        "duck_windows": duck,
        "music_mute_windows": fade_zones,
        "removed": removed_log,
    }

    # главы с абсолютными таймкодами
    order = ["ХУК", "ТИТР", "СПАВН", "ТУР ПО СПАВНУ", "ТОРГОВЛЯ", "ПРОГРЕСС", "БАЗА", "СКЛАД",
             "ДОНАТ-КЕЙСЫ", "ЛОКАЦИИ", "ПУТЕШЕСТВИЕ", "КУБ И БИОМЫ", "ЛУТ", "СТРОЙКА", "АРЕНА", "ФИНАЛ"]
    for name in order:
        if name in chapter_starts:
            edl["chapters"].append({
                "chapter": name,
                "start": chapter_starts[name],
                "tc": tc(chapter_starts[name]),
                "duration": round(sum(x["duration"] for x in out_segments if x["chapter"] == name), 2),
            })

    # 5) валидация: коридор, защита, минимальная длина сегмента
    problems = []
    for s in out_segments:
        if (s["out"] - s["in"]) / max(0.05, s["speed"]) < 0.35:
            problems.append(f"слишком короткий сегмент: {s['label']} "
                            f"({(s['out'] - s['in']) / s['speed']:.2f} с)")
    if not (600 <= total_dur <= 900):
        problems.append(f"длительность {tc(total_dur)} вне коридора 10:00–15:00")
    for s in out_segments:
        if s.get("protected") and abs(s["speed"] - 1.0) > 1e-6:
            problems.append(f"защищённый сегмент {s['label']} имеет speed={s['speed']}")
    edl["meta"]["validation"] = {"problems": problems, "ok": not problems}
    return edl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=750.0, help="целевая длительность, с (12:30)")
    ap.add_argument("--no-fit", action="store_true")
    ap.add_argument("--analysis",
                    default=os.environ.get("HOWEMINE_ANALYSIS", str(ANALYSIS / "analysis.json")))
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="масштаб таймкодов источника A (self-test на коротких дублях)")
    ap.add_argument("--out", default=str(EDIT / "timeline.json"))
    args = ap.parse_args()
    analysis = None
    if Path(args.analysis).exists():
        analysis = load_json(Path(args.analysis))
    else:
        log("[timeline] analysis.json не найден — собираю EDL без вырезки брака")
    edl = build(analysis, target=args.target, fit=not args.no_fit, time_scale=args.time_scale)
    edl["meta"]["time_scale"] = args.time_scale
    save_json(Path(args.out), edl)
    m = edl["meta"]
    log(f"[timeline] сегментов {m['counts']['segments']}, длительность {m['total_tc']}, "
        f"вырезано {m['counts']['cuts_removed']} фрагментов ({m['counts']['removed_seconds']} с)")
    for ch in edl["chapters"]:
        log(f"    {ch['tc']:>8}  {ch['chapter']}  ({ch['duration']:.0f} с)")
    if m["validation"]["problems"]:
        for p in m["validation"]["problems"]:
            log(f"[timeline] ПРОБЛЕМА: {p}")


if __name__ == "__main__":
    main()
