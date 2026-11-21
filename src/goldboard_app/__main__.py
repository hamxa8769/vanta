"""``python -m goldboard_app`` — launches the Vanta desktop app."""

from __future__ import annotations

# Absolute import (not relative): keeps this working if the file is
# ever executed as a top-level script, like main.py (see its comment).
from goldboard_app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
