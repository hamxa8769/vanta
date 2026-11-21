"""CLI entry point: ``python -m goldboard_core``.

Prints the version and the gate status from the default database.
Read-only check: it never creates a database and never writes gate
results (evaluate_gates is called with store=False).
"""

from __future__ import annotations

from pathlib import Path

from . import __version__
from .branding import APP_DISPLAY_NAME
from .config import load_config
from .gates import evaluate_gates, ready_for_muuk


def main() -> None:
    print(f"{APP_DISPLAY_NAME} core v{__version__}")
    cfg = load_config()
    db = Path(cfg.db_path)
    if not db.is_file():
        print(f"No database yet at {db} — run ingestion (Phase 2) first.")
        print("Gate status: not evaluated (no data). ready_for_muuk: False")
        return
    gates = evaluate_gates(str(db), store=False)
    for gate in gates:
        mark = "PASS" if gate.passed else "FAIL"
        print(f"[{mark}] {gate.name}: {gate.detail}")
    print(f"ready_for_muuk: {ready_for_muuk(gates)}")


if __name__ == "__main__":
    main()
