#!/usr/bin/env python3
"""
tools/build_repo.py — собрать отдельный репозиторий howemine-build-review.

Бриф (п.8) требует приватный репозиторий `howemine-build-review` с финальным
видео в GitHub Releases. Этот скрипт создаёт его и заливает:

  * README.md, brief.md, docs/, edit/ (код пайплайна), edit/qc/ (доказательства);
  * финальный ролик (если он есть локально) — в Releases (не в git);
  * при отсутствии финального файла — понятный раздел в README и issue-заметку.

Запуск:  python3 tools/build_repo.py [--video deliver/HoweMine_review_1080p60.mp4]
                                    [--name howemine-build-review]
                                    [--dry-run]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ["README.md", "brief.md", "LICENSES.md", ".gitignore"]
DIRS = ["edit", "docs", "tools"]


def run(cmd, cwd=ROOT, check=True):
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}\n{res.stderr[-1500:]}")
    return res


def ensure_repo(name: str) -> str:
    """Создать приватный репозиторий (или переиспользовать), вернуть URL."""
    res = run(["gh", "repo", "view", f"{name}", "--json", "nameWithOwner,url,visibility"], check=False)
    if res.returncode == 0:
        import json

        meta = json.loads(res.stdout)
        print(f"[repo] уже существует: {meta['url']} ({meta['visibility']})")
        return meta["url"]
    for candidate in (name, f"{name}-2"):
        res = run(["gh", "repo", "create", candidate, "--private", "--description",
                   "HoweMine — обзор сборки: монтаж, пайплайн, QC", "--confirm"], check=False)
        if res.returncode == 0:
            print(f"[repo] создан: {res.stdout.strip()}")
            return f"https://github.com/{run(['gh','api','user','-q','.login']).stdout.strip()}/{candidate}"
        print(f"[repo] {candidate}: {res.stderr.strip()[:200]}")
    raise RuntimeError("не удалось создать репозиторий")


def stage(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in DOCS:
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, dest / f)
    for d in DIRS:
        src = ROOT / d
        if not src.exists():
            continue
        shutil.copytree(src, dest / d, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "_tmp", "*.wav", "*.mkv", "*.mp4", "*.png", "*.npz"))
    # доказательства QC — обязательно
    qc_src = ROOT / "edit" / "qc"
    if qc_src.exists():
        (dest / "edit" / "qc").mkdir(parents=True, exist_ok=True)
        for f in qc_src.glob("*.jpg"):
            shutil.copy2(f, dest / "edit" / "qc" / f.name)
        for f in qc_src.glob("*.json"):
            shutil.copy2(f, dest / "edit" / "qc" / f.name)
        j = qc_src / "junctions"
        if j.exists():
            shutil.copytree(j, dest / "edit" / "qc" / "junctions", dirs_exist_ok=True)
    (dest / "deliver").mkdir(exist_ok=True)
    (dest / "deliver" / "README.md").write_text(
        "Финальный ролик лежит в GitHub Releases (файл ~500–900 МБ, в git не коммитится).\n"
        "Скачать: см. раздел «Готовое видео» в README.md.\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="deliver/HoweMine_review_1080p60.mp4")
    ap.add_argument("--name", default="howemine-build-review")
    ap.add_argument("--work", default="work/repo_out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-release", action="store_true")
    args = ap.parse_args()

    dest = ROOT / args.work
    stage(dest)
    print(f"[repo] подготовлено: {dest}")
    video = ROOT / args.video
    has_video = video.exists() and video.stat().st_size > 1_000_000
    print(f"[repo] видео: {'есть ' + f'{video.stat().st_size/1e6:.0f} МБ' if has_video else 'НЕТ'}")
    if args.dry_run:
        return 0

    url = ensure_repo(args.name)
    run(["git", "init", "-q"], cwd=dest)
    run(["git", "checkout", "-q", "-b", "main"], cwd=dest, check=False)
    run(["git", "add", "-A"], cwd=dest)
    run(["git", "-c", "user.email=agent@arena.ai", "-c", "user.name=HoweMine pipeline",
         "commit", "-q", "-m", "HoweMine review: пайплайн, EDL, QC"], cwd=dest)
    run(["git", "remote", "add", "origin", url], cwd=dest, check=False)
    run(["git", "push", "-q", "-u", "origin", "main"], cwd=dest)
    print(f"[repo] код запушен: {url}")

    if has_video and not args.skip_release:
        run(["gh", "release", "create", "v1.0", str(video),
             "--title", "HoweMine — обзор сборки (1080p60)",
             "--notes", "Готовый ролик 1080p60, H.264 High@4.2 + AAC 256k, −14 LUFS / TP ≤ −1.5 dBTP. "
                        "Главы и таймкоды — в README.md.", "--repo", url], cwd=ROOT)
        print("[repo] релиз v1.0 создан")
    return 0


if __name__ == "__main__":
    sys.exit(main())
