Hypr Sidebar - initial modular structure

config/
  waybar.toml
      Core sidebar appearance and geometry.

  layout.toml
      Which modules appear and in what order.

  monitors.toml
      Monitor discovery / explicit monitor slot overrides.

waybar/
  style.css
      Handwritten core structure only.

  generated.css
      Generated monitor-specific values.

modules/
  current-workspace/
      Existing current workspace label / future overview button.

  workspace-tiles/
      Existing 2x2 inactive workspace preview tiles.

Next:
  scripts/generate.py reads the TOML files and emits Waybar config.jsonc
  plus generated.css without knowing module internals.
