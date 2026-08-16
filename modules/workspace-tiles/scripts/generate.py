#!/usr/bin/env python3
"""Generate Waybar entries and CSS for the workspace-tiles module."""

from __future__ import annotations

import json
import math
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def load_context() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        fail(f"invalid generator context JSON: {exc}")

    if not isinstance(value, dict):
        fail("generator context must be a JSON object")

    return value


def load_config(module_dir: Path) -> dict[str, Any]:
    path = module_dir / "config.toml"
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        fail(f"missing module configuration: {path}")
    except tomllib.TOMLDecodeError as exc:
        fail(f"invalid TOML in {path}: {exc}")


def css_rgba(value: str) -> str:
    value = value.strip()
    if not value.startswith("rgba("):
        fail(f"expected rgba(...) color, got: {value}")
    return value


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def main() -> int:
    context = load_context()

    module_dir = Path(str(context["module_dir"]))
    monitor = context["monitor"]
    bar = context["bar"]

    monitor_name = str(monitor["name"])
    monitor_css = str(monitor["css"])
    monitor_slot = str(monitor["slot"])

    config = load_config(module_dir)
    grid = config["grid"]
    theme = config["theme"]
    workspace_slots = config["workspace_slots"]

    raw_workspaces = workspace_slots.get(monitor_slot)
    if not isinstance(raw_workspaces, list) or len(raw_workspaces) < 2:
        fail(
            f"monitor slot {monitor_slot} must define at least two workspaces "
            "in [workspace_slots]"
        )

    try:
        workspaces = [int(value) for value in raw_workspaces]
    except (TypeError, ValueError):
        fail(f"monitor slot {monitor_slot} contains a non-integer workspace")

    if len(set(workspaces)) != len(workspaces):
        fail(f"monitor slot {monitor_slot} contains duplicate workspaces")

    columns = int(grid.get("columns", 2))
    max_visible = int(grid.get("max_visible", 0))

    if columns <= 0:
        fail("grid.columns must be greater than zero")
    if max_visible < 0:
        fail("grid.max_visible must be zero or greater")

    capacity = len(workspaces) - 1
    visible_count = capacity if max_visible == 0 else min(max_visible, capacity)
    effective_columns = min(columns, visible_count)
    rows = math.ceil(visible_count / effective_columns)

    inner_width = int(bar["inner_width"])
    tile_border = int(grid.get("border_width", 1))

    grid_width_fraction = float(grid.get("grid_width_fraction", 0.92))
    gap_fraction = float(grid.get("gap_fraction", 0.02))
    aspect_ratio = float(grid.get("aspect_ratio", 1.6))
    interval = float(grid.get("refresh_interval", 1))

    if not 0 < grid_width_fraction <= 1:
        fail("grid.grid_width_fraction must be greater than 0 and at most 1")
    if gap_fraction < 0:
        fail("grid.gap_fraction must be zero or greater")
    if aspect_ratio <= 0:
        fail("grid.aspect_ratio must be greater than zero")
    if tile_border < 0:
        fail("grid.border_width must be zero or greater")
    if interval <= 0:
        fail("grid.refresh_interval must be greater than zero")

    if inner_width <= 0:
        fail("bar inner width must be greater than zero")

    gap = int(inner_width * gap_fraction)
    target_grid_width = int(inner_width * grid_width_fraction)
    total_gaps = gap * (effective_columns - 1)
    available_for_tiles = target_grid_width - total_gaps

    if available_for_tiles <= 0:
        fail("grid width and gap settings leave no room for tiles")

    tile_outer_width = available_for_tiles // effective_columns
    used_width = tile_outer_width * effective_columns + total_gaps
    remaining_width = inner_width - used_width

    if remaining_width < 0:
        fail("calculated grid exceeds the available bar width")

    outer_gap_left = remaining_width // 2
    outer_gap_right = remaining_width - outer_gap_left

    tile_width = tile_outer_width - 2 * tile_border
    tile_outer_height = int(tile_outer_width / aspect_ratio)
    tile_height = tile_outer_height - 2 * tile_border

    if tile_width <= 0 or tile_height <= 0:
        fail("calculated tile size is not positive")

    left_margin = outer_gap_left
    right_margin = outer_gap_right
    top_gap = outer_gap_left
    row_gap = gap

    root = f"group/workspace-tiles-{monitor_css}"
    tile_names = [
        f"image#workspace-tile-{monitor_css}-{index}"
        for index in range(1, visible_count + 1)
    ]
    tile_rows = chunked(tile_names, effective_columns)
    row_names = [
        f"group/workspace-tiles-{monitor_css}-row-{index}"
        for index in range(1, rows + 1)
    ]

    entries: dict[str, dict[str, Any]] = {
        root: {
            "orientation": "vertical",
            "modules": row_names,
        }
    }

    for row_name, row_tiles in zip(row_names, tile_rows):
        entries[row_name] = {
            "orientation": "horizontal",
            "modules": row_tiles,
        }

    runtime = module_dir / "scripts" / "workspace-tile.py"
    renderer = module_dir / "scripts" / "workspace-icons.py"

    if not runtime.is_file():
        fail(f"missing tile runtime: {runtime}")
    if not renderer.is_file():
        fail(
            f"missing workspace renderer: {renderer}; copy the active "
            "workspace-icons.py into this module"
        )

    workspace_args = [str(workspace) for workspace in workspaces]

    for index, entry_name in enumerate(tile_names, start=1):
        base_args = [
            sys.executable,
            str(runtime),
            "show",
            str(module_dir),
            monitor_name,
            str(index),
            *workspace_args,
        ]
        click_args = base_args.copy()
        click_args[2] = "switch"

        entries[entry_name] = {
            "exec": shlex.join(base_args),
            "interval": interval,
            "size": tile_width,
            "tooltip": True,
            "on-click": shlex.join(click_args),
        }

    selectors = {
        name: f"#image.workspace-tile-{monitor_css}-{index}"
        for index, name in enumerate(tile_names, start=1)
    }

    background = css_rgba(str(theme["background"]))
    border = css_rgba(str(theme["border"]))
    hover_background = css_rgba(str(theme["hover_background"]))
    hover_border = css_rgba(str(theme["hover_border"]))

    all_selectors = [selectors[name] for name in tile_names]
    css_parts = [
        f"/* Workspace tile geometry for {monitor_name}. */",
        ",\n".join(all_selectors)
        + " {\n"
        + f"    min-width: {tile_width}px;\n"
        + f"    min-height: {tile_height}px;\n"
        + "    padding: 0;\n"
        + f"    background: {background};\n"
        + f"    border: {tile_border}px solid {border};\n"
        + "}",
    ]

    for row_index, row_tiles in enumerate(tile_rows):
        for column_index, tile_name in enumerate(row_tiles):
            selector = selectors[tile_name]
            margin_left = left_margin if column_index == 0 else gap
            margin_top = top_gap if row_index == 0 else row_gap

            missing_columns = effective_columns - len(row_tiles)
            margin_right = 0
            if column_index == len(row_tiles) - 1:
                margin_right = right_margin + missing_columns * (tile_outer_width + gap)

            css_parts.append(
                f"{selector} {{\n"
                f"    margin-left: {margin_left}px;\n"
                f"    margin-right: {margin_right}px;\n"
                f"    margin-top: {margin_top}px;\n"
                "    margin-bottom: 0;\n"
                "}"
            )

    hover_selectors = [selector + ":hover" for selector in all_selectors]
    css_parts.append(
        ",\n".join(hover_selectors)
        + " {\n"
        + f"    background: {hover_background};\n"
        + f"    border-color: {hover_border};\n"
        + "}"
    )

    json.dump(
        {
            "root": root,
            "entries": entries,
            "css": "\n\n".join(css_parts) + "\n",
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
