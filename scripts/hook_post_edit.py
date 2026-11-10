#!/usr/bin/env python3
"""Claude Code PostToolUse hook (matchers: Edit|Write|MultiEdit|NotebookEdit and Bash).

- File tools: validates when the edited file is monitored (src/, scripts/, config.example.json,
  .claude/settings.json).
- Bash: validates when the monitored content hash differs from the last validated hash
  (detects changes made through the terminal). Running validate.py itself leaves the hash
  unchanged, so it does not retrigger (no loop).

On REPROVADO / NÃO VALIDADO: exit 2 with the report on stderr (delivered to Claude).
On APROVADO: exit 0 with a short additionalContext.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as validation  # noqa: E402

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def parse_payload(raw: str) -> dict:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def should_validate(payload: dict, root: Path) -> bool:
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return bool(path) and validation.is_monitored(root, path)
    if tool == "Bash":
        saved = validation.read_status(root) or {}
        return saved.get("content_hash") != validation.content_hash(root)
    return False


def handle(payload: dict, root: Path, suites=validation.TEST_SUITES) -> tuple:
    """Returns (exit_code, stdout, stderr)."""
    if not should_validate(payload, root):
        return 0, "", ""
    result = validation.validate(root, suites)
    report = validation.format_report(result)
    if result["status"] != validation.STATUS_APPROVED:
        return 2, "", f"Validação automática após {payload.get('tool_name')}:\n{report}"
    context = (f"Validação automática: APROVADO — {result['total_tests']} testes, 0 falhas, "
               f"{result['skipped_tests']} ignorados, conteúdo {result['content_hash']} ({result['timestamp']}).")
    output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
    return 0, json.dumps(output, ensure_ascii=False), ""


def main() -> int:
    payload = parse_payload(sys.stdin.read())
    try:
        code, stdout, stderr = handle(payload, validation.PROJECT_ROOT)
    except Exception as exc:  # a crashing hook must not look like approval
        sys.stderr.write(f"[NÃO VALIDADO] hook de validação falhou: {type(exc).__name__}: {exc}\n"
                         "Reproduzir: python scripts/validate.py\n")
        return 2
    if stdout:
        sys.stdout.write(stdout + "\n")
    if stderr:
        sys.stderr.write(stderr + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
