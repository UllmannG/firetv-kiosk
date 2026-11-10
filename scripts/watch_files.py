#!/usr/bin/env python3
"""Polling file watcher for changes made OUTSIDE Claude Code (editor, git pull, etc.).

    python scripts/watch_files.py [--interval 3]

Re-runs scripts/validate.py whenever the monitored content hash changes and prints the
health indicator. Generated files (validation status, logs) are not monitored, so the
watcher's own reports never retrigger it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as validation  # noqa: E402


def poll_once(root: Path, state: dict, validate_fn=validation.validate, out=sys.stdout) -> bool:
    """Validate when content changed since the last attempt. Returns True when it validated."""
    current = validation.content_hash(root)
    if current == state.get("last_hash"):
        return False
    saved = validation.read_status(root) or {}
    if state.get("last_hash") is None and saved.get("content_hash") == current:
        state["last_hash"] = current  # already validated (e.g. by a hook)
        print(validation.format_report(validation.current_status(root)), file=out, flush=True)
        return False
    result = validate_fn(root)
    state["last_hash"] = result.get("content_hash", current)
    print(validation.format_report(result), file=out, flush=True)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Observador de arquivos do firetv-kiosk")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--root", default=str(validation.PROJECT_ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    state: dict = {"last_hash": None}
    print(f"Observando {root} a cada {args.interval}s (Ctrl+C para sair).", flush=True)
    try:
        while True:
            poll_once(root, state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
