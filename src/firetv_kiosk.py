#!/usr/bin/env python3
"""Fire TV Kiosk Controller.

Controls a Fire TV Stick (Fire OS) over ADB Wi-Fi so it opens a web page in a
kiosk browser and recovers from hangs. Runs on the PC / Raspberry Pi, never on
the device (Amazon blocks local ADB on recent Fire OS builds).

Internal identifiers are in English; user-facing messages are in Portuguese.

Usage:
    python src/firetv_kiosk.py --config config.json <command> [--dry-run] [--yes]
    python src/firetv_kiosk.py discover [--subnet 192.168.0.0/24]
    python src/firetv_kiosk.py --self-test [--json] [--test NAME]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import io
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import urllib.parse
from pathlib import Path
from typing import Callable, Iterable, Optional

SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_REL = "src/firetv_kiosk.py"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NOT_VALIDATED = 2

SILK_PACKAGE = "com.amazon.cloud9"
FULLY_MAIN_ACTIVITY = ".MainActivity"
CONNECT_ATTEMPTS = 3
DEFAULT_ADB_TIMEOUT = 20
INSTALL_TIMEOUT = 300
LAUNCH_MODES = ("kiosk_app", "kiosk_app_url", "silk_intent")

VEGA_OS_HINT = (
    "Não foi possível conectar ao Fire Stick por ADB após {attempts} tentativas.\n"
    "Verifique: (1) o IP está correto e o aparelho está ligado na mesma rede; "
    "(2) Configurações > Minha Fire TV > Opções do desenvolvedor > Depuração ADB = ATIVADO; "
    "(3) na primeira conexão, aceite o pedido 'Permitir depuração USB?' na TV.\n"
    "Se o seu modelo for Fire TV Stick HD (2026), 4K Select ou 4K (3ª geração), ele pode usar "
    "Vega OS, que NÃO oferece ADB nem instalação de APK: este projeto não funciona nesses modelos."
)

LOGGER_NAME = "firetv_kiosk"
logger = logging.getLogger(LOGGER_NAME)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class KioskError(Exception):
    """Base error with a user-facing (Portuguese) message."""


class ConfigError(KioskError):
    pass


class ConnectionFailedError(KioskError):
    pass


class PreconditionError(KioskError):
    pass


class StateError(KioskError):
    pass


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Config:
    device_ip: str
    start_url: str
    adb_port: int = 5555
    adb_binary: str = "adb"
    kiosk_package: str = "de.ozerov.fully"
    kiosk_apk_path: Optional[str] = None
    launch_mode: str = "kiosk_app"
    watch_interval_seconds: int = 60
    max_relaunch_attempts: int = 3
    reboot_times: list = dataclasses.field(default_factory=list)
    disabled_packages: list = dataclasses.field(default_factory=list)
    log_file: str = "logs/firetv_kiosk.log"
    state_file: str = "logs/firetv_kiosk_state.json"

    @property
    def serial(self) -> str:
        return f"{self.device_ip}:{self.adb_port}"

    @property
    def expected_foreground_package(self) -> str:
        return SILK_PACKAGE if self.launch_mode == "silk_intent" else self.kiosk_package


REQUIRED_KEYS = ("device_ip", "start_url")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_config_dict(raw: dict, base_dir: Path) -> Config:
    """Validate a raw config dict and return a Config. Raises ConfigError listing every problem."""
    if not isinstance(raw, dict):
        raise ConfigError("Configuração inválida: o arquivo deve conter um objeto JSON.")
    errors = []
    known = {f.name for f in dataclasses.fields(Config)}
    for key in raw:
        if key not in known:
            errors.append(f"chave desconhecida '{key}' (verifique a grafia)")
    for key in REQUIRED_KEYS:
        if key not in raw or raw[key] in (None, ""):
            errors.append(f"campo obrigatório ausente: '{key}'")
    if errors and any(e.startswith("campo obrigatório") for e in errors):
        raise ConfigError("Configuração inválida:\n  - " + "\n  - ".join(errors))

    values = {k: v for k, v in raw.items() if k in known}
    cfg = Config(**values)

    try:
        ip = ipaddress.ip_address(str(cfg.device_ip))
        if ip.version != 4:
            errors.append(f"device_ip deve ser IPv4: '{cfg.device_ip}'")
    except ValueError:
        errors.append(f"device_ip inválido: '{cfg.device_ip}'")
    if not _is_int(cfg.adb_port) or not 1 <= cfg.adb_port <= 65535:
        errors.append(f"adb_port deve ser inteiro entre 1 e 65535: '{cfg.adb_port}'")
    parsed = urllib.parse.urlparse(str(cfg.start_url))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        errors.append(f"start_url deve começar com http:// ou https:// e ter domínio: '{cfg.start_url}'")
    if not isinstance(cfg.adb_binary, str) or not cfg.adb_binary.strip():
        errors.append("adb_binary deve ser um texto não vazio")
    if not isinstance(cfg.kiosk_package, str) or not PACKAGE_RE.match(cfg.kiosk_package):
        errors.append(f"kiosk_package inválido: '{cfg.kiosk_package}'")
    if cfg.kiosk_apk_path is not None and not isinstance(cfg.kiosk_apk_path, str):
        errors.append("kiosk_apk_path deve ser texto ou null")
    if cfg.launch_mode not in LAUNCH_MODES:
        errors.append(f"launch_mode deve ser um de {LAUNCH_MODES}: '{cfg.launch_mode}'")
    for name in ("watch_interval_seconds", "max_relaunch_attempts"):
        value = getattr(cfg, name)
        if not _is_int(value) or value <= 0:
            errors.append(f"{name} deve ser inteiro positivo: '{value}'")
    if not isinstance(cfg.reboot_times, list):
        errors.append("reboot_times deve ser uma lista de horários 'HH:MM'")
    else:
        for item in cfg.reboot_times:
            if not isinstance(item, str) or not TIME_RE.match(item):
                errors.append(f"horário inválido em reboot_times (use HH:MM, 00:00–23:59): '{item}'")
    if not isinstance(cfg.disabled_packages, list):
        errors.append("disabled_packages deve ser uma lista")
    else:
        for item in cfg.disabled_packages:
            if not isinstance(item, str) or not PACKAGE_RE.match(item):
                errors.append(f"package inválido em disabled_packages: '{item}'")
            elif item == cfg.kiosk_package:
                errors.append(f"disabled_packages não pode conter o próprio kiosk: '{item}'")
    for name in ("log_file", "state_file"):
        if not isinstance(getattr(cfg, name), str) or not getattr(cfg, name).strip():
            errors.append(f"{name} deve ser um caminho não vazio")
    if errors:
        raise ConfigError("Configuração inválida:\n  - " + "\n  - ".join(errors))

    # Resolve relative paths against the config file's directory.
    def resolve(path_value: str) -> str:
        path = Path(os.path.expanduser(path_value))
        return str(path if path.is_absolute() else (base_dir / path))

    cfg.log_file = resolve(cfg.log_file)
    cfg.state_file = resolve(cfg.state_file)
    if cfg.kiosk_apk_path:
        cfg.kiosk_apk_path = resolve(cfg.kiosk_apk_path)
    return cfg


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"Arquivo de configuração não encontrado: {path}\n"
            "Copie config.example.json para config.json e ajuste o device_ip."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON inválido em {path}: linha {exc.lineno}, coluna {exc.colno}: {exc.msg}") from exc
    return validate_config_dict(raw, path.resolve().parent)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def close_logging() -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


# --------------------------------------------------------------------------- #
# ADB runner (single substitutable interface)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class AdbResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        # A timeout is never success.
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


class AdbRunner:
    """Every ADB call goes through run(); subclasses implement _execute()."""

    def __init__(self, adb_binary: str = "adb"):
        self.adb_binary = adb_binary

    def run(self, args: list, timeout: float = DEFAULT_ADB_TIMEOUT) -> AdbResult:
        started = time.monotonic()
        result = self._execute(list(args), timeout)
        if not result.duration:
            result.duration = time.monotonic() - started
        logger.info(
            "adb %s | rc=%s timed_out=%s duration=%.3fs",
            shlex.join(str(a) for a in args), result.returncode, result.timed_out, result.duration,
        )
        return result

    def _execute(self, args: list, timeout: float) -> AdbResult:  # pragma: no cover - abstract
        raise NotImplementedError


class SubprocessAdbRunner(AdbRunner):
    def _execute(self, args: list, timeout: float) -> AdbResult:
        try:
            proc = subprocess.run(
                [self.adb_binary, *args], capture_output=True, text=True, timeout=timeout,
            )
            return AdbResult(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return AdbResult(-1, str(exc.stdout or ""), f"timeout após {timeout}s", timed_out=True)
        except FileNotFoundError:
            return AdbResult(127, "", f"executável adb não encontrado: {self.adb_binary}")


class DryRunAdbRunner(AdbRunner):
    """Prints commands instead of running them."""

    def __init__(self, adb_binary: str = "adb", out=None):
        super().__init__(adb_binary)
        self.out = out
        self.commands: list = []

    def _execute(self, args: list, timeout: float) -> AdbResult:
        command = shlex.join([self.adb_binary, *map(str, args)])
        self.commands.append(command)
        print(f"[dry-run] {command}", file=self.out or sys.stdout)
        return AdbResult(0, "", "")


class FakeAdbRunner(AdbRunner):
    """Simulator for tests: rules map a command substring to a result (or a list consumed in order).

    When several substrings match, the longest one wins.
    """

    def __init__(self, rules: Optional[dict] = None, default: Optional[AdbResult] = None):
        super().__init__("adb")
        self.rules = dict(rules or {})
        self.default = default or AdbResult(0, "", "")
        self.calls: list = []

    def set(self, pattern: str, result):
        self.rules[pattern] = result

    def _execute(self, args: list, timeout: float) -> AdbResult:
        command = " ".join(map(str, args))
        self.calls.append(command)
        # The most specific (longest) matching pattern wins.
        for pattern, result in sorted(self.rules.items(), key=lambda kv: -len(kv[0])):
            if pattern in command:
                if isinstance(result, list):
                    item = result.pop(0) if len(result) > 1 else result[0]
                    return dataclasses.replace(item)
                if callable(result):
                    return result(command)
                return dataclasses.replace(result)
        return dataclasses.replace(self.default)

    def commands_matching(self, fragment: str) -> list:
        return [c for c in self.calls if fragment in c]


def find_adb_binary(configured: str, which: Callable = shutil.which, exists: Callable = os.path.isfile,
                    env: Optional[dict] = None, platform: str = sys.platform) -> Optional[str]:
    """Locate the adb executable: configured value, PATH, then common SDK locations."""
    env = os.environ if env is None else env
    if configured and (os.sep in configured or "/" in configured):
        return configured if exists(configured) else None
    found = which(configured or "adb")
    if found:
        return found
    home = env.get("HOME") or env.get("USERPROFILE") or ""
    candidates = []
    if platform.startswith("win"):
        local = env.get("LOCALAPPDATA", "")
        candidates += [
            os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"),
            r"C:\platform-tools\adb.exe",
            os.path.join(home, "platform-tools", "adb.exe"),
        ]
    else:
        candidates += [
            os.path.join(home, "Library", "Android", "sdk", "platform-tools", "adb"),
            os.path.join(home, "Android", "Sdk", "platform-tools", "adb"),
            os.path.join(home, "platform-tools", "adb"),
            "/opt/homebrew/bin/adb",
            "/usr/local/bin/adb",
            "/usr/bin/adb",
        ]
    for candidate in candidates:
        if candidate and exists(candidate):
            return candidate
    return None


ADB_MISSING_MESSAGE = (
    "Executável 'adb' não encontrado. Instale o Android SDK Platform-Tools "
    "(https://developer.android.com/tools/releases/platform-tools), extraia e adicione a pasta ao PATH, "
    "ou informe o caminho completo em 'adb_binary' no config.json."
)


# --------------------------------------------------------------------------- #
# Persistent state (packages disabled by this tool)
# --------------------------------------------------------------------------- #
class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {"disabled_packages": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateError(
                f"Arquivo de estado corrompido: {self.path} ({exc}). "
                "Corrija ou remova o arquivo; a lista de pacotes desativados está em config.json."
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("disabled_packages"), list) \
                or not all(isinstance(p, str) for p in data["disabled_packages"]):
            raise StateError(f"Arquivo de estado com formato inesperado: {self.path}")
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def add_disabled(self, package: str) -> None:
        data = self.load()
        if package not in data["disabled_packages"]:
            data["disabled_packages"].append(package)
        self.save(data)

    def remove_disabled(self, package: str) -> None:
        data = self.load()
        data["disabled_packages"] = [p for p in data["disabled_packages"] if p != package]
        self.save(data)


# --------------------------------------------------------------------------- #
# Device controller
# --------------------------------------------------------------------------- #
FOCUS_RE = re.compile(r"(?:mCurrentFocus|mFocusedApp)=.*?\s([A-Za-z][\w.]*)/")


def parse_foreground_package(dumpsys_output: str) -> Optional[str]:
    """Extract the focused package from `dumpsys window` output (mCurrentFocus preferred)."""
    current = None
    focused_app = None
    for line in dumpsys_output.splitlines():
        match = FOCUS_RE.search(line)
        if not match:
            continue
        if "mCurrentFocus" in line and current is None:
            current = match.group(1)
        elif "mFocusedApp" in line and focused_app is None:
            focused_app = match.group(1)
    return current or focused_app


class DeviceController:
    def __init__(self, config: Config, runner: AdbRunner, dry_run: bool = False,
                 sleep_fn: Callable[[float], None] = time.sleep, out=None):
        self.config = config
        self.runner = runner
        self.dry_run = dry_run
        self.sleep_fn = sleep_fn
        self.out = out
        self.state = StateStore(config.state_file)

    # -- helpers ---------------------------------------------------------- #
    def say(self, message: str) -> None:
        print(message, file=self.out or sys.stdout)

    def shell(self, command: list, timeout: float = DEFAULT_ADB_TIMEOUT) -> AdbResult:
        return self.runner.run(["-s", self.config.serial, "shell", *command], timeout=timeout)

    # -- connection ------------------------------------------------------- #
    def connect_once(self) -> bool:
        result = self.runner.run(["connect", self.config.serial], timeout=DEFAULT_ADB_TIMEOUT)
        if self.dry_run:
            return True
        text = result.output.lower()
        if not result.ok or "failed" in text or "unable" in text or "cannot" in text:
            return False
        if "connected to" not in text:
            return False
        state = self.runner.run(["-s", self.config.serial, "get-state"], timeout=DEFAULT_ADB_TIMEOUT)
        return state.ok and state.stdout.strip() == "device"

    def connect(self, attempts: int = CONNECT_ATTEMPTS, base_delay: float = 2.0) -> None:
        for attempt in range(1, attempts + 1):
            if self.connect_once():
                logger.info("connected to %s on attempt %d", self.config.serial, attempt)
                return
            if attempt < attempts:
                self.sleep_fn(base_delay * (2 ** (attempt - 1)))
        raise ConnectionFailedError(VEGA_OS_HINT.format(attempts=attempts))

    def check_compatibility(self) -> dict:
        manufacturer = self.shell(["getprop", "ro.product.manufacturer"])
        sdk = self.shell(["getprop", "ro.build.version.sdk"])
        model = self.shell(["getprop", "ro.product.model"])
        if self.dry_run:
            return {"manufacturer": "(dry-run)", "sdk": "(dry-run)", "model": "(dry-run)"}
        if not manufacturer.ok or not sdk.ok:
            raise ConnectionFailedError("Falha ao ler propriedades do aparelho (conexão ADB instável ou expirada).")
        info = {"manufacturer": manufacturer.stdout.strip(), "sdk": sdk.stdout.strip(), "model": model.stdout.strip()}
        if "amazon" not in info["manufacturer"].lower():
            raise PreconditionError(
                f"O aparelho em {self.config.serial} não parece ser um Fire TV (fabricante: '{info['manufacturer']}')."
            )
        if not info["sdk"].isdigit():
            raise PreconditionError(f"Versão de Android não reconhecida: '{info['sdk']}'.")
        return info

    # -- packages --------------------------------------------------------- #
    def _package_listed(self, package: str, extra_flags: Iterable[str] = ()) -> bool:
        result = self.shell(["pm", "list", "packages", *extra_flags, package])
        if not result.ok:
            raise ConnectionFailedError(f"Falha ao consultar pacotes no aparelho: {result.output.strip()}")
        return any(line.strip() == f"package:{package}" for line in result.stdout.splitlines())

    def is_installed(self, package: Optional[str] = None) -> bool:
        return self._package_listed(package or self.config.kiosk_package)

    def is_disabled(self, package: str) -> bool:
        return self._package_listed(package, ["-d"])

    def install_apk(self) -> None:
        apk = self.config.kiosk_apk_path
        if not apk:
            raise PreconditionError(
                "O kiosk não está instalado e 'kiosk_apk_path' não foi definido no config.json."
            )
        path = Path(apk)
        if not path.is_file():
            raise PreconditionError(f"APK não encontrado: {apk}")
        if path.stat().st_size == 0:
            raise PreconditionError(f"APK vazio (0 bytes): {apk}")
        result = self.runner.run(["-s", self.config.serial, "install", "-r", str(path)], timeout=INSTALL_TIMEOUT)
        if self.dry_run:
            return
        if not result.ok or "Success" not in result.stdout:
            raise KioskError(f"Falha ao instalar o APK: {result.output.strip() or 'sem saída'}")

    def disable_packages(self) -> list:
        changed = []
        for package in self.config.disabled_packages:
            if not self.dry_run and not self.is_installed(package):
                self.say(f"  - {package}: não instalado, ignorado")
                continue
            if not self.dry_run and self.is_disabled(package):
                self.state.add_disabled(package)
                continue
            result = self.shell(["pm", "disable-user", "--user", "0", package])
            if self.dry_run:
                continue
            if not result.ok or "disabled" not in result.stdout:
                raise KioskError(f"Falha ao desativar {package}: {result.output.strip()}")
            self.state.add_disabled(package)
            changed.append(package)
        return changed

    def restore_packages(self) -> list:
        data = self.state.load()
        restored = []
        failures = []
        for package in list(data["disabled_packages"]):
            result = self.shell(["pm", "enable", package])
            if self.dry_run:
                continue
            if result.ok and "enabled" in result.stdout:
                self.state.remove_disabled(package)
                restored.append(package)
            else:
                failures.append(f"{package}: {result.output.strip()}")
        if failures:
            raise KioskError("Falha ao reativar: " + "; ".join(failures))
        return restored

    # -- kiosk ------------------------------------------------------------ #
    def launch_command(self) -> list:
        cfg = self.config
        quoted_url = shlex.quote(cfg.start_url)
        if cfg.launch_mode == "kiosk_app":
            return ["monkey", "-p", cfg.kiosk_package, "-c", "android.intent.category.LAUNCHER", "1"]
        if cfg.launch_mode == "kiosk_app_url":
            return ["am", "start", "-a", "android.intent.action.VIEW", "-d", quoted_url,
                    "-n", f"{cfg.kiosk_package}/{FULLY_MAIN_ACTIVITY}"]
        return ["am", "start", "-a", "android.intent.action.VIEW", "-d", quoted_url]

    def launch(self) -> None:
        result = self.shell(self.launch_command())
        if self.dry_run:
            return
        text = result.output
        failed = (not result.ok or "Error" in text or "Exception" in text
                  or "No activities found" in text or "aborted" in text.lower())
        if failed and self.config.launch_mode == "kiosk_app_url":
            logger.warning("VIEW intent to kiosk failed, falling back to LAUNCHER: %s", text.strip())
            fallback = self.shell(["monkey", "-p", self.config.kiosk_package,
                                   "-c", "android.intent.category.LAUNCHER", "1"])
            if fallback.ok and "No activities found" not in fallback.output and "aborted" not in fallback.output.lower():
                return
            text = fallback.output
        elif not failed:
            return
        raise KioskError(f"Falha ao abrir o kiosk: {text.strip() or 'sem saída'}")

    def foreground_package(self) -> Optional[str]:
        result = self.shell(["dumpsys", "window"])
        if not result.ok:
            raise ConnectionFailedError(f"Falha ao consultar a janela em primeiro plano: {result.output.strip()}")
        return parse_foreground_package(result.stdout)

    def reboot(self) -> None:
        result = self.runner.run(["-s", self.config.serial, "reboot"], timeout=DEFAULT_ADB_TIMEOUT)
        if not self.dry_run and not result.ok:
            raise KioskError(f"Falha ao reiniciar o aparelho: {result.output.strip()}")

    def trim_caches(self) -> None:
        """Non-destructive: asks Android to trim app caches (no user data removed)."""
        self.shell(["pm", "trim-caches", "999G"], timeout=60)

    def clear_kiosk_data(self, confirmed: bool) -> None:
        if not confirmed:
            raise PreconditionError(
                f"'clear-cache' executa 'pm clear {self.config.kiosk_package}', que APAGA todos os dados e "
                "configurações do kiosk. Repita com --yes para confirmar."
            )
        result = self.shell(["pm", "clear", self.config.kiosk_package])
        if not self.dry_run and (not result.ok or "Success" not in result.stdout):
            raise KioskError(f"Falha ao limpar dados do kiosk: {result.output.strip()}")


# --------------------------------------------------------------------------- #
# Watch mode
# --------------------------------------------------------------------------- #
class Watcher:
    """One tick = one observation/decision. run() loops ticks until a signal arrives."""

    def __init__(self, controller: DeviceController, reconnect_base: float = 5.0, reconnect_max: float = 300.0):
        self.controller = controller
        self.config = controller.config
        self.connected = False
        self.relaunch_failures = 0
        self.reconnect_failures = 0
        self.next_connect_at: Optional[dt.datetime] = None
        self.last_scheduled_reboot_key: Optional[str] = None
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self.stop_requested = False

    def _schedule_reconnect(self, now: dt.datetime) -> None:
        delay = min(self.reconnect_base * (2 ** self.reconnect_failures), self.reconnect_max)
        self.reconnect_failures += 1
        self.next_connect_at = now + dt.timedelta(seconds=delay)

    def tick(self, now: dt.datetime) -> str:
        ctl = self.controller
        if not self.connected:
            if self.next_connect_at and now < self.next_connect_at:
                return "waiting_reconnect"
            if ctl.connect_once():
                self.connected = True
                self.reconnect_failures = 0
                self.next_connect_at = None
                logger.info("watch: connected")
            else:
                self._schedule_reconnect(now)
                logger.warning("watch: reconnect failed, next attempt at %s", self.next_connect_at)
                return "disconnected"

        minute = now.strftime("%H:%M")
        reboot_key = now.strftime("%Y-%m-%d ") + minute
        if minute in self.config.reboot_times and self.last_scheduled_reboot_key != reboot_key:
            self.last_scheduled_reboot_key = reboot_key
            ctl.trim_caches()
            ctl.reboot()
            self.connected = False
            self.relaunch_failures = 0
            self.next_connect_at = now + dt.timedelta(seconds=60)
            logger.info("watch: scheduled reboot at %s", minute)
            return "scheduled_reboot"

        try:
            foreground = ctl.foreground_package()
        except ConnectionFailedError as exc:
            logger.warning("watch: connection lost: %s", exc)
            self.connected = False
            self._schedule_reconnect(now)
            return "connection_lost"

        if foreground == self.config.expected_foreground_package:
            self.relaunch_failures = 0
            return "ok"

        if self.relaunch_failures >= self.config.max_relaunch_attempts:
            logger.warning("watch: %d failed relaunches, rebooting device", self.relaunch_failures)
            ctl.reboot()
            self.relaunch_failures = 0
            self.connected = False
            self.next_connect_at = now + dt.timedelta(seconds=60)
            return "reboot"

        self.relaunch_failures += 1
        logger.info("watch: foreground is %s, relaunching (attempt %d)", foreground, self.relaunch_failures)
        try:
            ctl.launch()
        except KioskError as exc:
            logger.warning("watch: relaunch failed: %s", exc)
        return "relaunched"

    def run(self, clock: Callable[[], dt.datetime] = dt.datetime.now, sleep_fn: Callable = time.sleep) -> None:
        def handle_signal(signum, _frame):
            self.stop_requested = True
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        self.controller.say(f"Modo watch iniciado (intervalo {self.config.watch_interval_seconds}s). Ctrl+C para sair.")
        while not self.stop_requested:
            try:
                outcome = self.tick(clock())
            except KioskError as exc:
                outcome = f"error: {exc}"
                logger.error("watch: %s", exc)
            if outcome not in ("ok", "waiting_reconnect"):
                self.controller.say(f"[{clock():%Y-%m-%d %H:%M:%S}] {outcome}")
            # Sleep in small steps so signals stop the loop promptly.
            remaining = float(self.config.watch_interval_seconds)
            while remaining > 0 and not self.stop_requested:
                step = min(1.0, remaining)
                sleep_fn(step)
                remaining -= step
        self.controller.say("Modo watch encerrado.")


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def local_ipv4() -> Optional[str]:
    """Best-effort local IPv4 (UDP 'connect' sends no packets)."""
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            address = sock.getsockname()[0]
            if not address.startswith("127."):
                return address
    return None


def tcp_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_for_adb(network: ipaddress.IPv4Network, port: int = 5555,
                 probe: Callable[[str, int], bool] = tcp_port_open, max_workers: int = 64) -> list:
    hosts = [str(h) for h in network.hosts()]
    if len(hosts) > 1024:
        raise PreconditionError(f"Sub-rede grande demais para varredura ({len(hosts)} endereços); use /22 ou menor.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda h: (h, probe(h, port)), hosts))
    return [h for h, is_open in results if is_open]


def probe_fire_tv(runner: AdbRunner, host: str, port: int) -> tuple:
    """Connect to host:port and return (is_fire_tv, label)."""
    serial = f"{host}:{port}"
    connect = runner.run(["connect", serial])
    if not connect.ok or "connected to" not in connect.output.lower():
        return False, "porta aberta (conexão ADB não autorizada ou recusada — aceite o pedido na TV e tente de novo)"
    manufacturer = runner.run(["-s", serial, "shell", "getprop", "ro.product.manufacturer"]).stdout.strip()
    model = runner.run(["-s", serial, "shell", "getprop", "ro.product.model"]).stdout.strip()
    label = f"{manufacturer} {model}".strip() or "aparelho Android"
    is_fire_tv = "amazon" in manufacturer.lower()
    if is_fire_tv:
        label += "  <-- Fire TV"
    return is_fire_tv, label


def identify_device(runner: AdbRunner, host: str, port: int) -> str:
    return probe_fire_tv(runner, host, port)[1]


def local_network(local_ip_fn: Callable[[], Optional[str]] = local_ipv4) -> Optional[ipaddress.IPv4Network]:
    ip = local_ip_fn()
    return ipaddress.ip_network(f"{ip}/24", strict=False) if ip else None


def command_discover(subnet: Optional[str], port: int, runner: Optional[AdbRunner], out,
                     probe: Callable[[str, int], bool] = tcp_port_open,
                     local_ip_fn: Callable[[], Optional[str]] = local_ipv4) -> int:
    if subnet:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            print(f"Sub-rede inválida: '{subnet}' (exemplo: 192.168.0.0/24)", file=out)
            return EXIT_FAILURE
    else:
        ip = local_ip_fn()
        if not ip:
            print("Não foi possível descobrir o IP deste computador. Informe --subnet 192.168.X.0/24.", file=out)
            return EXIT_FAILURE
        network = ipaddress.ip_network(f"{ip}/24", strict=False)
    print(f"Procurando aparelhos com ADB (porta {port}) em {network} ...", file=out)
    found = scan_for_adb(network, port, probe)
    if not found:
        print(
            "Nenhum aparelho com ADB encontrado. No Fire Stick: Configurações > Minha Fire TV > "
            "Opções do desenvolvedor > Depuração ADB = ATIVADO. O IP aparece em "
            "Configurações > Minha Fire TV > Sobre > Rede.",
            file=out,
        )
        return EXIT_FAILURE
    for host in found:
        label = identify_device(runner, host, port) if runner else "porta ADB aberta"
        print(f"  {host}: {label}", file=out)
    print("Use o IP encontrado em 'device_ip' no config.json.", file=out)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def command_check(ctl: DeviceController) -> int:
    ctl.connect()
    info = ctl.check_compatibility()
    ctl.say(f"Conectado a {ctl.config.serial}: {info['manufacturer']} {info['model']} (SDK {info['sdk']}).")
    if not ctl.is_installed():
        if ctl.dry_run:
            return EXIT_OK
        raise PreconditionError(f"Kiosk '{ctl.config.kiosk_package}' não está instalado. Rode o comando 'setup'.")
    ctl.say(f"Kiosk '{ctl.config.kiosk_package}' instalado. Tudo pronto.")
    return EXIT_OK


def command_setup(ctl: DeviceController) -> int:
    ctl.connect()
    ctl.check_compatibility()
    if ctl.is_installed() and not ctl.dry_run:
        ctl.say(f"Kiosk '{ctl.config.kiosk_package}' já instalado; instalação ignorada.")
    else:
        ctl.say("Instalando o kiosk ...")
        ctl.install_apk()
        if not ctl.dry_run and not ctl.is_installed():
            raise KioskError("A instalação terminou, mas o pacote não aparece no aparelho.")
        ctl.say("Kiosk instalado.")
    if ctl.config.disabled_packages:
        changed = ctl.disable_packages()
        ctl.say(f"Pacotes desativados agora: {changed or 'nenhum (já estavam desativados)'}")
    ctl.say(
        "Setup concluído. Configure o Fully uma única vez (veja README, seção 'Configurar o Fully'):\n"
        f"  Start URL = {ctl.config.start_url}; Launch on Boot; Bring to Foreground; Auto Reload on error."
    )
    return EXIT_OK


def command_launch(ctl: DeviceController) -> int:
    ctl.connect()
    ctl.launch()
    ctl.say(f"Kiosk aberto ({ctl.config.launch_mode}): {ctl.config.start_url}")
    return EXIT_OK


def command_reboot(ctl: DeviceController) -> int:
    ctl.connect()
    ctl.reboot()
    ctl.say("Reinicialização solicitada.")
    return EXIT_OK


def command_clear_cache(ctl: DeviceController, confirmed: bool) -> int:
    if not confirmed:
        ctl.clear_kiosk_data(False)  # refuses before connecting
    ctl.connect()
    ctl.clear_kiosk_data(True)
    ctl.say("Dados do kiosk apagados. Reconfigure o Fully (Start URL etc.).")
    return EXIT_OK


def command_restore(ctl: DeviceController) -> int:
    ctl.state.load()  # fail fast on corrupt state before touching the device
    ctl.connect()
    restored = ctl.restore_packages()
    ctl.say(f"Pacotes reativados: {restored or 'nenhum'}")
    return EXIT_OK


def command_watch(ctl: DeviceController, once: bool = False) -> int:
    watcher = Watcher(ctl)
    if once or ctl.dry_run:
        ctl.say(f"Resultado: {watcher.tick(dt.datetime.now())}")
        return EXIT_OK
    watcher.run()
    return EXIT_OK


DEFAULT_CONFIG_PATH = SCRIPT_PATH.parent.parent / "config.json"
EXAMPLE_CONFIG_PATH = SCRIPT_PATH.parent.parent / "config.example.json"


def find_fire_tv(runner: AdbRunner, port: int, out,
                 probe: Callable[[str, int], bool] = tcp_port_open,
                 local_ip_fn: Callable[[], Optional[str]] = local_ipv4) -> str:
    """Scan the local /24 and return the IP of the first device identified as a Fire TV."""
    network = local_network(local_ip_fn)
    if network is None:
        raise PreconditionError("Não foi possível descobrir a rede deste computador. Ele está conectado ao Wi-Fi/cabo?")
    print(f"Procurando o Fire TV na rede {network} (pode levar alguns segundos) ...", file=out, flush=True)
    candidates = scan_for_adb(network, port, probe)
    labels = []
    for host in candidates:
        is_fire_tv, label = probe_fire_tv(runner, host, port)
        labels.append(f"{host}: {label}")
        if is_fire_tv:
            print(f"Fire TV encontrado: {host}", file=out, flush=True)
            return host
    detail = ("\n  Encontrados, mas não identificados como Fire TV:\n  " + "\n  ".join(labels)) if labels else ""
    raise PreconditionError(
        "Nenhum Fire TV encontrado na rede." + detail + "\n"
        "Confira no Fire TV: Configurações > Minha Fire TV > Opções do desenvolvedor > Depuração ADB = ATIVADO, "
        "e aceite 'Permitir depuração USB?' na tela da TV. Depois rode de novo."
    )


def create_config(config_path: Path, device_ip: str, example_path: Path = EXAMPLE_CONFIG_PATH) -> None:
    raw: dict = {}
    if example_path.is_file():
        try:
            raw = json.loads(example_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.example.json inválido: {exc}") from exc
    raw.setdefault("start_url", "https://piltover.grupoinlog.com.br/tv/parear")
    raw["device_ip"] = device_ip
    validate_config_dict(raw, config_path.resolve().parent)  # never write an invalid config
    config_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def command_open(config_path: Path, make_runner: Callable[[str], AdbRunner], out,
                 example_path: Path = EXAMPLE_CONFIG_PATH, dry_run: bool = False,
                 probe: Callable[[str, int], bool] = tcp_port_open,
                 local_ip_fn: Callable[[], Optional[str]] = local_ipv4,
                 sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """One-click flow: find the Fire TV (first run only), connect and open start_url.

    Opens in the kiosk app when installed; otherwise falls back to the Silk browser so the
    page still opens without any APK.
    """
    if not config_path.is_file():
        if dry_run:
            print("Erro: config.json não existe; rode sem --dry-run para procurar o Fire TV.", file=out)
            return EXIT_FAILURE
        print("Primeira execução: config.json ainda não existe.", file=out, flush=True)
        try:
            example = json.loads(example_path.read_text(encoding="utf-8")) if example_path.is_file() else {}
        except json.JSONDecodeError:
            example = {}
        adb_binary = example.get("adb_binary", "adb")
        port = example.get("adb_port", 5555)
        try:
            device_ip = find_fire_tv(make_runner(adb_binary), port, out, probe, local_ip_fn)
            create_config(config_path, device_ip, example_path)
        except KioskError as exc:
            print(f"Erro: {exc}", file=out)
            return EXIT_FAILURE
        print(f"Configuração salva em {config_path.name} (device_ip = {device_ip}).", file=out, flush=True)

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        print(f"Erro: {exc}", file=out)
        return EXIT_FAILURE
    setup_logging(cfg.log_file)
    try:
        ctl = DeviceController(cfg, make_runner(cfg.adb_binary), dry_run=dry_run, sleep_fn=sleep_fn, out=out)
        print(f"Conectando ao Fire TV {cfg.serial} ...", file=out, flush=True)
        ctl.connect()
        if not dry_run and cfg.launch_mode != "silk_intent" and not ctl.is_installed():
            print("Fully Kiosk não está instalado; abrindo no navegador Silk (rode 'setup' para o modo kiosk).",
                  file=out, flush=True)
            cfg.launch_mode = "silk_intent"
        ctl.launch()
        print(f"Pronto! A TV está abrindo {cfg.start_url}", file=out)
        logger.info("open finished (launch_mode=%s)", cfg.launch_mode)
        return EXIT_OK
    except KioskError as exc:
        logger.error("open failed: %s", exc)
        print(f"Erro: {exc}", file=out)
        return EXIT_FAILURE
    finally:
        close_logging()


COMMANDS = ("check", "setup", "launch", "watch", "reboot", "clear-cache", "restore", "discover", "open")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firetv_kiosk", description="Controlador de kiosk para Fire TV Stick (Fire OS).")
    parser.add_argument("--config", help="caminho do config.json")
    parser.add_argument("--dry-run", action="store_true", help="mostra os comandos ADB sem executá-los")
    parser.add_argument("--yes", action="store_true", help="confirma operações destrutivas")
    parser.add_argument("--self-test", action="store_true", help="executa os testes embutidos e encerra")
    parser.add_argument("--json", action="store_true", help="(com --self-test) saída em JSON")
    parser.add_argument("--test", help="(com --self-test) executa só testes cujo nome contém este texto")
    parser.add_argument("--subnet", help="(discover) sub-rede a varrer, ex. 192.168.0.0/24")
    parser.add_argument("--once", action="store_true", help="(watch) executa uma única verificação")
    parser.add_argument("command", nargs="?", choices=COMMANDS)
    return parser


def main(argv: Optional[list] = None, runner_factory: Optional[Callable[[Config, bool], AdbRunner]] = None,
         sleep_fn: Callable[[float], None] = time.sleep, out=None) -> int:
    out = out or sys.stdout
    err = sys.stderr if out is sys.stdout else out
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test(as_json=args.json, name_filter=args.test, out=out)
    if not args.command:
        parser.print_usage(err)
        print("Informe um comando ou --self-test.", file=err)
        return EXIT_FAILURE

    if args.command == "discover":
        runner = None
        port = 5555
        if args.config:
            try:
                cfg = load_config(args.config)
                port = cfg.adb_port
                adb_path = find_adb_binary(cfg.adb_binary)
                if adb_path:
                    runner = SubprocessAdbRunner(adb_path)
            except ConfigError:
                pass
        else:
            adb_path = find_adb_binary("adb")
            runner = SubprocessAdbRunner(adb_path) if adb_path else None
        return command_discover(args.subnet, port, runner, out)

    if args.command == "open":
        config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH

        def make_runner(adb_binary: str) -> AdbRunner:
            if runner_factory is not None:
                return runner_factory(None, args.dry_run)
            if args.dry_run:
                return DryRunAdbRunner(adb_binary, out=out)
            adb_path = find_adb_binary(adb_binary)
            if not adb_path:
                raise PreconditionError(ADB_MISSING_MESSAGE)
            return SubprocessAdbRunner(adb_path)
        return command_open(config_path, make_runner, out, dry_run=args.dry_run, sleep_fn=sleep_fn)

    if not args.config:
        print("Erro: --config é obrigatório para este comando.", file=err)
        return EXIT_FAILURE
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Erro: {exc}", file=err)
        return EXIT_FAILURE

    setup_logging(cfg.log_file)
    try:
        if runner_factory is not None:
            runner = runner_factory(cfg, args.dry_run)
        elif args.dry_run:
            runner = DryRunAdbRunner(cfg.adb_binary, out=out)
        else:
            adb_path = find_adb_binary(cfg.adb_binary)
            if not adb_path:
                print(f"Erro: {ADB_MISSING_MESSAGE}", file=err)
                return EXIT_FAILURE
            runner = SubprocessAdbRunner(adb_path)

        ctl = DeviceController(cfg, runner, dry_run=args.dry_run, sleep_fn=sleep_fn, out=out)
        logger.info("command %s started (dry_run=%s)", args.command, args.dry_run)
        try:
            if args.command == "check":
                code = command_check(ctl)
            elif args.command == "setup":
                code = command_setup(ctl)
            elif args.command == "launch":
                code = command_launch(ctl)
            elif args.command == "reboot":
                code = command_reboot(ctl)
            elif args.command == "clear-cache":
                code = command_clear_cache(ctl, args.yes)
            elif args.command == "restore":
                code = command_restore(ctl)
            elif args.command == "watch":
                code = command_watch(ctl, once=args.once)
            else:  # pragma: no cover - argparse restricts choices
                code = EXIT_FAILURE
        except KioskError as exc:
            logger.error("command %s failed: %s", args.command, exc)
            print(f"Erro: {exc}", file=err)
            return EXIT_FAILURE
        logger.info("command %s finished rc=%s", args.command, code)
        return code
    finally:
        close_logging()


# =========================================================================== #
# Embedded self-test suite (--self-test). Uses only FakeAdbRunner + tempfile.
# =========================================================================== #
def ok(stdout: str = "", stderr: str = "") -> AdbResult:
    return AdbResult(0, stdout, stderr)


def fail(stdout: str = "", stderr: str = "error", returncode: int = 1) -> AdbResult:
    return AdbResult(returncode, stdout, stderr)


def healthy_device_rules(installed: bool = True) -> dict:
    return {
        "connect ": ok("connected to 192.168.0.50:5555\n"),
        "get-state": ok("device\n"),
        "ro.product.manufacturer": ok("Amazon\n"),
        "ro.build.version.sdk": ok("28\n"),
        "ro.product.model": ok("AFTKA\n"),
        "pm list packages -d": ok(""),
        "pm list packages de.ozerov.fully": ok("package:de.ozerov.fully\n" if installed else ""),
    }


DUMPSYS_FULLY = (
    "  mCurrentFocus=Window{a1b2 u0 de.ozerov.fully/de.ozerov.fully.MainActivity}\n"
    "  mFocusedApp=AppWindowToken{c3 token=Token{d4 ActivityRecord{e5 u0 de.ozerov.fully/.MainActivity t12}}}\n"
)
DUMPSYS_LAUNCHER = "  mCurrentFocus=Window{f6 u0 com.amazon.tv.launcher/com.amazon.tv.launcher.ui.HomeActivity_vNext}\n"


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="firetv_selftest_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        close_logging()
        self._tmp.cleanup()

    def make_config(self, **overrides) -> Config:
        raw = {"device_ip": "192.168.0.50", "start_url": "https://piltover.grupoinlog.com.br/tv/parear",
               "log_file": "logs/test.log", "state_file": "logs/state.json"}
        raw.update(overrides)
        return validate_config_dict(raw, self.tmp)

    def write_config(self, **overrides) -> Path:
        raw = {"device_ip": "192.168.0.50", "start_url": "https://piltover.grupoinlog.com.br/tv/parear",
               "log_file": "logs/test.log", "state_file": "logs/state.json"}
        raw.update(overrides)
        path = self.tmp / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def controller(self, runner: AdbRunner, **overrides) -> DeviceController:
        return DeviceController(self.make_config(**overrides), runner, sleep_fn=lambda s: None, out=io.StringIO())

    def run_main(self, argv: list, runner: Optional[AdbRunner] = None):
        out = io.StringIO()
        factory = (lambda cfg, dry: runner) if runner is not None else None
        code = main(argv, runner_factory=factory, sleep_fn=lambda s: None, out=out)
        return code, out.getvalue()


class ConfigTests(_TempDirTestCase):
    def test_valid_config_loads_with_defaults(self):
        """Config válida carrega com defaults e caminhos relativos ao arquivo."""
        cfg = load_config(self.write_config())
        self.assertEqual(cfg.adb_port, 5555)
        self.assertEqual(cfg.kiosk_package, "de.ozerov.fully")
        self.assertEqual(cfg.serial, "192.168.0.50:5555")
        self.assertEqual(Path(cfg.log_file), self.tmp / "logs" / "test.log")

    def test_invalid_ip_rejected(self):
        """IP inválido interrompe com ConfigError citando device_ip."""
        with self.assertRaises(ConfigError) as ctx:
            self.make_config(device_ip="192.168.0.300")
        self.assertIn("device_ip", str(ctx.exception))

    def test_port_out_of_range_rejected(self):
        """Porta fora de 1–65535 é rejeitada."""
        for port in (0, 70000, "5555", True):
            with self.assertRaises(ConfigError, msg=f"port={port!r}"):
                self.make_config(adb_port=port)

    def test_url_without_scheme_rejected(self):
        """URL sem http/https é rejeitada."""
        for url in ("piltover.grupoinlog.com.br/tv/parear", "ftp://x.com", "https://"):
            with self.assertRaises(ConfigError, msg=url) as ctx:
                self.make_config(start_url=url)
            self.assertIn("start_url", str(ctx.exception))

    def test_malformed_reboot_time_rejected(self):
        """Horários fora de HH:MM são rejeitados."""
        for value in ("4:00", "24:00", "12:60", "noon", 400):
            with self.assertRaises(ConfigError, msg=repr(value)):
                self.make_config(reboot_times=[value])
        self.assertEqual(self.make_config(reboot_times=["04:00", "23:59"]).reboot_times, ["04:00", "23:59"])

    def test_missing_required_fields_rejected(self):
        """Campos obrigatórios ausentes geram erro citando cada um."""
        with self.assertRaises(ConfigError) as ctx:
            validate_config_dict({}, self.tmp)
        self.assertIn("device_ip", str(ctx.exception))
        self.assertIn("start_url", str(ctx.exception))

    def test_non_positive_intervals_and_unknown_keys_rejected(self):
        """Intervalos não positivos, launch_mode inválido e chaves desconhecidas são rejeitados."""
        for overrides in ({"watch_interval_seconds": 0}, {"max_relaunch_attempts": -1},
                          {"launch_mode": "browser"}, {"device_ipp": "1.2.3.4"},
                          {"disabled_packages": ["de.ozerov.fully"]}):
            with self.assertRaises(ConfigError, msg=str(overrides)):
                self.make_config(**overrides)

    def test_invalid_config_cli_exits_nonzero(self):
        """CLI com config inválida sai com código 1 e mensagem clara, sem chamar ADB."""
        path = self.write_config(adb_port=99999)
        runner = FakeAdbRunner()
        code, output = self.run_main(["--config", str(path), "check"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("adb_port", output)
        self.assertEqual(runner.calls, [])

    def test_missing_and_malformed_config_file(self):
        """Arquivo ausente ou JSON quebrado geram ConfigError."""
        with self.assertRaises(ConfigError):
            load_config(self.tmp / "nope.json")
        bad = self.tmp / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(bad)
        self.assertIn("linha", str(ctx.exception))

    def test_example_config_is_valid(self):
        """config.example.json do projeto é válido e aponta para o piltover."""
        example = SCRIPT_PATH.parent.parent / "config.example.json"
        if not example.exists():
            self.fail(f"config.example.json ausente em {example}")
        raw = json.loads(example.read_text(encoding="utf-8"))
        cfg = validate_config_dict(raw, self.tmp)
        self.assertEqual(cfg.start_url, "https://piltover.grupoinlog.com.br/tv/parear")


class ConnectionTests(_TempDirTestCase):
    def test_connect_success(self):
        """Conexão bem-sucedida usa 'adb connect ip:porta' e confirma get-state=device."""
        runner = FakeAdbRunner(healthy_device_rules())
        ctl = self.controller(runner)
        ctl.connect()
        self.assertEqual(runner.calls[0], "connect 192.168.0.50:5555")
        self.assertIn("-s 192.168.0.50:5555 get-state", runner.calls)

    def test_connect_fails_after_three_attempts_with_vega_hint(self):
        """Após 3 falhas, erro menciona Vega OS e não há 4ª tentativa."""
        runner = FakeAdbRunner({"connect ": ok("failed to connect to 192.168.0.50:5555")})
        sleeps = []
        ctl = DeviceController(self.make_config(), runner, sleep_fn=sleeps.append, out=io.StringIO())
        with self.assertRaises(ConnectionFailedError) as ctx:
            ctl.connect()
        self.assertIn("Vega OS", str(ctx.exception))
        self.assertEqual(len(runner.commands_matching("connect ")), 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_timeout_is_failure(self):
        """Timeout do ADB nunca é sucesso."""
        timeout = AdbResult(0, "connected to 192.168.0.50:5555", "", timed_out=True)
        self.assertFalse(timeout.ok)
        runner = FakeAdbRunner({"connect ": timeout})
        with self.assertRaises(ConnectionFailedError):
            self.controller(runner).connect()

    def test_subprocess_runner_timeout_and_missing_binary(self):
        """SubprocessAdbRunner: timeout real e binário ausente viram falha."""
        sleeper = SubprocessAdbRunner(sys.executable)
        result = sleeper.run(["-c", "import time; time.sleep(5)"], timeout=0.3)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        missing = SubprocessAdbRunner(str(self.tmp / "no-adb-here")).run(["devices"])
        self.assertEqual(missing.returncode, 127)
        self.assertFalse(missing.ok)

    def test_unauthorized_device_state_is_failure(self):
        """'connected' mas get-state=unauthorized conta como falha."""
        rules = healthy_device_rules()
        rules["get-state"] = ok("unauthorized\n")
        with self.assertRaises(ConnectionFailedError):
            self.controller(FakeAdbRunner(rules)).connect()

    def test_non_amazon_device_rejected(self):
        """Aparelho de outro fabricante falha na compatibilidade."""
        rules = healthy_device_rules()
        rules["ro.product.manufacturer"] = ok("Xiaomi\n")
        ctl = self.controller(FakeAdbRunner(rules))
        with self.assertRaises(PreconditionError):
            ctl.check_compatibility()

    def test_check_command_exit_codes(self):
        """check: 0 com kiosk instalado; 1 sem kiosk; nada é alterado."""
        path = self.write_config()
        runner = FakeAdbRunner(healthy_device_rules(installed=True))
        code, output = self.run_main(["--config", str(path), "check"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertFalse(any(c for c in runner.calls if " install" in c or "disable" in c or "monkey" in c))
        code, output = self.run_main(["--config", str(path), "check"], FakeAdbRunner(healthy_device_rules(installed=False)))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("setup", output)

    def test_adb_binary_discovery(self):
        """Localiza adb no PATH, em pastas comuns, ou retorna None."""
        self.assertEqual(find_adb_binary("adb", which=lambda n: "/x/adb", exists=lambda p: False), "/x/adb")
        win = find_adb_binary("adb", which=lambda n: None,
                              exists=lambda p: p.endswith(os.path.join("platform-tools", "adb.exe")) and "Sdk" in p,
                              env={"LOCALAPPDATA": "C:/Users/u/AppData/Local", "USERPROFILE": "C:/Users/u"},
                              platform="win32")
        self.assertTrue(win and win.endswith("adb.exe"), win)
        self.assertIsNone(find_adb_binary("adb", which=lambda n: None, exists=lambda p: False, env={}, platform="linux"))
        self.assertIsNone(find_adb_binary("/missing/adb", exists=lambda p: False))

    def test_missing_adb_reports_clear_error(self):
        """Sem adb disponível, CLI sai 1 com instrução de instalação."""
        path = self.write_config(adb_binary=str(self.tmp / "missing-adb"))
        out = io.StringIO()
        code = main(["--config", str(path), "check"], out=out)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Platform-Tools", out.getvalue())


class SetupTests(_TempDirTestCase):
    def apk(self, content: bytes = b"PK\x03\x04fake") -> Path:
        path = self.tmp / "fully.apk"
        path.write_bytes(content)
        return path

    def test_missing_apk_not_installed(self):
        """APK inexistente: setup falha sem chamar 'install'."""
        runner = FakeAdbRunner(healthy_device_rules(installed=False))
        path = self.write_config(kiosk_apk_path=str(self.tmp / "missing.apk"))
        code, output = self.run_main(["--config", str(path), "setup"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("APK não encontrado", output)
        self.assertEqual(runner.commands_matching(" install "), [])

    def test_empty_apk_not_installed(self):
        """APK vazio: setup falha sem chamar 'install'."""
        runner = FakeAdbRunner(healthy_device_rules(installed=False))
        path = self.write_config(kiosk_apk_path=str(self.apk(b"")))
        code, output = self.run_main(["--config", str(path), "setup"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("vazio", output)
        self.assertEqual(runner.commands_matching(" install "), [])

    def test_installs_when_absent(self):
        """Kiosk ausente + APK válido: instala com 'install -r' e confirma."""
        rules = healthy_device_rules(installed=False)
        rules["pm list packages de.ozerov.fully"] = [ok(""), ok("package:de.ozerov.fully\n")]
        rules[" install -r "] = ok("Performing Streamed Install\nSuccess\n")
        runner = FakeAdbRunner(rules)
        apk = self.apk()
        code, output = self.run_main(["--config", str(self.write_config(kiosk_apk_path=str(apk))), "setup"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching(" install -r "), [f"-s 192.168.0.50:5555 install -r {apk}"])

    def test_idempotent_when_already_installed_and_disabled(self):
        """Setup repetido não reinstala nem redesativa."""
        rules = healthy_device_rules(installed=True)
        rules["pm list packages com.amazon.venezia"] = ok("package:com.amazon.venezia\n")
        rules["pm list packages -d com.amazon.venezia"] = ok("package:com.amazon.venezia\n")
        runner = FakeAdbRunner(rules)
        path = self.write_config(disabled_packages=["com.amazon.venezia"])
        for _ in range(2):
            code, output = self.run_main(["--config", str(path), "setup"], runner)
            self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching(" install"), [])
        self.assertEqual(runner.commands_matching("disable-user"), [])

    def test_install_failure_stops_following_steps(self):
        """Falha de instalação interrompe as etapas seguintes (não desativa pacotes)."""
        rules = healthy_device_rules(installed=False)
        rules[" install -r "] = ok("Failure [INSTALL_FAILED_INVALID_APK]\n")
        runner = FakeAdbRunner(rules)
        path = self.write_config(kiosk_apk_path=str(self.apk()), disabled_packages=["com.amazon.venezia"])
        code, output = self.run_main(["--config", str(path), "setup"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("INSTALL_FAILED", output)
        self.assertEqual(runner.commands_matching("disable-user"), [])

    def test_no_connection_stops_setup(self):
        """Sem conexão, setup não consulta nem instala nada."""
        runner = FakeAdbRunner({"connect ": fail("failed to connect")})
        path = self.write_config(kiosk_apk_path=str(self.apk()))
        code, output = self.run_main(["--config", str(path), "setup"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Vega OS", output)
        self.assertTrue(all(c.startswith("connect ") for c in runner.calls), runner.calls)


class LaunchTests(_TempDirTestCase):
    def test_kiosk_app_command_exact(self):
        """kiosk_app usa 'monkey -p <pkg> -c android.intent.category.LAUNCHER 1'."""
        runner = FakeAdbRunner(healthy_device_rules())
        code, output = self.run_main(["--config", str(self.write_config(launch_mode="kiosk_app")), "launch"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching("monkey"),
                         ["-s 192.168.0.50:5555 shell monkey -p de.ozerov.fully -c android.intent.category.LAUNCHER 1"])

    def test_silk_intent_command_exact(self):
        """silk_intent usa 'am start -a android.intent.action.VIEW -d <url>'."""
        runner = FakeAdbRunner(healthy_device_rules())
        code, output = self.run_main(["--config", str(self.write_config(launch_mode="silk_intent")), "launch"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching("am start"), [
            "-s 192.168.0.50:5555 shell am start -a android.intent.action.VIEW -d https://piltover.grupoinlog.com.br/tv/parear"])

    def test_url_with_special_characters_is_shell_escaped(self):
        """URL com &, espaço e aspas é escapada para o shell do aparelho."""
        url = "https://example.com/tv?a=1&b=two words&c='x'"
        ctl = self.controller(FakeAdbRunner(), launch_mode="silk_intent", start_url=url)
        command = ctl.launch_command()
        quoted = command[command.index("-d") + 1]
        self.assertEqual(shlex.split(quoted), [url])
        self.assertTrue(quoted.startswith("'"), quoted)

    def test_kiosk_app_url_targets_fully_and_falls_back(self):
        """kiosk_app_url abre a URL no Fully; se o intent falhar, cai no LAUNCHER."""
        rules = healthy_device_rules()
        rules["am start"] = ok("Error: Activity class {de.ozerov.fully/.MainActivity} does not exist.")
        rules["monkey"] = ok("Events injected: 1")
        runner = FakeAdbRunner(rules)
        ctl = self.controller(runner, launch_mode="kiosk_app_url")
        ctl.launch()
        self.assertIn("-n de.ozerov.fully/.MainActivity", runner.commands_matching("am start")[0])
        self.assertEqual(len(runner.commands_matching("monkey")), 1)

    def test_launch_failure_exit_code(self):
        """Falha ao abrir retorna código 1 com mensagem."""
        rules = healthy_device_rules()
        rules["monkey"] = ok("** No activities found to run, monkey aborted.")
        code, output = self.run_main(["--config", str(self.write_config()), "launch"], FakeAdbRunner(rules))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Falha ao abrir", output)


class WatchTests(_TempDirTestCase):
    NOW = dt.datetime(2026, 11, 10, 12, 0, 0)

    def watcher(self, rules: dict, **overrides):
        runner = FakeAdbRunner(rules)
        return Watcher(self.controller(runner, **overrides)), runner

    def test_parse_foreground(self):
        """Extrai o package do mCurrentFocus / mFocusedApp."""
        self.assertEqual(parse_foreground_package(DUMPSYS_FULLY), "de.ozerov.fully")
        self.assertEqual(parse_foreground_package(DUMPSYS_LAUNCHER), "com.amazon.tv.launcher")
        self.assertEqual(parse_foreground_package(
            "mFocusedApp=AppWindowToken{x token=Token{y ActivityRecord{z u0 com.a.b/.Main t1}}}"), "com.a.b")
        self.assertIsNone(parse_foreground_package("nothing here"))

    def test_kiosk_in_foreground_no_action(self):
        """Kiosk em primeiro plano: nenhuma ação."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = ok(DUMPSYS_FULLY)
        watcher, runner = self.watcher(rules)
        self.assertEqual(watcher.tick(self.NOW), "ok")
        self.assertEqual(runner.commands_matching("monkey") + runner.commands_matching("reboot"), [])

    def test_kiosk_not_in_foreground_relaunches(self):
        """Kiosk fora de primeiro plano: reabre."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = ok(DUMPSYS_LAUNCHER)
        rules["monkey"] = ok("Events injected: 1")
        watcher, runner = self.watcher(rules)
        self.assertEqual(watcher.tick(self.NOW), "relaunched")
        self.assertEqual(len(runner.commands_matching("monkey")), 1)

    def test_repeated_failures_escalate_to_reboot_and_reset(self):
        """Após max_relaunch_attempts falhas seguidas: reinicia e zera o contador."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = ok(DUMPSYS_LAUNCHER)
        rules["monkey"] = ok("Events injected: 1")
        watcher, runner = self.watcher(rules, max_relaunch_attempts=3)
        outcomes = [watcher.tick(self.NOW + dt.timedelta(minutes=i)) for i in range(4)]
        self.assertEqual(outcomes, ["relaunched", "relaunched", "relaunched", "reboot"])
        self.assertEqual(watcher.relaunch_failures, 0)
        self.assertEqual(runner.commands_matching(" reboot"), ["-s 192.168.0.50:5555 reboot"])

    def test_success_resets_failure_counter(self):
        """Voltar ao primeiro plano zera o contador de falhas."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = [ok(DUMPSYS_LAUNCHER), ok(DUMPSYS_FULLY)]
        watcher, _ = self.watcher(rules)
        watcher.tick(self.NOW)
        self.assertEqual(watcher.relaunch_failures, 1)
        self.assertEqual(watcher.tick(self.NOW), "ok")
        self.assertEqual(watcher.relaunch_failures, 0)

    def test_adb_drop_reconnects_with_backoff(self):
        """ADB cai: marca desconectado, espera backoff crescente e reconecta."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = [fail("error: device offline"), ok(DUMPSYS_FULLY)]
        rules["connect "] = [ok("connected to x"), fail("failed to connect"), ok("connected to x")]
        watcher, _ = self.watcher(rules)
        t0 = self.NOW
        self.assertEqual(watcher.tick(t0), "connection_lost")
        self.assertFalse(watcher.connected)
        self.assertEqual(watcher.tick(t0 + dt.timedelta(seconds=1)), "waiting_reconnect")
        self.assertEqual(watcher.tick(t0 + dt.timedelta(seconds=6)), "disconnected")
        first_delay = (watcher.next_connect_at - (t0 + dt.timedelta(seconds=6))).total_seconds()
        self.assertEqual(first_delay, 10.0)  # backoff doubled (5s -> 10s)
        self.assertEqual(watcher.tick(t0 + dt.timedelta(seconds=17)), "ok")
        self.assertTrue(watcher.connected)
        self.assertEqual(watcher.reconnect_failures, 0)

    def test_respects_reboot_times_once_per_day(self):
        """reboot_times: reinicia (com limpeza de cache) uma vez no minuto configurado."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = ok(DUMPSYS_FULLY)
        watcher, runner = self.watcher(rules, reboot_times=["04:00"])
        at = dt.datetime(2026, 11, 10, 4, 0, 5)
        self.assertEqual(watcher.tick(dt.datetime(2026, 11, 10, 3, 59)), "ok")
        self.assertEqual(watcher.tick(at), "scheduled_reboot")
        self.assertEqual(len(runner.commands_matching("trim-caches")), 1)
        watcher.connected = True
        watcher.next_connect_at = None
        self.assertEqual(watcher.tick(at + dt.timedelta(seconds=30)), "ok")
        self.assertEqual(len(runner.commands_matching(" reboot")), 1)
        self.assertEqual(watcher.tick(at + dt.timedelta(days=1)), "scheduled_reboot")

    def test_silk_mode_watches_silk_package(self):
        """Em silk_intent o watch espera o Silk em primeiro plano."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = ok("mCurrentFocus=Window{1 u0 com.amazon.cloud9/com.amazon.cloud9.Browser}")
        watcher, _ = self.watcher(rules, launch_mode="silk_intent")
        self.assertEqual(watcher.tick(self.NOW), "ok")

    def test_run_loop_stops_on_request(self):
        """run() executa ticks no intervalo e sai quando solicitado (sinal)."""
        rules = healthy_device_rules()
        rules["dumpsys window"] = ok(DUMPSYS_FULLY)
        watcher, runner = self.watcher(rules, watch_interval_seconds=2)
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 4:
                watcher.stop_requested = True
        previous = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
        try:
            watcher.run(clock=lambda: self.NOW, sleep_fn=fake_sleep)
        finally:
            signal.signal(signal.SIGINT, previous[0])
            signal.signal(signal.SIGTERM, previous[1])
        self.assertEqual(len(runner.commands_matching("dumpsys window")), 2)


