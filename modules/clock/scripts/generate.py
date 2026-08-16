#!/usr/bin/env python3
"""Generate one Waybar built-in clock placement."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, NoReturn


ALIGNMENT = {
    "left": 0.0,
    "center": 0.5,
    "right": 1.0,
}

ALLOWED_PARAMETERS = {
    "format",
    "alignment",
    "color",
    "font",
    "font_size",
}


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def css_string(value: str) -> str:
    return json.dumps(value)


def require_positive_dimension(
    layout: dict[str, Any],
    key: str,
    placement_id: str,
) -> int:
    value = layout.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            f"clock placement '{placement_id}' requires an integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"clock placement '{placement_id}' allocated {key} "
            "must be greater than zero."
        )

    return value


def main() -> int:
    context = json.load(sys.stdin)

    module_context = context.get("module", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})
    monitor = context.get("monitor", {})

    if not isinstance(module_context, dict):
        fail("clock module context must be an object.")

    if module_context.get("instance") is not None:
        fail('clock does not use named instances. Use { module = "clock" }.')

    if not isinstance(placement, dict):
        fail("clock placement context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("clock did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("clock placement parameters must be an object.")

    unknown = set(parameters) - ALLOWED_PARAMETERS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        fail(
            f"clock placement '{placement_id}' has unsupported "
            f"parameter(s): {names}."
        )

    if not isinstance(layout, dict):
        fail("clock layout context must be an object.")

    width = require_positive_dimension(layout, "width", placement_id)
    height = require_positive_dimension(layout, "height", placement_id)

    format_text = parameters.get(
        "format",
        "{:%H:%M · %a %d-%m}",
    )
    if not isinstance(format_text, str) or not format_text:
        fail("clock format must be a non-empty string.")

    alignment = parameters.get("alignment", "center")
    if not isinstance(alignment, str):
        fail("clock alignment must be a string.")

    alignment = alignment.strip().lower()
    if alignment not in ALIGNMENT:
        fail('clock alignment must be "left", "center", or "right".')

    color = parameters.get("color", "#ffffff")
    if not isinstance(color, str) or not color.strip():
        fail("clock color must be a non-empty CSS color string.")
    color = color.strip()

    font = parameters.get("font")
    if font is not None:
        if not isinstance(font, str) or not font.strip():
            fail("clock font must be a non-empty string.")
        font = font.strip()

    font_size = parameters.get("font_size")
    if font_size is not None:
        if (
            isinstance(font_size, bool)
            or not isinstance(font_size, (int, float))
        ):
            fail("clock font_size must be a number.")

        font_size = float(font_size)
        if font_size <= 0:
            fail("clock font_size must be greater than zero.")

    if not isinstance(monitor, dict):
        fail("clock monitor context must be an object.")

    monitor_name = str(monitor.get("name", "unknown"))
    monitor_css = str(monitor.get("css", monitor_name))

    root = f"clock#{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#clock.{placement_id}"
    )

    css_lines = [
        f"{selector} {{",
        "    padding: 0;",
        "    margin: 0;",
        f"    min-width: {width}px;",
        f"    min-height: {height}px;",
        f"    color: {color};",
    ]

    if font is not None:
        css_lines.append(f"    font-family: {css_string(font)};")

    if font_size is not None:
        css_lines.append(f"    font-size: {font_size:g}px;")

    css_lines.append("}")

    entry: dict[str, Any] = {
        "format": format_text,
        "tooltip": False,
        "align": ALIGNMENT[alignment],
        "justify": alignment,
    }

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
