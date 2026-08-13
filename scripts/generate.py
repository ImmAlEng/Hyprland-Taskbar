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
    font_size: float


@dataclass(frozen=True)
class ModuleManifest:
    name: str
    directory: Path
    root: str
    entries: dict[str, dict[str, Any]]
    style: Path | None


@dataclass(frozen=True)
class LayoutBlock:
    modules: tuple[str, ...]


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

    return BarGeometry(
        width=calculate_sidebar_width(monitor.logical_height, width_cfg),
        font_size=calculate_font_size(monitor.logical_height, font_cfg),
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

        modules: list[str] = []
        for name in raw_modules:
            if not isinstance(name, str) or not name:
                raise SystemExit(
                    f"page.main block {index} contains an invalid module name."
                )
            modules.append(name)

        blocks.append(LayoutBlock(modules=tuple(modules)))

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

    style_path: Path | None = None
    style_value = module_cfg.get("style")
    if style_value is not None:
        style_path = module_dir / str(style_value)
        if not style_path.is_file():
            raise SystemExit(
                f"Module '{name}' declares style '{style_value}', "
                f"but {style_path} does not exist."
            )

    return ModuleManifest(
        name=name,
        directory=module_dir,
        root=root,
        entries=normalized_entries,
        style=style_path,
    )


def load_enabled_modules(
    blocks: list[LayoutBlock],
    modules_dir: Path,
) -> dict[str, ModuleManifest]:
    manifests: dict[str, ModuleManifest] = {}

    for block in blocks:
        for name in block.modules:
            if name not in manifests:
                manifests[name] = load_module_manifest(modules_dir, name)

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
) -> None:
    owners: dict[str, str] = {}

    for block_index, block in enumerate(blocks, start=1):
        roots: list[str] = []

        for module_name in block.modules:
            manifest = manifests[module_name]
            context = module_context(
                project_root,
                manifest,
                monitor,
                geometry,
            )

            for entry_name, raw_options in manifest.entries.items():
                options = expand_templates(raw_options, context)
                add_entry(
                    bar,
                    entry_name,
                    options,
                    module_name,
                    owners,
                )

            roots.append(manifest.root)

        if len(roots) == 1:
            bar["modules-left"].append(roots[0])
            continue

        group_name = f"group/layout-main-block-{block_index}"
        group_options = {
            "orientation": "horizontal",
            "modules": roots,
        }
        add_entry(
            bar,
            group_name,
            group_options,
            "core-layout",
            owners,
        )
        bar["modules-left"].append(group_name)


def generate_waybar_config(
    project_root: Path,
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    blocks: list[LayoutBlock],
    manifests: dict[str, ModuleManifest],
) -> list[dict[str, Any]]:
    bar_cfg = required(waybar_cfg, "bar", "waybar.toml")
    spacing_cfg = required(waybar_cfg, "spacing", "waybar.toml")

    position = str(bar_cfg.get("position", "right"))
    if position not in {"left", "right"}:
        raise SystemExit(
            "Hypr Sidebar currently supports only bar.position = 'left' or 'right'."
        )

    result: list[dict[str, Any]] = []

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
            "spacing": int(spacing_cfg.get("module", 0)),
            "margin-top": int(bar_cfg.get("margin_top", 0)),
            "margin-bottom": int(bar_cfg.get("margin_bottom", 0)),
            "margin-left": int(bar_cfg.get("margin_left", 0)),
            "margin-right": int(bar_cfg.get("margin_right", 0)),
            "modules-left": [],
            "modules-center": [],
            "modules-right": [],
        }

        build_layout_for_monitor(
            bar,
            project_root,
            blocks,
            manifests,
            monitor,
            geometry,
        )

        result.append(bar)

    return result


def quote_css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate_core_css(
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
) -> str:
    bar_cfg = required(waybar_cfg, "bar", "waybar.toml")
    border_cfg = required(bar_cfg, "border", "[bar]")
    font_cfg = required(waybar_cfg, "font", "waybar.toml")
    theme_cfg = required(waybar_cfg, "theme", "waybar.toml")

    position = str(bar_cfg.get("position", "right"))
    border_width = int(border_cfg.get("width", 0))
    radius = int(border_cfg.get("radius", 0))
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
        bar_name = f"sidebar-{css_safe(monitor.name)}"
        selector = f"window#waybar.{bar_name}"

        lines.extend(
            [
                f"/* {monitor.name}: "
                f"{monitor.logical_width}x{monitor.logical_height} logical px */",
                f"{selector} {{",
                f"    background: {background};",
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

    return "\n".join(lines)


def generate_css(
    monitors: list[Monitor],
    waybar_cfg: dict[str, Any],
    manifests: dict[str, ModuleManifest],
) -> str:
    parts = [generate_core_css(monitors, waybar_cfg).rstrip()]

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
    blocks: list[LayoutBlock],
) -> None:
    print("Hypr Sidebar generation plan")
    print()

    for monitor in monitors:
        geometry = calculate_geometry(monitor, waybar_cfg)
        print(
            f"{monitor.name}: "
            f"slot={monitor.slot}, "
            f"logical={monitor.logical_width}x{monitor.logical_height}, "
            f"scale={monitor.scale:g}, "
            f"sidebar={geometry.width}px, "
            f"font={geometry.font_size:.2f}px"
        )

    print()
    print("Layout:")
    for index, block in enumerate(blocks, start=1):
        print(f"  block {index}: {', '.join(block.modules)}")


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

    generated_config = generate_waybar_config(
        project_root,
        monitors,
        waybar_cfg,
        blocks,
        manifests,
    )
    generated_css = generate_css(
        monitors,
        waybar_cfg,
        manifests,
    )

    print_summary(monitors, waybar_cfg, blocks)

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