class PackageTests(_TempDirTestCase):
    def rules_with_packages(self) -> dict:
        rules = healthy_device_rules()
        for pkg in ("com.amazon.venezia", "com.amazon.bueller.photos"):
            rules[f"pm list packages {pkg}"] = ok(f"package:{pkg}\n")
            rules[f"pm list packages -d {pkg}"] = ok("")
            rules[f"disable-user --user 0 {pkg}"] = ok(f"Package {pkg} new state: disabled-user\n")
            rules[f"pm enable {pkg}"] = ok(f"Package {pkg} new state: enabled\n")
        return rules

    def test_disable_records_state(self):
        """Desativar grava os pacotes no arquivo de estado."""
        pkgs = ["com.amazon.venezia", "com.amazon.bueller.photos"]
        ctl = self.controller(FakeAdbRunner(self.rules_with_packages()), disabled_packages=pkgs)
        self.assertEqual(ctl.disable_packages(), pkgs)
        data = json.loads(Path(ctl.config.state_file).read_text(encoding="utf-8"))
        self.assertEqual(data["disabled_packages"], pkgs)

    def test_restore_reverts_from_state_after_restart(self):
        """restore reativa a partir do estado mesmo com nova instância e config sem a lista."""
        pkgs = ["com.amazon.venezia"]
        self.controller(FakeAdbRunner(self.rules_with_packages()), disabled_packages=pkgs).disable_packages()
        runner = FakeAdbRunner(self.rules_with_packages())
        code, output = self.run_main(["--config", str(self.write_config(disabled_packages=[])), "restore"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching("pm enable"), ["-s 192.168.0.50:5555 shell pm enable com.amazon.venezia"])
        self.assertEqual(StateStore(self.tmp / "logs" / "state.json").load()["disabled_packages"], [])

    def test_corrupt_state_clear_error(self):
        """Estado corrompido gera erro claro e restore não toca o aparelho."""
        state = self.tmp / "logs" / "state.json"
        state.parent.mkdir(parents=True)
        state.write_text("{corrupt", encoding="utf-8")
        runner = FakeAdbRunner(self.rules_with_packages())
        code, output = self.run_main(["--config", str(self.write_config()), "restore"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("corrompido", output)
        self.assertEqual(runner.calls, [])

    def test_disable_failure_raises(self):
        """Falha ao desativar um pacote é reportada e não grava estado."""
        rules = self.rules_with_packages()
        rules["disable-user --user 0 com.amazon.venezia"] = fail("Security exception")
        ctl = self.controller(FakeAdbRunner(rules), disabled_packages=["com.amazon.venezia"])
        with self.assertRaises(KioskError):
            ctl.disable_packages()
        self.assertEqual(ctl.state.load()["disabled_packages"], [])


class SafetyTests(_TempDirTestCase):
    def test_clear_cache_without_yes_does_nothing(self):
        """clear-cache sem --yes não executa nenhum comando ADB."""
        runner = FakeAdbRunner(healthy_device_rules())
        code, output = self.run_main(["--config", str(self.write_config()), "clear-cache"], runner)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("--yes", output)
        self.assertEqual(runner.calls, [])

    def test_clear_cache_with_yes_runs_pm_clear(self):
        """clear-cache --yes executa 'pm clear <kiosk>'."""
        rules = healthy_device_rules()
        rules["pm clear"] = ok("Success\n")
        runner = FakeAdbRunner(rules)
        code, output = self.run_main(["--config", str(self.write_config()), "--yes", "clear-cache"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching("pm clear"), ["-s 192.168.0.50:5555 shell pm clear de.ozerov.fully"])

    def test_dry_run_never_uses_real_runner(self):
        """--dry-run imprime os comandos e não chama o SubprocessAdbRunner."""
        original = SubprocessAdbRunner._execute

        def forbidden(self, args, timeout):
            raise AssertionError("SubprocessAdbRunner chamado em --dry-run")
        SubprocessAdbRunner._execute = forbidden
        try:
            out = io.StringIO()
            code = main(["--config", str(self.write_config()), "--dry-run", "launch"], out=out)
        finally:
            SubprocessAdbRunner._execute = original
        self.assertEqual(code, EXIT_OK, out.getvalue())
        self.assertIn("[dry-run] adb -s 192.168.0.50:5555 shell monkey -p de.ozerov.fully", out.getvalue())

    def test_every_adb_command_is_logged(self):
        """Cada comando ADB é registrado com timestamp, rc e duração."""
        runner = FakeAdbRunner(healthy_device_rules())
        path = self.write_config()
        self.run_main(["--config", str(path), "launch"], runner)
        log_text = (self.tmp / "logs" / "test.log").read_text(encoding="utf-8")
        self.assertRegex(log_text, r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ INFO adb connect 192\.168\.0\.50:5555 \| rc=0 .*duration=")
        self.assertEqual(log_text.count("INFO adb "), len(runner.calls))


class DiscoverTests(_TempDirTestCase):
    def test_scan_finds_open_ports(self):
        """Varredura retorna só hosts com a porta aberta."""
        network = ipaddress.ip_network("192.168.0.0/29")
        found = scan_for_adb(network, 5555, probe=lambda h, p: h in ("192.168.0.3", "192.168.0.5"))
        self.assertEqual(found, ["192.168.0.3", "192.168.0.5"])

    def test_discover_labels_fire_tv(self):
        """discover identifica o Fire TV via getprop."""
        rules = {"connect ": ok("connected to 192.168.1.7:5555"),
                 "ro.product.manufacturer": ok("Amazon\n"), "ro.product.model": ok("AFTSSS\n")}
        out = io.StringIO()
        code = command_discover(None, 5555, FakeAdbRunner(rules), out,
                                probe=lambda h, p: h == "192.168.1.7", local_ip_fn=lambda: "192.168.1.20")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("192.168.1.7: Amazon AFTSSS  <-- Fire TV", out.getvalue())

    def test_discover_nothing_found_and_invalid_subnet(self):
        """Nada encontrado ou sub-rede inválida: código 1 com orientação."""
        out = io.StringIO()
        self.assertEqual(command_discover("192.168.5.0/30", 5555, None, out, probe=lambda h, p: False), EXIT_FAILURE)
        self.assertIn("Depuração ADB", out.getvalue())
        self.assertEqual(command_discover("banana", 5555, None, io.StringIO()), EXIT_FAILURE)
        with self.assertRaises(PreconditionError):
            scan_for_adb(ipaddress.ip_network("10.0.0.0/16"), 5555, probe=lambda h, p: False)


class OpenTests(_TempDirTestCase):
    """One-click 'open' command used by the double-click launchers."""

    def example(self, **overrides) -> Path:
        raw = {"device_ip": "192.168.0.50", "start_url": "https://piltover.grupoinlog.com.br/tv/parear",
               "launch_mode": "kiosk_app_url", "log_file": "logs/test.log", "state_file": "logs/state.json"}
        raw.update(overrides)
        path = self.tmp / "config.example.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def run_open(self, runner: FakeAdbRunner, config_path: Optional[Path] = None, open_hosts=("192.168.1.7",),
                 dry_run: bool = False):
        out = io.StringIO()
        code = command_open(config_path or self.tmp / "config.json", lambda adb: runner, out,
                            example_path=self.example(), dry_run=dry_run,
                            probe=lambda h, p: h in open_hosts, local_ip_fn=lambda: "192.168.1.20",
                            sleep_fn=lambda s: None)
        return code, out.getvalue()

    def discovery_rules(self, installed: bool = True) -> dict:
        rules = healthy_device_rules(installed)
        rules["connect "] = ok("connected to 192.168.1.7:5555\n")
        return rules

    def test_first_run_discovers_fire_tv_and_writes_config(self):
        """1ª execução: acha o Fire TV, grava config.json com o IP e abre a URL."""
        runner = FakeAdbRunner(self.discovery_rules())
        code, output = self.run_open(runner)
        self.assertEqual(code, EXIT_OK, output)
        saved = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["device_ip"], "192.168.1.7")
        self.assertEqual(saved["start_url"], "https://piltover.grupoinlog.com.br/tv/parear")
        self.assertEqual(runner.commands_matching("am start"), [
            "-s 192.168.1.7:5555 shell am start -a android.intent.action.VIEW "
            "-d https://piltover.grupoinlog.com.br/tv/parear -n de.ozerov.fully/.MainActivity"])
        self.assertIn("Pronto!", output)

    def test_existing_config_skips_discovery(self):
        """Com config.json existente, não varre a rede e usa o IP salvo."""
        config = self.write_config(launch_mode="kiosk_app_url")
        runner = FakeAdbRunner(healthy_device_rules())
        code, output = self.run_open(runner, config_path=config, open_hosts=())
        self.assertEqual(code, EXIT_OK, output)
        self.assertNotIn("Procurando", output)
        self.assertEqual(runner.calls[0], "connect 192.168.0.50:5555")

    def test_falls_back_to_silk_when_kiosk_missing(self):
        """Sem Fully instalado, abre a URL no Silk (sem -n do Fully, sem monkey)."""
        config = self.write_config(launch_mode="kiosk_app_url")
        runner = FakeAdbRunner(healthy_device_rules(installed=False))
        code, output = self.run_open(runner, config_path=config)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(runner.commands_matching("am start"), [
            "-s 192.168.0.50:5555 shell am start -a android.intent.action.VIEW -d https://piltover.grupoinlog.com.br/tv/parear"])
        self.assertEqual(runner.commands_matching("monkey"), [])
        self.assertIn("Silk", output)

    def test_no_fire_tv_found_writes_no_config(self):
        """Nenhum Fire TV na rede: código 1, orientação clara e nenhum config.json criado."""
        code, output = self.run_open(FakeAdbRunner(self.discovery_rules()), open_hosts=())
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Nenhum Fire TV", output)
        self.assertFalse((self.tmp / "config.json").exists())

    def test_non_fire_tv_device_is_not_selected(self):
        """Aparelho Android que não é Fire TV não é escolhido."""
        rules = self.discovery_rules()
        rules["ro.product.manufacturer"] = ok("Xiaomi\n")
        code, output = self.run_open(FakeAdbRunner(rules))
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("192.168.1.7: Xiaomi", output)
        self.assertFalse((self.tmp / "config.json").exists())

    def test_connection_failure_reports_vega_hint(self):
        """Falha de conexão: código 1 com orientação (Vega OS / Depuração ADB), sem abrir nada."""
        config = self.write_config()
        runner = FakeAdbRunner({"connect ": ok("failed to connect to 192.168.0.50:5555")})
        code, output = self.run_open(runner, config_path=config)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("Vega OS", output)
        self.assertEqual(runner.commands_matching("am start") + runner.commands_matching("monkey"), [])

    def test_invalid_existing_config_is_rejected(self):
        """config.json inválido: código 1 citando o campo, sem chamar ADB."""
        config = self.write_config(start_url="piltover.grupoinlog.com.br/tv/parear")
        runner = FakeAdbRunner(healthy_device_rules())
        code, output = self.run_open(runner, config_path=config)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertIn("start_url", output)
        self.assertEqual(runner.calls, [])

    def test_dry_run_without_config_does_not_scan(self):
        """--dry-run sem config.json não varre a rede nem grava nada."""
        runner = FakeAdbRunner(self.discovery_rules())
        code, output = self.run_open(runner, dry_run=True)
        self.assertEqual(code, EXIT_FAILURE)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.tmp / "config.json").exists())

    def test_cli_open_accepts_missing_config_flag(self):
        """CLI: 'open --config X' usa o caminho informado (o --config é opcional)."""
        config = self.write_config(launch_mode="kiosk_app")
        runner = FakeAdbRunner(healthy_device_rules())
        code, output = self.run_main(["--config", str(config), "open"], runner)
        self.assertEqual(code, EXIT_OK, output)
        self.assertEqual(len(runner.commands_matching("monkey -p de.ozerov.fully")), 1)


class LauncherScriptTests(unittest.TestCase):
    """Double-click launchers shipped at the project root."""
    ROOT = SCRIPT_PATH.parent.parent

    def test_windows_launcher_runs_open_with_crlf(self):
        """open_kiosk.bat chama 'src\\firetv_kiosk.py open', usa CRLF e pausa no fim."""
        path = self.ROOT / "open_kiosk.bat"
        self.assertTrue(path.is_file(), f"ausente: {path}")
        data = path.read_bytes()
        self.assertIn(b"src\\firetv_kiosk.py open", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""), "o .bat deve usar quebras de linha CRLF")
        self.assertIn(b"pause", data.lower())

    def test_unix_launcher_runs_open_and_is_executable(self):
        """open_kiosk.command chama 'src/firetv_kiosk.py open' a partir da própria pasta e é executável."""
        path = self.ROOT / "open_kiosk.command"
        self.assertTrue(path.is_file(), f"ausente: {path}")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"), text[:30])
        self.assertIn('cd "$(dirname "$0")"', text)
        self.assertIn("src/firetv_kiosk.py open", text)
        self.assertNotIn("\r", text)
        if os.name == "posix":
            self.assertTrue(os.access(path, os.X_OK), "open_kiosk.command precisa de permissão de execução")


