import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import requests
from colab_cli.auth import PUBLIC_SCOPES
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(BASE_DIR, "vendor")
if os.path.isdir(VENDOR_DIR):
    sys.path.insert(0, VENDOR_DIR)


def _find_colab_bin() -> str:
    """Locate the `colab` CLI binary.

    Resolution order:
      1. $COLAB_BIN (explicit override)
      2. `colab` on $PATH (via shutil.which)
      3. bare "colab" (subprocess resolves it from PATH at spawn time)

    No machine-specific paths are hardcoded.
    """
    env_bin = os.environ.get("COLAB_BIN")
    if env_bin:
        return env_bin
    found = shutil.which("colab")
    if found:
        return found
    venv_bin = os.path.join(os.path.dirname(sys.executable), "colab")
    if os.path.isfile(venv_bin) and os.access(venv_bin, os.X_OK):
        return venv_bin
    return "colab"


_COLAB_BIN = _find_colab_bin()

# Cap concurrent `colab` CLI subprocesses to bound peak RAM/CPU. `colab new`
# / `colab exec` each spawn a full interpreter (~60-100MB); with several labs
# and users running at once this easily OOMs small hosts.
_COLAB_SEM = threading.BoundedSemaphore(2)

# ---------------------------------------------------------------------------
# In-process keep-alive (no per-session daemons)
#
# Mirrors colab_cli.client.Client.keep_alive_assignment(): TFE records the
# activity as soon as the request arrives, so the request usually read-times
# out even on success -> ReadTimeout is treated as success. We issue the ping
# from inside the bot process, so nothing needs to be respawned: as long as
# the bot is up, the sessions stay alive.
# ---------------------------------------------------------------------------

_KEEPALIVE_URL = "https://colab.research.google.com/tun/m/{endpoint}/keep-alive/"
_KEEPALIVE_HEADERS = {
    "Accept": "application/json",
    "X-Colab-Client-Agent": "colab-cli",
    "X-Colab-Tunnel": "Google",
}
_KEEPALIVE_TIMEOUT = 10

_TOKEN_LOCKS_GUARD = threading.Lock()
_TOKEN_LOCKS: dict[str, threading.Lock] = {}

_KA_STATUS: dict[str, dict] = {}
_KA_STATUS_GUARD = threading.Lock()
_KA_SUCCESS_LOG_AT: dict[str, float] = {}
_KA_SUCCESS_LOG_EVERY = 300.0


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _account_token_lock(home: str) -> threading.Lock:
    with _TOKEN_LOCKS_GUARD:
        return _TOKEN_LOCKS.setdefault(home, threading.Lock())


