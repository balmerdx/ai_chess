"""Reuse existing pure-Python/abi3 packages with CPython 3.12, offline.

Run using the interpreter where PySide6 and python-chess are already installed.
No pip, network access, or changes to the source packages are required.
"""
import importlib
import shutil
import subprocess
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    interpreter = shutil.which("python3.12")
    if not interpreter:
        raise SystemExit("Не найден python3.12. Нужен совместимый интерпретатор для установленной сборки Qt.")
    modules = {}
    for name in ("chess", "PySide6", "shiboken6"):
        modules[name] = Path(importlib.import_module(name).__file__).resolve().parent
    for name in ("PySide6", "shiboken6"):
        if any(not file.name.endswith(".abi3.so") for file in modules[name].glob("*.so")):
            raise SystemExit(f"{name}: расширения не используют abi3; повторное использование небезопасно")
    destination = ROOT / ".venv"
    python = destination / "bin" / "python"
    if python.exists():
        version = subprocess.check_output([str(python), "-c", "import sys; print(sys.version_info[:2])"], text=True).strip()
        if version != "(3, 12)":
            raise SystemExit("Существующее .venv использует другой Python; оно оставлено без изменений.")
    else:
        subprocess.run([interpreter, "-m", "venv", "--without-pip", str(destination)], check=True)
    site = destination / "lib" / "python3.12" / "site-packages"
    links = dict(modules)
    links["ai_chess"] = ROOT / "ai_chess"
    for name in ("chess", "python-chess", "PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6"):
        dist = distribution(name)
        for file in dist.files or ():
            if file.name == "METADATA" and file.parent.name.endswith(".dist-info"):
                source = Path(dist.locate_file(file.parent)).resolve()
                links[source.name] = source
                break
    # Check all destinations before linking; preserve any user-installed files.
    for name, source in links.items():
        target = site / name
        if (target.exists() or target.is_symlink()) and target.resolve() != source:
            raise SystemExit(f"{target} уже существует и отличается; файл оставлен без изменений")
    for name, source in links.items():
        target = site / name
        if not target.exists():
            target.symlink_to(source, target_is_directory=True)
    subprocess.run(
        [str(python), "-c", "from ai_chess.qt_runtime import ensure_gui_runtime; ensure_gui_runtime()"],
        cwd=ROOT, check=True,
    )
    print("Готово: .venv/bin/python -m ai_chess. Библиотеки используются повторно, без скачиваний.")


if __name__ == "__main__":
    main()
