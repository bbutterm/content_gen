"""SYNTHETIC UI fixture: loopback only, no backend/keys/Telegram."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/content-plan/12/video?workspace_id=1':
            data = (ROOT / 'tests/content_plan_ui_synthetic.mp4').read_bytes()
            self.send_response(200); self.send_header('Content-Type', 'video/mp4'); self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data); return
        if self.path != '/fixture':
            self.send_error(404); return
        html = (ROOT / 'static/index.html').read_text()
        script = (ROOT / 'tests/content_plan_fixture.js').read_text()
        html = html.replace('<body>', '<body><div style="padding:8px;text-align:center;background:#fdf1dc;color:#16181d">SYNTHETIC UI fixture · no real API, keys or Telegram</div>', 1)
        html = html.replace('<script>', '<script>' + script + '</script><script>', 1)
        self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
        self.wfile.write(html.encode())
    def log_message(self, *_): pass
if __name__ == '__main__': HTTPServer(('127.0.0.1', 18149), Handler).serve_forever()
