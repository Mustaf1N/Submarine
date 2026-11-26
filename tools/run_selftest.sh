#!/usr/bin/env bash
# tools/run_selftest.sh — сквозная проверка пайплайна без исходных записей.
#
# 1) генерирует синтетические «двойники» записей в work/selftest/raw/
#    (те же разрешения/пропорции/дефекты, что у реальных файлов);
# 2) гоняет анализ → EDL → рендер → QC → отчёты;
# 3) доказательства уходят в edit/qc/ и docs/ С БАННЕРОМ self-test,
#    чтобы их нельзя было спутать с результатами реальных записей.
#
# Пайплайн, который после этого отработал, — тот же, что пойдёт по raw/A.mp4 и raw/B.mp4.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
TS=${1:-0.0257}          # масштаб таймкодов: 0.0257 режет 23:19 → ~36 с

mkdir -p work/selftest
"$PY" tools/make_test_media.py --minutes 0.6 --fps-a 15 --fps-b 60 --out-dir work/selftest/raw

export HOWEMINE_RAW="$PWD/work/selftest/raw"
"$PY" edit/analyze.py --out work/selftest/analysis.json
"$PY" edit/timeline.py --analysis work/selftest/analysis.json \
      --time-scale "$TS" --out work/selftest/timeline.json
"$PY" edit/render.py --timeline work/selftest/timeline.json
"$PY" edit/qc.py --timeline work/selftest/timeline.json --expect-duration 100,130 \
      --note "**Данные синтетического self-test** (синтетические двойники записей), а не реальных исходников: подтверждают работоспособность пайплайна."
"$PY" edit/report.py --timeline work/selftest/timeline.json \
      --note "**Данные синтетического self-test** (синтетические двойники записей), а не реальных исходников: подтверждают работоспособность пайплайна."

echo
echo "self-test готов: work/selftest/, доказательства — edit/qc/ и docs/ (с баннером)"
