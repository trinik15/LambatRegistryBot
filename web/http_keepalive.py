# http_keepalive.py
import http.server
import socketserver
import os
import threading
import logging

logger = logging.getLogger(__name__)

PORT = int(os.environ.get('PORT', 10000))

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Pavia Registry Bot is running.")
    def log_message(self, format, *args):
        # Silence log messages
        pass

def run_http_server():
    try:
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            logger.info(f"HTTP keep-alive server listening on port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        # Never crash the bot if the keep-alive port is unavailable
        # (e.g. already in use, or PORT not set in the current environment).
        logger.warning(f"HTTP keep-alive server could not bind to port {PORT}: {e}")

def start_http_server():
    thread = threading.Thread(target=run_http_server, daemon=True)
    thread.start()
