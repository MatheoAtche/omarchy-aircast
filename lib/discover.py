#!/usr/bin/env python3
"""List Chromecast and AirPlay receivers on the LAN as JSON.

  discover.py [--timeout SECONDS]

Uses avahi-browse. Speakers (Google Home/Nest, Sonos, HomePod) are listed
with "video": false; they can only receive audio-only casts.
"""

import argparse
import json
import re
import subprocess
import sys

SERVICES = {
    "_googlecast._tcp": "chromecast",
    "_airplay._tcp": "airplay",
}


def unescape(value):
    # avahi-browse -p escapes non-printable and separator characters as \DDD.
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), value)


def parse_txt(raw):
    txt = {}
    for item in re.findall(r'"((?:[^"\\]|\\.)*)"', raw):
        key, _, value = unescape(item).partition("=")
        txt[key] = value
    return txt


def browse(service, timeout):
    try:
        result = subprocess.run(
            ["avahi-browse", "--resolve", "--parsable", "--terminate", service],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.splitlines()
    except subprocess.TimeoutExpired as error:
        out = error.stdout or b""
        return (out.decode(errors="replace") if isinstance(out, bytes) else out).splitlines()


def to_device(protocol, fields):
    _, iface, family, name, _, _, hostname, address, port, txt_raw = fields[:10]
    txt = parse_txt(txt_raw)
    if address.lower().startswith("fe80"):
        return None  # link-local needs a scope id; not worth the trouble

    if protocol == "chromecast":
        # "ca" is a capability bitmask: bit 0 video out, bit 2 audio out.
        try:
            caps = int(txt.get("ca") or 5)
        except ValueError:
            caps = 5  # malformed; list it like a TV rather than not at all
        video, audio = bool(caps & 1), bool(caps & 4)
        ident = txt.get("id") or name
        label = txt.get("fn") or unescape(name)
        model = txt.get("md", "")
    else:
        # "features" low word: bit 0 video, bit 9 audio.
        features = (txt.get("features") or txt.get("ft") or "0").split(",")[0]
        try:
            features = int(features, 16)
        except ValueError:
            return None
        video, audio = bool(features & 1), bool(features & 0x200)
        ident = txt.get("deviceid") or name
        label = unescape(name)
        model = txt.get("model", "")

    if not video and not audio:
        return None
    return {
        "id": "%s:%s" % (protocol, ident.replace(":", "").lower()),
        "video": video,
        "name": label,
        "protocol": protocol,
        "model": model,
        "host": hostname,
        "address": address,
        "family": "ipv6" if family == "IPv6" else "ipv4",
        "port": int(port),
        "interface": iface,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()

    devices = {}
    for service, protocol in SERVICES.items():
        for line in browse(service, args.timeout):
            fields = line.split(";")
            if fields[0] != "=" or len(fields) < 10:
                continue
            try:
                device = to_device(protocol, fields)
            except ValueError:
                continue  # one malformed announcement must not hide the others
            if not device:
                continue
            known = devices.get(device["id"])
            # Prefer IPv4: every receiver serves it and URLs stay simple.
            if not known or (known["family"] == "ipv6" and device["family"] == "ipv4"):
                devices[device["id"]] = device

    json.dump(sorted(devices.values(), key=lambda d: (d["name"].lower(), d["protocol"])), sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
