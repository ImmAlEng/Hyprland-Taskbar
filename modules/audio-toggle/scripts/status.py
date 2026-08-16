#!/usr/bin/env python3
"""Return PipeWire sink/source mute state as Waybar JSON."""

from __future__ import annotations

import json
import subprocess
import sys


TARGETS = {
    "sink": "@DEFAULT_AUDIO_SINK@",
    "source": "@DEFAULT_AUDIO_SOURCE@",
}


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: status.py TARGET UNMUTED_ICON MUTED_ICON",
            file=sys.stderr,
        )
        return 2

    target = sys.argv[1]
    unmuted_icon = sys.argv[2]
    muted_icon = sys.argv[3]

    wpctl_target = TARGETS.get(target)
    if wpctl_target is None:
        print(f"unsupported audio target: {target}", file=sys.stderr)
        return 2

    result = subprocess.run(
        ["wpctl", "get-volume", wpctl_target],
        check=False,
        capture_output=True,
        text=True,
    )

    muted = result.returncode != 0 or "[MUTED]" in result.stdout

    payload = {
        "text": muted_icon if muted else unmuted_icon,
        "class": "muted" if muted else "unmuted",
    }

    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
