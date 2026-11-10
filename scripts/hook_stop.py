#!/usr/bin/env python3
"""Claude Code Stop hook: blocks finishing while validation is REPROVADO or NÃO VALIDADO.

- Status for the current content hash is reused; a stale/missing status triggers a full validation.
- When stop_hook_active is true (Claude is already continuing because of this hook), it does
  not block again — it only surfaces a systemMessage — so it can never loop forever.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as validation  # noqa: E402


def handle(payload: dict, root: Path, suites=validation.TEST_SUITES) -> tuple:
    """Returns (exit_code, stdout, stderr)."""
    status = validation.current_status(root)
    if status["status"] == validation.STATUS_APPROVED:
        return 0, "", ""
    if payload.get("stop_hook_active"):
        message = (f"Atenção: validação {status['status']} para o conteúdo {status.get('content_hash')}. "
                   "Rode: python scripts/validate.py")
        return 0, json.dumps({"systemMessage": message}, ensure_ascii=False), ""
    if status.get("stale"):
        status = validation.validate(root, suites)
        if status["status"] == validation.STATUS_APPROVED:
            return 0, "", ""
    report = validation.format_report(status)
    return 2, "", ("Não encerre ainda: a validação não está APROVADA para o conteúdo atual. "
                   "Corrija e rode a bateria completa.\n" + report)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        code, stdout, stderr = handle(payload if isinstance(payload, dict) else {}, validation.PROJECT_ROOT)
    except Exception as exc:
        if isinstance(payload, dict) and payload.get("stop_hook_active"):
            return 0
        sys.stderr.write(f"[NÃO VALIDADO] hook Stop falhou: {type(exc).__name__}: {exc}\n")
        return 2
    if stdout:
        sys.stdout.write(stdout + "\n")
    if stderr:
        sys.stderr.write(stderr + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
