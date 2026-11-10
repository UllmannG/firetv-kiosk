#!/usr/bin/env python3
"""External validation: syntax check + every test suite + health indicator.

Runs OUTSIDE the main script so a syntax error in it is still detected.

    python scripts/validate.py              # validate, write .claude/validation-status.json
    python scripts/validate.py --status     # show the last result (and whether it is stale)
    python scripts/validate.py --self-test  # tests for the safeguards themselves
    python scripts/validate.py --root DIR   # validate another copy of the project

Exit codes: 0 APROVADO, 1 REPROVADO, 2 NÃO VALIDADO.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STATUS_APPROVED = "APROVADO"
STATUS_FAILED = "REPROVADO"
STATUS_NOT_VALIDATED = "NÃO VALIDADO"
EXIT_BY_STATUS = {STATUS_APPROVED: 0, STATUS_FAILED: 1, STATUS_NOT_VALIDATED: 2}

STATUS_FILE_REL = ".claude/validation-status.json"
MONITORED_GLOBS = ("src/**/*.py", "scripts/**/*.py")
MONITORED_FILES = ("config.example.json", ".claude/settings.json")
EXCLUDED_PARTS = {"__pycache__", "logs", ".git"}
SUITE_TIMEOUT_SECONDS = 180

# Each suite: (script relative path, arguments). A suite prints a JSON report.
TEST_SUITES = (
    ("src/firetv_kiosk.py", ["--self-test", "--json"]),
    ("scripts/validate.py", ["--self-test", "--json"]),
)


# --------------------------------------------------------------------------- #
# Monitored content and hashing
# --------------------------------------------------------------------------- #
def monitored_files(root: Path) -> list:
    files = set()
    for pattern in MONITORED_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and not (set(path.relative_to(root).parts) & EXCLUDED_PARTS):
                files.add(path)
    for rel in MONITORED_FILES:
        path = root / rel
        if path.is_file():
            files.add(path)
    return sorted(files)


def is_monitored(root: Path, file_path: str) -> bool:
    try:
        target = Path(file_path)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        rel = target.relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    if set(rel.parts) & EXCLUDED_PARTS or rel.as_posix() == STATUS_FILE_REL:
        return False
    if rel.as_posix() in MONITORED_FILES:
        return True
    return rel.suffix == ".py" and rel.parts[0] in ("src", "scripts")


def content_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in monitored_files(root):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_syntax(root: Path) -> list:
    """Compile every monitored .py and parse every JSON file without importing/executing them."""
    failures = []
    for path in monitored_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            if path.suffix == ".py":
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            elif path.suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            failures.append(_syntax_failure(rel, f"{rel}:{exc.lineno}", f"SyntaxError: {exc.msg}",
                                            f"python -m py_compile {rel}"))
        except json.JSONDecodeError as exc:
            failures.append(_syntax_failure(rel, f"{rel}:{exc.lineno}", f"JSON inválido: {exc.msg}",
                                            f"python -m json.tool {rel}"))
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(_syntax_failure(rel, rel, f"{type(exc).__name__}: {exc}", f"python -m py_compile {rel}"))
    return failures


def _syntax_failure(script: str, location: str, observed: str, reproduce: str) -> dict:
    return {
        "script": script,
        "behavior": "verificação de sintaxe (externa ao script)",
        "test_name": "syntax_check",
        "kind": "syntax",
        "expected": "arquivo compila / JSON válido",
        "observed": observed,
        "location": location,
        "reproduce": reproduce,
    }


def run_suite(root: Path, script_rel: str, args: list, timeout: int = SUITE_TIMEOUT_SECONDS) -> dict:
    """Run one suite in a subprocess. Any abnormal outcome is NOT VALIDATED, never APPROVED."""
    script = root / script_rel
    reproduce = f"python {script_rel} {' '.join(a for a in args if a != '--json')}".strip()
    base = {"suite": script_rel, "total_tests": 0, "failed_tests": 0, "skipped_tests": 0, "failures": []}
    if not script.is_file():
        return {**base, "status": STATUS_NOT_VALIDATED, "problem": f"suíte ausente: {script_rel}"}
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run([sys.executable, str(script), *args], cwd=root, capture_output=True,
                              text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {**base, "status": STATUS_NOT_VALIDATED, "problem": f"tempo esgotado ({timeout}s) — reproduzir: {reproduce}"}
    try:
        report = json.loads(proc.stdout)
        if not isinstance(report, dict) or "status" not in report:
            raise ValueError("formato inesperado")
    except ValueError:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        return {**base, "status": STATUS_NOT_VALIDATED,
                "problem": f"suíte não produziu relatório (rc={proc.returncode}): {' | '.join(detail)} — reproduzir: {reproduce}"}
    expected_rc = EXIT_BY_STATUS.get(report["status"])
    if expected_rc != proc.returncode:
        return {**base, **report, "status": STATUS_NOT_VALIDATED,
                "problem": f"código de saída {proc.returncode} incoerente com status {report['status']}"}
    return {**base, **report, "suite": script_rel}


def aggregate(syntax_failures: list, suite_reports: list, hash_before: str, hash_after: str) -> dict:
    failures = list(syntax_failures)
    problems = []
    total = failed = skipped = 0
    for report in suite_reports:
        total += report.get("total_tests", 0)
        failed += report.get("failed_tests", 0)
        skipped += report.get("skipped_tests", 0)
        failures += report.get("failures", [])
        if report.get("problem"):
            problems.append(f"{report['suite']}: {report['problem']}")
        elif report.get("status") == STATUS_NOT_VALIDATED:
            problems.append(f"{report['suite']}: suíte incompleta (total={report.get('total_tests')}, "
                            f"ignorados={report.get('skipped_tests')})")
    if hash_before != hash_after:
        problems.append("arquivos monitorados mudaram durante a validação")
    if syntax_failures or failed:
        status = STATUS_FAILED
    elif problems or total == 0 or skipped:
        status = STATUS_NOT_VALIDATED
        if total == 0 and not problems:
            problems.append("nenhum teste executado")
    else:
        status = STATUS_APPROVED
    return {
        "status": status,
        "total_tests": total,
        "failed_tests": failed + len(syntax_failures),
        "skipped_tests": skipped,
        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "content_hash": hash_after,
        "failures": failures,
        "problems": problems,
        "suites": [{k: r.get(k) for k in ("suite", "status", "total_tests", "failed_tests", "skipped_tests")}
                   for r in suite_reports],
    }


def validate(root: Path = PROJECT_ROOT, suites=TEST_SUITES, timeout: int = SUITE_TIMEOUT_SECONDS) -> dict:
    root = root.resolve()
    hash_before = content_hash(root)
    syntax_failures = check_syntax(root)
    reports = []
    if not syntax_failures:
        for script_rel, args in suites:
            reports.append(run_suite(root, script_rel, args, timeout))
    result = aggregate(syntax_failures, reports, hash_before, content_hash(root))
    write_status(root, result)
    return result


# --------------------------------------------------------------------------- #
# Status file and reporting
# --------------------------------------------------------------------------- #
def status_path(root: Path) -> Path:
    return root / STATUS_FILE_REL


def write_status(root: Path, result: dict) -> None:
    path = status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_status(root: Path) -> Optional[dict]:
    try:
        data = json.loads(status_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def current_status(root: Path) -> dict:
    """Last result, downgraded to NÃO VALIDADO when content changed since (hash mismatch)."""
    saved = read_status(root)
    current_hash = content_hash(root)
    if saved is None:
        return {"status": STATUS_NOT_VALIDATED, "content_hash": current_hash, "stale": True,
                "problems": ["nenhuma validação registrada"], "failures": [],
                "total_tests": 0, "failed_tests": 0, "skipped_tests": 0, "timestamp": None}
    if saved.get("content_hash") != current_hash:
        return {**saved, "status": STATUS_NOT_VALIDATED, "stale": True, "content_hash": current_hash,
                "problems": [f"conteúdo alterado desde a última validação ({saved.get('content_hash')} → {current_hash})"]}
    return {**saved, "stale": False}


def format_report(result: dict, max_failures: int = 15) -> str:
    lines = [
        f"[{result['status']}] validação firetv-kiosk — total={result.get('total_tests', 0)} "
        f"falhas={result.get('failed_tests', 0)} ignorados={result.get('skipped_tests', 0)} "
        f"| {result.get('timestamp')} | conteúdo {result.get('content_hash')}"
    ]
    for problem in result.get("problems", []):
        lines.append(f"  ! {problem}")
    failures = result.get("failures", [])
    for failure in failures[:max_failures]:
        lines += [
            f"  FALHA em {failure.get('script')}: {failure.get('behavior')}",
            f"    teste:      {failure.get('test_name')}",
            f"    esperado:   {failure.get('expected')}",
            f"    observado:  {failure.get('observed')}",
            f"    local:      {failure.get('location')}",
            f"    reproduzir: {failure.get('reproduce')}",
        ]
    if len(failures) > max_failures:
        lines.append(f"  ... mais {len(failures) - max_failures} falha(s) em {STATUS_FILE_REL}")
    if result["status"] != STATUS_APPROVED:
        lines.append("  Corrija e rode novamente a bateria completa: python scripts/validate.py")
    return "\n".join(lines)


# =========================================================================== #
# Self-tests for the safeguards (validate, hooks, file watcher)
# =========================================================================== #
FAKE_SUITE_PASS = """import json, sys
print(json.dumps({"status": "APROVADO", "total_tests": 2, "failed_tests": 0, "skipped_tests": 0, "failures": []}))
sys.exit(0)
"""
FAKE_SUITE_FAIL = """import json, sys
print(json.dumps({"status": "REPROVADO", "total_tests": 2, "failed_tests": 1, "skipped_tests": 0,
  "failures": [{"script": "src/firetv_kiosk.py", "behavior": "demo", "test_name": "t.test_x",
                "expected": "A", "observed": "B", "location": "src/firetv_kiosk.py:3",
                "reproduce": "python src/firetv_kiosk.py --self-test --test test_x"}]}))