class SelfTestTests(unittest.TestCase):
    def test_self_test_exit_codes(self):
        """Classificação 0/1/2: aprovado, reprovado, sem testes."""
        self.assertEqual(classify_result(total=5, failed=0, skipped=0), (EXIT_OK, "APROVADO"))
        self.assertEqual(classify_result(total=5, failed=1, skipped=0), (EXIT_FAILURE, "REPROVADO"))
        self.assertEqual(classify_result(total=0, failed=0, skipped=0), (EXIT_NOT_VALIDATED, "NÃO VALIDADO"))
        self.assertEqual(classify_result(total=5, failed=0, skipped=1), (EXIT_NOT_VALIDATED, "NÃO VALIDADO"))

    def test_self_test_does_not_run_normal_operation(self):
        """--self-test com filtro não executa comandos nem lê config real."""
        out = io.StringIO()
        sentinel = []
        original = DeviceController.connect
        DeviceController.connect = lambda self, *a, **k: sentinel.append("connect")
        try:
            code = main(["--self-test", "--json", "--test", "test_parse_foreground",
                         "--config", "/nonexistent/config.json", "launch"], out=out)
        finally:
            DeviceController.connect = original
        payload = json.loads(out.getvalue())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["total_tests"], 1)
        self.assertEqual(sentinel, [])

    def test_filter_matching_nothing_is_not_validated(self):
        """Filtro sem testes resulta em NÃO VALIDADO (código 2)."""
        out = io.StringIO()
        self.assertEqual(run_self_test(as_json=True, name_filter="no_such_test_xyz", out=out), EXIT_NOT_VALIDATED)
        self.assertEqual(json.loads(out.getvalue())["status"], "NÃO VALIDADO")

    def test_failure_report_contains_required_fields(self):
        """Falha reporta teste, comportamento, esperado/observado, local e reprodução."""
        class Demo(unittest.TestCase):
            def test_demo(self):
                """Comportamento demo."""
                self.assertEqual("observed-value", "expected-value")
        report = execute_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Demo))
        failure = report["failures"][0]
        self.assertEqual(report["status"], "REPROVADO")
        long_list = ["x" * 60, "monkey -p de.ozerov.fully -c android.intent.category.LAUNCHER 1"]

        class LongDemo(unittest.TestCase):
            def test_long(self):
                """Listas longas."""
                self.assertEqual(long_list[:1] + ["HOME"], long_list)
        long_failure = execute_suite(unittest.defaultTestLoader.loadTestsFromTestCase(LongDemo))["failures"][0]
        self.assertIn("category.LAUNCHER 1", long_failure["expected"])
        self.assertNotIn("chars]", long_failure["expected"] + long_failure["observed"])
        self.assertFalse(long_failure["observed"].startswith("Lists differ"), long_failure["observed"])
        self.assertIn("test_demo", failure["test_name"])
        self.assertEqual(failure["behavior"], "Comportamento demo.")
        self.assertIn("expected-value", failure["expected"])
        self.assertIn("observed-value", failure["observed"])
        self.assertRegex(failure["location"], r"firetv_kiosk\.py:\d+")
        self.assertIn("--self-test --test test_demo", failure["reproduce"])

    def test_tests_use_temporary_directories(self):
        """Os testes usam diretório temporário isolado."""
        case = _TempDirTestCase("run")
        case.setUp()
        try:
            self.assertTrue(str(case.tmp).startswith(tempfile.gettempdir()))
            self.assertIn("firetv_selftest_", case.tmp.name)
        finally:
            case.tearDown()


