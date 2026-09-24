#!/usr/bin/env python3
"""Cross-platform one-command bootstrap for the FaaS Optimizer.

Clone → run this script → browser opens with server ready.

Works on macOS, Linux, and Windows. Only requires Python 3.10+ on PATH.
Everything else (virtual env, pip install, Codex CLI check, port cleanup,
browser launch) happens automatically.

Entry points:
  - `./start.sh` on Unix/macOS
  - `start.bat`  on Windows
Both are thin wrappers that call `python bootstrap.py`.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from shutil import which

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQS = ROOT / "app" / "requirements.txt"
SERVER = ROOT / "app" / "server.py"
FLEET_CSV = ROOT / "data_csv" / "fleet_inventory.csv"
FLEET_BACKUP = ROOT / "data_csv" / "fleet_inventory_original.csv"
PORT = int(os.environ.get("PORT", "8000"))

IS_WINDOWS = platform.system() == "Windows"

# Venv layout differs between Unix and Windows
if IS_WINDOWS:
    VENV_PY = VENV / "Scripts" / "python.exe"
else:
    VENV_PY = VENV / "bin" / "python"

# When stdout is piped (CI, subprocess, tee), Python defaults to block
# buffering — the step banners sit in the buffer until exit and never appear.
# Force line-buffering so users see progress as it happens. TTY stdout is
# already line-buffered so this is a no-op there.
try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass  # Python <3.7; not reachable under the 3.10+ floor, but safe.

# Enable ANSI colors on Windows 10+ console. On cmd.exe without this, escape
# sequences render as literal text. `os.system("")` triggers the Win32 call
# SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING) as a side effect.
if IS_WINDOWS and sys.stdout.isatty():
    os.system("")

# ── Color helpers ───────────────────────────────────────────────────────────
if sys.stdout.isatty():
    BOLD, DIM, NC = "\033[1m", "\033[2m", "\033[0m"
    GREEN, YELLOW, CYAN, RED = "\033[32m", "\033[33m", "\033[36m", "\033[31m"
else:
    BOLD = DIM = NC = GREEN = YELLOW = CYAN = RED = ""


def rule(color: str = CYAN) -> None:
    print(f"{color}{BOLD}{'━' * 56}{NC}")


def step(n: int, total: int, msg: str) -> None:
    print(f"{YELLOW}[{n}/{total}]{NC} {msg}")


def ok(msg: str) -> None:
    print(f"   {GREEN}✓{NC} {msg}")


def warn(msg: str) -> None:
    print(f"   {YELLOW}⚠{NC} {msg}")


def fail(msg: str) -> None:
    print(f"   {RED}✗{NC} {msg}", file=sys.stderr)


# ── Steps ───────────────────────────────────────────────────────────────────


def check_python_version() -> None:
    if sys.version_info < (3, 10):
        fail(
            f"Python 3.10+ required; got {sys.version.split()[0]}. "
            f"Upgrade and retry."
        )
        sys.exit(1)


def ensure_venv() -> None:
    step(1, 5, "Checking Python virtual environment...")
    if not VENV_PY.exists():
        warn(f"No venv at {VENV.relative_to(ROOT)} — creating one")
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV)],
            check=True,
        )
    # Confirm the venv python runs
    result = subprocess.run(
        [str(VENV_PY), "--version"], capture_output=True, text=True, check=True
    )
    ok(f"Python: {result.stdout.strip()}")


def ensure_deps() -> None:
    step(2, 5, "Checking dependencies...")
    # Fast path: if core imports work, skip pip install. Avoids a slow pip
    # round-trip on every start, and keeps startup snappy after first install.
    check = subprocess.run(
        [str(VENV_PY), "-c", "import fastapi, uvicorn, pandas, numpy, pulp"],
        capture_output=True,
    )
    if check.returncode == 0:
        ok("requirements.txt already satisfied")
        return
    warn("Missing packages — running pip install (first run may take 60–90 s)")
    subprocess.run(
        [str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(REQS)],
        check=True,
    )
    ok("Installed from requirements.txt")


def check_codex_cli() -> None:
    step(3, 5, "Checking Codex CLI (chat agent)...")
    codex_path = which("codex")
    if codex_path:
        ok(f"Codex CLI: {codex_path}")
    else:
        warn("codex not on PATH — chat agent will be disabled")
        warn("install and authenticate the Codex CLI, then retry")


def maybe_reset_fleet(do_reset: bool) -> None:
    step(4, 5, "Fleet state...")
    if not do_reset:
        ok(f"Leaving {FLEET_CSV.name} as-is (pass --reset-fleet to restore baseline)")
        return
    if not FLEET_BACKUP.exists():
        fail(f"Backup missing: {FLEET_BACKUP.relative_to(ROOT)}")
        sys.exit(1)
    shutil.copy2(FLEET_BACKUP, FLEET_CSV)
    ok(f"Reset {FLEET_CSV.name} from {FLEET_BACKUP.name}")


def free_port(force: bool) -> None:
    step(5, 5, f"Reserving port {PORT}...")
    # Probe by connecting. If something answers, the port is taken; if we get
    # ECONNREFUSED, it's free. This is more reliable than bind-probing because
    # SO_REUSEADDR can mask a conflicting listener on some platforms.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        err = probe.connect_ex(("127.0.0.1", PORT))
    finally:
        probe.close()
    # connect_ex returns 0 on success, errno otherwise (111/10061 = refused)
    if err != 0:
        ok(f"Port {PORT} is free")
        return

    pid = _find_pid_on_port(PORT)
    pid_str = f"PID {pid}" if pid is not None else "unknown process"
    if not force:
        fail(f"Port {PORT} held by {pid_str}.")
        fail("Refusing to kill — pass --force-free-port (or set FORCE_FREE_PORT=1)")
        fail(f"to override, or use a different port (PORT=8080 ./start.sh).")
        sys.exit(1)
    warn(f"Port {PORT} held by {pid_str} — killing (--force-free-port set)")
    if pid is None:
        fail("Couldn't identify the holder PID — free the port manually and retry")
        sys.exit(1)
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True
        )
    else:
        subprocess.run(["kill", str(pid)], capture_output=True)
    # Give the OS a moment to release the socket before uvicorn tries to bind
    import time as _t
    _t.sleep(0.5)
    ok(f"Port {PORT} released (was held by PID {pid})")


def _find_pid_on_port(port: int) -> int | None:
    """Return PID holding `port`, or None if not found."""
    if IS_WINDOWS:
        # netstat -ano | find ":PORT" | LISTENING
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local_addr, state, pid = parts[1], parts[3], parts[4]
            if local_addr.endswith(f":{port}") and state == "LISTENING":
                try:
                    return int(pid)
                except ValueError:
                    return None
        return None
    # Unix: lsof -ti:PORT
    if which("lsof") is None:
        return None
    result = subprocess.run(
        ["lsof", "-ti", f":{port}"], capture_output=True, text=True
    )
    pid_str = result.stdout.strip().split("\n")[0]
    try:
        return int(pid_str) if pid_str else None
    except ValueError:
        return None


def open_browser_delayed(url: str) -> None:
    """Fire-and-forget: open default browser 2 s after launch. webbrowser.open
    handles the platform-specific invocation (uses `open` on macOS, `xdg-open`
    on Linux, `start` on Windows). Skipped on headless environments (Linux
    with no DISPLAY, or non-TTY stdout — typically systemd/SSH/container)."""
    if _is_headless():
        return
    import threading
    import time
    import webbrowser

    def _open() -> None:
        time.sleep(2)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass  # Silent — server logs are what matters

    threading.Thread(target=_open, daemon=True).start()


def _is_headless() -> bool:
    if not sys.stdout.isatty():
        return True
    if platform.system() == "Linux" and not os.environ.get("DISPLAY"):
        return True
    return False


def launch_server() -> None:
    print()
    rule(GREEN)
    print(f"{GREEN}{BOLD}  All checks passed. Launching server...{NC}")
    rule(GREEN)
    print(f"   {CYAN}→{NC} app:  {BOLD}http://localhost:{PORT}{NC}")
    print(f"   {CYAN}→{NC} api:  http://localhost:{PORT}/api/overview")
    print(f"   {DIM}ctrl-c to stop{NC}\n")

    open_browser_delayed(f"http://localhost:{PORT}")

    os.environ["PORT"] = str(PORT)
    # exec-replace on Unix so ctrl-c goes straight to uvicorn. On Windows
    # os.execv has caveats (the parent cmd.exe can return prematurely),
    # so use subprocess + wait instead.
    if IS_WINDOWS:
        try:
            subprocess.run([str(VENV_PY), str(SERVER)])
        except KeyboardInterrupt:
            pass
    else:
        os.execv(str(VENV_PY), [str(VENV_PY), str(SERVER)])


# ── Main ────────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="One-command launcher for the FaaS Optimizer.",
    )
    p.add_argument(
        "--reset-fleet",
        action="store_true",
        default=os.environ.get("RESET_FLEET") == "1",
        help="Restore data_csv/fleet_inventory.csv from the original backup "
             "before launching (env: RESET_FLEET=1).",
    )
    p.add_argument(
        "--force-free-port",
        action="store_true",
        default=os.environ.get("FORCE_FREE_PORT") == "1",
        help=f"If port {PORT} is held by another process, kill it instead of "
             f"aborting (env: FORCE_FREE_PORT=1).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    print()
    rule(CYAN)
    print(f"{CYAN}{BOLD}  FaaS Optimizer — Starting Up{NC}")
    rule(CYAN)

    check_python_version()
    try:
        ensure_venv()
        ensure_deps()
        check_codex_cli()
        maybe_reset_fleet(args.reset_fleet)
        free_port(args.force_free_port)
        launch_server()
    except subprocess.CalledProcessError as e:
        fail(f"Subprocess failed: {e}")
        sys.exit(e.returncode or 1)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
