# Исходные записи (в git не попадают)

Положите сюда два файла с этими именами:

| Файл | Что это | Параметры |
|---|---|---|
| `raw/A.mp4` | `Minecraft 2026.11.23 - 18.27.09.01.mp4` | 23:19, 1920×1080, 60 fps, H.264 + AAC stereo |
| `raw/B.mp4` | `Minecraft 2026.11.26 - 03.19.04.01.mp4` | 1:22, 1280×960 (4:3), 60 fps |

```bash
cp "Minecraft 2026.11.23 - 18.27.09.01.mp4" raw/A.mp4
cp "Minecraft 2026.11.26 - 03.19.04.01.mp4" raw/B.mp4
bash run_all.sh
```

Другие имена/пути — через переменные окружения:

```bash
HOWEMINE_SRC_A=/path/to/first.mp4 HOWEMINE_SRC_B=/path/to/second.mp4 bash run_all.sh
```

Без исходников пайплайн можно прогнать на синтетических двойниках:
`python3 tools/make_test_media.py --minutes 0.6` (см. `tools/run_selftest.sh`).
