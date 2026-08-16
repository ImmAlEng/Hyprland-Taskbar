#!/usr/bin/env python3
"""Format NetworkManager state as Waybar JSON."""

from __future__ import annotations

import json
import subprocess
import sys


def device_states() -> list[tuple[str, str]]:
    result = subprocess.run(
        ["nmcli", "-t", "-f", "TYPE,STATE", "device", "status"],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return []

    states: list[tuple[str, str]] = []

    for raw_line in result.stdout.splitlines():
        if ":" not in raw_line:
            continue

        device_type, state = raw_line.split(":", 1)
        states.append(
            (
                device_type.strip().lower(),
                state.strip().lower(),
            )
        )

    return states


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: waybar.py WIFI_ICON ETHERNET_ICON", file=sys.stderr)
        return 2

    wifi_icon = sys.argv[1]
    ethernet_icon = sys.argv[2]
    states = device_states()

    ethernet_connected = any(
        device_type == "ethernet" and state == "connected"
        for device_type, state in states
    )

    wifi_connected = any(
        device_type == "wifi" and state == "connected"
        for device_type, state in states
    )

    if ethernet_connected:
        payload = {
            "text": ethernet_icon,
            "class": "ethernet",
        }
    elif wifi_connected:
        payload = {
            "text": wifi_icon,
            "class": "wifi-connected",
        }
    else:
        payload = {
            "text": wifi_icon,
            "class": "wifi-disconnected",
        }

    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
