#!/usr/bin/env python3
"""Relay a fragmented MP4 from stdin to cast receivers as one endless HTTP response.

  <encoder> | live_server.py <port> <ready-file> <token>

This is the low-latency path: the receiver plays fragments as they arrive
(~0.1 s each, frag_duration) instead of waiting for several finished HLS
segments. A new client gets the init segment (ftyp+moov) and then joins at the
next fragment that opens on a keyframe, so it starts as close to live as
possible instead of replaying the current GOP. <ready-file> is created once the
first keyframe fragment is available. The stream is only served at
/<token>/live.mp4, a random path made for each cast, so devices and web pages
that weren't given the URL can't watch.
"""

import http.server
import queue
import socket
import socketserver
import struct
import sys
import threading

init_segment = b""
clients = set()  # mailboxes of clients that are streaming
joining = set()  # mailboxes waiting for the next keyframe fragment
lock = threading.Lock()
finished = threading.Event()

NON_SYNC = 0x00010000  # sample_is_non_sync_sample in ISO BMFF sample flags
MAX_CONNECTIONS = 16  # a receiver needs one or two; this bounds threads and memory

slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


def children(box, header=8):
    """Yield (type, payload) for the boxes nested in a container box."""
    i = header
    while i + 8 <= len(box):
        size, kind = struct.unpack(">I4s", box[i:i + 8])
        if size < 8:
            return
        yield kind, box[i + 8:i + size]
        i += size


def video_track_and_trex(moov):
    """Find the video track id and its default sample flags from mvex/trex."""
    video, trex_flags = None, {}
    for kind, payload in children(moov):
        if kind == b"trak":
            track_id, handler = None, None
            for k, p in children(payload, 0):
                if k == b"tkhd":
                    track_id = struct.unpack(">I", p[12:16] if p[0] == 0 else p[20:24])[0]
                elif k == b"mdia":
                    for k2, p2 in children(p, 0):
                        if k2 == b"hdlr":
                            handler = p2[8:12]
            if handler == b"vide":
                video = track_id
        elif kind == b"mvex":
            for k, p in children(payload, 0):
                if k == b"trex":
                    track_id, _, _, _, flags = struct.unpack(">IIIII", p[4:24])
                    trex_flags[track_id] = flags
    return video, trex_flags.get(video, 0)


def starts_with_keyframe(moof, video_track, trex_default):
    for kind, traf in children(moof):
        if kind != b"traf":
            continue
        track, default = None, trex_default
        for k, p in children(traf, 0):
            if k == b"tfhd":
                flags = struct.unpack(">I", p[:4])[0] & 0xFFFFFF
                track = struct.unpack(">I", p[4:8])[0]
                if track != video_track:
                    break
                offset = 8
                for bit, size in ((0x01, 8), (0x02, 4), (0x08, 4), (0x10, 4)):
                    if flags & bit:
                        offset += size
                if flags & 0x20:
                    default = struct.unpack(">I", p[offset:offset + 4])[0]
            elif k == b"trun" and track == video_track:
                flags = struct.unpack(">I", p[:4])[0] & 0xFFFFFF
                offset = 8 + (4 if flags & 0x01 else 0)
                if flags & 0x04:  # first_sample_flags
                    return not struct.unpack(">I", p[offset:offset + 4])[0] & NON_SYNC
                if flags & 0x400:  # per-sample flags; find the first sample's
                    offset += (4 if flags & 0x100 else 0) + (4 if flags & 0x200 else 0)
                    return not struct.unpack(">I", p[offset:offset + 4])[0] & NON_SYNC
                return not default & NON_SYNC
    return False


def read_boxes(stream):
    """Yield (type, bytes) for each top-level MP4 box on the stream."""
    buf = b""
    # gpu-screen-recorder can print its output path on stdout; skip to the first box.
    while b"ftyp" not in buf:
        chunk = stream.read1(65536)
        if not chunk:
            return
        buf += chunk
    buf = buf[buf.index(b"ftyp") - 4:]
    while True:
        while len(buf) < 8:
            chunk = stream.read1(65536)
            if not chunk:
                return
            buf += chunk
        size, kind = struct.unpack(">I4s", buf[:8])
        if size == 1:  # 64-bit size follows
            while len(buf) < 16:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                buf += chunk
            size = struct.unpack(">Q", buf[8:16])[0]
        if size < 8:
            return
        while len(buf) < size:
            chunk = stream.read1(max(65536, size - len(buf)))
            if not chunk:
                return
            buf += chunk
        yield kind, buf[:size]
        buf = buf[size:]


def deliver(mailbox, data):
    try:
        mailbox.put_nowait(data)
        return True
    except queue.Full:
        return False


def release(mailbox):
    """Free a client's backlog and wake its handler, which closes the connection."""
    while True:
        try:
            mailbox.get_nowait()
        except queue.Empty:
            break
    mailbox.put_nowait(None)


def pump(ready_file):
    global init_segment
    fragment = b""
    video_track, trex_default = None, 0
    ready = False
    for kind, box in read_boxes(sys.stdin.buffer):
        if kind == b"ftyp":
            init_segment = box
        elif kind == b"moov":
            init_segment += box
            video_track, trex_default = video_track_and_trex(box)
        elif kind == b"moof":
            fragment = box
        elif kind == b"mdat" and fragment:
            fragment += box
            # Audio-only streams can be joined at any fragment.
            keyframe = video_track is None or starts_with_keyframe(
                fragment[:len(fragment) - len(box)], video_track, trex_default)
            with lock:
                if keyframe:
                    clients.update(joining)
                    joining.clear()
                for client in list(clients):
                    if not deliver(client, fragment):
                        # Too slow: close it so it reconnects and joins live.
                        clients.discard(client)
                        release(client)
            if keyframe and not ready:
                open(ready_file, "w").close()
                ready = True
            fragment = b""
    finished.set()
    with lock:
        for client in clients | joining:
            release(client)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 10  # a client that stops reading this long is disconnected

    def headers_out(self):
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, no-store")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        if self.path.split("?")[0] != self.server.live_path:
            return self.send_error(404)
        self.send_response(200)
        self.headers_out()
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] != self.server.live_path or finished.is_set():
            return self.send_error(404)
        # Range requests are answered with the live stream from now on; there
        # is nothing to seek to.
        mailbox = queue.Queue(maxsize=100)  # ~10 s of 0.1 s fragments
        with lock:
            joining.add(mailbox)
        self.send_response(200)
        self.headers_out()
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(init_segment)
            while True:
                data = mailbox.get()
                if data is None:
                    break
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        finally:
            with lock:
                clients.discard(mailbox)
                joining.discard(mailbox)

    def log_message(self, fmt, *args):
        pass


class DualStackServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # HTTPServer.server_bind() reverse-resolves "::" via getfqdn(), which can
        # stall for seconds; the server name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "aircast", self.server_address[1]

    def process_request(self, request, client_address):
        # Refuse connections beyond what a receiver needs instead of starting
        # a thread for each.
        if not slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            slots.release()


def main():
    port, ready_file, token = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    server = DualStackServer(("::", port), Handler)
    server.live_path = "/%s/live.mp4" % token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pump(ready_file)  # returns when the encoder stops


if __name__ == "__main__":
    main()