TEST_CASES = (ConfigTests, ConnectionTests, SetupTests, LaunchTests, WatchTests,
              PackageTests, SafetyTests, DiscoverTests, OpenTests, LauncherScriptTests, SelfTestTests)


def classify_result(total: int, failed: int, skipped: int) -> tuple:
    if failed:
        return EXIT_FAILURE, "REPROVADO"
    if total == 0 or skipped:
        return EXIT_NOT_VALIDATED, "NÃO VALIDADO"
    return EXIT_OK, "APROVADO"


def _split_assertion(message: str) -> tuple:
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    first_line = re.sub(r"^\w+ differ: ", "", first_line)
    match = re.match(r"^(.*?) != (.*?)(?: : .*)?$", first_line)
    if match:
        return match.group(2), match.group(1)
    return "", message.strip()


def _location(tb) -> str:
    location = ""
    for frame in traceback.extract_tb(tb):
        location = f"{os.path.relpath(frame.filename)}:{frame.lineno}"
        if Path(frame.filename).resolve() == SCRIPT_PATH and frame.name.startswith("test_"):
            return f"{SCRIPT_REL}:{frame.lineno}"
    return location


class _CollectingResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.failure_records: list = []

    def _record(self, test, err, kind: str):
        exc_type, exc_value, tb = err
        message = str(exc_value)
        expected, observed = _split_assertion(message) if kind == "failure" else ("", f"{exc_type.__name__}: {message}")
        method = getattr(test, "_testMethodName", str(test))
        doc = (getattr(test, getattr(test, "_testMethodName", ""), None).__doc__ or "") if hasattr(test, "_testMethodName") else ""
        self.failure_records.append({
            "script": SCRIPT_REL,
            "behavior": doc.strip().splitlines()[0] if doc.strip() else method,
            "test_name": test.id(),
            "kind": kind,
            "expected": expected or f"asserção satisfeita: {doc.strip() or method}",
            "observed": observed or message,
            "location": _location(tb),
            "reproduce": f"python {SCRIPT_REL} --self-test --test {method}",
        })

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, err, "failure")

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, err, "error")


