#!/usr/bin/env python3
"""
tools/package_delivery.py — нарезать ролик на части ≤ 100 МБ и отдать батник для Windows.

Зачем: GitHub/Dropbox-коннекторы и часть браузеров не любят файлы > 100 МБ,
а ТЗ требует «если не проходит одним файлом — части ≤ 100 МБ на ключевых кадрах
+ инструкция склейки».

Что делает:
  1. режет финальный mp4 по ключевым кадрам (ffmpeg `-f segment -c copy`),
     каждая часть — самостоятельный валидный mp4, который играется сразу;
  2. считает SHA-256 и размер каждой части → `deliver/parts/manifest.json`;
  3. генерирует `deliver/install_windows.bat` — он на чистой Windows ставит
     Python + ffmpeg, качает все части, проверяет хэши и собирает ролик
     в папку «Загрузки».

Запуск:
    python3 tools/package_delivery.py                     # цель 90 МБ на часть
    python3 tools/package_delivery.py --target-mb 60
    python3 tools/package_delivery.py --base-url https://...   # ссылки на части
    python3 tools/package_delivery.py --urls urls.json         # свои прямые ссылки

`urls.json` — {"HoweMine_review_1080p60_part00.mp4": "https://...", ...}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "edit"))

from cfglib import FFMPEG, probe_brief  # noqa: E402

VIDEO = ROOT / "deliver" / "HoweMine_review_1080p60.mp4"
PARTS = ROOT / "deliver" / "parts"
BAT = ROOT / "deliver" / "install_windows.bat"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cut(target_mb: float, force: bool = False) -> list[Path]:
    dur = probe_brief(VIDEO)["duration"]
    size_mb = VIDEO.stat().st_size / 1e6
    # частей столько, чтобы каждая уверенно влезала в лимит с запасом на контейнер
    n = max(1, int(size_mb // target_mb) + (1 if size_mb % target_mb else 0))
    if n == 1:
        PARTS.mkdir(parents=True, exist_ok=True)
        out = PARTS / "HoweMine_review_1080p60_part01.mp4"
        shutil.copy2(VIDEO, out)
        return [out]

    if PARTS.exists() and force:
        shutil.rmtree(PARTS)
    PARTS.mkdir(parents=True, exist_ok=True)
    if not force and any(PARTS.glob("*_part*.mp4")):
        got = sorted(PARTS.glob("*_part*.mp4"))
        if len(got) == n:
            print(f"[parts] уже нарезано: {len(got)} частей (--force для пересборки)")
            return got

    seg = dur / n
    print(f"[parts] {size_mb:.0f} МБ / {dur:.1f} с → {n} частей по ~{seg:.0f} с")
    # -f segment + -c copy: режет по ключевым кадрам, каждая часть играется сама
    subprocess.run([
        FFMPEG, "-v", "error", "-y", "-i", str(VIDEO), "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_time", f"{seg:.3f}", "-reset_timestamps", "1",
        "-segment_format", "mp4", "-movflags", "+faststart",
        str(PARTS / "HoweMine_review_1080p60_part%02d.mp4"),
    ], check=True)
    return sorted(PARTS.glob("HoweMine_review_1080p60_part*.mp4"))


def manifest(parts: list[Path], base_url: str) -> dict:
    files = []
    for p in parts:
        b = probe_brief(p)
        files.append({
            "name": p.name,
            "size": p.stat().st_size,
            "size_mb": round(p.stat().st_size / 1e6, 2),
            "sha256": sha256(p),
            "duration": round(b["duration"], 2),
            "url": f"{base_url.rstrip('/')}/{p.name}" if base_url else "",
        })
    return {
        "title": "HoweMine — обзор сборки (1080p60)",
        "video": VIDEO.name,
        "total_size_mb": round(sum(f["size"] for f in files) / 1e6, 1),
        "parts": files,
    }


BAT_TEMPLATE = """@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title HoweMine — установка и сборка ролика
echo.
echo   HoweMine — обзор сборки, 1920x1080 60fps
echo   ------------------------------------------------
echo.
set "WORK=%USERPROFILE%\\HoweMine_build"
set "DL=%USERPROFILE%\\Downloads\\HoweMine"
if not exist "%WORK%" mkdir "%WORK%" >nul 2>&1
if not exist "%DL%" mkdir "%DL%" >nul 2>&1
cd /d "%WORK%"

echo [1/5] Проверяю Python...
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    echo       Python не найден, ставлю через winget...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements >nul 2>&1
    if not exist "!LOCALAPPDATA!\\Programs\\Python\\Python312\\python.exe" (
        echo       Качаю установщик с python.org...
        curl -L -o python-installer.exe "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" || goto :fail
        echo       Ставлю Python, подождите 1-2 минуты...
        start /wait "" python-installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
    )
    set "PY="
    if exist "!LOCALAPPDATA!\\Programs\\Python\\Python312\\python.exe" set "PY=!LOCALAPPDATA!\\Programs\\Python\\Python312\\python.exe"
    if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
    if not defined PY python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo       Python поставить не удалось. Поставьте вручную:
    echo       https://www.python.org/downloads/  — и запустите файл снова.
    goto :fail
)
echo       OK

