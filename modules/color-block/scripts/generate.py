#!/usr/bin/env python3
"""Generate one placement-specific color block."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn


HEX_COLOR = re.compile(
    r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$"
)


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
            f"color-block placement '{placement_id}' requires integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"color-block placement '{placement_id}' allocated {key} "
            "must be greater than zero."
        )

    return value


def normalize_hex_color(value: str) -> str:
    if not HEX_COLOR.fullmatch(value):
        fail(
            "color-block color must be a named preset or a hex color "
            "(#RGB, #RGBA, #RRGGBB or #RRGGBBAA)."
        )

    digits = value[1:]

    if len(digits) == 3:
        return "#" + "".join(char * 2 for char in digits)

    if len(digits) == 6:
        return value.lower()

    if len(digits) == 4:
        r, g, b, a = (int(char * 2, 16) for char in digits)
    else:
        r = int(digits[0:2], 16)
        g = int(digits[2:4], 16)
        b = int(digits[4:6], 16)
        a = int(digits[6:8], 16)

    return f"rgba({r}, {g}, {b}, {a / 255:.4f})"


def load_instances(module_dir: Path) -> dict[str, str]:
    path = module_dir / "config.toml"

    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except FileNotFoundError:
        fail(f"missing color-block configuration: {path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {path}: {exc}")

    raw_instances = config.get("instance", {})
    if not isinstance(raw_instances, dict):
        fail("color-block [instance] must be a TOML table.")

    instances: dict[str, str] = {}

    for name, raw_config in raw_instances.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            fail("color-block instance names may contain only letters, numbers, _ and -.")

        if not isinstance(raw_config, dict):
            fail(f"color-block [instance.{name}] must be a TOML table.")

        color = raw_config.get("color")
        if not isinstance(color, str) or not color.strip():
            fail(f"color-block [instance.{name}].color must be a non-empty string.")

        instances[name] = normalize_hex_color(color.strip())

    return instances


def resolve_color(
    instance: str | None,
    parameters: dict[str, Any],
    instances: dict[str, str],
    placement_id: str,
) -> str:
    unknown = set(parameters) - {"color"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        fail(
            f"color-block placement '{placement_id}' has unsupported "
            f"parameter(s): {names}."
        )

    raw_color = parameters.get("color")

    if instance is not None and raw_color is not None:
        fail(
            f"color-block placement '{placement_id}' must use either "
            "color-block:<preset> or color=..., not both."
        )

    if instance is not None:
        color = instances.get(instance)
        if color is None:
            available = ", ".join(sorted(instances))
            fail(
                f"unknown color-block preset '{instance}'. "
                f"Available presets: {available}."
            )
        return color

    if raw_color is None:
        fail(
            "color-block requires either a named preset such as "
            "'color-block:white' or a placement color such as "
            '{ module = "color-block", color = "#ffffff80" }.'
        )

    if not isinstance(raw_color, str) or not raw_color.strip():
        fail(
            f"color-block placement '{placement_id}' color must be "
            "a non-empty string."
        )

    raw_color = raw_color.strip()

    if raw_color in instances:
        return instances[raw_color]

    return normalize_hex_color(raw_color)


def main() -> int:
    context = json.load(sys.stdin)

    module_dir = Path(str(context["module_dir"]))
    module_context = context.get("module", {})
    monitor = context.get("monitor", {})
    placement = context.get("placement", {})
    layout = context.get("layout", {})

    if not isinstance(module_context, dict):
        fail("color-block module context must be an object.")

    if not isinstance(monitor, dict):
        fail("color-block monitor context must be an object.")

    if not isinstance(placement, dict):
        fail("color-block placement context must be an object.")

    if not isinstance(layout, dict):
        fail("color-block layout context must be an object.")

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("color-block did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("color-block placement parameters must be an object.")

    instance = module_context.get("instance")
    if instance is not None and not isinstance(instance, str):
        fail("color-block instance must be a string.")

    width = require_dimension(layout, "width", placement_id)
    height = require_dimension(layout, "height", placement_id)

    color = resolve_color(
        instance,
        parameters,
        load_instances(module_dir),
        placement_id,
    )

    monitor_css = str(monitor.get("css", monitor.get("name", "unknown")))

    root = f"custom/color-block#{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#custom-color-block.{placement_id}"
    )

    # A single whitespace keeps the custom module present. All intrinsic GTK
    # sizing is neutralized in CSS below; only the root allocation remains.
    entry = {
        "format": " ",
        "tooltip": False,
    }

    css = "\n".join(
        [
            f"{selector},",
            f"{selector} * {{",
            "    min-width: 0;",
            "    min-height: 0;",
            "    padding: 0;",
            "    margin: 0;",
            "    border: none;",
            "    font-size: 0px;",
            "}",
            "",
            f"{selector} {{",
            f"    min-width: {width}px;",
            f"    min-height: {height}px;",
            f"    background: {color};",
            "}",
        ]
    )

    json.dump(
        {
            "root": root,
            "entries": {root: entry},
            "css": css + "\n",
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
