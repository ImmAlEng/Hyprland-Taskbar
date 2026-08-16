#!/usr/bin/env python3
"""Resolve and operate one inactive workspace tile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CACHE_TTL = 0.35
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    / "hypr-sidebar"
    / "workspace-tiles"
)
MONITORS_CACHE = CACHE_DIR / "monitors.json"


def load_cached_monitors() -> list[dict[str, Any]] | None:
    try:
        if time.time() - MONITORS_CACHE.stat().st_mtime > CACHE_TTL:
            return None
        value = json.loads(MONITORS_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    return value if isinstance(value, list) else None


def save_cached_monitors(monitors: list[dict[str, Any]]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MONITORS_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(monitors), encoding="utf-8")
        tmp.replace(MONITORS_CACHE)
    except OSError:
        pass


def read_monitors() -> list[dict[str, Any]]:
    cached = load_cached_monitors()
    if cached is not None:
        return cached

    try:
        result = subprocess.run(
            ["hyprctl", "-j", "monitors"],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return []

    if not isinstance(value, list):
        return []

    save_cached_monitors(value)
    return value


def active_workspace(monitor_name: str) -> int | None:
    for monitor in read_monitors():
        if monitor.get("name") != monitor_name:
            continue

        workspace = monitor.get("activeWorkspace", {})
        try:
            return int(workspace.get("id"))
        except (TypeError, ValueError):
            return None

    return None


def resolve_workspace(
    monitor_name: str,
    tile_index: int,
    assigned: list[int],
) -> int | None:
    active = active_workspace(monitor_name)
    inactive = [workspace for workspace in assigned if workspace != active]

    offset = tile_index - 1
    if offset < 0 or offset >= len(inactive):
        return None

    return inactive[offset]


def show_tile(module_dir: Path, workspace: int) -> int:
    renderer = module_dir / "scripts" / "workspace-icons.py"

    try:
        result = subprocess.run(
            [sys.executable, str(renderer), str(workspace)],
            check=False,
            text=True,
        )
    except OSError:
        return 1

    return result.returncode


def lua_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def switch_workspace(monitor_name: str, workspace: int) -> int:
    monitor_dispatch = (
        'hl.dsp.focus({ monitor = "'
        + lua_string(monitor_name)
        + '" })'
    )
    workspace_dispatch = f'hl.dsp.focus({{ workspace = "{workspace}" }})'

    try:
        subprocess.run(
            ["hyprctl", "dispatch", monitor_dispatch],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["hyprctl", "dispatch", workspace_dispatch],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return 1

    return 0


def main() -> int:
    if len(sys.argv) < 6:
        print(
            "usage: workspace-tile.py ACTION MODULE_DIR MONITOR INDEX WORKSPACE...",
            file=sys.stderr,
        )
        return 2

    action = sys.argv[1]
    module_dir = Path(sys.argv[2])
    monitor_name = sys.argv[3]

    try:
        tile_index = int(sys.argv[4])
        assigned = [int(value) for value in sys.argv[5:]]
    except ValueError:
        return 2

    workspace = resolve_workspace(monitor_name, tile_index, assigned)
    if workspace is None:
        return 0

    if action == "show":
        return show_tile(module_dir, workspace)

    if action == "switch":
        return switch_workspace(monitor_name, workspace)

    print(f"unknown action: {action}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
