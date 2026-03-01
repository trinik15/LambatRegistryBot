# http_keepalive.py
import http.server
import socketserver
import os
import threading

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
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()

def start_http_server():
    thread = threading.Thread(target=run_http_server, daemon=True)
    thread.start()
