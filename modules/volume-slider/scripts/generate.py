#!/usr/bin/env python3
"""Generate one placement-specific Waybar PulseAudio slider."""

from __future__ import annotations

import json
import re
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
            f"volume-slider placement '{placement_id}' requires integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"volume-slider placement '{placement_id}' allocated {key} "
            "must be greater than zero."
        )

    return value


def require_int(
    mapping: dict[str, Any],
    key: str,
    context: str,
    minimum: int | None = None,
) -> int:
    value = mapping.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{context}.{key} must be an integer.")

    if minimum is not None and value < minimum:
        fail(f"{context}.{key} must be at least {minimum}.")

    return value


def require_bool(
    mapping: dict[str, Any],
    key: str,
    context: str,
) -> bool:
    value = mapping.get(key)

    if not isinstance(value, bool):
        fail(f"{context}.{key} must be true or false.")

    return value


def require_string(
    mapping: dict[str, Any],
    key: str,
    context: str,
) -> str:
    value = mapping.get(key)

    if not isinstance(value, str) or not value.strip():
        fail(f"{context}.{key} must be a non-empty string.")

    return value.strip()


def main() -> int:
    context = json.load(sys.stdin)

    module_context = context.get("module", {})
    module_dir = Path(str(context["module_dir"]))
    monitor = context.get("monitor", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})

    if not isinstance(module_context, dict):
        fail("volume-slider module context must be an object.")

    if module_context.get("instance") is not None:
        fail(
            "volume-slider does not use named instances. "
            'Use { module = "volume-slider" }.'
        )

    if not isinstance(monitor, dict):
        fail("volume-slider monitor context must be an object.")

    if not isinstance(placement, dict):
        fail("volume-slider placement context must be an object.")

    if not isinstance(layout, dict):
        fail("volume-slider layout context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("volume-slider did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("volume-slider placement parameters must be an object.")

    if parameters:
        names = ", ".join(sorted(str(name) for name in parameters))
        fail(
            f"volume-slider placement '{placement_id}' has unsupported "
            f"parameter(s): {names}."
        )

    width = require_dimension(layout, "width", placement_id)
    height = require_dimension(layout, "height", placement_id)

    config_path = module_dir / "config.toml"
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        fail(f"missing volume-slider configuration: {config_path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {config_path}: {exc}")

    slider = config.get("slider", {})
    display = config.get("display", {})

    if not isinstance(slider, dict):
        fail("volume-slider [slider] must be a table.")

    if not isinstance(display, dict):
        fail("volume-slider [display] must be a table.")

    minimum = require_int(slider, "min", "slider", 0)
    maximum = require_int(slider, "max", "slider", 1)

    if maximum <= minimum:
        fail("slider.max must be greater than slider.min.")

    target = require_string(slider, "target", "slider")
    if target not in {"sink", "source"}:
        fail('slider.target must be "sink" or "source".')

    zero_on_mute = require_bool(slider, "zero_on_mute", "slider")
    unmute_on_volume_change = require_bool(
        slider,
        "unmute_on_volume_change",
        "slider",
    )

    track_height = require_int(
        display,
        "track_height",
        "display",
        1,
    )
    radius = require_int(display, "radius", "display", 0)

    if track_height > height:
        fail(
            f"volume-slider placement '{placement_id}' track_height "
            f"{track_height}px exceeds allocated height {height}px."
        )

    background = require_string(display, "background", "display")
    fill = require_string(display, "fill", "display")
    muted_fill = require_string(display, "muted_fill", "display")

    monitor_css = str(monitor.get("css", monitor.get("name", "unknown")))

    root = f"pulseaudio/slider#{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#pulseaudio-slider.{placement_id}"
    )

    entry = {
        "min": minimum,
        "max": maximum,
        "target": target,
        "orientation": "horizontal",
        "zero-on-mute": zero_on_mute,
        "unmute-on-volume-change": unmute_on_volume_change,
    }

    css = "\n".join(
        [
            f"{selector} {{",
            "    padding: 0;",
            "    margin: 0;",
            f"    min-width: {width}px;",
            f"    min-height: {height}px;",
            "}",
            "",
            f"{selector} slider {{",
            "    min-width: 0;",
            "    min-height: 0;",
            "    opacity: 0;",
            "    background: none;",
            "    border: none;",
            "    box-shadow: none;",
            "}",
            "",
            f"{selector} trough {{",
            f"    min-width: {width}px;",
            f"    min-height: {track_height}px;",
            f"    border-radius: {radius}px;",
            f"    background: {background};",
            "}",
            "",
            f"{selector} highlight {{",
            f"    min-height: {track_height}px;",
            f"    border-radius: {radius}px;",
            f"    background: {fill};",
            "}",
            "",
            f"{selector}.muted highlight {{",
            f"    background: {muted_fill};",
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
