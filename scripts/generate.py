#!/usr/bin/env python3
"""
Generate the Hypr Sidebar Waybar configuration.

Core responsibilities:
- read global TOML configuration
- discover active Hyprland monitors
- calculate monitor-specific bar geometry
- load an independent layout TOML for each active monitor slot
- load module manifests
- expand module templates
- generate Waybar config.jsonc
- generate core and module CSS

The core does not contain module-specific behavior.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


SUPPORTED_MODULE_CONTRACT = 1


@dataclass(frozen=True)
class Monitor:
    name: str
    width: int
    height: int
    scale: float
    transform: int
    logical_width: int
    logical_height: int
    slot: int = 0


@dataclass(frozen=True)
class BarGeometry:
    width: int
    border_width: int
    inner_width: int
    inner_height: int
    font_size: float


@dataclass(frozen=True)
class DrawGeometry:
    margin_x: int
    margin_y: int
    width: int
    height: int


@dataclass(frozen=True)
class HyprlandGaps:
    top: int
    right: int
    bottom: int
    left: int


@dataclass(frozen=True)
class ModuleManifest:
    name: str
    directory: Path
    root: str | None
    entries: dict[str, dict[str, Any]]
    style: Path | None
    generator: Path | None
    child_parameter: str | None = None


@dataclass(frozen=True)
class LayoutModule:
    name: str
    instance: str | None = None
    overwrite_pad_x: int | None = None
    overwrite_pad_y: int | None = None
    width: int | None = None
    width_percent: float | None = None
    width_remaining: bool = False
    height: int | None = None
    height_percent: float | None = None
    height_remaining: bool = False
    parameters: dict[str, Any] | None = None

    @property
    def reference(self) -> str:
        if self.instance is None:
            return self.name
        return f"{self.name}:{self.instance}"


@dataclass(frozen=True)
class LayoutColumn:
    items: tuple["LayoutItem", ...]
    item_heights_percent: tuple[float, ...] | None = None
    overwrite_pad_x: int | None = None
    overwrite_pad_y: int | None = None
    width: int | None = None
    width_percent: float | None = None
    width_remaining: bool = False
    height: int | None = None
    height_percent: float | None = None
    height_remaining: bool = False


LayoutItem = LayoutModule | LayoutColumn


@dataclass(frozen=True)
class LayoutBlock:
    kind: str
    modules: tuple[LayoutItem, ...]
    width: int | None
    height: int | None
    height_percent: float | None
    height_remaining: bool
    item_widths_percent: tuple[float, ...] | None
    item_heights_percent: tuple[float, ...] | None
    overwrite_pad_y: int | None


@dataclass(frozen=True)
class SlotLayout:
    slot: int
    path: Path
    config: dict[str, Any]
    blocks: tuple[LayoutBlock, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the Hypr Sidebar Waybar configuration."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Project root. Defaults to the parent of scripts/.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print generated output without writing files.",
    )
    return parser.parse_args()


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise SystemExit(f"Missing configuration file: {path}")
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Invalid TOML in {path}: {exc}")



def load_app_commands(config: dict[str, Any]) -> dict[str, str]:
    """Load semantic application aliases from config/apps.toml."""
    raw_apps = config.get("app", {})

    if not isinstance(raw_apps, dict):
        raise SystemExit("apps.toml [app] must be a table.")

    commands: dict[str, str] = {}

    for name, raw_entry in raw_apps.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise SystemExit(
                f"apps.toml contains invalid app name {name!r}; "
                "use letters, numbers, '_' or '-'."
            )

        if not isinstance(raw_entry, dict):
            raise SystemExit(
                f"apps.toml [app.{name}] must be a table."
            )

        raw_command = raw_entry.get("command")
        if not isinstance(raw_command, str) or not raw_command.strip():
            raise SystemExit(
                f"apps.toml [app.{name}] command must be a non-empty string."
            )

        command = raw_command.strip()

        # Keep app aliases intentionally simple for now: one executable,
        # no shell fragments or arguments.
        if any(char.isspace() for char in command):
            raise SystemExit(
                f"apps.toml [app.{name}] command must be one executable "
                "without arguments."
            )

        candidate = Path(command).expanduser()

        if candidate.is_absolute():
            if not candidate.is_file():
                raise SystemExit(
                    f"apps.toml [app.{name}] executable does not exist: "
                    f"{candidate}"
                )
            resolved = str(candidate.resolve())
        else:
            found = shutil.which(command)
            if found is None:
                raise SystemExit(
                    f"apps.toml [app.{name}] executable was not found in PATH: "
                    f"{command}"
                )
            resolved = str(Path(found).resolve())

        commands[name] = resolved

    return commands


def resolve_app_alias(value: str, app_commands: dict[str, str], context: str) -> str:
    """Resolve app:<name> into the configured executable path."""
    if not value.startswith("app:"):
        return value

    name = value[4:]
    if not name:
        raise SystemExit(f"{context} contains empty app: reference.")

    command = app_commands.get(name)
    if command is None:
        available = ", ".join(sorted(app_commands)) or "(none)"
        raise SystemExit(
            f"{context} references unknown app alias '{name}'. "
            f"Configured aliases: {available}."
        )

    return command


def resolve_placement_app_aliases(
    parameters: dict[str, Any],
    app_commands: dict[str, str],
    context: str,
) -> dict[str, Any]:
    """Resolve direct placement string parameters that use app:<name>."""
    resolved = dict(parameters)

    for key, value in parameters.items():
        if isinstance(value, str) and value.startswith("app:"):
            resolved[key] = resolve_app_alias(
                value,
                app_commands,
                f"{context} parameter '{key}'",
            )

    return resolved


def required(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise SystemExit(f"Missing required key '{key}' in {context}")
    return mapping[key]


def css_safe(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    if not value:
        return "monitor"
    if value[0].isdigit():
        return f"monitor-{value}"
    return value


def read_hyprland_monitors() -> list[Monitor]:
    try:
        result = subprocess.run(
            ["hyprctl", "-j", "monitors"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise SystemExit("hyprctl was not found. Is Hyprland installed?")
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()
        message = "hyprctl failed while reading monitors"
        if detail:
            message += f": {detail}"
        raise SystemExit(message)

    try:
        raw_monitors = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"hyprctl returned invalid JSON: {exc}")

    monitors: list[Monitor] = []

    for raw in raw_monitors:
        if raw.get("disabled", False):
            continue

        name = str(required(raw, "name", "hyprctl monitor data"))
        width = int(required(raw, "width", f"monitor {name}"))
        height = int(required(raw, "height", f"monitor {name}"))
        scale = float(raw.get("scale", 1.0))
        transform = int(raw.get("transform", 0))

        if scale <= 0:
            raise SystemExit(f"Monitor {name} reported invalid scale: {scale}")

        logical_width = round(width / scale)
        logical_height = round(height / scale)

        # Hyprland transform values 1/3/5/7 rotate the output by 90/270 degrees.
        if transform in {1, 3, 5, 7}:
            logical_width, logical_height = logical_height, logical_width

        monitors.append(
            Monitor(
                name=name,
                width=width,
                height=height,
                scale=scale,
                transform=transform,
                logical_width=logical_width,
                logical_height=logical_height,
            )
        )

    if not monitors:
        raise SystemExit("Hyprland reported no active monitors.")

    return monitors


@lru_cache(maxsize=1)
def read_hyprland_gaps_out() -> HyprlandGaps:
    """Read general:gaps_out from the running Hyprland session once."""
    try:
        result = subprocess.run(
            ["hyprctl", "getoption", "general:gaps_out"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise SystemExit("hyprctl was not found. Is Hyprland installed?")
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()
        message = "hyprctl failed while reading general:gaps_out"
        if detail:
            message += f": {detail}"
        raise SystemExit(message)

    match = re.search(
        r"^css gap data:\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*$",
        result.stdout,
        re.MULTILINE,
    )

    if match is None:
        raise SystemExit(
            "Could not parse Hyprland general:gaps_out. "
            "Expected 'css gap data: TOP RIGHT BOTTOM LEFT'."
        )

    top, right, bottom, left = (int(value) for value in match.groups())

    if min(top, right, bottom, left) < 0:
        raise SystemExit(
            "Hyprland general:gaps_out contains a negative value, "
            "which cannot be used as a Waybar margin."
        )

    return HyprlandGaps(
        top=top,
        right=right,
        bottom=bottom,
        left=left,
    )


@lru_cache(maxsize=None)
def read_hyprland_int_option(option: str) -> int:
    """Read one integer option from the running Hyprland session."""
    try:
        result = subprocess.run(
            ["hyprctl", "getoption", option],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise SystemExit("hyprctl was not found. Is Hyprland installed?")
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()
        message = f"hyprctl failed while reading {option}"
        if detail:
            message += f": {detail}"
        raise SystemExit(message)

    match = re.search(r"^int:\s*(-?\d+)\s*$", result.stdout, re.MULTILINE)
    if match is None:
        raise SystemExit(
            f"Could not parse Hyprland {option}. Expected an integer option."
        )

    value = int(match.group(1))
    if value < 0:
        raise SystemExit(f"Hyprland {option} must not be negative.")

    return value


def assign_monitor_slots(
    monitors: list[Monitor],
    monitors_cfg: dict[str, Any],
) -> list[Monitor]:
    """
    Assign a stable logical slot to each active monitor.

    Explicit [monitor."NAME"].slot overrides always win.
    In auto mode, unassigned monitors receive the first free slots in the
    order returned by Hyprland.

    Slots are generic core metadata. The core does not attach workspace or
    module-specific meaning to them.
    """
    policy = required(monitors_cfg, "monitors", "monitors.toml")
    mode = str(policy.get("mode", "auto"))
    max_slots = int(policy.get("max_slots", 4))

    if mode not in {"auto", "explicit"}:
        raise SystemExit(
            "monitors.mode must be either 'auto' or 'explicit'."
        )

    if max_slots <= 0:
        raise SystemExit("monitors.max_slots must be greater than zero.")

    overrides = monitors_cfg.get("monitor", {})
    if not isinstance(overrides, dict):
        raise SystemExit("[monitor.\"NAME\"] entries must be TOML tables.")

    explicit: dict[str, int] = {}
    used_slots: set[int] = set()

    for name, cfg in overrides.items():
        if not isinstance(cfg, dict):
            raise SystemExit(f"Monitor override '{name}' must be a table.")

        if "slot" not in cfg:
            continue

        slot = int(cfg["slot"])
        if not 1 <= slot <= max_slots:
            raise SystemExit(
                f"Monitor '{name}' uses slot {slot}; "
                f"valid slots are 1..{max_slots}."
            )

        if slot in used_slots:
            raise SystemExit(
                f"Monitor slot {slot} is assigned more than once."
            )

        explicit[str(name)] = slot
        used_slots.add(slot)

    result: list[Monitor] = []
    next_free = 1

    for monitor in monitors:
        slot = explicit.get(monitor.name)

        if slot is None:
            if mode == "explicit":
                # Explicit mode intentionally ignores active outputs that have
                # no configured slot.
                continue

            while next_free in used_slots and next_free <= max_slots:
                next_free += 1

            if next_free > max_slots:
                raise SystemExit(
                    "More active monitors were discovered than available "
                    f"monitor slots ({max_slots})."
                )

            slot = next_free
            used_slots.add(slot)
            next_free += 1

        result.append(
            Monitor(
                name=monitor.name,
                width=monitor.width,
                height=monitor.height,
                scale=monitor.scale,
                transform=monitor.transform,
                logical_width=monitor.logical_width,
                logical_height=monitor.logical_height,
                slot=slot,
            )
        )

    if not result:
        raise SystemExit(
            "No active monitors remain after applying monitors.toml."
        )

    return result


def calculate_sidebar_width(logical_height: int, width_cfg: dict[str, Any]) -> int:
    mode = str(required(width_cfg, "mode", "[bar.width]"))

    if mode == "height-ratio":
        ratio = float(required(width_cfg, "ratio", "[bar.width]"))
        width = round(logical_height * ratio)
    elif mode == "fixed":
        width = int(required(width_cfg, "pixels", "[bar.width]"))
    else:
        raise SystemExit(f"Unsupported [bar.width] mode: {mode}")

    min_pixels = int(width_cfg.get("min_pixels", 1))
    max_pixels = int(width_cfg.get("max_pixels", 100000))

    if min_pixels <= 0:
        raise SystemExit("[bar.width].min_pixels must be greater than zero.")
    if max_pixels < min_pixels:
        raise SystemExit("[bar.width].max_pixels must be >= min_pixels.")

    return max(min_pixels, min(width, max_pixels))


def calculate_font_size(logical_height: int, font_cfg: dict[str, Any]) -> float:
    mode = str(font_cfg.get("mode", "dynamic"))

    if mode == "fixed":
        return round(float(required(font_cfg, "size", "[font]")), 2)

    if mode != "dynamic":
        raise SystemExit(f"Unsupported [font] mode: {mode}")

    h1 = float(required(font_cfg, "reference_height_1", "[font]"))
    s1 = float(required(font_cfg, "reference_size_1", "[font]"))
    h2 = float(required(font_cfg, "reference_height_2", "[font]"))
    s2 = float(required(font_cfg, "reference_size_2", "[font]"))

    if h1 == h2:
        raise SystemExit("[font] reference heights must be different.")

    slope = (s2 - s1) / (h2 - h1)
    size = s1 + (logical_height - h1) * slope

    min_size = float(font_cfg.get("min_size", 1.0))
    max_size = float(font_cfg.get("max_size", 100.0))

    return round(max(min_size, min(size, max_size)), 2)


def calculate_geometry(
    monitor: Monitor,
    waybar_cfg: dict[str, Any],
) -> BarGeometry:
    bar_cfg = required(waybar_cfg, "bar", "waybar.toml")
    width_cfg = required(bar_cfg, "width", "[bar]")
    font_cfg = required(waybar_cfg, "font", "waybar.toml")

    width = calculate_sidebar_width(monitor.logical_height, width_cfg)
    border_width = read_hyprland_int_option("general:border_size")

    gaps_out = read_hyprland_gaps_out()
    margin_top = gaps_out.top
    margin_bottom = gaps_out.bottom

    # The sidebar has one vertical border (left or right) and two horizontal
    # borders (top and bottom). Calculate the content rectangle here once;
    # downstream layout/module code must use these inner dimensions directly.
    inner_width = width - border_width
    inner_height = (
        monitor.logical_height
        - margin_top
        - margin_bottom
        - (2 * border_width)
    )

    if inner_width <= 0 or inner_height <= 0:
        raise SystemExit(
            f"Monitor '{monitor.name}' has no usable sidebar inner space."
        )

    return BarGeometry(
        width=width,
        border_width=border_width,
        inner_width=inner_width,
        inner_height=inner_height,
        font_size=calculate_font_size(monitor.logical_height, font_cfg),
    )



def calculate_draw_geometry(
    geometry: BarGeometry,
    layout_cfg: dict[str, Any],
) -> DrawGeometry:
    layout = layout_cfg.get("layout", {})
    if not isinstance(layout, dict):
        raise SystemExit("[layout] must be a TOML table.")

    fraction = float(layout.get("draw_margin_fraction", 0.02))
    if fraction < 0 or fraction >= 0.5:
        raise SystemExit(
            "[layout].draw_margin_fraction must be at least 0 and less than 0.5."
        )

    margin_x = round(geometry.inner_width * fraction)
    margin_y = margin_x

    draw_width = geometry.inner_width - (2 * margin_x)
    draw_height = geometry.inner_height - (2 * margin_y)

    if draw_width <= 0 or draw_height <= 0:
        raise SystemExit("The configured draw margin leaves no drawable area.")

    return DrawGeometry(
        margin_x=margin_x,
        margin_y=margin_y,
        width=draw_width,
        height=draw_height,
    )


def calculate_layout_pad_x(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
) -> int:
    """Resolve the default horizontal gap between items in a row."""
    layout = layout_cfg.get("layout", {})
    if not isinstance(layout, dict):
        raise SystemExit("[layout] must be a TOML table.")

    raw_pad_x = layout.get("pad_x")
    if raw_pad_x is None:
        return draw_geometry.margin_x

    if isinstance(raw_pad_x, bool) or not isinstance(raw_pad_x, int):
        raise SystemExit("[layout].pad_x must be an integer.")

    if raw_pad_x < 0:
        raise SystemExit("[layout].pad_x must not be negative.")

    return raw_pad_x


def calculate_item_pad_xs(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
) -> tuple[int, ...]:
    """Return each horizontal gap after an item in a row."""
    if block.kind != "row":
        return ()

    count = len(block.modules)
    if count <= 1:
        return ()

    inherited_pad_x = calculate_layout_pad_x(draw_geometry, layout_cfg)
    return tuple(
        (
            module.overwrite_pad_x
            if module.overwrite_pad_x is not None
            else inherited_pad_x
        )
        for module in block.modules[:-1]
    )


def calculate_layout_pad_y(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
) -> int:
    """Resolve the default vertical gap between top-level page items."""
    layout = layout_cfg.get("layout", {})
    if not isinstance(layout, dict):
        raise SystemExit("[layout] must be a TOML table.")

    raw_pad_y = layout.get("pad_y")
    if raw_pad_y is None:
        return draw_geometry.margin_y

    if isinstance(raw_pad_y, bool) or not isinstance(raw_pad_y, int):
        raise SystemExit("[layout].pad_y must be an integer.")

    if raw_pad_y < 0:
        raise SystemExit("[layout].pad_y must not be negative.")

    return raw_pad_y


def calculate_block_pad_y(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
    is_last: bool,
) -> int:
    """Return the gap after one page item; the final page item has no gap."""
    if is_last:
        return 0

    if block.overwrite_pad_y is not None:
        return block.overwrite_pad_y

    return calculate_layout_pad_y(draw_geometry, layout_cfg)


def parse_layout_module_reference(value: str, context: str) -> tuple[str, str | None]:
    if not value:
        raise SystemExit(f"{context} contains an empty module reference.")

    if ":" not in value:
        return value, None

    name, instance = value.split(":", 1)

    if not name or not instance:
        raise SystemExit(
            f"{context} contains invalid module instance reference '{value}'. "
            "Expected 'module:instance'."
        )

    if not re.fullmatch(r"[A-Za-z0-9_-]+", instance):
        raise SystemExit(
            f"{context} instance '{instance}' contains unsupported characters. "
            "Use letters, numbers, '_' or '-'."
        )

    return name, instance



def parse_axis_allocation(
    mapping: dict[str, Any],
    axis: str,
    context: str,
) -> tuple[int | None, float | None, bool]:
    """Parse px, percent, or remaining allocation for one axis."""
    raw_size = mapping.get(axis)
    raw_percent = mapping.get(f"{axis}_percent")

    pixels: int | None = None
    percent: float | None = None
    remaining = False

    if raw_size is not None:
        if isinstance(raw_size, str):
            if raw_size != "remaining":
                raise SystemExit(
                    f'{context} {axis} string value must be "remaining".'
                )
            remaining = True
        else:
            if isinstance(raw_size, bool) or not isinstance(raw_size, int):
                raise SystemExit(
                    f'{context} {axis} must be a positive integer or "remaining".'
                )
            if raw_size <= 0:
                raise SystemExit(
                    f"{context} {axis} must be greater than zero."
                )
            pixels = raw_size

    if raw_percent is not None:
        if (
            isinstance(raw_percent, bool)
            or not isinstance(raw_percent, (int, float))
        ):
            raise SystemExit(
                f"{context} {axis}_percent must be a number."
            )

        percent = float(raw_percent)
        if percent <= 0 or percent > 100:
            raise SystemExit(
                f"{context} {axis}_percent must be greater than 0 "
                "and at most 100."
            )

    mode_count = (
        int(pixels is not None)
        + int(percent is not None)
        + int(remaining)
    )

    if mode_count > 1:
        raise SystemExit(
            f"{context} may set only one of {axis}=<px>, "
            f'{axis}_percent=<percent>, or {axis}="remaining".'
        )

    return pixels, percent, remaining


def has_axis_allocation(item: LayoutItem, axis: str) -> bool:
    return bool(
        getattr(item, axis) is not None
        or getattr(item, f"{axis}_percent") is not None
        or getattr(item, f"{axis}_remaining")
    )


def describe_axis_allocation(item: LayoutItem, axis: str) -> str | None:
    pixels = getattr(item, axis)
    percent = getattr(item, f"{axis}_percent")
    remaining = getattr(item, f"{axis}_remaining")

    if pixels is not None:
        return f"{pixels}px"
    if percent is not None:
        return f"{percent:g}%"
    if remaining:
        return "remaining"
    return None


def parse_layout_module(value: Any, context: str) -> LayoutModule:
    """Parse a short module reference or an explicit module placement."""
    overwrite_pad_x: int | None = None
    overwrite_pad_y: int | None = None
    width: int | None = None
    width_percent: float | None = None
    width_remaining = False
    height: int | None = None
    height_percent: float | None = None
    height_remaining = False
    parameters: dict[str, Any] = {}

    if isinstance(value, str):
        reference = value
    elif isinstance(value, dict):
        reference = value.get("module")
        if not isinstance(reference, str) or not reference:
            raise SystemExit(
                f"{context} module placement requires a non-empty 'module' string."
            )

        raw_overwrite_pad_x = value.get("overwrite_pad_x")
        if raw_overwrite_pad_x is not None:
            if (
                isinstance(raw_overwrite_pad_x, bool)
                or not isinstance(raw_overwrite_pad_x, int)
            ):
                raise SystemExit(
                    f"{context} overwrite_pad_x must be an integer."
                )

            if raw_overwrite_pad_x < 0:
                raise SystemExit(
                    f"{context} overwrite_pad_x must not be negative."
                )

            overwrite_pad_x = raw_overwrite_pad_x

        raw_overwrite_pad_y = value.get("overwrite_pad_y")
        if raw_overwrite_pad_y is not None:
            if (
                isinstance(raw_overwrite_pad_y, bool)
                or not isinstance(raw_overwrite_pad_y, int)
            ):
                raise SystemExit(
                    f"{context} overwrite_pad_y must be an integer."
                )

            if raw_overwrite_pad_y < 0:
                raise SystemExit(
                    f"{context} overwrite_pad_y must not be negative."
                )

            overwrite_pad_y = raw_overwrite_pad_y

        width, width_percent, width_remaining = parse_axis_allocation(
            value,
            "width",
            context,
        )
        height, height_percent, height_remaining = parse_axis_allocation(
            value,
            "height",
            context,
        )

        parameters = {
            str(key): item
            for key, item in value.items()
            if key not in {
                "module",
                "overwrite_pad_x",
                "overwrite_pad_y",
                "width",
                "width_percent",
                "height",
                "height_percent",
            }
        }
    else:
        raise SystemExit(
            f"{context} contains an invalid module placement. "
            "Use a module string or an inline table with 'module'."
        )

    name, instance = parse_layout_module_reference(reference, context)
    return LayoutModule(
        name=name,
        instance=instance,
        overwrite_pad_x=overwrite_pad_x,
        overwrite_pad_y=overwrite_pad_y,
        width=width,
        width_percent=width_percent,
        width_remaining=width_remaining,
        height=height,
        height_percent=height_percent,
        height_remaining=height_remaining,
        parameters=parameters,
    )


def parse_layout_column(value: dict[str, Any], context: str) -> LayoutColumn:
    """Parse a vertical layout container."""
    allowed_keys = {
        "col",
        "item_heights_percent",
        "overwrite_pad_x",
        "overwrite_pad_y",
        "width",
        "width_percent",
        "height",
        "height_percent",
    }
    unknown_keys = set(value) - allowed_keys
    if unknown_keys:
        unknown = ", ".join(sorted(str(key) for key in unknown_keys))
        raise SystemExit(
            f"{context} col contains unsupported key(s): {unknown}."
        )

    raw_items = value.get("col")
    if not isinstance(raw_items, list) or not raw_items:
        raise SystemExit(f"{context} col must contain a non-empty list.")

    items = tuple(
        parse_layout_item(item, f"{context} col item {index}")
        for index, item in enumerate(raw_items, start=1)
    )

    # A column has vertical siblings only. Horizontal gap overrides on its
    # children would have no meaning.
    for index, item in enumerate(items, start=1):
        if item.overwrite_pad_x is not None:
            raise SystemExit(
                f"{context} col item {index} sets overwrite_pad_x, but "
                "column children have no horizontal sibling gap."
            )

    if items[-1].overwrite_pad_y is not None:
        raise SystemExit(
            f"{context} col item {len(items)} sets overwrite_pad_y, but "
            "the final item has no vertical gap after it."
        )

    for index, item in enumerate(items, start=1):
        if has_axis_allocation(item, "width"):
            raise SystemExit(
                f"{context} col item {index} sets a width allocation, but "
                "column children inherit the column width."
            )

    raw_heights = value.get("item_heights_percent")
    item_heights_percent: tuple[float, ...] | None = None

    if raw_heights is not None:
        if not isinstance(raw_heights, list):
            raise SystemExit(
                f"{context} item_heights_percent must be a list."
            )

        if len(raw_heights) != len(items):
            raise SystemExit(
                f"{context} item_heights_percent must contain exactly "
                "one value per col item."
            )

        percentages: list[float] = []
        for raw_percent in raw_heights:
            if (
                isinstance(raw_percent, bool)
                or not isinstance(raw_percent, (int, float))
            ):
                raise SystemExit(
                    f"{context} item_heights_percent values must be numbers."
                )

            percent = float(raw_percent)
            if percent <= 0:
                raise SystemExit(
                    f"{context} item_heights_percent values must be "
                    "greater than zero."
                )

            percentages.append(percent)

        total = sum(percentages)
        if abs(total - 100.0) > 1e-6:
            raise SystemExit(
                f"{context} item_heights_percent must total 100; "
                f"got {total:g}."
            )

        item_heights_percent = tuple(percentages)

    explicit_child_heights = any(
        has_axis_allocation(item, "height")
        for item in items
    )

    if explicit_child_heights and item_heights_percent is not None:
        raise SystemExit(
            f"{context} cannot mix explicit child height allocations with "
            "item_heights_percent."
        )

    if explicit_child_heights:
        missing = [
            index
            for index, item in enumerate(items, start=1)
            if not has_axis_allocation(item, "height")
        ]
        if missing:
            raise SystemExit(
                f"{context} uses explicit child height allocation, so every "
                "child must define height; missing item(s): "
                + ", ".join(str(value) for value in missing)
                + "."
            )

        remaining_count = sum(
            1
            for item in items
            if item.height_remaining
        )
        if remaining_count > 1:
            raise SystemExit(
                f'{context} may contain at most one height="remaining" child.'
            )

    def parse_gap_override(key: str) -> int | None:
        raw = value.get(key)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SystemExit(f"{context} {key} must be an integer.")
        if raw < 0:
            raise SystemExit(f"{context} {key} must not be negative.")
        return raw

    width, width_percent, width_remaining = parse_axis_allocation(
        value,
        "width",
        context,
    )
    height, height_percent, height_remaining = parse_axis_allocation(
        value,
        "height",
        context,
    )

    return LayoutColumn(
        items=items,
        item_heights_percent=item_heights_percent,
        overwrite_pad_x=parse_gap_override("overwrite_pad_x"),
        overwrite_pad_y=parse_gap_override("overwrite_pad_y"),
        width=width,
        width_percent=width_percent,
        width_remaining=width_remaining,
        height=height,
        height_percent=height_percent,
        height_remaining=height_remaining,
    )


def parse_layout_item(value: Any, context: str) -> LayoutItem:
    """Parse either a module placement or a vertical col container."""
    if isinstance(value, dict) and "col" in value:
        if "module" in value:
            raise SystemExit(
                f"{context} cannot define both 'module' and 'col'."
            )
        return parse_layout_column(value, context)

    return parse_layout_module(value, context)



def parse_percentages(
    raw: Any,
    expected_count: int,
    context: str,
) -> tuple[float, ...] | None:
    """Parse an optional percentage list that must total exactly 100."""
    if raw is None:
        return None

    if not isinstance(raw, list):
        raise SystemExit(f"{context} must be a list.")

    if len(raw) != expected_count:
        raise SystemExit(
            f"{context} must contain exactly one value per item."
        )

    percentages: list[float] = []

    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SystemExit(f"{context} values must be numbers.")

        percent = float(value)
        if percent <= 0:
            raise SystemExit(
                f"{context} values must be greater than zero."
            )

        percentages.append(percent)

    total = sum(percentages)
    if abs(total - 100.0) > 1e-6:
        raise SystemExit(
            f"{context} must total 100; got {total:g}."
        )

    return tuple(percentages)


def load_layout(layout_cfg: dict[str, Any]) -> list[LayoutBlock]:
    """Load the ordered page layout.

    User-facing page syntax is an ordered list:

        [[page.main.item]]
        type = "row"

        [[page.main.item]]
        type = "col"

    This preserves arbitrary row/col ordering.
    """
    page_cfg = required(layout_cfg, "page", "layout.toml")
    main_cfg = required(page_cfg, "main", "[page.main]")

    if "block" in main_cfg:
        raise SystemExit(
            "[[page.main.block]] has been replaced by [[page.main.item]]. "
            'Set type = "row" or type = "col" on each page item.'
        )

    raw_items = main_cfg.get("item", [])

    if not isinstance(raw_items, list):
        raise SystemExit("[[page.main.item]] entries must form a list.")

    page_items: list[LayoutBlock] = []

    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise SystemExit(f"page.main item {index} must be a TOML table.")

        kind = raw_item.get("type")
        if kind not in {"row", "col"}:
            raise SystemExit(
                f"page.main item {index} type must be \"row\" or \"col\"."
            )

        raw_modules = raw_item.get("modules", [])
        if not isinstance(raw_modules, list) or not raw_modules:
            raise SystemExit(
                f"page.main {kind} {index} must contain a non-empty modules list."
            )

        modules: list[LayoutItem] = []
        for module_index, value in enumerate(raw_modules, start=1):
            modules.append(
                parse_layout_item(
                    value,
                    f"page.main {kind} {index} item {module_index}",
                )
            )

        if kind == "row":
            if modules[-1].overwrite_pad_x is not None:
                raise SystemExit(
                    f"page.main row {index} item {len(modules)} sets "
                    "overwrite_pad_x, but the final item has no horizontal "
                    "gap after it."
                )

            for module_index, item in enumerate(modules, start=1):
                if item.overwrite_pad_y is not None:
                    raise SystemExit(
                        f"page.main row {index} item {module_index} sets "
                        "overwrite_pad_y, but row children have no vertical "
                        "sibling gap."
                    )
        else:
            if modules[-1].overwrite_pad_y is not None:
                raise SystemExit(
                    f"page.main col {index} item {len(modules)} sets "
                    "overwrite_pad_y, but the final item has no vertical "
                    "gap after it."
                )

            for module_index, item in enumerate(modules, start=1):
                if item.overwrite_pad_x is not None:
                    raise SystemExit(
                        f"page.main col {index} item {module_index} sets "
                        "overwrite_pad_x, but col children have no horizontal "
                        "sibling gap."
                    )

        if kind == "row":
            for module_index, item in enumerate(modules, start=1):
                if has_axis_allocation(item, "height"):
                    raise SystemExit(
                        f"page.main row {index} item {module_index} sets a "
                        "height allocation, but row children inherit row height."
                    )
        else:
            for module_index, item in enumerate(modules, start=1):
                if has_axis_allocation(item, "width"):
                    raise SystemExit(
                        f"page.main col {index} item {module_index} sets a "
                        "width allocation, but col children inherit col width."
                    )

        raw_width = raw_item.get("width")
        width: int | None = None

        if raw_width is not None:
            if isinstance(raw_width, bool) or not isinstance(raw_width, int):
                raise SystemExit(
                    f"page.main {kind} {index} width must be an integer."
                )
            if raw_width <= 0:
                raise SystemExit(
                    f"page.main {kind} {index} width must be greater than zero."
                )
            width = raw_width

        raw_height = raw_item.get("height")
        height: int | None = None
        height_remaining = False

        if raw_height is not None:
            if isinstance(raw_height, str):
                if raw_height != "remaining":
                    raise SystemExit(
                        f'page.main {kind} {index} height string value must be "remaining".'
                    )
                height_remaining = True
            else:
                if isinstance(raw_height, bool) or not isinstance(raw_height, int):
                    raise SystemExit(
                        f'page.main {kind} {index} height must be a positive integer or "remaining".'
                    )
                if raw_height <= 0:
                    raise SystemExit(
                        f"page.main {kind} {index} height must be greater than zero."
                    )
                height = raw_height

        if "height_fraction" in raw_item:
            raise SystemExit(
                f"page.main {kind} {index} uses obsolete height_fraction. "
                "Use height_percent instead."
            )

        raw_height_percent = raw_item.get("height_percent")
        height_percent: float | None = None

        if raw_height_percent is not None:
            if isinstance(raw_height_percent, bool) or not isinstance(
                raw_height_percent,
                (int, float),
            ):
                raise SystemExit(
                    f"page.main {kind} {index} height_percent must be a number."
                )

            height_percent = float(raw_height_percent)
            if height_percent <= 0 or height_percent > 100:
                raise SystemExit(
                    f"page.main {kind} {index} height_percent must be "
                    "greater than 0 and at most 100."
                )

        mode_count = (
            int(height is not None)
            + int(height_percent is not None)
            + int(height_remaining)
        )

        if mode_count > 1:
            raise SystemExit(
                f"page.main {kind} {index} may set only one of height=<px>, "
                'height_percent=<percent>, or height="remaining".'
            )

        if mode_count == 0:
            raise SystemExit(
                f"page.main {kind} {index} must set height=<px>, "
                'height_percent=<percent>, or height="remaining".'
            )

        item_widths_percent = parse_percentages(
            raw_item.get("item_widths_percent"),
            len(modules),
            f"page.main row {index} item_widths_percent",
        )
        item_heights_percent = parse_percentages(
            raw_item.get("item_heights_percent"),
            len(modules),
            f"page.main col {index} item_heights_percent",
        )

        if kind == "row" and item_heights_percent is not None:
            raise SystemExit(
                f"page.main row {index} cannot set item_heights_percent; "
                "use item_widths_percent."
            )

        if kind == "col" and item_widths_percent is not None:
            raise SystemExit(
                f"page.main col {index} cannot set item_widths_percent; "
                "use item_heights_percent."
            )

        axis = "width" if kind == "row" else "height"
        explicit_child_axis = any(
            has_axis_allocation(item, axis)
            for item in modules
        )
        legacy_split = (
            item_widths_percent
            if kind == "row"
            else item_heights_percent
        )

        if explicit_child_axis and legacy_split is not None:
            raise SystemExit(
                f"page.main {kind} {index} cannot mix explicit child "
                f"{axis} allocations with "
                f"{'item_widths_percent' if kind == 'row' else 'item_heights_percent'}."
            )

        if explicit_child_axis:
            missing = [
                module_index
                for module_index, item in enumerate(modules, start=1)
                if not has_axis_allocation(item, axis)
            ]
            if missing:
                raise SystemExit(
                    f"page.main {kind} {index} uses explicit child allocation, "
                    f"so every child must define {axis}; missing item(s): "
                    + ", ".join(str(value) for value in missing)
                    + "."
                )

            remaining_count = sum(
                1
                for item in modules
                if getattr(item, f"{axis}_remaining")
            )
            if remaining_count > 1:
                raise SystemExit(
                    f'page.main {kind} {index} may contain at most one {axis}="remaining" child.'
                )

        if "overwrite_pad_x" in raw_item:
            raise SystemExit(
                f"page.main {kind} {index} overwrite_pad_x is not a page-item "
                "property. Put it on the row child that owns the horizontal gap."
            )

        raw_overwrite_pad_y = raw_item.get("overwrite_pad_y")
        overwrite_pad_y: int | None = None

        if raw_overwrite_pad_y is not None:
            if (
                isinstance(raw_overwrite_pad_y, bool)
                or not isinstance(raw_overwrite_pad_y, int)
            ):
                raise SystemExit(
                    f"page.main {kind} {index} overwrite_pad_y must be an integer."
                )
            if raw_overwrite_pad_y < 0:
                raise SystemExit(
                    f"page.main {kind} {index} overwrite_pad_y must not be negative."
                )
            overwrite_pad_y = raw_overwrite_pad_y

        page_items.append(
            LayoutBlock(
                kind=kind,
                modules=tuple(modules),
                width=width,
                height=height,
                height_percent=height_percent,
                height_remaining=height_remaining,
                item_widths_percent=item_widths_percent,
                item_heights_percent=item_heights_percent,
                overwrite_pad_y=overwrite_pad_y,
            )
        )

    percent_total = sum(
        item.height_percent
        for item in page_items
        if item.height_percent is not None
    )
    remaining_count = sum(
        1
        for item in page_items
        if item.height_remaining
    )

    if percent_total > 100.0 + 1e-6:
        raise SystemExit(
            "page.main height_percent values must not exceed 100; "
            f"got {percent_total:g}."
        )

    if remaining_count > 1:
        raise SystemExit(
            'page.main may contain at most one height="remaining" item.'
        )

    return page_items


def load_slot_layouts(
    monitors: list[Monitor],
    layouts_dir: Path,
) -> dict[int, SlotLayout]:
    """Load one independent layout TOML for every active monitor slot."""
    layouts: dict[int, SlotLayout] = {}

    for monitor in monitors:
        if monitor.slot <= 0:
            raise SystemExit(
                f"Monitor '{monitor.name}' does not have a valid slot assignment."
            )

        if monitor.slot in layouts:
            continue

        path = layouts_dir / f"slot-{monitor.slot}.toml"
        config = load_toml(path)
        blocks = tuple(load_layout(config))

        if not blocks:
            raise SystemExit(
                f"Slot {monitor.slot} layout contains no page.main items: {path}"
            )

        layouts[monitor.slot] = SlotLayout(
            slot=monitor.slot,
            path=path,
            config=config,
            blocks=blocks,
        )

    return layouts


def load_slot_manifests(
    slot_layouts: dict[int, SlotLayout],
    modules_dir: Path,
) -> dict[str, ModuleManifest]:
    """Load the union of modules referenced by all active slot layouts."""
    manifests: dict[str, ModuleManifest] = {}

    for slot in sorted(slot_layouts):
        slot_manifests = load_enabled_modules(
            list(slot_layouts[slot].blocks),
            modules_dir,
        )

        for name, manifest in slot_manifests.items():
            existing = manifests.get(name)

            if existing is not None and existing.directory != manifest.directory:
                raise SystemExit(
                    f"Module '{name}' resolves to conflicting directories "
                    f"across slot layouts."
                )

            manifests[name] = manifest

    return manifests


def load_module_manifest(modules_dir: Path, name: str) -> ModuleManifest:
    module_dir = modules_dir / name
    manifest_path = module_dir / "module.toml"
    cfg = load_toml(manifest_path)

    module_cfg = required(cfg, "module", str(manifest_path))
    manifest_name = str(required(module_cfg, "name", f"{manifest_path} [module]"))
    version = int(required(module_cfg, "version", f"{manifest_path} [module]"))

    if manifest_name != name:
        raise SystemExit(
            f"Module directory '{name}' has manifest name '{manifest_name}'. "
            "They must match."
        )

    if version != SUPPORTED_MODULE_CONTRACT:
        raise SystemExit(
            f"Module '{name}' uses contract version {version}; "
            f"supported version is {SUPPORTED_MODULE_CONTRACT}."
        )

    style_path: Path | None = None
    style_value = module_cfg.get("style")
    if style_value is not None:
        style_path = module_dir / str(style_value)
        if not style_path.is_file():
            raise SystemExit(
                f"Module '{name}' declares style '{style_value}', "
                f"but {style_path} does not exist."
            )

    has_waybar = "waybar" in cfg
    has_generator = "generator" in cfg

    if has_waybar == has_generator:
        raise SystemExit(
            f"Module '{name}' must define exactly one of [waybar] or [generator]."
        )

    if has_generator:
        generator_cfg = required(cfg, "generator", str(manifest_path))
        entrypoint = str(
            required(generator_cfg, "entrypoint", f"{manifest_path} [generator]")
        )
        generator_path = module_dir / entrypoint

        if not generator_path.is_file():
            raise SystemExit(
                f"Module '{name}' declares generator '{entrypoint}', "
                f"but {generator_path} does not exist."
            )

        child_parameter_raw = generator_cfg.get("child_parameter")
        child_parameter: str | None = None
        if child_parameter_raw is not None:
            if (
                not isinstance(child_parameter_raw, str)
                or not child_parameter_raw.strip()
            ):
                raise SystemExit(
                    f"Module '{name}' generator child_parameter must be "
                    "a non-empty string."
                )
            child_parameter = child_parameter_raw.strip()

        return ModuleManifest(
            name=name,
            directory=module_dir,
            root=None,
            entries={},
            style=style_path,
            generator=generator_path,
            child_parameter=child_parameter,
        )

    waybar_cfg = required(cfg, "waybar", str(manifest_path))
    root = str(required(waybar_cfg, "root", f"{manifest_path} [waybar]"))
    entries = required(waybar_cfg, "entries", f"{manifest_path} [waybar]")

    if not isinstance(entries, dict) or not entries:
        raise SystemExit(f"Module '{name}' must define at least one Waybar entry.")

    normalized_entries: dict[str, dict[str, Any]] = {}
    for entry_name, options in entries.items():
        if not isinstance(entry_name, str) or not entry_name:
            raise SystemExit(f"Module '{name}' contains an invalid Waybar entry name.")
        if not isinstance(options, dict):
            raise SystemExit(
                f"Waybar entry '{entry_name}' in module '{name}' must be a table."
            )
        normalized_entries[entry_name] = options

    if root not in normalized_entries:
        raise SystemExit(
            f"Module '{name}' exposes root '{root}', "
            "but that entry is not defined in [waybar.entries]."
        )

    return ModuleManifest(
        name=name,
        directory=module_dir,
        root=root,
        entries=normalized_entries,
        style=style_path,
        generator=None,
    )

def load_enabled_modules(
    blocks: list[LayoutBlock],
    modules_dir: Path,
) -> dict[str, ModuleManifest]:
    """Load modules referenced by rows, cols, and wrapper children."""
    manifests: dict[str, ModuleManifest] = {}

    def register_module(module: LayoutModule, context: str) -> None:
        manifest = manifests.get(module.name)
        if manifest is None:
            manifest = load_module_manifest(modules_dir, module.name)
            manifests[module.name] = manifest

        if manifest.child_parameter is None:
            return

        parameters = module.parameters or {}
        child_raw = parameters.get(manifest.child_parameter)
        if child_raw is None:
            raise SystemExit(
                f"{context} module '{module.name}' requires child parameter "
                f"'{manifest.child_parameter}'."
            )

        child = parse_layout_module(
            child_raw,
            f"{context} module '{module.name}' child",
        )
        if child.overwrite_pad_x is not None or child.overwrite_pad_y is not None:
            raise SystemExit(
                f"{context} module '{module.name}' child cannot set "
                "overwrite_pad_x/overwrite_pad_y because a single wrapped "
                "child has no sibling gap."
            )

        if has_axis_allocation(child, "width") or has_axis_allocation(child, "height"):
            raise SystemExit(
                f"{context} module '{module.name}' child cannot set width/height "
                "allocation because the wrapper defines the child rectangle."
            )

        register_module(child, f"{context} module '{module.name}' child")

    def register_item(item: LayoutItem, context: str) -> None:
        if isinstance(item, LayoutColumn):
            for index, child in enumerate(item.items, start=1):
                register_item(child, f"{context} col item {index}")
            return

        register_module(item, context)

    for block_index, block in enumerate(blocks, start=1):
        for item_index, item in enumerate(block.modules, start=1):
            register_item(
                item,
                f"page.main block {block_index} item {item_index}",
            )

    return manifests

def expand_string(value: str, context: dict[str, str]) -> str:
    result = value
    for key, replacement in context.items():
        result = result.replace("{" + key + "}", replacement)
    return result


def expand_templates(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return expand_string(value, context)

    if isinstance(value, list):
        return [expand_templates(item, context) for item in value]

    if isinstance(value, dict):
        return {
            key: expand_templates(item, context)
            for key, item in value.items()
        }

    return value


def module_context(
    project_root: Path,
    manifest: ModuleManifest,
    monitor: Monitor,
    geometry: BarGeometry,
) -> dict[str, str]:
    return {
        "project_root": str(project_root),
        "module_dir": str(manifest.directory),
        "monitor": monitor.name,
        "monitor_css": css_safe(monitor.name),
        "monitor_slot": str(monitor.slot),
        "logical_width": str(monitor.logical_width),
        "logical_height": str(monitor.logical_height),
        "bar_width": str(geometry.width),
        "font_size": f"{geometry.font_size:.2f}",
        "python": sys.executable,
    }


def calculate_block_width(
    draw_geometry: DrawGeometry,
    block: LayoutBlock,
) -> int:
    if block.width is None:
        return draw_geometry.width

    if block.width > draw_geometry.width:
        raise SystemExit(
            f"Configured block width {block.width}px exceeds "
            f"draw width {draw_geometry.width}px."
        )

    return block.width



def resolve_explicit_axis(
    total_size: int,
    gaps: tuple[int, ...],
    items: tuple[LayoutItem, ...],
    axis: str,
    context: str,
) -> tuple[tuple[int, ...], int]:
    """Resolve explicit px / percent / remaining allocations for one axis."""
    gap_total = sum(gaps)
    available = total_size - gap_total

    if available <= 0:
        raise SystemExit(
            f"{context} has no usable {axis} after padding: "
            f"total={total_size}px, padding={gap_total}px."
        )

    fixed_total = sum(
        getattr(item, axis)
        for item in items
        if getattr(item, axis) is not None
    )
    flexible_pool = available - fixed_total

    if flexible_pool < 0:
        raise SystemExit(
            f"{context} fixed {axis} allocations exceed available space: "
            f"available={available}px, fixed={fixed_total}px."
        )

    percent_indexes = [
        index
        for index, item in enumerate(items)
        if getattr(item, f"{axis}_percent") is not None
    ]
    remaining_indexes = [
        index
        for index, item in enumerate(items)
        if getattr(item, f"{axis}_remaining")
    ]

    if len(remaining_indexes) > 1:
        raise SystemExit(
            f'{context} may contain at most one {axis}="remaining" item.'
        )

    percent_total = sum(
        getattr(items[index], f"{axis}_percent")
        for index in percent_indexes
    )

    if percent_total > 100.0 + 1e-6:
        raise SystemExit(
            f"{context} {axis}_percent values must not exceed 100; "
            f"got {percent_total:g}."
        )

    sizes = [
        getattr(item, axis) if getattr(item, axis) is not None else 0
        for item in items
    ]

    used_percent = 0

    for percent_position, item_index in enumerate(percent_indexes):
        percent = getattr(items[item_index], f"{axis}_percent")
        if percent is None:
            raise SystemExit("Internal allocation error.")

        closes_pool = (
            not remaining_indexes
            and abs(percent_total - 100.0) <= 1e-6
            and percent_position == len(percent_indexes) - 1
        )

        if closes_pool:
            size = flexible_pool - used_percent
        else:
            size = round(flexible_pool * percent / 100.0)

        sizes[item_index] = size
        used_percent += size

    if remaining_indexes:
        remaining_index = remaining_indexes[0]
        sizes[remaining_index] = flexible_pool - used_percent

    for index, size in enumerate(sizes, start=1):
        if size <= 0:
            raise SystemExit(
                f"{context} item {index} resolved to {size}px on the {axis} axis; "
                "every explicitly allocated item must receive at least 1px."
            )

    used = sum(sizes) + gap_total
    leftover = total_size - used

    if leftover < 0:
        raise SystemExit(
            f"Internal {context} allocation error: resolved items exceed "
            f"the inherited {axis}."
        )

    if remaining_indexes and leftover != 0:
        raise SystemExit(
            f"Internal {context} allocation error: "
            f'{axis}="remaining" did not consume all remaining space.'
        )

    return tuple(sizes), leftover


def calculate_item_widths(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
) -> tuple[int, ...]:
    if block.kind != "row":
        raise SystemExit("Internal error: horizontal widths requested for a col.")

    block_width = calculate_block_width(draw_geometry, block)
    count = len(block.modules)
    item_pad_xs = calculate_item_pad_xs(draw_geometry, layout_cfg, block)
    total_pad_x = sum(item_pad_xs)

    if total_pad_x + count > block_width:
        raise SystemExit(
            "Horizontal layout has no usable item width: "
            f"block-width={block_width}px, modules={count}, "
            f"pad-x-total={total_pad_x}px, pad-x={list(item_pad_xs)}."
        )

    explicit = any(
        has_axis_allocation(item, "width")
        for item in block.modules
    )

    if explicit:
        widths, _ = resolve_explicit_axis(
            block_width,
            item_pad_xs,
            block.modules,
            "width",
            "Horizontal row layout",
        )
        return widths

    available_width = block_width - total_pad_x

    if count == 1:
        return (available_width,)

    if block.item_widths_percent is None:
        base = available_width // count
        remainder = available_width % count
        widths = tuple(
            base + (1 if index < remainder else 0)
            for index in range(count)
        )
    else:
        resolved: list[int] = []
        used = 0

        for index, percent in enumerate(block.item_widths_percent):
            if index == count - 1:
                width = available_width - used
            else:
                width = round(available_width * percent / 100.0)
                used += width

            resolved.append(width)

        widths = tuple(resolved)

    for index, width in enumerate(widths, start=1):
        if width <= 0:
            raise SystemExit(
                f"Horizontal layout item {index} resolved to {width}px; "
                "every module must receive at least 1px."
            )

    if sum(widths) + total_pad_x != block_width:
        raise SystemExit(
            "Internal horizontal layout error: item widths and pad_x do not "
            "exactly fill the inherited block width."
        )

    return widths

def calculate_col_item_pad_ys(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    col: LayoutColumn,
) -> tuple[int, ...]:
    """Return each vertical gap after a col item; the final item has no gap."""
    count = len(col.items)
    if count <= 1:
        return ()

    inherited_pad_y = calculate_layout_pad_y(draw_geometry, layout_cfg)
    return tuple(
        (
            item.overwrite_pad_y
            if item.overwrite_pad_y is not None
            else inherited_pad_y
        )
        for item in col.items[:-1]
    )


def calculate_col_item_heights(
    allocated_height: int,
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    col: LayoutColumn,
    context: str,
) -> tuple[int, ...]:
    """Split a col's allocated height after reserving all vertical gaps."""
    count = len(col.items)
    item_pad_ys = calculate_col_item_pad_ys(
        draw_geometry,
        layout_cfg,
        col,
    )
    total_pad_y = sum(item_pad_ys)

    if total_pad_y + count > allocated_height:
        raise SystemExit(
            f"Vertical col layout has no usable item height in {context}: "
            f"height={allocated_height}px, items={count}, "
            f"pad-y-total={total_pad_y}px, pad-y={list(item_pad_ys)}."
        )

    explicit = any(
        has_axis_allocation(item, "height")
        for item in col.items
    )

    if explicit:
        heights, _ = resolve_explicit_axis(
            allocated_height,
            item_pad_ys,
            col.items,
            "height",
            f"Vertical col layout in {context}",
        )
        return heights

    available_height = allocated_height - total_pad_y

    if count == 1:
        return (available_height,)

    if col.item_heights_percent is None:
        base = available_height // count
        remainder = available_height % count
        heights = tuple(
            base + (1 if index < remainder else 0)
            for index in range(count)
        )
    else:
        resolved: list[int] = []
        used = 0

        for index, percent in enumerate(col.item_heights_percent):
            if index == count - 1:
                height = available_height - used
            else:
                height = round(available_height * percent / 100.0)
                used += height

            resolved.append(height)

        heights = tuple(resolved)

    for index, height in enumerate(heights, start=1):
        if height <= 0:
            raise SystemExit(
                f"Vertical col item {index} in {context} resolved to "
                f"{height}px; every item must receive at least 1px."
            )

    if sum(heights) + total_pad_y != allocated_height:
        raise SystemExit(
            f"Internal vertical col layout error in {context}: item heights "
            "and pad_y do not exactly fill the inherited height."
        )

    return heights