echo [2/5] Проверяю ffmpeg...
set "FF="
if exist "%WORK%\\ffmpeg\\bin\\ffmpeg.exe" set "FF=%WORK%\\ffmpeg\\bin\\ffmpeg.exe"
where ffmpeg >nul 2>&1 && set "FF=ffmpeg"
if not defined FF (
    echo       Качаю ffmpeg (около 40 МБ)...
    curl -L --retry 3 -o ffmpeg.zip "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" || goto :fail
    powershell -NoProfile -Command "Expand-Archive -Force -Path 'ffmpeg.zip' -DestinationPath 'ffmpeg_tmp'" || goto :fail
    for /d %%D in (ffmpeg_tmp\\ffmpeg-*) do move "%%D" "ffmpeg" >nul
    rmdir /s /q ffmpeg_tmp >nul 2>&1
    del /q ffmpeg.zip >nul 2>&1
    if not exist "%WORK%\\ffmpeg\\bin\\ffmpeg.exe" goto :fail
    set "FF=%WORK%\\ffmpeg\\bin\\ffmpeg.exe"
)
echo       OK

echo [3/5] Качаю части ролика...
__PARTS__

echo [4/5] Проверяю целостность файлов...
__HASHES__

echo [5/5] Собираю ролик (без перекодирования, качество не теряется)...
__LIST__
"%FF%" -v error -y -f concat -safe 0 -i join.txt -c copy -movflags +faststart "%DL%\\HoweMine_review_1080p60.mp4"
if not exist "%DL%\\HoweMine_review_1080p60.mp4" goto :fail

echo.
echo   ============ ГОТОВО ============
echo    Ролик: "%DL%\\HoweMine_review_1080p60.mp4"
echo.
start "" "%DL%"
pause
exit /b 0

:fail
echo.
echo   Что-то не получилось. Запустите файл ещё раз: уже скачанное
echo   повторно не качается. Если не помогает — пришлите скриншот окна.
echo.
pause
exit /b 1
"""


def write_bat(man: dict) -> Path:
    """Батник для чистой Windows: Python + ffmpeg + части + сборка в «Загрузки»."""
    lines = []
    for i, f in enumerate(man["parts"], 1):
        lines.append(f'rem {f["name"]}: {f["size_mb"]} МБ')
        lines.append(f'set "URL{i}={f.get("url", "")}"')
    lines.append("")
    for i, f in enumerate(man["parts"], 1):
        name, h = f["name"], f["sha256"].upper()
        lines.append(f'rem --- {name} ({f["size_mb"]} МБ)')
        lines.append(f'if exist "{name}" goto have{i}')
        lines.append(f'echo       {name} ...')
        lines.append(f'curl -L --retry 3 --retry-delay 2 -o "{name}" "%URL{i}%"')
        lines.append(f'if not exist "{name}" (')
        lines.append(f'    echo       Не скачался {name}. Проверьте интернет и запустите снова.')
        lines.append("    goto :fail")
        lines.append(")")
        lines.append(f":have{i}")
        # Проверяем сразу: если вместо файла пришла HTML-страница Dropbox,
        # размер будет крошечный — сообщаем понятно, а не «повреждён»
        lines.append(f'for %%S in ("{name}") do if %%~zS LSS 1000000 (')
        lines.append(f'    echo       {name} скачался не полностью.')
        lines.append(f'    echo       Удалите его из "%WORK%" и запустите файл снова.')
        lines.append("    goto :fail")
        lines.append(")")
        lines.append(f'powershell -NoProfile -Command "if ((Get-FileHash -Algorithm SHA256 '
                     f"'{name}').Hash -ne '{h}') {{ exit 1 }}\"")
        lines.append("if errorlevel 1 (")
        lines.append(f'    echo       {name}: хэш не совпал — файл удалён, запустите снова.')
        lines.append(f'    del /q "{name}"')
        lines.append("    goto :fail")
        lines.append(")")
        lines.append(f'echo       OK {name}')

    hashes = ["echo       все части на месте и совпадают по SHA-256"]

    listing = [f'(echo file \'%WORK%\\{man["parts"][0]["name"]}\')>join.txt']
    for f in man["parts"][1:]:
        listing.append(f"(echo file '%WORK%\\{f['name']}')>>join.txt")

    bat = (BAT_TEMPLATE
           .replace("__PARTS__", "\n".join(lines))
           .replace("__HASHES__", "\n".join(hashes))
           .replace("__LIST__", "\n".join(listing)))
    BAT.write_text(bat, encoding="utf-8")
    return BAT


def main() -> int:
    global VIDEO
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=str(VIDEO))
    ap.add_argument("--target-mb", type=float, default=90.0)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--urls", default="", help="json со своими ссылками на части")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    VIDEO = Path(args.video)
    if not VIDEO.exists():
        print(f"[parts] нет файла {VIDEO}")
        return 2
    parts = cut(args.target_mb, force=args.force)
    man = manifest(parts, args.base_url)
    if args.urls and Path(args.urls).exists():
        overrides = json.loads(Path(args.urls).read_text(encoding="utf-8"))
        for f in man["parts"]:
            if f["name"] in overrides:
                f["url"] = overrides[f["name"]]
    (PARTS / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    bat = write_bat(man)
    for f in man["parts"]:
        print(f"[parts] {f['name']}: {f['size_mb']} МБ, {f['duration']} с, sha {f['sha256'][:12]}…")
    print(f"[parts] итого {man['total_size_mb']} МБ, частей {len(man['parts'])}")
    print(f"[parts] батник: {bat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
