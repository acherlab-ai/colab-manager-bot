import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(BASE_DIR, "vendor")
if os.path.isdir(VENDOR_DIR):
    sys.path.insert(0, VENDOR_DIR)
_COLAB_BIN = (
    os.environ.get("COLAB_BIN")
    or shutil.which("colab")
    or "/tmp/opencode/colab-venv/bin/colab"
)

_KEEPALIVE_PROCS: dict[tuple[str, str], subprocess.Popen] = {}
_KEEPALIVE_STARTED_AT: dict[tuple[str, str], float] = {}

_KA_STUCK_GRACE_SECS = 120


def _ka_started_event(home: str, session: str, pid: int) -> bool:
    """True if the history file shows a keep_alive_started event for `pid`."""
    try:
        hist = os.path.join(home, ".config", "colab-cli", "history", f"{session}.jsonl")
        if not os.path.exists(hist):
            return False
        with open(hist) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                et = ev.get("event_type") or ev.get("event")
                if et == "keep_alive_started" and str(ev.get("pid")) == str(pid):
                    return True
    except Exception:
        pass
    return False


def _clean_env(account_home: str) -> dict:
    """Build an env isolated to one Google account, proxy-free.

    Strips sandbox-injected proxy vars: the proxy (127.0.0.1:7890) only lives
    while the launching shell runs, so detached children (the keep-alive
    daemon) that inherit it fail with ProxyError and the VM gets idle-pruned.
    Direct connections work fine.
    """
    env = dict(os.environ)
    env["HOME"] = account_home
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    if os.path.isdir(VENDOR_DIR):
        env["PYTHONPATH"] = VENDOR_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _ka_log_path(home: str, session: str) -> str:
    return os.path.join(home, ".config", "colab-cli", f"ka-{session}.log")


def _ka_death_reason(home: str, session: str) -> str:
    try:
        hist_dir = os.path.join(home, ".config", "colab-cli", "history")
        fn = os.path.join(hist_dir, f"{session}.jsonl")
        if not os.path.exists(fn):
            return "no history file"
        with open(fn) as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                et = ev.get("event_type") or ev.get("event")
                if et != "keep_alive_stopped":
                    continue
                payload = ev.get("payload") or ev
                return json.dumps(payload)[:200]
        return "no keep_alive_stopped event found"
    except Exception as e:
        return f"history read failed: {e}"


def reconcile_keepalives(active: list[tuple[str, str, str]]):
    """Keep one `colab keep-alive` subprocess alive per active session.

    The `colab new` CLI spawns its own detached keep-alive daemon, but on
    Railway that detached grandchild can die (container restarts, process
    reaping) and the VM then gets idle-pruned by Colab after ~15 min. This
    function lets the bot's main process (which is guaranteed to be running)
    own the keep-alive loops instead, respawning any that exit.

    `active` is a list of (account_home, session_name, endpoint).
    """
    wanted = {(home, name) for home, name, _ in active}

    for key in list(_KEEPALIVE_PROCS.keys()):
        if key not in wanted:
            p = _KEEPALIVE_PROCS.pop(key)
            if p.poll() is None:
                p.terminate()
            logger.info("keep-alive stopped for %s (%s)", key[1], key[0])

    for home, name, endpoint in active:
        key = (home, name)
        p = _KEEPALIVE_PROCS.get(key)
        if p is not None and p.poll() is None:
            # Process alive, but if it never entered the keep-alive loop (auth
            # refresh can hang at startup) the VM is not actually being kept
            # alive and gets idle-pruned after ~15 min. Respawn stuck daemons.
            age = time.time() - _KEEPALIVE_STARTED_AT.get(key, 0)
            if age > _KA_STUCK_GRACE_SECS and not _ka_started_event(home, name, p.pid):
                logger.warning(
                    "keep-alive %s/%s pid=%s alive %ss but never started pinging; killing",
                    home,
                    name,
                    p.pid,
                    int(age),
                )
                try:
                    p.terminate()
                except Exception:
                    pass
                _KEEPALIVE_PROCS.pop(key, None)
                _KEEPALIVE_STARTED_AT.pop(key, None)
                p = None
            else:
                continue
        if p is not None:
            logger.warning(
                "keep-alive %s/%s exited rc=%s reason=%s log=%s; respawning",
                home,
                name,
                p.returncode,
                _ka_death_reason(home, name),
                _ka_log_path(home, name),
            )
        config = os.path.join(home, ".config", "colab-cli", "sessions.json")
        cmd = [
            sys.executable,
            "-m",
            "colab_cli.cli",
            "--config",
            config,
            "keep-alive",
            endpoint,
            name,
        ]
        logf = open(_ka_log_path(home, name), "a")
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            env=_clean_env(home),
            start_new_session=True,
        )
        _KEEPALIVE_PROCS[key] = proc
        _KEEPALIVE_STARTED_AT[key] = time.time()
        logger.info("keep-alive spawned for %s (endpoint %s) pid=%s", name, endpoint, proc.pid)