def block_as_column(block: LayoutBlock) -> LayoutColumn:
    if block.kind != "col":
        raise SystemExit("Internal error: row cannot be converted to a col.")

    return LayoutColumn(
        items=block.modules,
        item_heights_percent=block.item_heights_percent,
    )


def calculate_top_col_item_heights(
    allocated_height: int,
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
    context: str,
) -> tuple[int, ...]:
    return calculate_col_item_heights(
        allocated_height,
        draw_geometry,
        layout_cfg,
        block_as_column(block),
        context,
    )


def calculate_top_col_item_pad_ys(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
) -> tuple[int, ...]:
    return calculate_col_item_pad_ys(
        draw_geometry,
        layout_cfg,
        block_as_column(block),
    )


def resolve_page_heights(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
    context: str,
) -> tuple[tuple[int, ...], int]:
    """Resolve top-level page heights using px, percent, and remaining."""
    padding_height = sum(
        calculate_block_pad_y(
            draw_geometry,
            layout_cfg,
            block,
            is_last=index == len(blocks) - 1,
        )
        for index, block in enumerate(blocks)
    )

    available = draw_geometry.height - padding_height

    if available <= 0:
        raise SystemExit(
            f"Layout has no usable height on {context}:\n"
            f"  available: {draw_geometry.height}px\n"
            f"  padding:   {padding_height}px"
        )

    fixed_height = sum(
        block.height
        for block in blocks
        if block.height is not None
    )
    flexible_pool = available - fixed_height

    if flexible_pool < 0:
        raise SystemExit(
            f"Layout exceeds available height on {context}:\n"
            f"  available: {draw_geometry.height}px\n"
            f"  fixed:     {fixed_height}px\n"
            f"  padding:   {padding_height}px\n"
            f"  overflow:  {-flexible_pool}px"
        )

    percent_indexes = [
        index
        for index, block in enumerate(blocks)
        if block.height_percent is not None
    ]
    remaining_indexes = [
        index
        for index, block in enumerate(blocks)
        if block.height_remaining
    ]

    if len(remaining_indexes) > 1:
        raise SystemExit(
            f'{context} may contain at most one height="remaining" page item.'
        )

    percent_total = sum(
        blocks[index].height_percent
        for index in percent_indexes
        if blocks[index].height_percent is not None
    )

    if percent_total > 100.0 + 1e-6:
        raise SystemExit(
            f"{context} height_percent values must not exceed 100; "
            f"got {percent_total:g}."
        )

    heights: list[int] = [
        block.height if block.height is not None else 0
        for block in blocks
    ]

    used_percent = 0

    for percent_position, block_index in enumerate(percent_indexes):
        percent = blocks[block_index].height_percent
        if percent is None:
            raise SystemExit("Internal height allocation error.")

        closes_pool = (
            not remaining_indexes
            and abs(percent_total - 100.0) <= 1e-6
            and percent_position == len(percent_indexes) - 1
        )

        if closes_pool:
            height = flexible_pool - used_percent
        else:
            height = round(flexible_pool * percent / 100.0)

        heights[block_index] = height
        used_percent += height

    if remaining_indexes:
        remaining_index = remaining_indexes[0]
        heights[remaining_index] = flexible_pool - used_percent

    for index, height in enumerate(heights, start=1):
        if height <= 0:
            raise SystemExit(
                f"page.main item {index} resolves to {height}px on {context}; "
                "every page item must receive at least 1px."
            )

    used_height = sum(heights) + padding_height
    remaining_height = draw_geometry.height - used_height

    if remaining_height < 0:
        raise SystemExit(
            f"Internal page height allocation error on {context}: "
            "resolved items exceed the draw height."
        )

    if remaining_indexes and remaining_height != 0:
        raise SystemExit(
            f"Internal page height allocation error on {context}: "
            'height="remaining" did not consume all remaining height.'
        )

    return tuple(heights), remaining_height

