"""Daemon process: runs orchestrator + web dashboard in background."""

import datetime
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional

import uvicorn

from core.config import load_config
from core.orchestrator import Orchestrator
from web.app import app, set_orchestrator

PID_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "daemon.pid"
)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def setup_logging(config: dict) -> str:
    """Configure logging to a timestamped file. Returns the actual log file path."""
    base = config["logging"]["file"]  # e.g. logs/agent.log
    stem, ext = os.path.splitext(base)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = f"{stem}_{ts}{ext}"
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    level = getattr(logging, config["logging"]["level"].upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
        ],
    )
    return log_file


def write_pid():
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def read_pid() -> int:
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return 0
    return 0


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_listener_pid(port: int) -> int:
    """Return the process id listening on *port* (0 if none/unknown)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return 0
    for line in result.stdout.splitlines():
        s = line.strip()
        if s.isdigit():
            return int(s)
    return 0


def _pid_matches_project(pid: int) -> bool:
    """Best-effort check whether PID belongs to this project's daemon process."""
    if pid <= 0:
        return False
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = ""
    if cwd.startswith(PROJECT_ROOT):
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ").decode(errors="ignore")
    except OSError:
        cmdline = ""
    return "multi-agent-todo" in cmdline


def _wait_until_stopped(pid: int, timeout_sec: float = 5.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _is_pid_alive(pid)


def _terminate_pid(pid: int) -> bool:
    """Terminate process politely, then force kill if needed."""
    if not _is_pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    if _wait_until_stopped(pid, timeout_sec=5.0):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return True
    return _wait_until_stopped(pid, timeout_sec=2.0)


def is_running() -> bool:
    pid = read_pid()
    return _is_pid_alive(pid)


def start(config_path: str = None, foreground: bool = False):
    """Start the daemon."""
    config = load_config(config_path)
    port = int(config["web"]["port"])

    pid = read_pid()
    if pid and _is_pid_alive(pid):
        print(f"Daemon already running (pid={pid})")
        return
    if pid and not _is_pid_alive(pid):
        remove_pid()

    listener_pid = _find_listener_pid(port)
    if listener_pid:
        if _pid_matches_project(listener_pid):
            print(f"Daemon already running (pid={listener_pid})")
        else:
            print(
                f"Cannot start daemon: port {port} is already in use by pid={listener_pid}."
            )
        return

    if not foreground:
        # Fork to background — logging is set up only in the child
        pid = os.fork()
        if pid > 0:
            # Parent: wait briefly to verify child actually bound the port.
            deadline = time.time() + 5.0
            started = False
            while time.time() < deadline:
                if not _is_pid_alive(pid):
                    break
                if _find_listener_pid(port) == pid:
                    started = True
                    break
                time.sleep(0.1)
            if started:
                print(f"Daemon started (pid={pid})")
                print(f"Dashboard: http://localhost:{config['web']['port']}")
                print(f"Logs: {os.path.dirname(os.path.abspath(config['logging']['file']))}")
            else:
                print(
                    f"Daemon failed to start (pid={pid}). "
                    f"Check logs under {os.path.dirname(os.path.abspath(config['logging']['file']))}."
                )
            return
        # Child process
        os.setsid()

    log_file = setup_logging(config)
    log = logging.getLogger("daemon")
    log.info("Log file: %s", log_file)

    write_pid()
    log.info("Daemon starting (pid=%d)", os.getpid())

    def handle_signal(signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        orch.stop()
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Initialize orchestrator
    orch = Orchestrator(config)
    set_orchestrator(orch)

    # Start orchestrator loop
    orch.start()
    log.info("Orchestrator started, launching web dashboard on port %d", config["web"]["port"])

    # Run web server (blocks)
    try:
        uvicorn.run(
            app,
            host=config["web"]["host"],
            port=config["web"]["port"],
            log_level="warning",
        )
    finally:
        try:
            orch.stop()
        except Exception:
            pass
        remove_pid()


def stop(config_path: Optional[str] = None):
    """Stop the daemon."""
    config = load_config(config_path)
    port = int(config["web"]["port"])

    pid = read_pid()
    stopped_pids = []

    if pid and _is_pid_alive(pid):
        if _terminate_pid(pid):
            stopped_pids.append(pid)
    elif pid:
        remove_pid()

    listener_pid = _find_listener_pid(port)
    if listener_pid and listener_pid not in stopped_pids:
        if _pid_matches_project(listener_pid):
            if _terminate_pid(listener_pid):
                stopped_pids.append(listener_pid)
        else:
            print(
                f"Port {port} is occupied by non-project pid={listener_pid}; not terminating it."
            )

    if stopped_pids:
        print("Daemon stopped (pid=" + ",".join(str(p) for p in stopped_pids) + ")")
    else:
        print("Daemon is not running")

    remove_pid()


def status(config_path: Optional[str] = None):
    """Check daemon status."""
    config = load_config(config_path)
    port = int(config["web"]["port"])

    pid = read_pid()
    running = _is_pid_alive(pid)
    listener_pid = _find_listener_pid(port)

    if running:
        print(f"Daemon is running (pid={pid})")
    else:
        print("Daemon is not running")
        if pid:
            remove_pid()

    if listener_pid and listener_pid != pid:
        owner = "project" if _pid_matches_project(listener_pid) else "non-project"
        print(
            f"Port {port} listener detected (pid={listener_pid}, owner={owner})"
        )
