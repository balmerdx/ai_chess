"""Qt binding regression checks without QApplication, windows or GUI rendering."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ai_chess import qt_runtime


class QtRuntimeTests(unittest.TestCase):
    def test_incompatible_runtime_fails_before_widgets(self):
        with patch.object(qt_runtime, "qt_runtime_error", return_value="test broken binding"):
            with self.assertRaisesRegex(qt_runtime.QtRuntimeError, "test broken binding"):
                qt_runtime.ensure_gui_runtime()

    def test_cli_fallback_preserves_arguments(self):
        candidate = qt_runtime.ROOT / ".venv/bin/python"
        if not candidate.exists() or candidate.resolve() == Path(sys.executable).resolve():
            self.skipTest("A distinct project runtime is not available")
        with patch.object(qt_runtime, "qt_runtime_error", return_value="test broken binding"), \
             patch.object(qt_runtime.subprocess, "run", return_value=Mock(returncode=0)) as probe, \
             patch.object(qt_runtime.os, "execv", side_effect=SystemExit) as reexec, \
             patch.object(sys, "argv", ["ai_chess", "gui", "--engine", "custom-engine"]):
            with self.assertRaises(SystemExit):
                qt_runtime.ensure_gui_runtime(allow_reexec=True)
            probe.assert_called_once()
            reexec.assert_called_once_with(str(candidate), [str(candidate), "-m", "ai_chess", "gui", "--engine", "custom-engine"])

    def test_rejects_broken_fallback(self):
        with patch.object(qt_runtime, "qt_runtime_error", return_value="test broken binding"), \
             patch.object(qt_runtime.subprocess, "run", return_value=Mock(returncode=1)), \
             patch.object(qt_runtime.os, "execv") as reexec:
            with self.assertRaises(qt_runtime.QtRuntimeError):
                qt_runtime.ensure_gui_runtime(allow_reexec=True)
            reexec.assert_not_called()

    def test_installed_bindings_do_not_lose_none_in_safe_runtime(self):
        python = qt_runtime.ROOT / ".venv/bin/python"
        if not python.exists():
            self.skipTest("Project GUI runtime is not prepared")
        result = subprocess.run([str(python), "-c", """
import sys
from PySide6.QtCore import QObject
from PySide6.QtGui import QFont, QImage, QPainter, QColor
from ai_chess.qt_runtime import ensure_gui_runtime
ensure_gui_runtime()
obj = QObject()
font = QFont('DejaVu Sans')
image = QImage(32, 32, QImage.Format.Format_ARGB32)
painter = QPainter(image)
def calls():
    obj.setObjectName('test')
    font.setPixelSize(20)
    painter.setFont(font)
    painter.setPen(QColor('#161b22'))
    painter.fillRect(0, 0, 32, 32, QColor('#eadcc4'))
for _ in range(16): calls()
before = sys.getrefcount(None)
for _ in range(10000): calls()
lost = before - sys.getrefcount(None)
painter.end()
assert lost <= 8, f'None refcount deficit: {lost}'
print('50000 Qt calls without None refcount deficit')
"""], cwd=qt_runtime.ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("50000 Qt calls", result.stdout)


if __name__ == "__main__":
    unittest.main()
