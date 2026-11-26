#!/usr/bin/env bash
# run_all.sh — весь пайплайн HoweMine одной командой.
#   bash run_all.sh            # анализ → EDL → рендер → QC → отчёты
#   bash run_all.sh --fast     # без повторного анализа (если analysis.json уже есть)
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

# исходники: канонические raw/A.mp4, raw/B.mp4 либо «как из OBS» — пайплайн найдёт сам
mapfile -t SRC < <("$PY" - <<'EOF'
import sys
sys.path.insert(0, "edit")
import cfglib
for p in (cfglib.SRC_A, cfglib.SRC_B):
    if p.exists():
        print(p)
EOF
)
if [[ ${#SRC[@]} -lt 2 ]]; then
  echo "НЕТ ИСХОДНИКОВ в raw/ (ни A.mp4/B.mp4, ни 'Minecraft 2026.11.2*.mp4')"
  echo "Положите записи: A — 23:19, 1080p; B — 1:22, 1280x960 (см. raw/README.md)."
  echo "Проверка пайплайна без исходников: bash tools/run_selftest.sh"
  exit 2
fi
echo "== исходники: ${SRC[0]} | ${SRC[1]}"

if [[ $FAST -eq 0 || ! -s edit/analysis/analysis.json ]]; then
  echo "== 1/5 анализ кадров"; "$PY" edit/analyze.py
fi
echo "== 2/5 EDL";            "$PY" edit/timeline.py
echo "== 3/5 рендер";         "$PY" edit/render.py --all
echo "== 4/5 QC";             "$PY" edit/qc.py || true
echo "== 5/5 отчёты";         "$PY" edit/report.py --appendix docs/ПРИМЕЧАНИЕ-self-test.md 2>/dev/null \
                              || "$PY" edit/report.py

echo
echo "Готово: deliver/HoweMine_review_1080p60.mp4"
echo "Отчёты:  docs/ОТЧЁТ.md, docs/QC.md, edit/qc/*.jpg"
