#!/usr/bin/env python3
"""Minimal Cast v2 sender: play a live HLS URL on a Chromecast and stay attached.

Stdlib only. The CastMessage protobuf is tiny, so it is encoded by hand.

  chromecast.py status <host>
  chromecast.py play <host> <url> [--title TITLE]
  chromecast.py stop <host>

`play` keeps running while the device plays the stream. It exits 0 when the
receiver stops (someone pressed stop on the TV or another app took over) and
sends STOP to the receiver when it gets SIGTERM/SIGINT.
"""

import argparse
import json
import signal
import socket
import ssl
import struct
import sys
import time

PORT = 8009
SENDER = "sender-aircast"
RECEIVER = "receiver-0"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"

MAX_MESSAGE = 65536  # the Cast protocol's own limit
MESSAGE_TIMEOUT = 10  # for the rest of a message once it has started
# Connecting, launching the player and loading the stream all fit in this;
# bin/aircast waits a little longer before it gives up on the receiver.
STARTUP_TIMEOUT = 45

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"


def _varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_str(num, value):
    data = value.encode()
    return _varint(num << 3 | 2) + _varint(len(data)) + data


def encode(source, destination, namespace, payload):
    return (
        _varint(1 << 3) + _varint(0)  # protocol_version = CASTV2_1_0
        + _field_str(2, source)
        + _field_str(3, destination)
        + _field_str(4, namespace)
        + _varint(5 << 3) + _varint(0)  # payload_type = STRING
        + _field_str(6, json.dumps(payload))
    )


def decode(data):
    fields = {}
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(data, i)
        elif wire == 2:
            length, i = _read_varint(data, i)
            value = data[i:i + length]
            i += length
        else:
            raise ValueError("unsupported wire type %d" % wire)
        fields[num] = value
    text = lambda n: fields[n].decode(errors="replace") if isinstance(fields.get(n), bytes) else ""
    payload = text(6)
    try:
        payload = json.loads(payload) if payload else {}
    except (ValueError, RecursionError):
        payload = {}
    return text(2), text(3), text(4), payload if isinstance(payload, dict) else {}


def _read_varint(data, i):
    shift = result = 0
    while True:
        if i >= len(data):
            raise ValueError("truncated varint")
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


class Cast:
    def __init__(self, host, timeout=10):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # devices use self-signed certificates
        raw = socket.create_connection((host, PORT), timeout=timeout)
        self.sock = ctx.wrap_socket(raw)
        self.request_id = 0
        self.connected = set()
        self.connect(RECEIVER)

    def send(self, destination, namespace, payload):
        if namespace not in (NS_CONNECTION, NS_HEARTBEAT) and "requestId" not in payload:
            self.request_id += 1
            payload = dict(payload, requestId=self.request_id)
        msg = encode(SENDER, destination, namespace, payload)
        self.sock.sendall(struct.pack(">I", len(msg)) + msg)
        return payload.get("requestId")

    def connect(self, destination):
        if destination not in self.connected:
            self.send(destination, NS_CONNECTION, {"type": "CONNECT"})
            self.connected.add(destination)

    def recv(self):
        """Read one message. socket.timeout means none started arriving in time.

        Once a message has started, all of it has to arrive within
        MESSAGE_TIMEOUT and fit in MAX_MESSAGE; a device that breaks either is
        dropped, so it cannot hold the sender or grow its memory.
        """
        timeout = self.sock.gettimeout()
        first = self.sock.recv(4)
        if not first:
            raise ConnectionError("device closed the connection")
        deadline = time.monotonic() + MESSAGE_TIMEOUT
        try:
            size = struct.unpack(">I", first + self._read_exact(4 - len(first), deadline))[0]
            if size > MAX_MESSAGE:
                raise ConnectionError("device sent an oversized message (%d bytes)" % size)
            source, destination, namespace, payload = decode(self._read_exact(size, deadline))
        except ValueError as error:
            raise ConnectionError("device sent a malformed message: %s" % error) from None
        finally:
            self.sock.settimeout(timeout)
        if namespace == NS_HEARTBEAT and payload.get("type") == "PING":
            self.send(source, NS_HEARTBEAT, {"type": "PONG"})
        return source, namespace, payload

    def _read_exact(self, n, deadline):
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionError("device sent an incomplete message")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                raise ConnectionError("device sent an incomplete message") from None
            if not chunk:
                raise ConnectionError("device closed the connection")
            buf += chunk
        return bytes(buf)

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                _, namespace, payload = self.recv()
            except socket.timeout:
                break
            if predicate(namespace, payload):
                return payload
        raise TimeoutError("device did not answer in time")

    def receiver_status(self):
        rid = self.send(RECEIVER, NS_RECEIVER, {"type": "GET_STATUS"})
        return self.wait_for(lambda ns, p: ns == NS_RECEIVER and p.get("requestId") == rid)["status"]

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def find_app(status, app_id=None):
    for app in status.get("applications") or []:
        if app_id is None or app.get("appId") == app_id:
            return app
    return None