sys.exit(1)
"""
FAKE_SUITE_EMPTY = """import json, sys
print(json.dumps({"status": "NÃO VALIDADO", "total_tests": 0, "failed_tests": 0, "skipped_tests": 0, "failures": []}))
sys.exit(2)
"""
FAKE_SUITE_LIES = """import json, sys
print(json.dumps({"status": "APROVADO", "total_tests": 2, "failed_tests": 0, "skipped_tests": 0, "failures": []}))
sys.exit(1)
"""
FAKE_SUITE_SLOW = "import time\ntime.sleep(10)\n"
FAKE_SUITES = (("src/firetv_kiosk.py", ["--self-test", "--json"]),)


def make_fake_project(base: Path, suite_source: str = FAKE_SUITE_PASS) -> Path:
    root = base / "project"
    (root / "src").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".claude").mkdir()
    (root / "src" / "firetv_kiosk.py").write_text(suite_source, encoding="utf-8")
    (root / "scripts" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "config.example.json").write_text('{"a": 1}', encoding="utf-8")
    return root


class _ProjectCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="firetv_validate_selftest_")
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def project(self, source: str = FAKE_SUITE_PASS) -> Path:
        return make_fake_project(self.base, source)


class ValidationTests(_ProjectCase):
    def test_passing_project_is_approved_and_status_written(self):
        """Projeto com testes passando → APROVADO e status gravado com todos os campos."""
        root = self.project()
        result = validate(root, FAKE_SUITES)
        self.assertEqual(result["status"], STATUS_APPROVED)
        saved = read_status(root)
        for key in ("status", "total_tests", "failed_tests", "skipped_tests", "timestamp", "content_hash", "failures"):
            self.assertIn(key, saved)
        self.assertEqual(saved["total_tests"], 2)

    def test_syntax_error_is_failed(self):
        """Erro de sintaxe → REPROVADO com arquivo:linha, sem rodar a suíte."""
        root = self.project()
        (root / "scripts" / "helper.py").write_text("def broken(:\n", encoding="utf-8")
        result = validate(root, FAKE_SUITES)
        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertEqual(result["failures"][0]["location"], "scripts/helper.py:1")
        self.assertEqual(result["total_tests"], 0)

    def test_invalid_json_is_failed(self):
        """JSON monitorado inválido → REPROVADO."""
        root = self.project()
        (root / "config.example.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(validate(root, FAKE_SUITES)["status"], STATUS_FAILED)

    def test_failing_test_is_failed_with_details(self):
        """Teste falhando → REPROVADO e relatório com esperado/observado/local/reprodução."""
        root = self.project(FAKE_SUITE_FAIL)
        result = validate(root, FAKE_SUITES)
        self.assertEqual(result["status"], STATUS_FAILED)
        text = format_report(result)
        for fragment in ("esperado:   A", "observado:  B", "src/firetv_kiosk.py:3", "--self-test --test test_x"):
            self.assertIn(fragment, text)

    def test_no_tests_is_not_validated(self):
        """Sem testes → NÃO VALIDADO (nunca APROVADO)."""
        self.assertEqual(validate(self.project(FAKE_SUITE_EMPTY), FAKE_SUITES)["status"], STATUS_NOT_VALIDATED)

    def test_missing_suite_is_not_validated(self):
        """Suíte ausente → NÃO VALIDADO."""
        root = self.project()
        self.assertEqual(validate(root, (("src/missing.py", []),))["status"], STATUS_NOT_VALIDATED)

    def test_timeout_is_not_validated(self):
        """Tempo esgotado → NÃO VALIDADO."""
        result = validate(self.project(FAKE_SUITE_SLOW), FAKE_SUITES, timeout=1)
        self.assertEqual(result["status"], STATUS_NOT_VALIDATED)
        self.assertIn("tempo esgotado", " ".join(result["problems"]))

    def test_inconsistent_exit_code_is_not_validated(self):
        """Relatório APROVADO com código de saída ≠ 0 → NÃO VALIDADO."""
        self.assertEqual(validate(self.project(FAKE_SUITE_LIES), FAKE_SUITES)["status"], STATUS_NOT_VALIDATED)

    def test_hash_changes_after_edit_and_invalidates_status(self):
        """Editar arquivo monitorado muda o hash e invalida o resultado anterior."""
        root = self.project()
        validate(root, FAKE_SUITES)
        self.assertEqual(current_status(root)["status"], STATUS_APPROVED)
        before = content_hash(root)
        (root / "scripts" / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(content_hash(root), before)
        status = current_status(root)
        self.assertEqual(status["status"], STATUS_NOT_VALIDATED)
        self.assertTrue(status["stale"])

    def test_report_and_logs_do_not_change_hash(self):
        """Relatório e logs gerados não alteram o hash (evita loop de validação)."""
        root = self.project()
        before = content_hash(root)
        validate(root, FAKE_SUITES)
        (root / "logs").mkdir(exist_ok=True)
        (root / "logs" / "x.log").write_text("log", encoding="utf-8")
        (root / "src" / "__pycache__").mkdir()
        (root / "src" / "__pycache__" / "m.py").write_text("x", encoding="utf-8")
        self.assertEqual(content_hash(root), before)
        self.assertFalse(is_monitored(root, str(root / STATUS_FILE_REL)))
        self.assertFalse(is_monitored(root, str(root / "logs" / "x.log")))
        self.assertTrue(is_monitored(root, str(root / "src" / "firetv_kiosk.py")))
        self.assertTrue(is_monitored(root, "config.example.json"))
        self.assertFalse(is_monitored(root, "/etc/passwd"))

    def test_missing_status_is_not_validated(self):
        """Sem status gravado → NÃO VALIDADO."""
        self.assertEqual(current_status(self.project())["status"], STATUS_NOT_VALIDATED)


def _load_module(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HookTests(_ProjectCase):
    def setUp(self):
        super().setUp()
        self.post = _load_module("hook_post_edit")
        self.stop = _load_module("hook_stop")

    def test_post_edit_on_monitored_file_failure_exit_2(self):
        """PostToolUse em arquivo monitorado com falha → exit 2 e detalhes no stderr."""
        root = self.project(FAKE_SUITE_FAIL)
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit",
                   "tool_input": {"file_path": str(root / "src" / "firetv_kiosk.py")}}
        code, stdout, stderr = self.post.handle(payload, root, suites=FAKE_SUITES)
        self.assertEqual(code, 2)
        self.assertIn("REPROVADO", stderr)
        self.assertIn("reproduzir", stderr)

    def test_post_edit_on_monitored_file_success_exit_0(self):
        """PostToolUse com sucesso → exit 0 e contexto APROVADO."""
        root = self.project()
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(root / "scripts" / "helper.py")}}
        code, stdout, _ = self.post.handle(payload, root, suites=FAKE_SUITES)
        self.assertEqual(code, 0)
        self.assertIn("APROVADO", json.loads(stdout)["hookSpecificOutput"]["additionalContext"])

    def test_post_edit_ignores_unmonitored_file(self):
        """Edição fora do conjunto monitorado não dispara validação."""
        root = self.project(FAKE_SUITE_FAIL)
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(root / "README.md")}}
        self.assertEqual(self.post.handle(payload, root, suites=FAKE_SUITES), (0, "", ""))
        self.assertIsNone(read_status(root))

    def test_post_bash_detects_terminal_change(self):
        """Bash: só valida quando o hash mudou (alteração pelo terminal)."""
        root = self.project()
        validate(root, FAKE_SUITES)
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        self.assertEqual(self.post.handle(payload, root, suites=FAKE_SUITES), (0, "", ""))
        (root / "src" / "firetv_kiosk.py").write_text(FAKE_SUITE_FAIL, encoding="utf-8")
        code, _, stderr = self.post.handle(payload, root, suites=FAKE_SUITES)
        self.assertEqual(code, 2)
        self.assertIn("REPROVADO", stderr)

    def test_stop_blocks_when_failing_and_respects_stop_hook_active(self):
        """Stop bloqueia (exit 2) com falha; com stop_hook_active não bloqueia (sem loop)."""
        root = self.project(FAKE_SUITE_FAIL)
        code, _, stderr = self.stop.handle({"stop_hook_active": False}, root, suites=FAKE_SUITES)
        self.assertEqual(code, 2)
        self.assertIn("REPROVADO", stderr)
        code, stdout, _ = self.stop.handle({"stop_hook_active": True}, root, suites=FAKE_SUITES)
        self.assertEqual(code, 0)
        self.assertIn("REPROVADO", json.loads(stdout)["systemMessage"])

    def test_stop_allows_when_approved_without_revalidating(self):
        """Stop libera quando o status atual é APROVADO para o conteúdo atual."""
        root = self.project()
        validate(root, FAKE_SUITES)
        stamp = read_status(root)["timestamp"]
        self.assertEqual(self.stop.handle({"stop_hook_active": False}, root, suites=FAKE_SUITES)[0], 0)
        self.assertEqual(read_status(root)["timestamp"], stamp)

    def test_stop_revalidates_stale_status(self):
        """Stop revalida quando o conteúdo mudou desde o último APROVADO."""
        root = self.project()
        validate(root, FAKE_SUITES)
        (root / "src" / "firetv_kiosk.py").write_text(FAKE_SUITE_EMPTY, encoding="utf-8")
        code, _, stderr = self.stop.handle({"stop_hook_active": False}, root, suites=FAKE_SUITES)
        self.assertEqual(code, 2)
        self.assertIn(STATUS_NOT_VALIDATED, stderr)

    def test_hook_with_invalid_stdin_does_not_crash(self):
        """Entrada inválida no hook gera exit 0 com aviso (não trava o Claude)."""
        self.assertEqual(self.post.parse_payload("not json"), {})


class WatcherTests(_ProjectCase):
    def test_watcher_validates_only_on_change(self):
        """Observador valida quando o hash muda e não repete para o mesmo conteúdo."""
        watcher = _load_module("watch_files")
        root = self.project()
        calls = []
        state = {"last_hash": None}

        def fake_validate(r):
            calls.append(content_hash(r))
            return {"status": STATUS_APPROVED, "content_hash": content_hash(r), "total_tests": 1,
                    "failed_tests": 0, "skipped_tests": 0, "timestamp": "t", "failures": [], "problems": []}
        watcher.poll_once(root, state, validate_fn=fake_validate, out=open(os.devnull, "w"))
        watcher.poll_once(root, state, validate_fn=fake_validate, out=open(os.devnull, "w"))
        self.assertEqual(len(calls), 1)
        (root / "scripts" / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
        watcher.poll_once(root, state, validate_fn=fake_validate, out=open(os.devnull, "w"))
        self.assertEqual(len(calls), 2)


SELF_TEST_CASES = (ValidationTests, HookTests, WatcherTests)


def run_self_test(as_json: bool, name_filter: Optional[str] = None) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in SELF_TEST_CASES:
        for test in loader.loadTestsFromTestCase(case):
            if not name_filter or name_filter in test.id():
                suite.addTest(test)
    result = unittest.TestResult()
    suite.run(result)
    failures = []
    for kind, items in (("failure", result.failures), ("error", result.errors)):
        for test, tb_text in items:
            method = test._testMethodName
            doc = (getattr(test, method).__doc__ or method).strip()
            last = tb_text.strip().splitlines()[-1]
            location = ""
            for line in tb_text.splitlines():
                if 'File "' in line and "validate.py" in line:
                    location = "scripts/validate.py:" + line.split("line ")[1].split(",")[0]
            expected, observed = "", last
            if " != " in last:
                observed, expected = last.split(": ", 1)[-1].split(" != ", 1)
            failures.append({"script": "scripts/validate.py", "behavior": doc, "test_name": test.id(),
                             "kind": kind, "expected": expected or f"asserção satisfeita: {doc}",
                             "observed": observed, "location": location,
                             "reproduce": f"python scripts/validate.py --self-test --test {method}"})
    total, failed, skipped = result.testsRun, len(failures), len(result.skipped)
    status = STATUS_FAILED if failed else (STATUS_NOT_VALIDATED if total == 0 or skipped else STATUS_APPROVED)
    report = {"status": status, "total_tests": total, "failed_tests": failed, "skipped_tests": skipped,
              "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "failures": failures}
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report({**report, "content_hash": "-", "problems": []}))
    return EXIT_BY_STATUS[status]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validação externa do firetv-kiosk")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    parser.add_argument("--status", action="store_true", help="mostra o último resultado (sem validar)")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--test")
    args = parser.parse_args(argv)
    if args.self_test:
        try:
            return run_self_test(args.json, args.test)
        except Exception:  # never report a crash as approval
            traceback.print_exc()
            return EXIT_BY_STATUS[STATUS_NOT_VALIDATED]
    root = Path(args.root)
    result = current_status(root) if args.status else validate(root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return EXIT_BY_STATUS[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
