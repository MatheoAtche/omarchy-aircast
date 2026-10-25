#!/usr/bin/env python3
"""Serve a live HLS directory to cast receivers.

  hls_server.py <directory> <port>

Listens on every IPv4 and IPv6 address. Chromecast's media receiver fetches
the playlist with XHR, so responses carry CORS headers; nothing is cached
because the playlist changes every segment.
"""

import functools
import http.server
import socket
import socketserver
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".m3u8": "application/vnd.apple.mpegurl",
        ".ts": "video/mp2t",
    }

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


def main():
    directory, port = sys.argv[1], int(sys.argv[2])
    handler = functools.partial(Handler, directory=directory)
    DualStackServer(("::", port), handler).serve_forever()


if __name__ == "__main__":
    main()
