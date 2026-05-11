import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Load .env from the project root (one level above this api/ folder)
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH)

if not os.environ.get("MONGO_URI"):
    print(f"CRITICAL: MONGO_URI not found in {_ENV_PATH}")
else:
    print("MONGO_URI loaded successfully from .env")

from hourly_alert import run_hourly_alert
from weekly import run_weekly_report
from main_tracker import run_main_tracker
from stocks_manager import (
    handle_get_stocks,
    handle_validate_symbol,
    handle_add_symbol,
    handle_remove_symbol,
)
from birthday_alert import run_birthday_alert


# ==========================================
# SERVERLESS HANDLER
# ==========================================
class handler(BaseHTTPRequestHandler):
    def _send_cors(self, status_code: int, content_type: str, body: bytes):
        """Send response with CORS headers."""
        self.send_response(status_code)
        self.send_header('Content-type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self._send_cors(204, 'text/plain', b'')

    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query = parse_qs(parsed_path.query)

        # ---------------------------------------------------------
        # ROUTE: /api/stocks/validate?symbol=XYZ
        # ---------------------------------------------------------
        if path.endswith('/stocks/validate'):
            symbol = query.get('symbol', [''])[0]
            try:
                status, ct, body = handle_validate_symbol(symbol)
                self._send_cors(status, ct, body)
            except Exception as e:
                self._send_cors(500, 'application/json',
                                json.dumps({'error': str(e)}).encode())
            return

        # ---------------------------------------------------------
        # ROUTE: GET /api/stocks
        # ---------------------------------------------------------
        if path.endswith('/stocks'):
            try:
                status, ct, body = handle_get_stocks()
                self._send_cors(status, ct, body)
            except Exception as e:
                self._send_cors(500, 'application/json',
                                json.dumps({'error': str(e)}).encode())
            return

        # ---------------------------------------------------------
        # ROUTE: /api/hourly_alert
        # ---------------------------------------------------------
        if path.endswith('/hourly_alert') or 'action=hourly' in parsed_path.query:
            try:
                resp_data = run_hourly_alert()
                self._send_cors(200, 'application/json',
                                json.dumps(resp_data, indent=2).encode('utf-8'))
            except Exception as e:
                self._send_cors(500, 'text/plain',
                                f"Error running hourly alert: {str(e)}".encode())
            return

        # ---------------------------------------------------------
        # ROUTE: /api/weekly
        # ---------------------------------------------------------
        if path.endswith('/weekly') or 'action=weekly' in parsed_path.query:
            try:
                resp_data = run_weekly_report()
                self._send_cors(200, 'application/json',
                                json.dumps(resp_data, indent=2).encode('utf-8'))
            except Exception as e:
                self._send_cors(500, 'text/plain',
                                f"Error running weekly report: {str(e)}".encode())
            return

        # ---------------------------------------------------------
        # ROUTE: /api/birthday_alert
        # ---------------------------------------------------------
        if path.endswith('/birthday_alert') or 'action=birthday_alert' in parsed_path.query:
            try:
                resp_data = run_birthday_alert()
                self._send_cors(200, 'application/json',
                                json.dumps(resp_data, indent=2).encode('utf-8'))
            except Exception as e:
                self._send_cors(500, 'text/plain',
                                f"Error running birthday alert: {str(e)}".encode())
            return

        # ---------------------------------------------------------
        # ROUTE: / (Main Tracker Logic)
        # ---------------------------------------------------------
        try:
            status_code, content_type, response_bytes = run_main_tracker()
            self._send_cors(status_code, content_type, response_bytes)
        except Exception as e:
            self._send_cors(500, 'text/plain',
                            f"Error running main tracker: {str(e)}".encode())
        return

    def do_POST(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        # ---------------------------------------------------------
        # ROUTE: POST /api/stocks  — add a symbol
        # ---------------------------------------------------------
        if path.endswith('/stocks'):
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw)
                symbol = data.get('symbol', '')
                status, ct, body = handle_add_symbol(symbol)
                self._send_cors(status, ct, body)
            except Exception as e:
                self._send_cors(500, 'application/json',
                                json.dumps({'error': str(e)}).encode())
            return

        self._send_cors(404, 'text/plain', b'Not found')

    def do_DELETE(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query = parse_qs(parsed_path.query)

        # ---------------------------------------------------------
        # ROUTE: DELETE /api/stocks?symbol=XYZ  — remove a symbol
        # ---------------------------------------------------------
        if path.endswith('/stocks'):
            try:
                symbol = query.get('symbol', [''])[0]
                status, ct, body = handle_remove_symbol(symbol)
                self._send_cors(status, ct, body)
            except Exception as e:
                self._send_cors(500, 'application/json',
                                json.dumps({'error': str(e)}).encode())
            return

        self._send_cors(404, 'text/plain', b'Not found')