def ping_keepalive(home: str, endpoint: str) -> str | None:
    """Ping the Tunnel Frontend keep-alive endpoint for one session.

    Returns None on success, or a short error string on failure. Never raises.
    """
    token_path = os.path.join(home, ".config", "colab-cli", "token.json")
    try:
        with open(token_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return f"token load failed: {e}"
    try:
        creds = Credentials.from_authorized_user_info(data, PUBLIC_SCOPES)
    except Exception as e:
        return f"token parse failed: {e}"

    sess = None
    try:
        with _account_token_lock(home):
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    return f"token refresh failed: {e}"
                try:
                    _atomic_write(token_path, creds.to_json())
                except Exception as e:
                    logger.warning("keep-alive token write failed for %s: %s", home, e)
            sess = AuthorizedSession(creds)
            try:
                resp = sess.get(
                    _KEEPALIVE_URL.format(endpoint=endpoint),
                    headers=_KEEPALIVE_HEADERS,
                    params={"authuser": "0"},
                    timeout=_KEEPALIVE_TIMEOUT,
                )
            except requests.exceptions.ReadTimeout:
                return None
            if resp.ok:
                return None
            return f"HTTP {resp.status_code}"
    except Exception as e:
        return f"keep-alive failed: {e}"
    finally:
        if sess is not None:
            sess.close()


def _record_ka(name: str, ok: bool, detail: str, now: float) -> None:
    with _KA_STATUS_GUARD:
        _KA_STATUS[name] = {"ok": ok, "detail": detail, "at": now}


def keepalive_once(active: list[tuple[str, str, str]]) -> None:
    """Ping every active (home, name, endpoint) session once.

    A failing session is recorded/logged individually; it never propagates,
    so one broken session cannot crash the keep-alive loop.
    """
    now = time.time()
    for home, name, endpoint in active:
        try:
            err = ping_keepalive(home, endpoint)
        except Exception:  # defensive: never crash the loop
            logger.exception("keep-alive ping crashed for session %s", name)
            _record_ka(name, False, "unexpected exception", now)
            continue
        if err is None:
            _record_ka(name, True, "", now)
            last = _KA_SUCCESS_LOG_AT.get(name, 0.0)
            if now - last >= _KA_SUCCESS_LOG_EVERY:
                _KA_SUCCESS_LOG_AT[name] = now
                logger.info("keep-alive OK session=%s endpoint=%s", name, endpoint)
        else:
            _record_ka(name, False, err, now)
            logger.warning("keep-alive FAILED session=%s endpoint=%s: %s", name, endpoint, err)


def keepalive_status() -> dict:
    with _KA_STATUS_GUARD:
        return dict(_KA_STATUS)


# ---------------------------------------------------------------------------
# colab CLI helpers
# ---------------------------------------------------------------------------


def _clean_env(account_home: str) -> dict:
    """Build an env isolated to one Google account, proxy-free.

    Strips sandbox-injected proxy vars: a proxy only lives while the launching
    shell runs, so detached children that inherit it fail with ProxyError.
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


def run_colab(account_home: str, args, timeout: int = 600, input_text: str | None = None) -> str:
    """Run the colab CLI isolated to one Google account (HOME-based isolation).

    Subprocesses are transient (subprocess.run), guarded by a global semaphore
    so concurrent invocations cannot blow up memory on small hosts.
    """
    env = _clean_env(account_home)
    config = os.path.join(account_home, ".config", "colab-cli", "sessions.json")
    cmd = [_COLAB_BIN, "--config", config] + args
    logger.info("RUN %s", " ".join(cmd))
    retryable = ("ReadTimeout", "Read timed out", "ConnectionError", "ProxyError", "Connection reset")
    with _COLAB_SEM:
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=timeout,
                    input=input_text,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    f"colab CLI not found: '{_COLAB_BIN}'. "
                    "Install google-colab-cli or set COLAB_BIN to the binary path."
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"colab {args[0] if args else ''} timed out (>{timeout}s)")
            output = proc.stdout + proc.stderr
            if proc.returncode == 0:
                return output
            if not any(m in output for m in retryable) or attempt == 2:
                hint = _error_hint(output)
                raise RuntimeError(
                    f"colab {args[0] if args else ''} failed (rc={proc.returncode}):\n{output.strip()[-1500:]}"
                    + hint
                )
            logger.warning("colab %s transient error, retry %d/3", args[0] if args else "", attempt + 1)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")  # pragma: no cover


def _error_hint(output: str) -> str:
    """Append a short, actionable hint for recognizable colab CLI failures."""
    if "Connection reset by peer" in output or "ConnectionError" in output:
        return (
            "\n\n⚠️ Không kết nối được tới máy chủ Colab (Connection reset). "
            "Môi trường này đang chặn egress tới colab.research.google.com. "
            "Kiểm tra firewall/proxy của host hoặc deploy ở nơi có mạng mở."
        )
    if "quota" in output.lower() or "Quota" in output:
        return "\n\n⚠️ Có vẻ là lỗi quota/giới hạn tài nguyên của tài khoản Google."
    if "not logged in" in output.lower() or "authorization code" in output.lower():
        return "\n\n⚠️ Tài khoản chưa đăng nhập. Chạy /login lại."
    return ""


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
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "unassign_orphans.py"), account_home, config],
                capture_output=True,
                text=True,
                env=_clean_env(account_home),
                timeout=120,
            )
            logger.warning(
                "orphan cleanup after failed create: %s %s",
                proc.stdout.strip(),
                proc.stderr.strip(),
            )
        except Exception:
            logger.exception("orphan cleanup failed for %s", account_home)
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
    return run_colab(account_home, ["sessions"], timeout=60)


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
    except Exception:
        logger.warning("force-kill on %s failed (continuing stop)", name, exc_info=True)
    return run_colab(account_home, ["stop", "-s", name], timeout=120)


def new_name() -> str:
    return "lab-" + uuid.uuid4().hex[:4]


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