def validate_vertical_layout(
    monitor: Monitor,
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
) -> int:
    """Validate and resolve the complete vertical page allocation."""
    _, remaining = resolve_page_heights(
        draw_geometry,
        layout_cfg,
        blocks,
        f"monitor '{monitor.name}'",
    )
    return remaining


def generator_context(
    project_root: Path,
    manifest: ModuleManifest,
    instance: str | None,
    placement_parameters: dict[str, Any],
    monitor: Monitor,
    geometry: BarGeometry,
    waybar_cfg: dict[str, Any],
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
    block_index: int,
    item_index: int,
    placement_id: str,
    allocated_width: int,
    allocated_height: int,
    nested: bool = False,
) -> dict[str, Any]:
    bar_cfg = required(waybar_cfg, "bar", "waybar.toml")
    spacing_cfg = required(waybar_cfg, "spacing", "waybar.toml")
    font_cfg = required(waybar_cfg, "font", "waybar.toml")
    theme_cfg = required(waybar_cfg, "theme", "waybar.toml")

    return {
        "contract_version": SUPPORTED_MODULE_CONTRACT,
        "project_root": str(project_root),
        "module_dir": str(manifest.directory),
        "module": {
            "name": manifest.name,
            "instance": instance,
        },
        "placement": {
            "id": placement_id,
            "page": "main",
            "page_item": block_index,
            "container": block.kind,
            "item": item_index + 1,
            "block": block_index,
            "parameters": placement_parameters,
        },
        "monitor": {
            "name": monitor.name,
            "css": css_safe(monitor.name),
            "slot": monitor.slot,
            "logical_width": monitor.logical_width,
            "logical_height": monitor.logical_height,
            "scale": monitor.scale,
        },
        "bar": {
            "width": geometry.width,
            "inner_width": geometry.inner_width,
            "inner_height": geometry.inner_height,
            "position": str(bar_cfg.get("position", "right")),
            "border_width": geometry.border_width,
            "border_radius": read_hyprland_int_option("decoration:rounding"),
            "border_color": str(required(theme_cfg, "border", "[theme]")),
        },
        "layout": {
            "page": "main",
            "page_item": block_index,
            "container": block.kind,
            "block": block_index,
            "width": allocated_width,
            "height": allocated_height,
            "height_pixels": allocated_height,
            "height_percent": None if nested else block.height_percent,
            "height_remaining": False if nested else block.height_remaining,
        },
        "spacing": {
            "edge": int(spacing_cfg.get("edge", 0)),
            "row": int(spacing_cfg.get("row", 0)),
            "column": int(spacing_cfg.get("column", 0)),
        },
        "font": {
            "family": str(font_cfg.get("family", "monospace")),
            "size": geometry.font_size,
        },
    }