def cmd_status(args):
    cast = Cast(args.host)
    status = cast.receiver_status()
    app = find_app(status)
    print(json.dumps({
        "volume": (status.get("volume") or {}).get("level"),
        "app": app.get("displayName") if app else None,
        "appId": app.get("appId") if app else None,
    }))
    cast.close()


def cmd_stop(args):
    cast = Cast(args.host)
    app = find_app(cast.receiver_status(), DEFAULT_MEDIA_RECEIVER)
    if app:
        cast.send(RECEIVER, NS_RECEIVER, {"type": "STOP", "sessionId": app["sessionId"]})
    cast.close()


def cmd_play(args):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    cast = Cast(args.host)

    rid = cast.send(RECEIVER, NS_RECEIVER, {"type": "LAUNCH", "appId": DEFAULT_MEDIA_RECEIVER})
    app = None
    while app is None:
        payload = cast.wait_for(lambda ns, p: ns == NS_RECEIVER and p.get("type") in ("RECEIVER_STATUS", "LAUNCH_ERROR"),
                                timeout=deadline - time.monotonic())
        if payload.get("type") == "LAUNCH_ERROR":
            raise RuntimeError("launch failed: %s" % payload.get("reason"))
        app = find_app(payload.get("status") or {}, DEFAULT_MEDIA_RECEIVER)

    session_id = app.get("sessionId")
    transport = app.get("transportId")
    if not session_id or not transport:
        raise RuntimeError("launch failed: the device sent no session")
    cast.connect(transport)

    def stop(*_):
        try:
            cast.send(RECEIVER, NS_RECEIVER, {"type": "STOP", "sessionId": session_id})
        except OSError:
            pass
        cast.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    rid = cast.send(transport, NS_MEDIA, {
        "type": "LOAD",
        "sessionId": session_id,
        "autoplay": True,
        "currentTime": 0,
        "media": dict({
            "contentId": args.url,
            "contentUrl": args.url,
            "streamType": "LIVE",
            "contentType": args.content_type,
            "metadata": {"metadataType": 0, "title": args.title},
        }, **({"hlsSegmentFormat": "ts"} if "mpegurl" in args.content_type.lower() else {})),
    })
    loaded = cast.wait_for(
        lambda ns, p: ns == NS_MEDIA and p.get("requestId") == rid and p.get("type") in ("MEDIA_STATUS", "LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST"),
        timeout=deadline - time.monotonic(),
    )
    if loaded.get("type") != "MEDIA_STATUS":
        raise RuntimeError("device refused the stream: %s" % loaded.get("type"))
    print("playing", flush=True)

    # Stay attached: answer heartbeats and leave when the receiver stops playing.
    last_ping = time.monotonic()
    last_state = None
    cast.sock.settimeout(1)
    while True:
        if time.monotonic() - last_ping > 5:
            cast.send(RECEIVER, NS_HEARTBEAT, {"type": "PING"})
            last_ping = time.monotonic()
        try:
            _, namespace, payload = cast.recv()
        except socket.timeout:
            continue
        except OSError as error:
            print("device disconnected: %s" % error, flush=True)
            return
        if namespace == NS_RECEIVER and payload.get("type") == "RECEIVER_STATUS":
            current = find_app(payload.get("status") or {})
            if not current or current.get("sessionId") != session_id:
                print("receiver closed the cast session", flush=True)
                return
        if namespace == NS_MEDIA and payload.get("type") == "MEDIA_STATUS":
            for status in payload.get("status") or []:
                state = status.get("playerState")
                if state and state != last_state:
                    print("receiver: %s" % state.lower(), flush=True)  # BUFFERING here means a stall
                    last_state = state
                if status.get("playerState") == "IDLE" and status.get("idleReason") in ("CANCELLED", "INTERRUPTED", "ERROR"):
                    print("playback ended: %s" % status.get("idleReason"), flush=True)
                    return


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "stop"):
        sub.add_parser(name).add_argument("host")
    play = sub.add_parser("play")
    play.add_argument("host")
    play.add_argument("url")
    play.add_argument("--title", default="Omarchy")
    play.add_argument("--content-type", default="application/x-mpegURL")
    args = parser.parse_args()
    try:
        {"status": cmd_status, "stop": cmd_stop, "play": cmd_play}[args.command](args)
    except (OSError, TimeoutError, RuntimeError, ConnectionError) as error:
        print("chromecast: %s" % error, file=sys.stderr)
        sys.exit(1)
    except (KeyError, TypeError, AttributeError) as error:
        # Replies come from whatever answered on the network; don't trust their shape.
        print("chromecast: unexpected reply from the device (%r)" % error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
