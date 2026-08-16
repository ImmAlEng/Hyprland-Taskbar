#!/usr/bin/env python3
"""Generate one invisible empty placement."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, NoReturn


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def require_positive_dimension(
    layout: dict[str, Any],
    key: str,
    placement_id: str,
) -> int:
    value = layout.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            f"empty placement '{placement_id}' requires an integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"empty placement '{placement_id}' allocated {key} "
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
        fail("empty module context must be an object.")

    if module_context.get("instance") is not None:
        fail('empty does not use named instances. Use { module = "empty" }.')

    if not isinstance(placement, dict):
        fail("empty placement context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("empty did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("empty placement parameters must be an object.")

    if parameters:
        names = ", ".join(sorted(str(name) for name in parameters))
        fail(
            f"empty placement '{placement_id}' has unsupported "
            f"parameter(s): {names}."
        )

    if not isinstance(layout, dict):
        fail("empty layout context must be an object.")

    width = require_positive_dimension(layout, "width", placement_id)
    height = require_positive_dimension(layout, "height", placement_id)

    if not isinstance(monitor, dict):
        fail("empty monitor context must be an object.")

    monitor_name = str(monitor.get("name", "unknown"))
    monitor_css = str(monitor.get("css", monitor_name))

    root = f"custom/empty-{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#custom-empty-{placement_id}"
    )

    css = "\n".join(
        [
            f"{selector} {{",
            "    padding: 0;",
            "    margin: 0;",
            f"    min-width: {width}px;",
            f"    min-height: {height}px;",
            "    opacity: 0;",
            "}",
            "",
        ]
    )

    json.dump(
        {
            "root": root,
            "entries": {
                root: {
                    # Keep a real label alive so Waybar/GTK preserves geometry.
                    "format": "\u00a0",
                    "tooltip": False,
                }
            },
            "css": css,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
