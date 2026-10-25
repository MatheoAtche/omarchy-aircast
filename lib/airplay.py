#!/usr/bin/env python3
"""Legacy AirPlay (v1) video sender, used when pyatv's atvremote is not installed.

  airplay.py play <host> <port> <url>

Works with receivers that accept unauthenticated /play requests (older Apple
TVs, Kodi, many third-party receivers). Modern Apple TVs want a paired
session; install pyatv for those. Exits when the receiver stops playing and
sends /stop on SIGTERM/SIGINT.
"""

import http.client
import signal
import sys
import time
import uuid

HEADERS = {"User-Agent": "MediaControl/1.0", "X-Apple-Session-ID": str(uuid.uuid4())}


def request(conn, method, path, body=None, content_type=None):
    headers = dict(HEADERS)
    if content_type:
        headers["Content-Type"] = content_type
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    return response.status, response.read().decode(errors="replace")


def main():
    if len(sys.argv) != 5 or sys.argv[1] != "play":
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    host, port, url = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    conn = http.client.HTTPConnection(host, port, timeout=10)

    def stop(*_):
        try:
            request(conn, "POST", "/stop")
        except (OSError, http.client.HTTPException):
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        status, _ = request(conn, "POST", "/play", "Content-Location: %s\nStart-Position: 0\n" % url, "text/parameters")
        if status == 403 or status == 470:
            raise RuntimeError("receiver requires pairing; install pyatv and run: atvremote -s %s --protocol airplay pair" % host)
        if status >= 300:
            raise RuntimeError("receiver refused the stream (HTTP %d)" % status)
        print("playing", flush=True)

        # The session lives as long as this connection; poll it so the
        # receiver keeps it open and so we notice when playback stops.
        started = False
        while True:
            time.sleep(2)
            status, body = request(conn, "GET", "/scrub")
            fields = dict(line.split(": ", 1) for line in body.splitlines() if ": " in line)
            position = float(fields.get("position", 0) or 0)
            if position > 0:
                started = True
            elif started:
                print("receiver stopped playback", flush=True)
                return
    except (OSError, http.client.HTTPException, RuntimeError) as error:
        print("airplay: %s" % error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
