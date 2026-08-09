#!/usr/bin/env python3
"""Crew discovery — find all Straw Hat crew members on the network.

Inspects the `starter-app-net` Docker network for `backend-*` DNS names,
checks which are reachable, and prints a roster.
"""

import json
import os
import socket
import subprocess
import sys


# Container name prefixes to skip (not crew — other host stacks).
_SKIP_PREFIXES = ("miko-", "eva-", "ibgateway", "ib_gateway")


def discover_crew() -> list[dict]:
    """Return a list of crew member dicts: {name, alias, address, online}."""
    members = []
    seen = set()

    try:
        result = subprocess.run(
            ["docker", "network", "inspect", "starter-app-net",
             "--format", "{{json .Containers}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"Error: could not inspect starter-app-net: {result.stderr}",
                  file=sys.stderr)
            return []
        containers = json.loads(result.stdout) if result.stdout.strip() else {}
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return []

    for info in containers.values():
        cname = info.get("Name", "").lstrip("/")
        if not cname or any(cname.startswith(p) for p in _SKIP_PREFIXES):
            continue

        try:
            ci = subprocess.run(
                ["docker", "inspect", cname,
                 "--format",
                 "{{range $k, $v := .NetworkSettings.Networks}}"
                 "{{$k}}:{{json $v.DNSNames}};{{end}}"],
                capture_output=True, text=True, timeout=10,
            )
            if ci.returncode != 0:
                continue
        except Exception:
            continue

        # Parse "network1:[dns1,dns2];network2:[dns3,dns4];"
        dns_names = []
        for chunk in ci.stdout.split(";"):
            chunk = chunk.strip()
            if ":" not in chunk:
                continue
            net_name, dns_json = chunk.split(":", 1)
            if "starter-app-net" not in net_name:
                continue
            try:
                dns_names.extend(json.loads(dns_json))
            except json.JSONDecodeError:
                pass

        # Find the backend-* alias (our naming convention)
        backend_alias = None
        for d in dns_names:
            if d.startswith("backend-"):
                backend_alias = d
                break

        if not backend_alias:
            continue

        name = backend_alias.replace("backend-", "", 1)
        if name in seen:
            continue
        seen.add(name)

        address = f"http://{backend_alias}:8000"
        online = _check_online(backend_alias, 8000)

        members.append({
            "name": name,
            "alias": backend_alias,
            "address": address,
            "online": online,
        })

    # Sort: self first, then alphabetical
    self_name = os.environ.get("CLONE_NAME", "")
    members.sort(key=lambda m: (m["name"] != self_name, m["name"]))
    return members


def _check_online(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP connect check."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def format_roster(members: list[dict]) -> str:
    """Pretty-print the crew roster."""
    if not members:
        return "No crew members found on the network."

    lines = [
        "🏴‍☠️  Straw Hat Crew Roster",
        "─" * 55,
        f"{'Name':<14} {'Address':<32} {'Status'}",
        "─" * 55,
    ]
    online_count = 0
    for m in members:
        status = "✅ online" if m["online"] else "❌ offline"
        online_count += m["online"]
        lines.append(f"{m['name']:<14} {m['address']:<32} {status}")

    lines.append("─" * 55)
    lines.append(f"{len(members)} crew members — "
                 f"{online_count} online, {len(members) - online_count} offline")
    return "\n".join(lines)


def main():
    members = discover_crew()
    print(format_roster(members))
    if members:
        print()
        print("💡 Use talk_to(name=\"<name>\", message=\"...\") "
              "to message a crew member.")


if __name__ == "__main__":
    main()