def render_generated_module(
    project_root: Path,
    manifest: ModuleManifest,
    instance: str | None,
    placement_parameters: dict[str, Any],
    monitor: Monitor,
    geometry: BarGeometry,
    waybar_cfg: dict[str, Any],
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
    block_index: int,
    item_index: int,
    placement_id: str,
    allocated_width: int,
    allocated_height: int,
    nested: bool = False,
) -> tuple[
    str,
    dict[str, dict[str, Any]],
    str,
    int | None,
    str | None,
]:
    if manifest.generator is None:
        raise SystemExit(
            f"Internal error: module '{manifest.name}' has no generator."
        )

    context = generator_context(
        project_root,
        manifest,
        instance,
        placement_parameters,
        monitor,
        geometry,
        waybar_cfg,
        layout_cfg,
        block,
        block_index,
        item_index,
        placement_id,
        allocated_width,
        allocated_height,
        nested,
    )

    try:
        result = subprocess.run(
            [sys.executable, str(manifest.generator)],
            input=json.dumps(context),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()
        message = (
            f"Generator for module '{manifest.name}' failed on "
            f"monitor '{monitor.name}'"
        )
        if detail:
            message += f": {detail}"
        raise SystemExit(message)

    try:
        rendered = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Generator for module '{manifest.name}' returned invalid JSON "
            f"on monitor '{monitor.name}': {exc}"
        )

    if not isinstance(rendered, dict):
        raise SystemExit(
            f"Generator for module '{manifest.name}' must return a JSON object."
        )

    root = rendered.get("root")
    entries = rendered.get("entries")
    css = rendered.get("css", "")
    container = rendered.get("container")

    if not isinstance(root, str) or not root:
        raise SystemExit(
            f"Generator for module '{manifest.name}' returned an invalid root."
        )

    if not isinstance(entries, dict) or not entries:
        raise SystemExit(
            f"Generator for module '{manifest.name}' returned no Waybar entries."
        )

    normalized_entries: dict[str, dict[str, Any]] = {}
    for entry_name, options in entries.items():
        if not isinstance(entry_name, str) or not entry_name:
            raise SystemExit(
                f"Generator for module '{manifest.name}' returned "
                "an invalid Waybar entry name."
            )
        if not isinstance(options, dict):
            raise SystemExit(
                f"Generated Waybar entry '{entry_name}' in module "
                f"'{manifest.name}' must be an object."
            )
        normalized_entries[entry_name] = options

    if root not in normalized_entries:
        raise SystemExit(
            f"Generator for module '{manifest.name}' exposes root '{root}', "
            "but that entry is not present in its generated entries."
        )

    if not isinstance(css, str):
        raise SystemExit(
            f"Generator for module '{manifest.name}' returned non-string CSS."
        )

    child_inset: int | None = None
    forwarded_click: str | None = None

    if manifest.child_parameter is None:
        if container is not None:
            raise SystemExit(
                f"Generator for module '{manifest.name}' returned container "
                "metadata but its manifest declares no child_parameter."
            )
    else:
        if not isinstance(container, dict):
            raise SystemExit(
                f"Generator for wrapper module '{manifest.name}' must return "
                "a container object."
            )

        raw_inset = container.get("inset")
        if isinstance(raw_inset, bool) or not isinstance(raw_inset, int):
            raise SystemExit(
                f"Generator for wrapper module '{manifest.name}' returned "
                "an invalid container inset."
            )
        if raw_inset < 0:
            raise SystemExit(
                f"Generator for wrapper module '{manifest.name}' returned "
                "a negative container inset."
            )
        child_inset = raw_inset

        raw_click = container.get("on_click")
        if raw_click is not None:
            if not isinstance(raw_click, str) or not raw_click:
                raise SystemExit(
                    f"Generator for wrapper module '{manifest.name}' returned "
                    "an invalid container on_click path."
                )
            forwarded_click = raw_click

    return root, normalized_entries, css, child_inset, forwarded_click


