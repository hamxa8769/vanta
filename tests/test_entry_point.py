"""Entry-point regression tests — v0.1.0 launched from the release zip
crashed immediately with::

    File "main.py", line 9, in <module>
    ImportError: attempted relative import with no known parent package

Root cause: PyInstaller runs ``src/goldboard_app/main.py`` as a
top-level script ("main"), which has no parent package, so its
``from .main_window import ...`` failed. The fix is absolute imports
in every module that can be executed as a script.
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
ENTRY_MODULES = [
    SRC / "goldboard_app" / "main.py",
    SRC / "goldboard_app" / "__main__.py",
]

_RELATIVE_IMPORT_RE = re.compile(r"^\s*from\s+\.+")


def test_entry_modules_use_no_relative_imports():
    """A module PyInstaller may run as a script must not use relative imports."""
    for path in ENTRY_MODULES:
        text = path.read_text(encoding="utf-8")
        bad = [line for line in text.splitlines() if _RELATIVE_IMPORT_RE.match(line)]
        assert not bad, f"{path.name} still uses relative imports: {bad}"


def test_entry_script_starts_without_import_error():
    """Run main.py exactly as PyInstaller does (top-level script).

    The frozen app opens a window and runs its event loop, so the
    process is killed by timeout — the assertion is that it stays
    alive until then with no ImportError/Traceback on stderr, i.e.
    it gets past the imports that crashed v0.1.0.
    """
    env = dict(__import__("os").environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(SRC)
    try:
        proc = subprocess.run(
            [sys.executable, str(SRC / "goldboard_app" / "main.py")],
            capture_output=True, text=True, timeout=10, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # Expected: the app was still running (event loop) when killed.
        stderr = (exc.stderr or "").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        assert "ImportError" not in stderr
        assert "attempted relative import" not in stderr
        assert "Traceback" not in stderr
        return
    # If it exited on its own, it must have exited cleanly.
    assert proc.returncode == 0, proc.stderr
    assert "ImportError" not in proc.stderr
    assert "Traceback" not in proc.stderr
