#!/usr/bin/env python3
"""Generate one placement-specific Wi-Fi status module."""

from __future__ import annotations

import json
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn


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
            f"wifi placement '{placement_id}' requires integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"wifi placement '{placement_id}' allocated {key} "
            "must be greater than zero."
        )

    return value


def require_non_empty_string(
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

    module_dir = Path(str(context["module_dir"]))
    monitor = context.get("monitor", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})

    if not isinstance(monitor, dict):
        fail("wifi monitor context must be an object.")

    if not isinstance(placement, dict):
        fail("wifi placement context must be an object.")

    if not isinstance(layout, dict):
        fail("wifi layout context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("wifi did not receive a valid placement id from the core.")

    width = require_dimension(layout, "width", placement_id)
    height = require_dimension(layout, "height", placement_id)

    config_path = module_dir / "config.toml"
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        fail(f"missing wifi configuration: {config_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {config_path}: {exc}")

    display = config.get("display", {})
    runtime = config.get("runtime", {})

    if not isinstance(display, dict):
        fail("wifi [display] must be a table.")

    if not isinstance(runtime, dict):
        fail("wifi [runtime] must be a table.")

    wifi_icon = require_non_empty_string(
        display,
        "wifi_icon",
        "display",
    )
    ethernet_icon = require_non_empty_string(
        display,
        "ethernet_icon",
        "display",
    )
    connected_color = require_non_empty_string(
        display,
        "connected_color",
        "display",
    )
    disconnected_color = require_non_empty_string(
        display,
        "disconnected_color",
        "display",
    )

    interval = runtime.get("refresh_interval", 2)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or interval <= 0
    ):
        fail("runtime.refresh_interval must be greater than zero.")

    status_script = module_dir / "scripts" / "status.py"
    if not status_script.is_file():
        fail(f"missing wifi runtime: {status_script}")

    monitor_css = str(monitor.get("css", monitor.get("name", "unknown")))

    root = f"custom/wifi#{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#custom-wifi.{placement_id}"
    )

    entry = {
        "exec": shlex.join([sys.executable, str(status_script)]),
        "interval": float(interval),
        "return-type": "json",
        "format": "{}",
        "tooltip": False,
        "align": 0.5,
        "justify": "center",
    }

    css = "\n".join(
        [
            f"{selector} {{",
            "    padding: 0;",
            "    margin: 0;",
            f"    min-width: {width}px;",
            f"    min-height: {height}px;",
            f"    color: {connected_color};",
            "}",
            "",
            f"{selector}.wifi-connected {{",
            f"    color: {connected_color};",
            "}",
            "",
            f"{selector}.wifi-connected label {{",
            f"    color: {connected_color};",
            "}",
            "",
            f"{selector}.wifi-disconnected {{",
            f"    color: {disconnected_color};",
            "}",
            "",
            f"{selector}.wifi-disconnected label {{",
            f"    color: {disconnected_color};",
            "}",
            "",
            f"{selector}.ethernet {{",
            f"    color: {connected_color};",
            "}",
            "",
            f"{selector}.ethernet label {{",
            f"    color: {connected_color};",
            "}",
        ]
    )

    # Waybar's custom JSON output supplies the state name in "text".
    # Use format-alt style replacement via generated state classes is not
    # available, so emit a tiny shell-free formatter wrapper command.
    formatter = module_dir / "scripts" / "waybar.py"
    if not formatter.is_file():
        fail(f"missing wifi formatter runtime: {formatter}")

    entry["exec"] = shlex.join(
        [
            sys.executable,
            str(formatter),
            wifi_icon,
            ethernet_icon,
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
