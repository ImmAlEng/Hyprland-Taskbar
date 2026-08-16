#!/usr/bin/env python3
"""Generate one static textbox placement and validate that its text fits."""

from __future__ import annotations

import html
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
    "text",
    "font",
    "font_size",
    "alignment",
    "color",
}


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def css_string(value: str) -> str:
    return json.dumps(value)


def waybar_static_text(value: str) -> str:
    """Escape plain text for fmt-style formatting and Pango markup."""
    escaped = html.escape(value, quote=False)
    return escaped.replace("{", "{{").replace("}", "}}")


def measure_text(
    text: str,
    font_family: str,
    font_size: float,
) -> tuple[int, int]:
    """Measure logical pixel size using Pango."""
    try:
        import gi

        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")

        from gi.repository import Pango, PangoCairo
    except (ImportError, ValueError) as exc:
        fail(
            "textbox requires PyGObject with Pango/PangoCairo support. "
            f"Import failed: {exc}"
        )

    font_map = PangoCairo.FontMap.get_default()
    if font_map is None:
        fail("textbox could not obtain the default PangoCairo font map.")

    pango_context = font_map.create_context()
    layout = Pango.Layout.new(pango_context)
    layout.set_text(text, -1)

    description = Pango.FontDescription()
    description.set_family(font_family)
    description.set_absolute_size(font_size * Pango.SCALE)
    layout.set_font_description(description)

    width, height = layout.get_pixel_size()
    return int(width), int(height)


def require_positive_dimension(
    layout: dict[str, Any],
    key: str,
    placement_id: str,
) -> int:
    value = layout.get(key)

    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            f"textbox placement '{placement_id}' requires an integer "
            f"allocated {key} from the layout core."
        )

    if value <= 0:
        fail(
            f"textbox placement '{placement_id}' allocated {key} "
            "must be greater than zero."
        )

    return value


def main() -> int:
    context = json.load(sys.stdin)

    monitor = context.get("monitor", {})
    module_context = context.get("module", {})
    placement = context.get("placement", {})
    layout_context = context.get("layout", {})
    core_font = context.get("font", {})

    if not isinstance(monitor, dict):
        fail("textbox monitor context must be an object.")
    if not isinstance(module_context, dict):
        fail("textbox module context must be an object.")
    if not isinstance(placement, dict):
        fail("textbox placement context must be an object.")
    if not isinstance(layout_context, dict):
        fail("textbox layout context must be an object.")
    if not isinstance(core_font, dict):
        fail("textbox font context must be an object.")

    # textbox is placement-parameterized; named instances are unnecessary.
    instance = module_context.get("instance")
    if instance is not None:
        fail(
            "textbox does not use named instances. "
            'Use { module = "textbox", text = "..." } in layout.toml.'
        )

    placement_id = placement.get("id")
    if not isinstance(placement_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+",
        placement_id,
    ):
        fail("textbox did not receive a valid placement id from the core.")

    parameters = placement.get("parameters", {})
    if not isinstance(parameters, dict):
        fail("textbox placement parameters must be an object.")

    unknown = set(parameters) - ALLOWED_PARAMETERS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        fail(
            f"textbox placement '{placement_id}' has unsupported "
            f"parameter(s): {names}."
        )

    text = parameters.get("text", "Textbox")
    if not isinstance(text, str):
        fail("textbox text must be a string.")

    inherited_family = core_font.get("family")
    if not isinstance(inherited_family, str) or not inherited_family.strip():
        fail("textbox did not receive a valid inherited core font family.")

    inherited_size = core_font.get("size")
    if (
        isinstance(inherited_size, bool)
        or not isinstance(inherited_size, (int, float))
        or float(inherited_size) <= 0
    ):
        fail("textbox did not receive a valid inherited core font size.")

    raw_font = parameters.get("font")
    if raw_font is None:
        font_family = inherited_family.strip()
    else:
        if not isinstance(raw_font, str) or not raw_font.strip():
            fail("textbox font must be a non-empty string.")
        font_family = raw_font.strip()

    raw_font_size = parameters.get("font_size")
    if raw_font_size is None:
        font_size = float(inherited_size)
    else:
        if (
            isinstance(raw_font_size, bool)
            or not isinstance(raw_font_size, (int, float))
        ):
            fail("textbox font_size must be a number.")

        font_size = float(raw_font_size)
        if font_size <= 0:
            fail("textbox font_size must be greater than zero.")

    alignment = parameters.get("alignment", "center")
    if not isinstance(alignment, str):
        fail("textbox alignment must be a string.")

    alignment = alignment.strip().lower()
    if alignment not in ALIGNMENT:
        fail('textbox alignment must be "left", "center", or "right".')

    color = parameters.get("color", "#ffffff")
    if not isinstance(color, str) or not color.strip():
        fail("textbox color must be a non-empty CSS color string.")
    color = color.strip()

    allocated_width = require_positive_dimension(
        layout_context,
        "width",
        placement_id,
    )
    allocated_height = require_positive_dimension(
        layout_context,
        "height",
        placement_id,
    )

    rendered_width, rendered_height = measure_text(
        text,
        font_family,
        font_size,
    )

    monitor_name = str(monitor.get("name", "unknown"))
    monitor_css = str(monitor.get("css", monitor_name))

    if rendered_width > allocated_width or rendered_height > allocated_height:
        fail(
            f"textbox does not fit on monitor '{monitor_name}':\n"
            f"  placement: {placement_id}\n"
            f"  text:      {text!r}\n"
            f"  allocated: {allocated_width}x{allocated_height}px\n"
            f"  rendered:  {rendered_width}x{rendered_height}px\n"
            f"  font:      {font_family} {font_size:g}px"
        )

    root = f"custom/textbox-{placement_id}"
    selector = (
        f"window#waybar.sidebar-{monitor_css} "
        f"#custom-textbox-{placement_id}"
    )

    css_lines = [
        f"{selector} {{",
        "    padding: 0;",
        "    margin: 0;",
        f"    min-width: {allocated_width}px;",
        f"    min-height: {allocated_height}px;",
        f"    color: {color};",
    ]

    # Omitted font properties truly inherit core CSS.
    if raw_font is not None:
        css_lines.append(f"    font-family: {css_string(font_family)};")

    if raw_font_size is not None:
        css_lines.append(f"    font-size: {font_size:g}px;")

    css_lines.append("}")

    json.dump(
        {
            "root": root,
            "entries": {
                root: {
                    "format": waybar_static_text(text),
                    "tooltip": False,
                    "align": ALIGNMENT[alignment],
                    "justify": alignment,
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
