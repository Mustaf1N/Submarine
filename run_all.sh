#!/usr/bin/env bash
# run_all.sh — весь пайплайн HoweMine одной командой.
#   bash run_all.sh            # анализ → EDL → рендер → QC → отчёты
#   bash run_all.sh --fast     # без повторного анализа (если analysis.json уже есть)
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

for f in raw/A.mp4 raw/B.mp4; do
  if [[ ! -s "$f" ]]; then
    echo "НЕТ ИСХОДНИКА: $f"
    echo "Положите записи как raw/A.mp4 (23:19, 1080p) и raw/B.mp4 (1:22, 1280x960)."
    echo "Проверка пайплайна без исходников: python3 tools/make_test_media.py --minutes 0.6"
    exit 2
  fi
done

if [[ $FAST -eq 0 || ! -s edit/analysis/analysis.json ]]; then
  echo "== 1/5 анализ кадров"; "$PY" edit/analyze.py
fi
echo "== 2/5 EDL";            "$PY" edit/timeline.py
echo "== 3/5 рендер";         "$PY" edit/render.py --all
echo "== 4/5 QC";             "$PY" edit/qc.py || true
echo "== 5/5 отчёты";         "$PY" edit/report.py

echo
echo "Готово: deliver/HoweMine_review_1080p60.mp4"
echo "Отчёты:  docs/ОТЧЁТ.md, docs/QC.md, edit/qc/*.jpg"
