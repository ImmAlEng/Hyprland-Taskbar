#!/usr/bin/env python3

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WORKSPACE = int(sys.argv[1])
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "waybar" / "workspace-icons"
CANVAS_W = 800
CANVAS_H = 500
MAX_ICONS = 4
DESKTOP_CACHE_TTL = 300

# Window classes that represent terminal emulators rather than the program
# the user is actually working in. Values are normalized with normalize().
TERMINAL_CLASSES = {
    "alacritty",
    "kitty",
    "foot",
    "wezterm",
    "orgwezfurlongwezterm",
    "konsole",
    "gnometerminal",
    "gnometerminalserver",
}

# Prefer theme-generic icons for apps whose bundled icon does not fit the
# current desktop theme well. The original application icon remains a fallback.
ICON_OVERRIDES = {
    # File managers -> generic directory icon
    "thunar": ["folder", "folder-open", "system-file-manager", "org.xfce.Thunar", "thunar"],
    "orgkdedolphin": ["folder", "folder-open", "system-file-manager", "org.kde.dolphin", "dolphin"],
    "dolphin": ["folder", "folder-open", "system-file-manager", "dolphin"],
    "orggnomenautilus": ["folder", "folder-open", "system-file-manager", "org.gnome.Nautilus", "nautilus"],
    "nautilus": ["folder", "folder-open", "system-file-manager", "nautilus"],
    "pcmanfm": ["folder", "folder-open", "system-file-manager", "pcManFM"],
    "pcmanfmqt": ["folder", "folder-open", "system-file-manager", "pcmanfm-qt"],
    "nemo": ["folder", "folder-open", "system-file-manager", "nemo"],
    "caja": ["folder", "folder-open", "system-file-manager", "caja"],

    "alacritty": ["waybar-terminal", "utilities-terminal", "Alacritty", "alacritty"],
}

# Terminal applications we want to surface instead of the terminal emulator.
# These are executable names as seen in /proc/<pid>/comm or cmdline.
TUI_APPS = {
    "nvim": {"label": "Neovim", "icons": ["nvim", "neovim", "org.neovim.nvim"], "priority": 100},
    "vim": {"label": "Vim", "icons": ["vim", "gvim"], "priority": 90},
    "btop": {"label": "btop", "icons": ["btop", "utilities-system-monitor"], "priority": 80},
    "htop": {"label": "htop", "icons": ["htop", "utilities-system-monitor"], "priority": 80},
    "tmux": {"label": "tmux", "icons": ["waybar-tmux", "utilities-terminal"], "priority": 85},
    "ssh": {"label": "SSH", "icons": ["waybar-ssh", "utilities-terminal"], "priority": 95},
    "yazi": {"label": "Yazi", "icons": ["yazi", "system-file-manager"], "priority": 80},
    "ranger": {"label": "ranger", "icons": ["ranger", "system-file-manager"], "priority": 70},
    "lazygit": {"label": "lazygit", "icons": ["lazygit", "git", "vcs-normal"], "priority": 70},
}

CACHE_DIR.mkdir(parents=True, exist_ok=True)
NORMALIZED_ICON_DIR = CACHE_DIR / "normalized-icons"
NORMALIZED_ICON_DIR.mkdir(parents=True, exist_ok=True)

DESKTOP_CACHE = CACHE_DIR / "desktop-index.json"
ICON_CACHE = CACHE_DIR / "icon-paths.json"
CLIENTS_CACHE = CACHE_DIR / "clients.json"


