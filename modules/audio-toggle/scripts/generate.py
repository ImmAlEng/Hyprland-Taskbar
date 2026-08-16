#!/usr/bin/env python3
"""Generate one placement-specific audio mute toggle module."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn


TARGETS = {
    "sink": "@DEFAULT_AUDIO_SINK@",
    "source": "@DEFAULT_AUDIO_SOURCE@",
}


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def require_dimension(
    layout: dict[str, Any],
    key: str,
    placement_id: str,
) -> int:
    value = layout.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            f"audio-toggle placement '{placement_id}' requires integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"audio-toggle placement '{placement_id}' allocated {key} "
            "must be greater than zero."
        )

    return value


def require_string(
    mapping: dict[str, Any],
    key: str,
    context: str,
) -> str:
    value = mapping.get(key)

    if not isinstance(value, str) or not value:
        fail(f"{context}.{key} must be a non-empty string.")

    return value


def main() -> int:
    context = json.load(sys.stdin)

    module_context = context.get("module", {})
    module_dir = Path(str(context["module_dir"]))
    monitor = context.get("monitor", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})

    if not isinstance(module_context, dict):
        fail("audio-toggle module context must be an object.")

    if module_context.get("instance") is not None:
        fail(
            "audio-toggle does not use named instances. "
            'Use { module = "audio-toggle", target = "sink" }.'
        )

    if not isinstance(monitor, dict):
        fail("audio-toggle monitor context must be an object.")

    if not isinstance(placement, dict):
        fail("audio-toggle placement context must be an object.")

    if not isinstance(layout, dict):
        fail("audio-toggle layout context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("audio-toggle did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("audio-toggle placement parameters must be an object.")

    unknown = set(parameters) - {"target"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        fail(
            f"audio-toggle placement '{placement_id}' has unsupported "
            f"parameter(s): {names}."
        )

    target = parameters.get("target")
    if target not in TARGETS:
        fail(
            f"audio-toggle placement '{placement_id}' target must be "
            '"sink" or "source".'
        )

    width = require_dimension(layout, "width", placement_id)
    height = require_dimension(layout, "height", placement_id)

    config_path = module_dir / "config.toml"
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        fail(f"missing audio-toggle configuration: {config_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {config_path}: {exc}")

    display = config.get("display", {})
    runtime = config.get("runtime", {})

    if not isinstance(display, dict):
        fail("audio-toggle [display] must be a table.")

    if not isinstance(runtime, dict):
        fail("audio-toggle [runtime] must be a table.")

    target_display = display.get(target, {})
    if not isinstance(target_display, dict):
        fail(f"audio-toggle [display.{target}] must be a table.")

    unmuted_color = require_string(
        display,
        "unmuted_color",
        "display",
    )
    muted_color = require_string(
        display,
        "muted_color",
        "display",
    )
    unmuted_icon = require_string(
        target_display,
        "unmuted_icon",
        f"display.{target}",
    )
    muted_icon = require_string(
        target_display,
        "muted_icon",
        f"display.{target}",
    )

    interval = runtime.get("refresh_interval", 1)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or interval <= 0
    ):
        fail("runtime.refresh_interval must be greater than zero.")

    status_script = module_dir / "scripts" / "status.py"
    if not status_script.is_file():
        fail(f"missing audio-toggle runtime: {status_script}")

    wpctl = shutil.which("wpctl")
    if wpctl is None:
        fail("audio-toggle requires wpctl in PATH.")

    monitor_css = str(monitor.get("css", monitor.get("name", "unknown")))

    root = f"custom/audio-toggle#{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#custom-audio-toggle.{placement_id}"
    )

    entry = {
        "exec": shlex.join(
            [
                sys.executable,
                str(status_script),
                target,
                unmuted_icon,
                muted_icon,
            ]
        ),
        "interval": float(interval),
        "return-type": "json",
        "format": "{}",
        "tooltip": False,
        "align": 0.5,
        "justify": "center",
        "on-click": shlex.join(
            [
                wpctl,
                "set-mute",
                TARGETS[target],
                "toggle",
            ]
        ),
    }

    css = "\n".join(
        [
            f"{selector} {{",
            "    padding: 0;",
            "    margin: 0;",
            f"    min-width: {width}px;",
            f"    min-height: {height}px;",
            f"    color: {unmuted_color};",
            "}",
            "",
            f"{selector}.unmuted {{",
            f"    color: {unmuted_color};",
            "}",
            "",
            f"{selector}.muted {{",
            f"    color: {muted_color};",
            "}",
        ]
    )

    json.dump(
        {
            "root": root,
            "entries": {
                root: entry,
            },
            "css": css + "\n",
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