def add_entry(
    bar: dict[str, Any],
    entry_name: str,
    options: dict[str, Any],
    owner: str,
    owners: dict[str, str],
) -> None:
    if entry_name in owners:
        raise SystemExit(
            f"Waybar entry collision: '{entry_name}' is provided by both "
            f"'{owners[entry_name]}' and '{owner}'."
        )

    if entry_name in bar:
        raise SystemExit(
            f"Waybar entry '{entry_name}' from module '{owner}' "
            "collides with a core bar key."
        )

    owners[entry_name] = owner
    bar[entry_name] = options


def build_layout_for_monitor(
    bar: dict[str, Any],
    project_root: Path,
    blocks: list[LayoutBlock],
    manifests: dict[str, ModuleManifest],
    monitor: Monitor,
    geometry: BarGeometry,
    waybar_cfg: dict[str, Any],
    layout_cfg: dict[str, Any],
    app_commands: dict[str, str],
) -> list[tuple[str, str]]:
    owners: dict[str, str] = {}
    generated_css: list[tuple[str, str]] = []
    frame_modules: list[str] = []

    draw_geometry = calculate_draw_geometry(geometry, layout_cfg)
    page_heights, _ = resolve_page_heights(
        draw_geometry,
        layout_cfg,
        blocks,
        f"monitor '{monitor.name}'",
    )

    def render_placement(
        module_ref: LayoutModule,
        block: LayoutBlock,
        block_index: int,
        item_index: int,
        placement_id: str,
        allocated_width: int,
        allocated_height: int,
        nested: bool = False,
        inherited_click: str | None = None,
    ) -> str:
        manifest = manifests[module_ref.name]
        owner = f"{module_ref.reference}@{placement_id}"

        if manifest.generator is not None:
            placement_parameters = resolve_placement_app_aliases(
                module_ref.parameters or {},
                app_commands,
                owner,
            )

            root, entries, css, child_inset, wrapper_click = render_generated_module(
                project_root,
                manifest,
                module_ref.instance,
                placement_parameters,
                monitor,
                geometry,
                waybar_cfg,
                layout_cfg,
                block,
                block_index,
                item_index,
                placement_id,
                allocated_width,
                allocated_height,
                nested,
            )
            if css.strip():
                generated_css.append((owner, css.rstrip()))
        else:
            if module_ref.instance is not None:
                raise SystemExit(
                    f"Static module '{manifest.name}' cannot currently be "
                    f"instantiated as '{module_ref.reference}'. Use a generated "
                    "module for multiple instances."
                )

            if module_ref.parameters:
                raise SystemExit(
                    f"Static module '{manifest.name}' does not accept placement "
                    "parameters."
                )

            if manifest.root is None:
                raise SystemExit(
                    f"Internal error: static module '{manifest.name}' has no root."
                )

            context = module_context(
                project_root,
                manifest,
                monitor,
                geometry,
            )
            root = manifest.root
            entries = {
                entry_name: expand_templates(raw_options, context)
                for entry_name, raw_options in manifest.entries.items()
            }
            child_inset = None
            wrapper_click = None

        click_for_leaf = inherited_click

        if manifest.child_parameter is not None:
            if child_inset is None:
                raise SystemExit(
                    f"Wrapper module '{manifest.name}' did not provide a child inset."
                )

            parameters = module_ref.parameters or {}
            child_raw = parameters.get(manifest.child_parameter)
            if child_raw is None:
                raise SystemExit(
                    f"Wrapper module '{manifest.name}' requires child parameter "
                    f"'{manifest.child_parameter}'."
                )

            child_ref = parse_layout_module(
                child_raw,
                f"wrapper '{module_ref.reference}' child",
            )
            if (
                child_ref.overwrite_pad_x is not None
                or child_ref.overwrite_pad_y is not None
            ):
                raise SystemExit(
                    f"Wrapper module '{manifest.name}' child cannot set "
                    "overwrite_pad_x/overwrite_pad_y because it has no sibling."
                )

            if (
                has_axis_allocation(child_ref, "width")
                or has_axis_allocation(child_ref, "height")
            ):
                raise SystemExit(
                    f"Wrapper module '{manifest.name}' child cannot set "
                    "width/height allocation because the wrapper defines the "
                    "child rectangle."
                )

            child_width = allocated_width - (2 * child_inset)
            child_height = allocated_height - (2 * child_inset)
            if child_width <= 0 or child_height <= 0:
                raise SystemExit(
                    f"Wrapper module '{manifest.name}' has no usable child area "
                    f"on monitor '{monitor.name}':\n"
                    f"  allocated: {allocated_width}x{allocated_height}px\n"
                    f"  inset:     {child_inset}px\n"
                    f"  child:     {child_width}x{child_height}px"
                )

            next_click = wrapper_click if wrapper_click is not None else inherited_click
            child_root = render_placement(
                child_ref,
                block,
                block_index,
                item_index,
                f"{placement_id}-c1",
                child_width,
                child_height,
                True,
                next_click,
            )

            root_options = entries[root]
            root_options["modules"] = [child_root]
        elif click_for_leaf is not None:
            root_options = entries[root]
            existing_click = root_options.get("on-click")
            if existing_click is not None and existing_click != click_for_leaf:
                raise SystemExit(
                    f"Wrapper click for '{owner}' conflicts with the child "
                    "module's own on-click action."
                )
            root_options["on-click"] = click_for_leaf

        for entry_name, options in entries.items():
            add_entry(
                bar,
                entry_name,
                options,
                owner,
                owners,
            )

        return root

    def render_layout_item(
        item: LayoutItem,
        block: LayoutBlock,
        block_index: int,
        item_index: int,
        placement_id: str,
        allocated_width: int,
        allocated_height: int,
        nested: bool = False,
    ) -> str:
        if isinstance(item, LayoutModule):
            return render_placement(
                item,
                block,
                block_index,
                item_index,
                placement_id,
                allocated_width,
                allocated_height,
                nested,
            )

        col = item
        child_heights = calculate_col_item_heights(
            allocated_height,
            draw_geometry,
            layout_cfg,
            col,
            placement_id,
        )
        child_pad_ys = calculate_col_item_pad_ys(
            draw_geometry,
            layout_cfg,
            col,
        )

        col_modules: list[str] = []
        col_css: list[str] = []
        selector = f"window#waybar.sidebar-{css_safe(monitor.name)}"
        group_name = f"group/layout-col-{placement_id}"

        col_css.extend(
            [
                f"{selector} #layout-col-{placement_id} {{",
                f"    min-width: {allocated_width}px;",
                f"    min-height: {allocated_height}px;",
                "    padding: 0;",
                "    margin: 0;",
                "}",
                "",
            ]
        )

        for child_index, child in enumerate(col.items):
            child_id = f"{placement_id}-v{child_index + 1}"
            child_root = render_layout_item(
                child,
                block,
                block_index,
                item_index,
                child_id,
                allocated_width,
                child_heights[child_index],
                True,
            )
            col_modules.append(child_root)

            if child_index < len(col.items) - 1:
                pad_y = child_pad_ys[child_index]
                if pad_y > 0:
                    spacer_name = (
                        f"custom/layout-col-pad-y-"
                        f"{placement_id}-{child_index + 1}"
                    )
                    add_entry(
                        bar,
                        spacer_name,
                        {
                            "format": " ",
                            "tooltip": False,
                        },
                        "core-layout",
                        owners,
                    )
                    col_modules.append(spacer_name)

                    col_css.extend(
                        [
                            f"{selector} #custom-layout-col-pad-y-"
                            f"{placement_id}-{child_index + 1} {{",
                            f"    min-height: {pad_y}px;",
                            "    min-width: 1px;",
                            "    padding: 0;",
                            "    margin: 0;",
                            "    font-size: 1px;",
                            "}",
                            "",
                        ]
                    )

        add_entry(
            bar,
            group_name,
            {
                "orientation": "vertical",
                "modules": col_modules,
            },
            "core-layout",
            owners,
        )

        generated_css.append(
            (
                f"core-layout-col@{placement_id}",
                "\n".join(col_css).rstrip(),
            )
        )
        return group_name

    for block_index, block in enumerate(blocks, start=1):
        block_width = calculate_block_width(draw_geometry, block)
        block_height = page_heights[block_index - 1]

        container_modules: list[str] = []
        selector = f"window#waybar.sidebar-{css_safe(monitor.name)}"

        if block.kind == "row":
            item_widths = calculate_item_widths(
                draw_geometry,
                layout_cfg,
                block,
            )
            item_pad_xs = calculate_item_pad_xs(
                draw_geometry,
                layout_cfg,
                block,
            )

            for item_index, layout_item in enumerate(block.modules):
                allocated_width = item_widths[item_index]
                placement_id = f"main-r{block_index}-i{item_index + 1}"
                root = render_layout_item(
                    layout_item,
                    block,
                    block_index,
                    item_index,
                    placement_id,
                    allocated_width,
                    block_height,
                )
                container_modules.append(root)

                if item_index < len(block.modules) - 1:
                    pad_x = item_pad_xs[item_index]
                    if pad_x > 0:
                        spacer_name = (
                            f"custom/layout-row-pad-x-"
                            f"{block_index}-{item_index + 1}"
                        )
                        add_entry(
                            bar,
                            spacer_name,
                            {
                                "format": " ",
                                "tooltip": False,
                            },
                            "core-layout",
                            owners,
                        )
                        container_modules.append(spacer_name)

                        generated_css.append(
                            (
                                f"core-layout-row-gap@main-r{block_index}-"
                                f"{item_index + 1}",
                                "\n".join(
                                    [
                                        f"{selector} #custom-layout-row-pad-x-"
                                        f"{block_index}-{item_index + 1} {{",
                                        f"    min-width: {pad_x}px;",
                                        "    min-height: 1px;",
                                        "    padding: 0;",
                                        "    margin: 0;",
                                        "    font-size: 1px;",
                                        "}",
                                    ]
                                ),
                            )
                        )

            group_name = f"group/layout-main-row-{block_index}"
            orientation = "horizontal"

        else:
            item_heights = calculate_top_col_item_heights(
                block_height,
                draw_geometry,
                layout_cfg,
                block,
                f"page.main col {block_index}",
            )
            item_pad_ys = calculate_top_col_item_pad_ys(
                draw_geometry,
                layout_cfg,
                block,
            )

            for item_index, layout_item in enumerate(block.modules):
                allocated_height = item_heights[item_index]
                placement_id = f"main-c{block_index}-i{item_index + 1}"
                root = render_layout_item(
                    layout_item,
                    block,
                    block_index,
                    item_index,
                    placement_id,
                    block_width,
                    allocated_height,
                )
                container_modules.append(root)

                if item_index < len(block.modules) - 1:
                    pad_y = item_pad_ys[item_index]
                    if pad_y > 0:
                        spacer_name = (
                            f"custom/layout-col-pad-y-main-"
                            f"{block_index}-{item_index + 1}"
                        )
                        add_entry(
                            bar,
                            spacer_name,
                            {
                                "format": " ",
                                "tooltip": False,
                            },
                            "core-layout",
                            owners,
                        )
                        container_modules.append(spacer_name)

                        generated_css.append(
                            (
                                f"core-layout-col-gap@main-c{block_index}-"
                                f"{item_index + 1}",
                                "\n".join(
                                    [
                                        f"{selector} #custom-layout-col-pad-y-main-"
                                        f"{block_index}-{item_index + 1} {{",
                                        f"    min-height: {pad_y}px;",
                                        "    min-width: 1px;",
                                        "    padding: 0;",
                                        "    margin: 0;",
                                        "    font-size: 1px;",
                                        "}",
                                    ]
                                ),
                            )
                        )

            group_name = f"group/layout-main-col-{block_index}"
            orientation = "vertical"

        add_entry(
            bar,
            group_name,
            {
                "orientation": orientation,
                "modules": container_modules,
            },
            "core-layout",
            owners,
        )
        frame_modules.append(group_name)

        generated_css.append(
            (
                f"core-layout-{block.kind}@main-{block_index}",
                "\n".join(
                    [
                        f"{selector} #layout-main-{block.kind}-"
                        f"{block_index} {{",
                        f"    min-width: {block_width}px;",
                        f"    min-height: {block_height}px;",
                        "    padding: 0;",
                        "    margin: 0;",
                        "}",
                    ]
                ),
            )
        )

        block_pad_y = calculate_block_pad_y(
            draw_geometry,
            layout_cfg,
            block,
            is_last=block_index == len(blocks),
        )

        if block_pad_y > 0:
            spacer_name = f"custom/layout-page-pad-y-{block_index}"
            add_entry(
                bar,
                spacer_name,
                {
                    "format": " ",
                    "tooltip": False,
                },
                "core-layout",
                owners,
            )
            frame_modules.append(spacer_name)

            generated_css.append(
                (
                    f"core-layout-page-gap@{block_index}",
                    "\n".join(
                        [
                            f"{selector} #custom-layout-page-pad-y-"
                            f"{block_index} {{",
                            f"    min-height: {block_pad_y}px;",
                            "    min-width: 1px;",
                            "    padding: 0;",
                            "    margin: 0;",
                            "    font-size: 1px;",
                            "}",
                        ]
                    ),
                )
            )

    frame_name = "group/sidebar-inner-frame"
    add_entry(
        bar,
        frame_name,
        {
            "orientation": "vertical",
            "expand": True,
            "modules": frame_modules,
        },
        "core-inner-frame",
        owners,
    )
    bar["modules-left"].append(frame_name)

    return generated_css

