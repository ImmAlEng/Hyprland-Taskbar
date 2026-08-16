# Hypr Sidebar Module Contract v1

This document defines the boundary between the Hypr Sidebar core and a module.

The core is responsible for:

- reading global configuration
- discovering monitors
- calculating bar geometry
- reading `layout.toml`
- loading module manifests
- placing module roots into the requested layout blocks
- generating Waybar configuration
- combining enabled module styles
- validating manifest structure

A module is responsible for:

- declaring its Waybar entries
- declaring exactly one root Waybar entry
- providing its own scripts
- providing its own style
- declaring runtime dependencies
- providing its own assets when needed

The core must not contain module-specific behavior.

## Directory layout

Each module lives in its own directory:

```text
modules/
└── example/
    ├── module.toml
    ├── style.css
    ├── scripts/
    └── assets/
```

Only `module.toml` is mandatory.

## Manifest

Minimal example:

```toml
[module]
name = "example"
version = 1
style = "style.css"

[dependencies]
commands = ["python3"]

[dependencies.arch]
packages = ["python"]

[waybar]
root = "custom/example"

[waybar.entries."custom/example"]
exec = "\"{python}\" \"{module_dir}/scripts/example.py\" \"{monitor}\""
interval = 1
format = "{}"
```

### `[module]`

`name`
: Must exactly match the module directory name.

`version`
: Module contract version. Version `1` is currently supported.

`style`
: Optional path to a CSS file relative to the module directory.

### `[dependencies]`

Dependency metadata is declarative. The generator validates the manifest but does not install packages.

`commands`
: Commands required by the module at runtime.

### `[dependencies.arch]`

`packages`
: Arch Linux packages that provide the required runtime dependencies.

Optional packages may be declared under `[dependencies.arch.optional]`.

### `[waybar]`

`root`
: The single Waybar entry exposed to the layout system.

A complex module may define many private Waybar entries, but the layout only sees its root.

### `[waybar.entries]`

Each key is a real Waybar module identifier. The value is copied into the generated Waybar configuration after template expansion.

This means modules can use native Waybar modules, custom modules, image modules, groups, and nested internal layouts without teaching the core what the module does.

The root entry must exist in `waybar.entries`.

## Template variables

String values inside `waybar.entries` may use these variables:

```text
{project_root}
{module_dir}
{monitor}
{monitor_css}
{monitor_slot}
{logical_width}
{logical_height}
{bar_width}
{font_size}
{python}
```

Only these exact variables are expanded. Normal Waybar format strings such as `{}`, `{icon}`, or `{text}` are left untouched.

## Layout blocks

`layout.toml` controls placement:

```toml
[page.main]

[[page.main.block]]
modules = ["current-workspace"]

[[page.main.block]]
modules = ["workspace-tiles"]
```

A block containing one module inserts that module's root directly.

A block containing multiple modules is wrapped by the core in a horizontal Waybar group:

```toml
[[page.main.block]]
modules = ["bluetooth", "wifi"]
```

The modules themselves do not decide where they appear.

## Styling

The global `waybar/style.css` owns the bar structure and global theme.

Each enabled module may provide its own `style.css`. The generator appends those styles to `waybar/generated.css`.

Module CSS should only style selectors belonging to that module.

## Design rule

The core may know how Waybar works.

The core may not know what a workspace, audio device, VPN, clipboard, password manager, media player, or LLM is.


## Monitor slots

The core assigns every active output a generic logical slot.

`config/monitors.toml` controls slot assignment:

```toml
[monitors]
mode = "auto"
max_slots = 4
```

In `auto` mode:

1. explicit monitor overrides are applied first
2. remaining active outputs receive the first free slots in Hyprland order

Optional overrides:

```toml
[monitor."DP-1"]
slot = 1

[monitor."DP-2"]
slot = 2
```

In `explicit` mode, active outputs without a configured slot are ignored.

The core does not attach any workspace or module-specific meaning to a slot.
Modules receive the assigned value through `{monitor_slot}` and may interpret
it according to their own configuration.


## Generated modules

Static modules declare `[waybar]` entries directly in `module.toml`.

Complex modules may instead declare a generator:

```toml
[generator]
entrypoint = "scripts/generate.py"
```

A module must define exactly one of `[waybar]` or `[generator]`.

The core runs a generated module once per active monitor and passes a JSON object
on standard input. The context contains only generic core metadata:

```text
contract_version

project_root
module_dir

monitor.name
monitor.css
monitor.slot
monitor.logical_width
monitor.logical_height
monitor.scale

bar.width
bar.position
bar.border_width
bar.border_radius

spacing.edge
spacing.module
spacing.row
spacing.column

font.size
```

The generator must write one JSON object to standard output:

```json
{
  "root": "group/example",
  "entries": {
    "group/example": {
      "orientation": "horizontal",
      "modules": ["custom/example-child"]
    },
    "custom/example-child": {
      "format": "example"
    }
  },
  "css": "optional monitor-specific CSS"
}
```

`root` must refer to an entry in `entries`.

This hook is intended for modules whose internal Waybar structure or geometry
depends on monitor context. The core still does not know what the module does.
