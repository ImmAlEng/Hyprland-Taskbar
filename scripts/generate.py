#!/usr/bin/env python3
"""
Generate the Hypr Sidebar Waybar configuration.

Core responsibilities:
- read global TOML configuration
- discover active Hyprland monitors
- calculate monitor-specific bar geometry
- read layout blocks
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
    parameters: dict[str, Any] | None = None

    @property
    def reference(self) -> str:
        if self.instance is None:
            return self.name
        return f"{self.name}:{self.instance}"


@dataclass(frozen=True)
class LayoutBlock:
    modules: tuple[LayoutModule, ...]
    width: int | None
    height: int | None
    height_fraction: float | None
    item_widths_percent: tuple[float, ...] | None
    overwrite_pad_y: int | None


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
    """Return each horizontal gap after an item; the final item has no gap."""
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
    """Resolve the default vertical gap between layout blocks."""
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
    """Return the gap after one block; the final block never has a gap."""
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


def parse_layout_module(value: Any, context: str) -> LayoutModule:
    """Parse a short module reference or an explicit placement object."""
    overwrite_pad_x: int | None = None
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

        parameters = {
            str(key): item
            for key, item in value.items()
            if key not in {"module", "overwrite_pad_x"}
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
        parameters=parameters,
    )


def load_layout(layout_cfg: dict[str, Any]) -> list[LayoutBlock]:
    page_cfg = required(layout_cfg, "page", "layout.toml")
    main_cfg = required(page_cfg, "main", "[page.main]")
    raw_blocks = main_cfg.get("block", [])

    if not isinstance(raw_blocks, list):
        raise SystemExit("[[page.main.block]] entries must form a list.")

    blocks: list[LayoutBlock] = []

    for index, raw_block in enumerate(raw_blocks, start=1):
        raw_modules = raw_block.get("modules", [])

        if not isinstance(raw_modules, list) or not raw_modules:
            raise SystemExit(
                f"page.main block {index} must contain a non-empty modules list."
            )

        modules: list[LayoutModule] = []
        for module_index, value in enumerate(raw_modules, start=1):
            modules.append(
                parse_layout_module(
                    value,
                    f"page.main block {index} module {module_index}",
                )
            )

        if modules[-1].overwrite_pad_x is not None:
            raise SystemExit(
                f"page.main block {index} module {len(modules)} sets "
                "overwrite_pad_x, but the final module has no horizontal gap after it."
            )

        raw_width = raw_block.get("width")
        width: int | None = None

        if raw_width is not None:
            if isinstance(raw_width, bool) or not isinstance(raw_width, int):
                raise SystemExit(
                    f"page.main block {index} width must be an integer."
                )

            if raw_width <= 0:
                raise SystemExit(
                    f"page.main block {index} width must be greater than zero."
                )

            width = raw_width

        raw_height = raw_block.get("height")
        height: int | None = None

        if raw_height is not None:
            if isinstance(raw_height, bool) or not isinstance(raw_height, int):
                raise SystemExit(
                    f"page.main block {index} height must be an integer."
                )

            if raw_height <= 0:
                raise SystemExit(
                    f"page.main block {index} height must be greater than zero."
                )

            height = raw_height

        raw_height_fraction = raw_block.get("height_fraction")
        height_fraction: float | None = None

        if raw_height_fraction is not None:
            if isinstance(raw_height_fraction, bool) or not isinstance(
                raw_height_fraction, (int, float)
            ):
                raise SystemExit(
                    f"page.main block {index} height_fraction must be a number."
                )

            height_fraction = float(raw_height_fraction)

            if height_fraction <= 0 or height_fraction > 1:
                raise SystemExit(
                    f"page.main block {index} height_fraction must be "
                    "greater than 0 and at most 1."
                )

        if height is not None and height_fraction is not None:
            raise SystemExit(
                f"page.main block {index} cannot set both height and "
                "height_fraction."
            )

        raw_item_widths = raw_block.get("item_widths_percent")
        item_widths_percent: tuple[float, ...] | None = None

        if raw_item_widths is not None:
            if not isinstance(raw_item_widths, list):
                raise SystemExit(
                    f"page.main block {index} item_widths_percent must be a list."
                )

            if len(raw_item_widths) != len(modules):
                raise SystemExit(
                    f"page.main block {index} item_widths_percent must contain "
                    "exactly one value per module."
                )

            percentages: list[float] = []
            for value in raw_item_widths:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SystemExit(
                        f"page.main block {index} item_widths_percent values "
                        "must be numbers."
                    )

                percent = float(value)
                if percent <= 0:
                    raise SystemExit(
                        f"page.main block {index} item_widths_percent values "
                        "must be greater than zero."
                    )

                percentages.append(percent)

            total = sum(percentages)
            if abs(total - 100.0) > 1e-6:
                raise SystemExit(
                    f"page.main block {index} item_widths_percent must total "
                    f"100; got {total:g}."
                )

            item_widths_percent = tuple(percentages)

        if "overwrite_pad_x" in raw_block:
            raise SystemExit(
                f"page.main block {index} overwrite_pad_x is no longer a block "
                "property. Put it on the module placement that owns the gap, for "
                "example: { module = \"color-block:red\", overwrite_pad_x = 15 }."
            )

        raw_overwrite_pad_y = raw_block.get("overwrite_pad_y")
        overwrite_pad_y: int | None = None

        if raw_overwrite_pad_y is not None:
            if (
                isinstance(raw_overwrite_pad_y, bool)
                or not isinstance(raw_overwrite_pad_y, int)
            ):
                raise SystemExit(
                    f"page.main block {index} overwrite_pad_y must be an integer."
                )

            if raw_overwrite_pad_y < 0:
                raise SystemExit(
                    f"page.main block {index} overwrite_pad_y must not be negative."
                )

            overwrite_pad_y = raw_overwrite_pad_y

        blocks.append(
            LayoutBlock(
                modules=tuple(modules),
                width=width,
                height=height,
                height_fraction=height_fraction,
                item_widths_percent=item_widths_percent,
                overwrite_pad_y=overwrite_pad_y,
            )
        )

    return blocks


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
    """Load top-level modules and recursively declared single-child wrappers."""
    manifests: dict[str, ModuleManifest] = {}

    def register(module: LayoutModule, context: str) -> None:
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
        if child.overwrite_pad_x is not None:
            raise SystemExit(
                f"{context} module '{module.name}' child sets overwrite_pad_x, "
                "but a single wrapped child has no horizontal sibling gap."
            )

        register(child, f"{context} module '{module.name}' child")

    for block_index, block in enumerate(blocks, start=1):
        for item_index, module in enumerate(block.modules, start=1):
            register(
                module,
                f"page.main block {block_index} module {item_index}",
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


def calculate_item_widths(
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    block: LayoutBlock,
) -> tuple[int, ...]:
    block_width = calculate_block_width(draw_geometry, block)
    count = len(block.modules)

    if count == 1:
        return (block_width,)

    item_pad_xs = calculate_item_pad_xs(draw_geometry, layout_cfg, block)
    total_pad_x = sum(item_pad_xs)

    # Keep horizontal gaps outside the item allocations. The inherited width
    # must still leave at least one pixel for every module.
    if total_pad_x + count > block_width:
        raise SystemExit(
            "Horizontal layout has no usable item width: "
            f"block-width={block_width}px, modules={count}, "
            f"pad-x-total={total_pad_x}px, pad-x={list(item_pad_xs)}."
        )

    available_width = block_width - total_pad_x

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


def calculate_block_height(
    draw_geometry: DrawGeometry,
    block: LayoutBlock,
) -> int | None:
    if block.height is not None:
        return block.height

    if block.height_fraction is None:
        return None

    return max(1, round(draw_geometry.height * block.height_fraction))


def validate_vertical_layout(
    monitor: Monitor,
    draw_geometry: DrawGeometry,
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
) -> int:
    """Ensure fixed layout allocations and their inter-block gaps fit vertically."""
    module_height = 0
    padding_height = 0

    for index, block in enumerate(blocks):
        height = calculate_block_height(draw_geometry, block)
        if height is None:
            raise SystemExit(
                f"Cannot validate vertical layout for monitor '{monitor.name}': "
                f"block {index + 1} has auto height. Set height or height_fraction."
            )

        module_height += height
        padding_height += calculate_block_pad_y(
            draw_geometry,
            layout_cfg,
            block,
            is_last=index == len(blocks) - 1,
        )

    remaining = draw_geometry.height - module_height - padding_height

    if remaining < 0:
        raise SystemExit(
            f"Layout exceeds available height on monitor '{monitor.name}':\n"
            f"  available: {draw_geometry.height}px\n"
            f"  modules:   {module_height}px\n"
            f"  padding:   {padding_height}px\n"
            f"  overflow:  {-remaining}px"
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
            "block": block_index,
            "item": item_index + 1,
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
            "block": block_index,
            "width": allocated_width,
            "height": allocated_height,
            "height_pixels": allocated_height if nested else block.height,
            "height_fraction": None if nested else block.height_fraction,
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
) -> list[tuple[str, str]]:
    owners: dict[str, str] = {}
    generated_css: list[tuple[str, str]] = []
    frame_modules: list[str] = []

    draw_geometry = calculate_draw_geometry(geometry, layout_cfg)
    validate_vertical_layout(
        monitor,
        draw_geometry,
        layout_cfg,
        blocks,
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
            root, entries, css, child_inset, wrapper_click = render_generated_module(
                project_root,
                manifest,
                module_ref.instance,
                module_ref.parameters or {},
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
            if child_ref.overwrite_pad_x is not None:
                raise SystemExit(
                    f"Wrapper module '{manifest.name}' child cannot set "
                    "overwrite_pad_x because it has no horizontal sibling."
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

    for block_index, block in enumerate(blocks, start=1):
        row_modules: list[str] = []
        item_widths = calculate_item_widths(draw_geometry, layout_cfg, block)
        item_pad_xs = calculate_item_pad_xs(
            draw_geometry,
            layout_cfg,
            block,
        )
        block_height = calculate_block_height(draw_geometry, block)
        if block_height is None:
            raise SystemExit(
                f"page.main block {block_index} does not resolve to a fixed "
                "height; nested module geometry requires a concrete height."
            )

        for item_index, module_ref in enumerate(block.modules):
            allocated_width = item_widths[item_index]
            placement_id = f"main-b{block_index}-i{item_index + 1}"
            root = render_placement(
                module_ref,
                block,
                block_index,
                item_index,
                placement_id,
                allocated_width,
                block_height,
            )
            row_modules.append(root)

            if item_index < len(block.modules) - 1:
                pad_x = item_pad_xs[item_index]
                if pad_x > 0:
                    spacer_name = (
                        f"custom/layout-pad-x-{block_index}-{item_index + 1}"
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
                    row_modules.append(spacer_name)

        group_name = f"group/layout-main-block-{block_index}"
        group_options = {
            "orientation": "horizontal",
            "modules": row_modules,
        }
        add_entry(
            bar,
            group_name,
            group_options,
            "core-layout",
            owners,
        )
        frame_modules.append(group_name)

        block_pad_y = calculate_block_pad_y(
            draw_geometry,
            layout_cfg,
            block,
            is_last=block_index == len(blocks),
        )

        if block_pad_y > 0:
            spacer_name = f"custom/layout-pad-y-{block_index}"
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
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
    manifests: dict[str, ModuleManifest],
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
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
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

        for block_index, block in enumerate(blocks, start=1):
            block_width = calculate_block_width(
                draw_geometry,
                block,
            )
            block_height = calculate_block_height(
                draw_geometry,
                block,
            )
            item_pad_xs = calculate_item_pad_xs(
                draw_geometry,
                layout_cfg,
                block,
            )
            calculate_item_widths(draw_geometry, layout_cfg, block)
            block_pad_y = calculate_block_pad_y(
                draw_geometry,
                layout_cfg,
                block,
                is_last=block_index == len(blocks),
            )

            lines.extend(
                [
                    f"{selector} #group-layout-main-block-{block_index} {{",
                    f"    min-width: {block_width}px;",
                ]
            )

            if block_height is not None:
                lines.append(f"    min-height: {block_height}px;")

            lines.extend(
                [
                    "}",
                    "",
                ]
            )

            for gap_index, pad_x in enumerate(item_pad_xs, start=1):
                if pad_x <= 0:
                    continue

                spacer_selector = (
                    f"{selector} #custom-layout-pad-x-"
                    f"{block_index}-{gap_index}"
                )
                lines.extend(
                    [
                        f"{spacer_selector} {{",
                        f"    min-width: {pad_x}px;",
                        "    min-height: 1px;",
                        "    padding: 0;",
                        "    margin: 0;",
                        "    font-size: 1px;",
                        "}",
                        "",
                    ]
                )

            if block_pad_y > 0:
                spacer_selector = (
                    f"{selector} #custom-layout-pad-y-{block_index}"
                )
                lines.extend(
                    [
                        f"{spacer_selector} {{",
                        f"    min-height: {block_pad_y}px;",
                        "    min-width: 1px;",
                        "    padding: 0;",
                        "    margin: 0;",
                        "    font-size: 1px;",
                        "}",
                        "",
                    ]
                )

    return "\n".join(lines)

def generate_css(
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
    manifests: dict[str, ModuleManifest],
    generated_module_css: list[tuple[str, str, str]],
) -> str:
    parts = [
        generate_core_css(
            monitors,
            waybar_cfg,
            layout_cfg,
            blocks,
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
    layout_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
) -> None:
    print("Hypr Sidebar generation plan")
    print()

    for monitor in monitors:
        geometry = calculate_geometry(monitor, waybar_cfg)
        draw_geometry = calculate_draw_geometry(geometry, layout_cfg)
        remaining_height = validate_vertical_layout(
            monitor,
            draw_geometry,
            layout_cfg,
            blocks,
        )
        print(
            f"{monitor.name}: "
            f"slot={monitor.slot}, "
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

    print()
    print("Layout:")
    for index, block in enumerate(blocks, start=1):
        width = "full" if block.width is None else f"{block.width}px"

        if block.height is not None:
            height = f"{block.height}px"
        elif block.height_fraction is None:
            height = "auto"
        else:
            resolved = ", ".join(
                f"{monitor.name}="
                f"{calculate_block_height(calculate_draw_geometry(calculate_geometry(monitor, waybar_cfg), layout_cfg), block)}px"
                for monitor in monitors
            )
            height = (
                f"{block.height_fraction:.4f} of usable height; "
                f"{resolved}"
            )

        item_widths = (
            "equal"
            if block.item_widths_percent is None
            else "[" + ", ".join(f"{value:g}%" for value in block.item_widths_percent) + "]"
        )

        module_summary = []
        for module_index, module in enumerate(block.modules):
            module_text = module.reference
            if module_index < len(block.modules) - 1:
                pad_x = (
                    "inherit"
                    if module.overwrite_pad_x is None
                    else f"{module.overwrite_pad_x}px"
                )
                module_text += f"{{pad-x={pad_x}}}"
            module_summary.append(module_text)

        pad_y = (
            "inherit"
            if block.overwrite_pad_y is None
            else f"{block.overwrite_pad_y}px"
        )

        print(
            f"  block {index}: "
            f"{', '.join(module_summary)} "
            f"(width={width}, item-widths={item_widths}, "
            f"height={height}, overwrite-pad-y={pad_y})"
        )


def main() -> int:
    args = parse_args()
    project_root = args.project_root.expanduser().resolve()

    config_dir = project_root / "config"
    modules_dir = project_root / "modules"
    waybar_dir = project_root / "waybar"

    waybar_cfg = load_toml(config_dir / "waybar.toml")
    layout_cfg = load_toml(config_dir / "layout.toml")

    monitors_cfg = load_toml(config_dir / "monitors.toml")

    blocks = load_layout(layout_cfg)
    manifests = load_enabled_modules(blocks, modules_dir)
    monitors = assign_monitor_slots(
        read_hyprland_monitors(),
        monitors_cfg,
    )

    generated_config, generated_module_css = generate_waybar_config(
        project_root,
        monitors,
        waybar_cfg,
        layout_cfg,
        blocks,
        manifests,
    )
    generated_css = generate_css(
        monitors,
        waybar_cfg,
        layout_cfg,
        blocks,
        manifests,
        generated_module_css,
    )

    print_summary(monitors, waybar_cfg, layout_cfg, blocks)

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