def generate_waybar_config(
    project_root: Path,
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    slot_layouts: dict[int, SlotLayout],
    manifests: dict[str, ModuleManifest],
    app_commands: dict[str, str],
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    bar_cfg = required(waybar_cfg, "bar", "waybar.toml")

    position = str(bar_cfg.get("position", "right"))
    if position not in {"left", "right"}:
        raise SystemExit(
            "Hypr Sidebar currently supports only bar.position = 'left' or 'right'."
        )

    result: list[dict[str, Any]] = []
    generated_module_css: list[tuple[str, str, str]] = []

    for monitor in monitors:
        slot_layout = slot_layouts.get(monitor.slot)
        if slot_layout is None:
            raise SystemExit(
                f"No layout was loaded for monitor '{monitor.name}' "
                f"(slot {monitor.slot})."
            )

        layout_cfg = slot_layout.config
        blocks = list(slot_layout.blocks)

        geometry = calculate_geometry(monitor, waybar_cfg)
        bar_name = f"sidebar-{css_safe(monitor.name)}"

        bar: dict[str, Any] = {
            "name": bar_name,
            "output": monitor.name,
            "layer": str(bar_cfg.get("layer", "top")),
            "position": position,
            "exclusive": bool(bar_cfg.get("exclusive", True)),
            "width": geometry.width,
            "spacing": 0,
            "expand-left": True,
            "no-center": True,
            "margin-top": read_hyprland_gaps_out().top,
            "margin-bottom": read_hyprland_gaps_out().bottom,
            "margin-left": 0,
            "margin-right": 0,
            "modules-left": [],
            "modules-center": [],
            "modules-right": [],
        }

        monitor_css = build_layout_for_monitor(
            bar,
            project_root,
            blocks,
            manifests,
            monitor,
            geometry,
            waybar_cfg,
            layout_cfg,
            app_commands,
        )

        for module_name, css in monitor_css:
            generated_module_css.append((module_name, monitor.name, css))

        result.append(bar)

    return result, generated_module_css

def quote_css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate_core_css(
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    slot_layouts: dict[int, SlotLayout],
) -> str:
    bar_cfg = required(waybar_cfg, "bar", "waybar.toml")
    font_cfg = required(waybar_cfg, "font", "waybar.toml")
    theme_cfg = required(waybar_cfg, "theme", "waybar.toml")

    position = str(bar_cfg.get("position", "right"))
    radius = read_hyprland_int_option("decoration:rounding")
    background = str(required(theme_cfg, "background", "[theme]"))
    border_color = str(required(theme_cfg, "border", "[theme]"))
    font_family = str(font_cfg.get("family", "monospace"))

    lines = [
        "/*",
        " * AUTO-GENERATED FILE",
        " * Generated by scripts/generate.py.",
        " * Do not edit manually.",
        " */",
        "",
    ]

    for monitor in monitors:
        slot_layout = slot_layouts.get(monitor.slot)
        if slot_layout is None:
            raise SystemExit(
                f"No layout was loaded for monitor '{monitor.name}' "
                f"(slot {monitor.slot})."
            )

        layout_cfg = slot_layout.config
        blocks = list(slot_layout.blocks)

        geometry = calculate_geometry(monitor, waybar_cfg)
        draw_geometry = calculate_draw_geometry(geometry, layout_cfg)
        border_width = geometry.border_width
        bar_name = f"sidebar-{css_safe(monitor.name)}"
        selector = f"window#waybar.{bar_name}"
        frame_selector = f"{selector} #sidebar-inner-frame"

        lines.extend(
            [
                f"/* {monitor.name}: "
                f"{monitor.logical_width}x{monitor.logical_height} logical px */",
                f"{selector} {{",
                "    background: transparent;",
                "}",
                "",
                f"{frame_selector} {{",
                f"    background: {background};",
                f"    padding: {draw_geometry.margin_y}px {draw_geometry.margin_x}px;",
            ]
        )

        if position == "right":
            lines.extend(
                [
                    f"    border-left: {border_width}px solid {border_color};",
                    f"    border-top: {border_width}px solid {border_color};",
                    f"    border-bottom: {border_width}px solid {border_color};",
                    f"    border-radius: {radius}px 0 0 {radius}px;",
                ]
            )
        else:
            lines.extend(
                [
                    f"    border-right: {border_width}px solid {border_color};",
                    f"    border-top: {border_width}px solid {border_color};",
                    f"    border-bottom: {border_width}px solid {border_color};",
                    f"    border-radius: 0 {radius}px {radius}px 0;",
                ]
            )

        lines.extend(
            [
                "}",
                "",
                f"{selector} * {{",
                f"    font-family: {quote_css_string(font_family)};",
                f"    font-size: {geometry.font_size:.2f}px;",
                "}",
                "",
            ]
        )

        validate_vertical_layout(
            monitor,
            draw_geometry,
            layout_cfg,
            blocks,
        )

        # Exact row/col group and spacer geometry is emitted while
        # building each monitor layout, because nested containers need the
        # same recursive mechanism.


    return "\n".join(lines)

def generate_css(
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    slot_layouts: dict[int, SlotLayout],
    manifests: dict[str, ModuleManifest],
    generated_module_css: list[tuple[str, str, str]],
) -> str:
    parts = [
        generate_core_css(
            monitors,
            waybar_cfg,
            slot_layouts,
        ).rstrip()
    ]

    for manifest in manifests.values():
        if manifest.style is None:
            continue

        style_text = manifest.style.read_text(encoding="utf-8").rstrip()
        parts.extend(
            [
                "",
                f"/* Module: {manifest.name} */",
                style_text,
            ]
        )

    for module_name, monitor_name, css in generated_module_css:
        parts.extend(
            [
                "",
                f"/* Module: {module_name} / Monitor: {monitor_name} */",
                css,
            ]
        )

    return "\n".join(parts) + "\n"

def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(content)

    temp_path.replace(path)


def print_summary(
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    slot_layouts: dict[int, SlotLayout],
) -> None:
    print("Hypr Sidebar generation plan")
    print()

    def summarize_nested(item: LayoutItem) -> str:
        if isinstance(item, LayoutModule):
            text_value = item.reference
        else:
            heights = (
                "equal"
                if item.item_heights_percent is None
                else "["
                + ", ".join(f"{value:g}%" for value in item.item_heights_percent)
                + "]"
            )
            children = ", ".join(
                summarize_nested(child)
                for child in item.items
            )
            text_value = f"col[{children}; heights={heights}]"

        width_mode = describe_axis_allocation(item, "width")
        height_mode = describe_axis_allocation(item, "height")
        allocation = []

        if width_mode is not None:
            allocation.append(f"width={width_mode}")
        if height_mode is not None:
            allocation.append(f"height={height_mode}")

        if allocation:
            text_value += "{" + ", ".join(allocation) + "}"

        return text_value

    for monitor in monitors:
        slot_layout = slot_layouts[monitor.slot]
        layout_cfg = slot_layout.config
        blocks = list(slot_layout.blocks)

        geometry = calculate_geometry(monitor, waybar_cfg)
        draw_geometry = calculate_draw_geometry(geometry, layout_cfg)
        resolved_heights, remaining_height = resolve_page_heights(
            draw_geometry,
            layout_cfg,
            blocks,
            f"monitor '{monitor.name}'",
        )

        print(
            f"{monitor.name}: "
            f"slot={monitor.slot}, "
            f"layout={slot_layout.path.name}, "
            f"logical={monitor.logical_width}x{monitor.logical_height}, "
            f"scale={monitor.scale:g}, "
            f"sidebar={geometry.width}px, "
            f"inner={geometry.inner_width}x{geometry.inner_height}px, "
            f"draw={draw_geometry.width}x{draw_geometry.height}px, "
            f"draw-margin={draw_geometry.margin_x}px, "
            f"pad-x={calculate_layout_pad_x(draw_geometry, layout_cfg)}px, "
            f"pad-y={calculate_layout_pad_y(draw_geometry, layout_cfg)}px, "
            f"remaining-height={remaining_height}px, "
            f"border={geometry.border_width}px, "
            f"font={geometry.font_size:.2f}px"
        )

        print(f"  page.main ({slot_layout.path}):")

        for index, page_item in enumerate(blocks, start=1):
            width = (
                "full"
                if page_item.width is None
                else f"{page_item.width}px"
            )

            resolved_height = resolved_heights[index - 1]

            if page_item.height is not None:
                height = f"{page_item.height}px"
            elif page_item.height_percent is not None:
                height = (
                    f"{page_item.height_percent:g}% of flexible height; "
                    f"{resolved_height}px"
                )
            else:
                height = f"remaining; {resolved_height}px"

            children = ", ".join(
                summarize_nested(item)
                for item in page_item.modules
            )

            if page_item.kind == "row":
                split = (
                    "equal"
                    if page_item.item_widths_percent is None
                    else "["
                    + ", ".join(
                        f"{value:g}%"
                        for value in page_item.item_widths_percent
                    )
                    + "]"
                )
                split_text = f"item-widths={split}"
            else:
                split = (
                    "equal"
                    if page_item.item_heights_percent is None
                    else "["
                    + ", ".join(
                        f"{value:g}%"
                        for value in page_item.item_heights_percent
                    )
                    + "]"
                )
                split_text = f"item-heights={split}"

            pad_y = (
                "inherit"
                if page_item.overwrite_pad_y is None
                else f"{page_item.overwrite_pad_y}px"
            )

            print(
                f"    {page_item.kind} {index}: {children} "
                f"(width={width}, {split_text}, height={height}, "
                f"overwrite-pad-y={pad_y})"
            )

        print()

def main() -> int:
    args = parse_args()
    project_root = args.project_root.expanduser().resolve()

    config_dir = project_root / "config"
    layouts_dir = config_dir / "layouts"
    modules_dir = project_root / "modules"
    waybar_dir = project_root / "waybar"

    waybar_cfg = load_toml(config_dir / "waybar.toml")
    monitors_cfg = load_toml(config_dir / "monitors.toml")
    apps_cfg = load_toml(config_dir / "apps.toml")
    app_commands = load_app_commands(apps_cfg)

    monitors = assign_monitor_slots(
        read_hyprland_monitors(),
        monitors_cfg,
    )

    slot_layouts = load_slot_layouts(
        monitors,
        layouts_dir,
    )
    manifests = load_slot_manifests(
        slot_layouts,
        modules_dir,
    )

    generated_config, generated_module_css = generate_waybar_config(
        project_root,
        monitors,
        waybar_cfg,
        slot_layouts,
        manifests,
        app_commands,
    )
    generated_css = generate_css(
        monitors,
        waybar_cfg,
        slot_layouts,
        manifests,
        generated_module_css,
    )

    print_summary(
        monitors,
        waybar_cfg,
        slot_layouts,
    )

    config_text = json.dumps(generated_config, indent=2) + "\n"

    if args.dry_run:
        print()
        print("--- waybar/config.jsonc ---")
        print(config_text, end="")
        print()
        print("--- waybar/generated.css ---")
        print(generated_css, end="")
        return 0

    atomic_write(waybar_dir / "config.jsonc", config_text)
    atomic_write(waybar_dir / "generated.css", generated_css)

    print()
    print(f"Wrote {waybar_dir / 'config.jsonc'}")
    print(f"Wrote {waybar_dir / 'generated.css'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
