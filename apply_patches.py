"""Re-apply colab-cli patches that the bot relies on.

google-colab-cli is installed from PyPI as-is; these behaviour fixes are
idempotently applied to the installed package at container start so local and
Railway behaviour match.

1. client.py: default per-request timeout 600s (TPU/GPU provisioning can exceed
   google-auth's 120s read timeout).
2. commands/session.py stop(): unassign the endpoint BEFORE the (slow) kernel
   shutdown so the VM is released immediately.
3. commands/session.py new(): disable the CLI's own detached keep-alive daemon
   (`spawn_keep_alive`). The bot runs an in-process keep-alive loop instead, so
   the CLI's per-session daemon would only waste RAM (~60-100MB per lab) and
   fight the bot's pinger. With the daemon disabled, `colab stop` simply skips
   killing a pid (0 is falsy) and the bot's loop stops when the session is
   removed from sessions.json.
"""
import re
import sys


def patch_client():
    import colab_cli.client as client

    path = client.__file__
    with open(path, encoding="utf-8") as f:
        src = f.read()

    marker = 'kwargs.setdefault("timeout", 600)'
    if marker in src:
        print(f"[patch] client.py already patched: {path}")
        return

    needle = "        response = self.session.request("
    if needle not in src:
        print(f"[patch] WARN: needle not found in {path}")
        return

    patched = src.replace(needle, f"        {marker}\n{needle}", 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
    print(f"[patch] client.py timeout 600 applied: {path}")


def patch_stop_order():
    from colab_cli.commands import session

    path = session.__file__
    with open(path, encoding="utf-8") as f:
        src = f.read()

    # Vanilla stop() runs the slow kernel shutdown BEFORE unassigning the
    # endpoint, so the VM lingers for ~45s. Move unassign() above the shutdown.
    m = re.search(
        r"(?P<block>\n    try:\n        runtime = ColabRuntime\(s\.url, s\.token, kernel_id=s\.kernel_id\)\n        runtime\.stop\(shutdown_kernel=True\)\n    except Exception:\n        pass\n\n)(?P<unassign>    state\.client\.unassign\(s\.endpoint\))",
        src,
    )
    if not m:
        print(f"[patch] session.py: stop-order pattern not matched (skip) {path}")
        return

    new = m.group("unassign") + "\n" + m.group("block").lstrip("\n")
    src = src.replace(m.group(0), "\n" + new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[patch] session.py stop order fixed: {path}")


def patch_disable_cli_keepalive_daemon():
    from colab_cli.commands import session

    path = session.__file__
    with open(path, encoding="utf-8") as f:
        src = f.read()

    if "colab-manager-bot: keep-alive managed in-process" in src:
        print(f"[patch] session.py keep-alive daemon already disabled: {path}")
        return

    # Replace `s.keep_alive_pid = spawn_keep_alive(...)` with a no-op pid so
    # `colab new` never spawns a detached per-session daemon.
    m = re.search(
        r"s\.keep_alive_pid = spawn_keep_alive\(\s*"
        r"endpoint,\s*name,\s*"
        r"auth_provider=state\.auth_provider,\s*"
        r"config_path=state\.config_path,\s*\)",
        src,
    )
    if not m:
        print(f"[patch] session.py: spawn_keep_alive pattern not matched (skip) {path}")
        return

    replacement = (
        "s.keep_alive_pid = 0  # colab-manager-bot: keep-alive managed in-process"
    )
    src = src[: m.start()] + replacement + src[m.end():]
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[patch] session.py keep-alive daemon disabled: {path}")


def main():
    if sys.version_info < (3, 12):
        print("[patch] WARNING: google-colab-cli 0.6.0 requires Python >=3.12; "
              "patches target that version")
    for fn in (patch_client, patch_stop_order, patch_disable_cli_keepalive_daemon):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"[patch] FAILED {fn.__name__}:")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    sys.exit(main())
