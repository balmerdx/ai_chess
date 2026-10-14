"""Check Qt's Python bindings before constructing widgets or worker threads.

Some abi3 wheels built with newer CPython incorrectly return borrowed None on
older interpreters. Void Qt calls then decrement None until Python aborts.
Probe a small number of QObject calls; never modify Python object refcounts.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class QtRuntimeError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def qt_runtime_error() -> str | None:
    if platform.python_implementation() != "CPython":
        return None
    from PySide6 import __version__
    from PySide6.QtCore import QObject

    obj = QObject()
    # Warm up lazy bindings before measuring. Keep the probe short enough that
    # even the affected runtime remains alive and can print a useful error.
    for _ in range(16):
        obj.setObjectName("ai_chess_runtime_probe")
    before = sys.getrefcount(None)
    for _ in range(64):
        obj.setObjectName("ai_chess_runtime_probe")
    lost = before - sys.getrefcount(None)
    if lost >= 32:
        return (
            f"Несовместимая сборка PySide6 {__version__} с Python {platform.python_version()}: "
            f"64 вызова Qt потеряли {lost} ссылок на None. Эта сборка приводит к none_dealloc. "
            "Используйте .venv/bin/python -m ai_chess. Для подготовки локального окружения "
            "с уже установленными библиотеками: python3 tools/create_gui_env.py."
        )
    return None


def ensure_gui_runtime(*, allow_reexec: bool = False) -> None:
    error = qt_runtime_error()
    if not error:
        return
    candidate = ROOT / ".venv" / "bin" / "python"
    if allow_reexec and candidate.is_file() and candidate.resolve() != Path(sys.executable).resolve():
        try:
            probe = subprocess.run(
                [str(candidate), "-c", "from ai_chess.qt_runtime import qt_runtime_error; "
                 "raise SystemExit(1 if qt_runtime_error() else 0)"],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        if probe is not None and probe.returncode == 0:
            print("GUI: используется совместимый Python из .venv", file=sys.stderr, flush=True)
            os.execv(str(candidate), [str(candidate), "-m", "ai_chess", *sys.argv[1:]])
    raise QtRuntimeError(error)
