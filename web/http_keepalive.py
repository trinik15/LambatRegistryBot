# http_keepalive.py
"""Tiny HTTP server so host platforms (Render, Railway, etc.) don't mark the
bot as idle and shut it down. Responds 200 OK on any path."""

import http.server
import socketserver
import os
import threading
import logging

logger = logging.getLogger(__name__)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Lambat Registry Bot is running.")

    def log_message(self, format, *args):
        # Silence default request logging (keeps the bot log clean).
        pass


def run_http_server():
    # Read PORT at call time (not import time) so env changes take effect.
    port = int(os.environ.get('PORT', 10000))
    try:
        with socketserver.TCPServer(("", port), Handler) as httpd:
            logger.info(f"HTTP keep-alive server listening on port {port}")
            httpd.serve_forever()
    except Exception as e:
        # Never crash the bot if the keep-alive port is unavailable
        # (e.g. already in use, or PORT not set in the current environment).
        logger.warning(f"HTTP keep-alive server could not bind to port {port}: {e}")


def start_http_server():
    thread = threading.Thread(target=run_http_server, daemon=True)
    thread.start()
