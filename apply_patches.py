"""Re-apply colab-cli patches that the bot relies on.

google-colab-cli is installed from PyPI as-is; these two behaviour fixes are
idempotently applied to the installed package at container start so local and
Railway behaviour match.

1. client.py: default per-request timeout 600s (TPU/GPU provisioning can exceed
   google-auth's 120s read timeout).
2. commands/session.py stop(): unassign the endpoint BEFORE the (slow) kernel
   shutdown so the VM is released immediately.
"""
import os
import re
import sys


def patch_client():
    import colab_cli.client as client

    path = client.__file__
    with open(path) as f:
        src = f.read()

    marker = 'kwargs.setdefault("timeout", 600)'
    if marker in src:
        print(f"[patch] client.py already patched: {path}")
        return

    needle = '        response = self.session.request('
    if needle not in src:
        print(f"[patch] WARN: needle not found in {path}")
        return

    patched = src.replace(
        needle,
        f'        {marker}\n{needle}',
        1,
    )
    with open(path, "w") as f:
        f.write(patched)
    print(f"[patch] client.py timeout 600 applied: {path}")


def patch_stop_order():
    from colab_cli.commands import session

    path = session.__file__
    with open(path) as f:
        src = f.read()

    # Vanilla stop() runs the slow kernel shutdown BEFORE unassigning the
    # endpoint, so the VM lingers for ~45s. Move unassign() above the shutdown.
    m = re.search(
        r"(?P<block>\n    try:\n        runtime = ColabRuntime\(s\.url, s\.token, kernel_id=s\.kernel_id\)\n        runtime\.stop\(shutdown_kernel=True\)\n    except Exception:\n        pass\n\n)(?P<unassign>    state\.client\.unassign\(s\.endpoint\))",
        src,
    )
    if not m:
        print(f"[patch] session.py: skip (pattern not matched) {path}")
        return

    new = m.group("unassign") + "\n" + m.group("block").lstrip("\n")
    src = src.replace(m.group(0), "\n" + new, 1)
    with open(path, "w") as f:
        f.write(src)
    print(f"[patch] session.py stop order fixed: {path}")


def main():
    for fn in (patch_client, patch_stop_order):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[patch] FAILED {fn.__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
