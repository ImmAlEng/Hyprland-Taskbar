#!/usr/bin/env python3
"""Generate one box wrapper around a core-rendered child module."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, NoReturn


ALLOWED_PARAMETERS = {"child", "border", "radius", "click"}


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def require_dimension(layout: dict[str, Any], key: str, placement_id: str) -> int:
    value = layout.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(
            f"box placement '{placement_id}' requires a positive integer "
            f"allocated {key} from the layout core."
        )
    return value


def resolve_click(raw: Any, project_root: Path, placement_id: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        fail(f"box placement '{placement_id}' click must be a non-empty path.")

    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        path = project_root / path

    try:
        path = path.resolve(strict=True)
    except FileNotFoundError:
        fail(f"box placement '{placement_id}' click path does not exist: {path}")

    if not path.is_file():
        fail(f"box placement '{placement_id}' click path is not a file: {path}")
    if not os.access(path, os.X_OK):
        fail(f"box placement '{placement_id}' click path is not executable: {path}")

    return str(path)


def main() -> int:
    context = json.load(sys.stdin)

    module_context = context.get("module", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})
    bar = context.get("bar", {})
    monitor = context.get("monitor", {})

    if not isinstance(module_context, dict):
        fail("box module context must be an object.")
    if module_context.get("instance") is not None:
        fail('box does not use named instances. Use { module = "box", ... }.')
    if not isinstance(placement, dict):
        fail("box placement context must be an object.")
    if not isinstance(layout, dict):
        fail("box layout context must be an object.")
    if not isinstance(bar, dict):
        fail("box bar context must be an object.")
    if not isinstance(monitor, dict):
        fail("box monitor context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", placement_id
    ):
        fail("box did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("box placement parameters must be an object.")

    unknown = set(parameters) - ALLOWED_PARAMETERS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        fail(
            f"box placement '{placement_id}' has unsupported parameter(s): {names}."
        )

    if "child" not in parameters:
        fail(f"box placement '{placement_id}' requires child = {{ ... }}.")

    width = require_dimension(layout, "width", placement_id)
    height = require_dimension(layout, "height", placement_id)

    core_border = bar.get("border_width")
    if isinstance(core_border, bool) or not isinstance(core_border, int) or core_border < 0:
        fail("box did not receive a valid core border width.")

    raw_border = parameters.get("border")
    if raw_border is None:
        border = max(1, round(core_border * 0.25))
    else:
        if isinstance(raw_border, bool) or not isinstance(raw_border, int):
            fail(f"box placement '{placement_id}' border must be an integer.")
        if raw_border < 0:
            fail(f"box placement '{placement_id}' border must not be negative.")
        border = raw_border

    raw_radius = parameters.get("radius")
    radius: int | None = None
    if raw_radius is not None:
        if isinstance(raw_radius, bool) or not isinstance(raw_radius, int):
            fail(f"box placement '{placement_id}' radius must be an integer.")
        if raw_radius < 0:
            fail(f"box placement '{placement_id}' radius must not be negative.")
        radius = raw_radius

    child_width = width - (2 * border)
    child_height = height - (2 * border)
    if child_width <= 0 or child_height <= 0:
        fail(
            f"box placement '{placement_id}' has no usable child area:\n"
            f"  allocated: {width}x{height}px\n"
            f"  border:    {border}px\n"
            f"  child:     {child_width}x{child_height}px"
        )

    project_root_raw = context.get("project_root")
    if not isinstance(project_root_raw, str) or not project_root_raw:
        fail("box did not receive a valid project_root.")
    click = resolve_click(parameters.get("click"), Path(project_root_raw), placement_id)

    border_color = bar.get("border_color")
    if not isinstance(border_color, str) or not border_color.strip():
        fail("box did not receive a valid core border color.")

    monitor_css = monitor.get("css")
    if not isinstance(monitor_css, str) or not monitor_css:
        fail("box did not receive a valid monitor CSS name.")

    root = f"group/box-{placement_id}"
    css_id = f"box-{placement_id}"
    selector = f"window#waybar.sidebar-{monitor_css} #{css_id}"
    css_lines = [
        f"{selector} {{",
        "    padding: 0;",
        "    margin: 0;",
    ]
    if border > 0:
        css_lines.append(f"    border: {border}px solid {border_color.strip()};")
    if radius is not None:
        css_lines.append(f"    border-radius: {radius}px;")
    css_lines.append("}")

    json.dump(
        {
            "root": root,
            "entries": {
                root: {
                    "orientation": "horizontal",
                    "modules": [],
                }
            },
            "css": "\n".join(css_lines) + "\n",
            "container": {
                "inset": border,
                "on_click": click,
            },
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