def execute_suite(suite: unittest.TestSuite) -> dict:
    result = _CollectingResult()
    buffer = io.StringIO()
    previous_max_length = unittest.util._MAX_LENGTH
    unittest.util._MAX_LENGTH = 2000  # keep expected/observed values unabridged in reports
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            suite.run(result)
    finally:
        unittest.util._MAX_LENGTH = previous_max_length
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)
    _, status = classify_result(total, failed, skipped)
    return {
        "status": status,
        "total_tests": total,
        "failed_tests": failed,
        "skipped_tests": skipped,
        "timestamp": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "failures": result.failure_records,
    }


def _iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_tests(item)
        else:
            yield item


def run_self_test(as_json: bool = False, name_filter: Optional[str] = None, out=None) -> int:
    out = out or sys.stdout
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in TEST_CASES:
        for test in _iter_tests(loader.loadTestsFromTestCase(case)):
            if not name_filter or name_filter in test.id():
                suite.addTest(test)
    report = execute_suite(suite)
    code, _ = classify_result(report["total_tests"], report["failed_tests"], report["skipped_tests"])
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2), file=out)
        return code
    print(f"[{report['status']}] {SCRIPT_REL} self-test: total={report['total_tests']} "
          f"falhas={report['failed_tests']} ignorados={report['skipped_tests']} em {report['timestamp']}", file=out)
    for failure in report["failures"]:
        print(f"\n  FALHA: {failure['test_name']}\n"
              f"    comportamento: {failure['behavior']}\n"
              f"    esperado:      {failure['expected']}\n"
              f"    observado:     {failure['observed']}\n"
              f"    local:         {failure['location']}\n"
              f"    reproduzir:    {failure['reproduce']}", file=out)
    return code


if __name__ == "__main__":
    sys.exit(main())
