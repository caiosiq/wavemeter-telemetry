import os
import http.server
import socketserver
import urllib.parse
import json

PORT = 5000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INBOX_DIR = os.path.join(BASE_DIR, "inbox")
OUTBOX_DIR = os.path.join(BASE_DIR, "outbox")

os.makedirs(INBOX_DIR, exist_ok=True)
os.makedirs(OUTBOX_DIR, exist_ok=True)

class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        """Mac sends parameters here (Upload to Inbox)"""
        if self.path == "/upload_params":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            # Save incoming parameters to Inbox
            filename = self.headers.get('X-Filename', 'params.txt')
            filepath = os.path.join(INBOX_DIR, filename)
            
            with open(filepath, 'wb') as f:
                f.write(post_data)
                
            print(f"[Lab B Server] Received new parameters: {filename}")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        """Mac requests telemetry list or downloads outbox files"""
        parsed_path = urllib.parse.urlparse(self.path)
        
        if parsed_path.path == "/list_outbox":
            # Return list of CSV files waiting in Outbox
            files = [f for f in os.listdir(OUTBOX_DIR) if f.endswith(".csv")]
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(files).encode('utf-8'))
            
        elif parsed_path.path.startswith("/download/"):
            # Stream a specific telemetry file to the Mac
            filename = parsed_path.path.split("/")[-1]
            filepath = os.path.join(OUTBOX_DIR, filename)
            if os.path.exists(filepath):
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.end_headers()
                with open(filepath, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy default logging, print clean custom status
        print(f"[HTTP] {self.client_address[0]} - {format%args}")

if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), BridgeHandler) as httpd:
        print(f"=== Lab B HTTP Bridge Server Active on port {PORT} ===")
        print(f"Inbox: {INBOX_DIR}")
        print(f"Outbox: {OUTBOX_DIR}")
        httpd.serve_forever()