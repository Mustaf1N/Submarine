#!/usr/bin/env python3
"""
tools/ffprobe_shim.py — минимальная замена ffprobe (если его нет в системе).

Понимает ровно те вызовы, которые делает пайплайн:
    ffprobe -v error -print_format json -show_format -show_streams FILE
печатает JSON в формате ffprobe. Используется через HOWEMINE_FFPROBE=.../ffprobe_shim.py.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "edit"))
from cfglib import _probe_pyav  # noqa: E402

if __name__ == "__main__":
    target = [a for a in sys.argv[1:] if not a.startswith("-")][-1]
    print(json.dumps(_probe_pyav(Path(target)), ensure_ascii=False))
