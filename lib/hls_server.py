#!/usr/bin/env python3
"""Serve a live HLS directory to cast receivers.

  hls_server.py <directory> <port> <token>

Listens on every IPv4 and IPv6 address. Files are only served under
/<token>/, a random path made for each cast, so devices and web pages that
weren't given the URL can't watch. Chromecast's media receiver fetches the
playlist with XHR, so responses carry CORS headers; nothing is cached because
the playlist changes every segment.
"""

import functools
import http.server
import socket
import socketserver
import sys
import threading

MAX_CONNECTIONS = 16  # a receiver needs a few; this bounds threads

slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


class Handler(http.server.SimpleHTTPRequestHandler):
    timeout = 10  # drop connections that stall instead of keeping a thread on them
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
    }

    def send_head(self):
        if not self.path.startswith(self.server.prefix):
            self.send_error(404)
            return None
        self.path = self.path[len(self.server.prefix) - 1:]
        return super().send_head()

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def list_directory(self, path):
        self.send_error(404)

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
    directory, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    handler = functools.partial(Handler, directory=directory)
    server = DualStackServer(("::", port), handler)
    server.prefix = "/%s/" % token
    server.serve_forever()


if __name__ == "__main__":
    main()