SSHX_URL_RE = re.compile(r"https://sshx\.io/s/[^\s\]\)]+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# sshx is started inside a detached tmux session so it survives the Jupyter
# cell (plain detached Popen dies when the cell finishes on TPU runtimes).
_SSHX_BOOT_CODE = """\
import subprocess
shell = (
    "tmux kill-session -t sshx 2>/dev/null; "
    "rm -f /content/sshx.log; "
    "tmux new-session -d -s sshx "
    "'curl -sSf https://sshx.io/get | sh -s run > /content/sshx.log 2>&1'"
)
r = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30)
print(r.stdout)
print(r.stderr)
if r.returncode != 0:
    raise SystemExit('sshx start failed rc=%s' % r.returncode)
"""

_READ_LOG_CODE = """\
import subprocess
out = subprocess.run(['cat', '/content/sshx.log'], capture_output=True, text=True)
print(out.stdout)
"""

_FORCE_KILL_CODE = """\
import subprocess
subprocess.Popen(['sh', '-c', 'sleep 2; kill -9 -1'], start_new_session=True)
print('kill scheduled')
"""


def run_colab(account_home: str, args, timeout: int = 600, input_text: str | None = None) -> str:
    """Run the colab CLI isolated to one Google account (HOME-based isolation).

    Strips sandbox-injected proxy vars: the proxy (127.0.0.1:7890) only lives
    while the launching shell runs, so detached children (the keep-alive daemon)
    that inherit it fail with ProxyError and the VM gets idle-pruned. Direct
    connections work fine.
    """
    env = _clean_env(account_home)
    config = os.path.join(account_home, ".config", "colab-cli", "sessions.json")
    cmd = [_COLAB_BIN]
    cmd += ["--config", config]
    cmd += args
    logger.info("RUN %s", " ".join(cmd))
    retryable = ("ReadTimeout", "Read timed out", "ConnectionError", "ProxyError", "Connection reset")
    last = None
    for attempt in range(3):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, env=env, timeout=timeout,
                input=input_text,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"colab {args[0] if args else ''} timed out (>{timeout}s)")
        output = proc.stdout + proc.stderr
        if proc.returncode == 0:
            return output
        if not any(m in output for m in retryable) or attempt == 2:
            raise RuntimeError(f"colab {args[0] if args else ''} failed (rc={proc.returncode}):\n{output.strip()[-1500:]}")
        logger.warning("colab %s transient error, retry %d/3", args[0] if args else "", attempt + 1)
        time.sleep(5 * (attempt + 1))
    return last  # unreachable


def create_lab(account_home: str, name: str, gpu: str | None = None, tpu: str | None = None):
    args = ["new", "-s", name]
    if gpu:
        args += ["--gpu", gpu]
    elif tpu:
        args += ["--tpu", tpu]
    try:
        return run_colab(account_home, args)
    except RuntimeError:
        # `colab new` can assign the VM server-side and then fail locally (e.g.
        # a read timeout). Don't leak the orphan: unassign anything on the
        # server with no local record, then re-raise.
        config = os.path.join(account_home, ".config", "colab-cli", "sessions.json")
        env = dict(os.environ)
        env["HOME"] = account_home
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        env["NO_PROXY"] = "*"
        env["no_proxy"] = "*"
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "unassign_orphans.py"), account_home, config],
                capture_output=True, text=True, env=env, timeout=120,
            )
            logger.warning("orphan cleanup after failed create: %s %s",
                           proc.stdout.strip(), proc.stderr.strip())
        except Exception as e:
            logger.warning("orphan cleanup failed: %s", e)
        raise


def exec_code(account_home: str, session: str, code: str, timeout: int = 90) -> str:
    # `colab exec` reads the code from stdin (or --file); the bot must pipe it.
    return run_colab(
        account_home,
        ["exec", "-s", session, "--timeout", str(timeout)],
        timeout=timeout + 60,
        input_text=code,
    )


def start_sshx(account_home: str, session: str, deadline_sec: int = 90) -> str:
    """Start sshx (tmux) on the session, poll until the public link appears."""
    exec_code(account_home, session, _SSHX_BOOT_CODE, timeout=60)
    deadline = time.time() + deadline_sec
    while time.time() < deadline:
        time.sleep(6)
        try:
            out = exec_code(account_home, session, _READ_LOG_CODE, timeout=60)
        except RuntimeError:
            continue
        m = SSHX_URL_RE.search(ANSI_RE.sub("", out))
        if m:
            return m.group(0)
    raise RuntimeError("sshx link not ready within deadline")


def list_labs(account_home: str):
    out = run_colab(account_home, ["sessions"], timeout=60)
    return out


def status_lab(account_home: str, name: str):
    return run_colab(account_home, ["status", "-s", name], timeout=60)


def stop_lab(account_home: str, name: str):
    # `colab stop` only releases the assignment; the VM itself may keep running
    # for a while and the sshx tunnel (an outbound websocket from the VM) stays
    # alive, so the machine looks "still on" after the bot says stopped.
    # Kill every user process on the VM first (no systemd on Colab, so a plain
    # poweroff fails): sshx/tmux/kernel all die -> link dead immediately.
    try:
        exec_code(account_home, name, _FORCE_KILL_CODE, timeout=40)
    except Exception as e:
        logger.warning("force-kill on %s failed (continuing stop): %s", name, e)
    return run_colab(account_home, ["stop", "-s", name], timeout=120)


def new_name() -> str:
    return "lab-" + uuid.uuid4().hex[:4]
