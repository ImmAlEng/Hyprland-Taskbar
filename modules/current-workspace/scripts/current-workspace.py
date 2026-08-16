#!/usr/bin/env python3
"""Print the active workspace number for one Hyprland monitor."""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: current-workspace.py MONITOR", file=sys.stderr)
        return 2

    monitor_name = sys.argv[1]

    try:
        result = subprocess.run(
            ["hyprctl", "-j", "monitors"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("hyprctl was not found", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return exc.returncode or 1

    try:
        monitors = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"invalid hyprctl JSON: {exc}", file=sys.stderr)
        return 1

    for monitor in monitors:
        if monitor.get("name") != monitor_name:
            continue

        workspace = monitor.get("activeWorkspace", {}).get("id")

        if workspace is None:
            return 1

        print(workspace)
        return 0

    print(f"monitor not found: {monitor_name}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