def load_json_file(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json_file(path, value):
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(value), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def normalize(value):
    value = (value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def desktop_dirs():
    home = Path.home()
    dirs = [
        home / ".local/share/applications",
        home / ".local/share/flatpak/exports/share/applications",
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
    ]
    return [p for p in dirs if p.is_dir()]


def parse_desktop(path):
    data = {}
    in_entry = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    in_entry = line == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key in {"Name", "Icon", "Exec", "StartupWMClass", "Hidden", "Type"}:
                    data[key] = value.strip()
    except OSError:
        return None
    return data


def exec_basename(exec_value):
    if not exec_value:
        return ""
    try:
        parts = shlex.split(exec_value)
    except ValueError:
        parts = exec_value.split()

    while parts and (parts[0] == "env" or "=" in parts[0]):
        parts.pop(0)

    for part in parts:
        if not part.startswith("%"):
            return Path(part).name
    return ""


def rebuild_desktop_entries():
    entries = []
    seen = set()
    for directory in desktop_dirs():
        for path in directory.rglob("*.desktop"):
            desktop_id = path.name
            if desktop_id in seen:
                continue
            seen.add(desktop_id)

            data = parse_desktop(path)
            if not data or data.get("Hidden", "").lower() == "true":
                continue
            if data.get("Type", "Application") != "Application":
                continue

            entries.append({
                "path": str(path),
                "id": path.stem,
                "name": data.get("Name", ""),
                "icon": data.get("Icon", ""),
                "startup": data.get("StartupWMClass", ""),
                "exec": exec_basename(data.get("Exec", "")),
            })

    save_json_file(DESKTOP_CACHE, entries)
    return entries


def load_desktop_entries():
    try:
        age = time.time() - DESKTOP_CACHE.stat().st_mtime
        if age <= DESKTOP_CACHE_TTL:
            entries = load_json_file(DESKTOP_CACHE, [])
            if entries:
                return entries
    except OSError:
        pass
    return rebuild_desktop_entries()


DESKTOP_ENTRIES = load_desktop_entries()


def score_desktop(window_class, initial_class, entry):
    classes = {normalize(window_class), normalize(initial_class)} - {""}
    desktop_id = normalize(entry.get("id"))
    startup = normalize(entry.get("startup"))
    executable = normalize(entry.get("exec"))
    name = normalize(entry.get("name"))

    score = 0
    for cls in classes:
        if startup and cls == startup:
            score = max(score, 120)
        if cls == desktop_id:
            score = max(score, 110)
        if executable and cls == executable:
            score = max(score, 100)
        if name and cls == name:
            score = max(score, 90)
        if desktop_id and (desktop_id.endswith(cls) or cls.endswith(desktop_id)):
            score = max(score, 75)
        if executable and (executable.endswith(cls) or cls.endswith(executable)):
            score = max(score, 70)
    return score


def desktop_for(window_class, initial_class):
    best = None
    best_score = 0
    for entry in DESKTOP_ENTRIES:
        score = score_desktop(window_class, initial_class, entry)
        if score > best_score and entry.get("icon"):
            best = entry
            best_score = score
    return best


def resolve_theme_icon(icon_name):
    if not icon_name:
        return None

    candidate = Path(os.path.expanduser(icon_name))
    if candidate.is_absolute() and candidate.is_file():
        return str(candidate)

    cache = load_json_file(ICON_CACHE, {})
    cached = cache.get(icon_name)
    if cached and Path(cached).is_file():
        return cached

    resolved = None
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        Gtk.init_check([])
        theme = Gtk.IconTheme.get_default()
        if theme is not None:
            info = theme.lookup_icon(icon_name, 256, Gtk.IconLookupFlags.FORCE_SIZE)
            if info and info.get_filename():
                path = Path(info.get_filename())
                if path.is_file():
                    resolved = str(path)
    except Exception:
        resolved = None

    if resolved is None:
        stem = icon_name.rsplit(".", 1)[0]
        for name in (icon_name, stem):
            for ext in ("svg", "png", "xpm"):
                path = Path("/usr/share/pixmaps") / f"{name}.{ext}"
                if path.is_file():
                    resolved = str(path)
                    break
            if resolved:
                break

    if resolved:
        cache[icon_name] = resolved
        save_json_file(ICON_CACHE, cache)
    return resolved


def first_theme_icon(icon_names):
    for icon_name in icon_names:
        path = resolve_theme_icon(icon_name)
        if path:
            return path
    return None


def prepare_icon(icon_path):
    """
    Normalize any theme icon (SVG/PNG/XPM/...) into a transparent RGBA PNG.

    Important for SVG icons: ImageMagick's transparent background must be set
    BEFORE reading the source image, otherwise some SVG renderers produce an
    opaque background.
    """
    source = Path(icon_path)

    try:
        stat = source.stat()
        fingerprint = f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        return icon_path

    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
    output = NORMALIZED_ICON_DIR / f"{digest}.png"

    if output.is_file():
        return str(output)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{digest}.",
        suffix=".png",
        dir=NORMALIZED_ICON_DIR,
    )
    os.close(fd)
    tmp = Path(tmp_name)

    try:
        subprocess.run(
            [
                "magick",
                "-background", "none",
                str(source),
                "-alpha", "on",
                "-trim",
                "+repage",
                "-resize", "256x256",
                f"PNG32:{tmp}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp.replace(output)
        return str(output)
    except (subprocess.CalledProcessError, OSError):
        tmp.unlink(missing_ok=True)
        return icon_path


def proc_name(pid):
    try:
        name = (Path("/proc") / str(pid) / "comm").read_text(encoding="utf-8").strip()
        if name:
            return Path(name).name
    except OSError:
        pass

    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        first = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
        if first:
            return Path(first).name
    except OSError:
        pass

    return ""


def proc_children(pid):
    path = Path("/proc") / str(pid) / "task" / str(pid) / "children"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return []

    children = []
    for value in text.split():
        try:
            children.append(int(value))
        except ValueError:
            pass
    return children


def descendants(root_pid):
    """Return (pid, depth, executable-name) below root_pid."""
    try:
        root_pid = int(root_pid)
    except (TypeError, ValueError):
        return []

    result = []
    queue = [(root_pid, 0)]
    seen = {root_pid}

    while queue and len(seen) < 256:
        pid, depth = queue.pop(0)
        for child in proc_children(pid):
            if child in seen:
                continue
            seen.add(child)
            child_depth = depth + 1
            name = proc_name(child).lower()
            result.append((child, child_depth, name))
            queue.append((child, child_depth))

    return result


def terminal_tui_for_pid(pid):
    matches = []
    for child_pid, depth, name in descendants(pid):
        app_name = name

        # /proc/<pid>/comm reports tmux clients as e.g. "tmux: client".
        # Treat every tmux-prefixed process as the tmux TUI app.
        if name.startswith("tmux"):
            app_name = "tmux"

        app = TUI_APPS.get(app_name)
        if app:
            matches.append((depth, app.get("priority", 0), child_pid, app_name, app))

    if not matches:
        return None

    # Prefer the deepest recognized process; priority breaks ties.
    _, _, child_pid, name, app = max(matches, key=lambda item: (item[0], item[1]))
    return {"pid": child_pid, "name": name, **app}


def app_from_client(client):
    window_class = client.get("class", "")
    initial_class = client.get("initialClass", "")
    class_key = normalize(initial_class or window_class)

    # A terminal window may actually be Neovim/btop/etc. Inspect its process
    # tree and represent the foreground TUI app when we recognize one.
    if class_key in TERMINAL_CLASSES:
        tui = terminal_tui_for_pid(client.get("pid"))
        if tui:
            desktop = desktop_for(tui["name"], tui["name"])
            label = desktop.get("name", "") if desktop else tui["label"]

            # Prefer our explicit TUI icon choices over any matching .desktop
            # entry (e.g. Avahi's SSH entry for the "ssh" executable).
            candidates = []
            candidates.extend(tui["icons"])
            if desktop and desktop.get("icon"):
                candidates.append(desktop["icon"])

            icon_path = first_theme_icon(candidates)
            if icon_path:
                return (f"tui:{tui['name']}", label, icon_path)

    desktop = desktop_for(window_class, initial_class)
    label = desktop.get("name", "") if desktop else (initial_class or window_class)

    candidates = []
    candidates.extend(ICON_OVERRIDES.get(class_key, []))
    if desktop and desktop.get("icon"):
        candidates.append(desktop["icon"])
    candidates.extend([initial_class, window_class])

    icon_path = first_theme_icon(candidates)
    if not icon_path:
        return None

    return (f"gui:{class_key}", label, icon_path)


def get_clients():
    # All eight tile modules tend to refresh together. Reuse a very fresh
    # hyprctl result so one Waybar refresh doesn't spawn eight identical calls.
    try:
        if time.time() - CLIENTS_CACHE.stat().st_mtime < 0.35:
            clients = load_json_file(CLIENTS_CACHE, [])
            if isinstance(clients, list):
                return clients
    except OSError:
        pass

    try:
        out = subprocess.check_output(
            ["hyprctl", "clients", "-j"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        clients = json.loads(out)
        save_json_file(CLIENTS_CACHE, clients)
        return clients
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return []


def apps_for_workspace():
    clients = get_clients()
    clients.sort(key=lambda c: c.get("focusHistoryID", 999999))

    apps = []
    seen = set()
    for client in clients:
        if not client.get("mapped", True):
            continue
        if client.get("workspace", {}).get("id") != WORKSPACE:
            continue

        app = app_from_client(client)
        if not app:
            continue

        key, label, icon_path = app

        # Dedupe by the application we actually show, not merely by the window
        # class. This lets one Alacritty window show Neovim while another still
        # shows a terminal icon.
        if key in seen:
            continue
        seen.add(key)

        apps.append((label, icon_path))
        if len(apps) >= MAX_ICONS:
            break

    return apps


def layout(count):
    # Coordinates on an 800x500 transparent 16:10 canvas.
    #
    # Keep 1-3 apps large and immediately recognizable.
    # At 4 apps use a centered 2x2 layout.
    if count <= 0:
        return []
    if count == 1:
        return [(170, 20, 460)]
    if count == 2:
        return [(70, 95, 310), (420, 95, 310)]
    if count == 3:
        return [(25, 135, 230), (285, 135, 230), (545, 135, 230)]
    return [
        (135, 30, 210),
        (455, 30, 210),
        (135, 260, 210),
        (455, 260, 210),
    ]


def render_tile(apps):
    # Convert theme assets to RGBA PNG first. This makes SVG/XPM icons and PNGs
    # without an alpha channel behave consistently on the transparent tile.
    prepared_apps = [(label, prepare_icon(path)) for label, path in apps]

    signature = "|".join(path for _, path in prepared_apps) if prepared_apps else "empty"
    digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
    output = CACHE_DIR / f"ws-{WORKSPACE}-{digest}.png"

    if output.is_file():
        return output

    fd, tmp_name = tempfile.mkstemp(prefix=f".ws-{WORKSPACE}-", suffix=".png", dir=CACHE_DIR)
    os.close(fd)
    tmp = Path(tmp_name)

    cmd = ["magick", "-size", f"{CANVAS_W}x{CANVAS_H}", "xc:none"]

    for (_, icon_path), (x, y, size) in zip(prepared_apps, layout(len(prepared_apps))):
        cmd.extend([
            "(", icon_path,
            "-resize", f"{size}x{size}",
            ")",
            "-geometry", f"+{x}+{y}",
            "-composite",
        ])

    # The tile itself stays icon-only; workspace number/app names live in the tooltip.
    cmd.append(str(tmp))

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp.replace(output)
    except (subprocess.CalledProcessError, OSError):
        tmp.unlink(missing_ok=True)
        fallback = CACHE_DIR / f"ws-{WORKSPACE}-empty.png"
        if not fallback.is_file():
            subprocess.run(
                ["magick", "-size", f"{CANVAS_W}x{CANVAS_H}", "xc:none", str(fallback)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        output = fallback

    # Keep just the current render for this workspace.
    for old in CACHE_DIR.glob(f"ws-{WORKSPACE}-*.png"):
        if old != output:
            old.unlink(missing_ok=True)

    return output


apps = apps_for_workspace()
image = render_tile(apps)
tooltip = ", ".join(label for label, _ in apps) if apps else "empty"

# Waybar image module: first line = image path, second line = tooltip.
print(image)
print(f"Workspace - {WORKSPACE} · {tooltip}")
