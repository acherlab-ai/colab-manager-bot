#!/usr/bin/env python3
"""Clean up server-side assignments that have no local session record.

Usage: unassign_orphans.py <account_home> <sessions_config_path>
Calls unassign() on every server assignment whose endpoint is not tracked in
the local sessions.json. Used as a safety net when `colab new` times out after
the VM was already assigned (which leaks an orphan, billable runtime).
"""
import json
import os
import sys

from colab_cli.auth import AuthProvider, get_credentials
from colab_cli.client import Client, Prod

home, config_path = sys.argv[1], sys.argv[2]
os.environ["HOME"] = home

creds = get_credentials(None, provider=AuthProvider.OAUTH2)
client = Client(Prod(), creds)

try:
    assignments = client.list_assignments()
except Exception as e:
    print(f"list_assignments failed: {e}", file=sys.stderr)
    sys.exit(0)

local = set()
try:
    with open(config_path) as f:
        data = json.load(f)
    for s in data.values():
        if isinstance(s, dict) and s.get("endpoint"):
            local.add(s["endpoint"])
except Exception:
    pass

for a in assignments:
    if a.endpoint not in local:
        try:
            client.unassign(a.endpoint)
            print(f"unassigned orphan: {a.endpoint}")
        except Exception as e:
            print(f"unassign failed {a.endpoint}: {e}", file=sys.stderr)

print(f"orphan scan done (local={len(local)}, server={len(assignments)})")
