#!/usr/bin/env python3
"""Generate one color-block module instance."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import NoReturn


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    context = json.load(sys.stdin)

    module_dir = Path(context["module_dir"])
    monitor_css = str(context["monitor"]["css"])
    module_context = context.get("module", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})

    if not isinstance(module_context, dict):
        fail("module context must be an object")

    if not isinstance(placement, dict):
        fail("placement context must be an object")

    instance = module_context.get("instance")
    if not isinstance(instance, str) or not instance:
        fail("color-block requires an instance, for example color-block:red")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", instance):
        fail("color-block instance contains unsupported characters")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("color-block did not receive a valid placement id from the core")

    with (module_dir / "config.toml").open("rb") as handle:
        config = tomllib.load(handle)

    instances = config.get("instance", {})
    if not isinstance(instances, dict):
        fail("[instance] must be a TOML table")

    instance_cfg = instances.get(instance)
    if not isinstance(instance_cfg, dict):
        fail(f"missing [instance.{instance}] in config.toml")

    color = str(instance_cfg.get("color", "")).strip()
    if not color:
        fail(f"[instance.{instance}].color must not be empty")

    width = layout.get("width")
    height = layout.get("height")

    root = f"custom/color-block-{instance}-{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#custom-color-block-{instance}-{placement_id}"
    )

    css_lines = [
        f"{selector} {{",
        f"    background: {color};",
    ]

    if width is not None:
        css_lines.append(f"    min-width: {int(width)}px;")

    if height is not None:
        css_lines.append(f"    min-height: {int(height)}px;")

    css_lines.append("}")

    json.dump(
        {
            "root": root,
            "entries": {
                root: {
                    "format": " ",
                    "tooltip": False,
                }
            },
            "css": "\n".join(css_lines) + "\n",
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
