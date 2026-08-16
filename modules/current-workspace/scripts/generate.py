#!/usr/bin/env python3
"""Generate Waybar entries and CSS for the current-workspace module."""

from __future__ import annotations

import json
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def load_context() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        fail(f"invalid generator context JSON: {exc}")

    if not isinstance(value, dict):
        fail("generator context must be a JSON object")

    return value


def load_config(module_dir: Path) -> dict[str, Any]:
    path = module_dir / "config.toml"

    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        fail(f"missing module configuration: {path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {path}: {exc}")


def css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    context = load_context()

    module_dir = Path(str(context["module_dir"]))
    monitor = context["monitor"]

    monitor_name = str(monitor["name"])
    monitor_css = str(monitor["css"])

    config = load_config(module_dir)
    display = config.get("display", {})
    runtime = config.get("runtime", {})
    action = config.get("action", {})
    layout = context.get("layout", {})

    if not isinstance(display, dict):
        fail("[display] must be a TOML table")

    if not isinstance(runtime, dict):
        fail("[runtime] must be a TOML table")

    if not isinstance(action, dict):
        fail("[action] must be a TOML table")

    if not isinstance(layout, dict):
        fail("layout context must be an object")

    text = str(display.get("text", "Workspace - {workspace}"))

    if text.count("{workspace}") != 1:
        fail("display.text must contain {workspace} exactly once")

    color = str(display.get("color", "#cccccc")).strip()
    font_family = str(display.get("font_family", "monospace")).strip()

    try:
        interval = float(runtime.get("refresh_interval", 1))
    except (TypeError, ValueError):
        fail("runtime.refresh_interval must be a number")

    if not color:
        fail("display.color must not be empty")

    if not font_family:
        fail("display.font_family must not be empty")

    if interval <= 0:
        fail("runtime.refresh_interval must be greater than zero")

    font_size: float | None = None

    if "font_size" in display:
        try:
            font_size = float(display["font_size"])
        except (TypeError, ValueError):
            fail("display.font_size must be a number")

        if font_size <= 0:
            fail("display.font_size must be greater than zero")
    block_height = layout.get("height")

    if block_height is not None:
        if isinstance(block_height, bool) or not isinstance(block_height, int):
            fail("layout height must be an integer")

        if block_height <= 0:
            fail("layout height must be greater than zero")

    action_script = str(action.get("script", "")).strip()
    click_command: str | None = None

    if action_script:
        action_path = Path(action_script)

        if not action_path.is_absolute():
            action_path = module_dir / action_path

        if not action_path.is_file():
            fail(f"missing action script: {action_path}")

        click_command = shlex.join([str(action_path)])

    root = "custom/current-workspace"
    runtime_script = module_dir / "scripts" / "current-workspace.py"

    if not runtime_script.is_file():
        fail(f"missing current workspace runtime: {runtime_script}")

    format_text = text.replace("{workspace}", "{}")

    command = shlex.join(
        [
            sys.executable,
            str(runtime_script),
            monitor_name,
        ]
    )

    selector = (
        f"window#waybar.sidebar-{monitor_css} "
            "#custom-current-workspace"
    )

    css_lines = [
        f"/* Current workspace appearance for {monitor_name}. */",
        f"{selector} {{",
        f"    color: {color};",
        f"    font-family: {css_string(font_family)};",
    ]

    # Deliberately omit font-size when no override is configured.
    # The module then inherits the monitor-aware size from the core.
    if block_height is not None:
        css_lines.append(f"    min-height: {block_height}px;")

    if font_size is not None:
        css_lines.append(f"    font-size: {font_size:g}px;")

    css_lines.append("}")

    entry = {
        "exec": command,
        "interval": interval,
        "format": format_text,
        "tooltip": False,
    }

    if click_command is not None:
        entry["on-click"] = click_command

    json.dump(
        {
            "root": root,
            "entries": {
                root: entry,
            },
            "css": "\n".join(css_lines) + "\n",
        },
        sys.stdout,
    )
    sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
