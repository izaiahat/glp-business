#!/usr/bin/env python3
"""Only exact GET evidence; no filesystem routes or execution API."""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from evidence_projection import snapshot
ADDRESS='100.96.222.106'
PORT=18909
PATH='/evidence/codex.json'
# R1 operator explicitly requires this exact read-only fleet-status route.
PATHS=frozenset((PATH,'/v1/fleet-status.json'))
class Handler(BaseHTTPRequestHandler):
    server_version='Evidence/1'
    sys_version=''
    def log_message(self,*args):
        pass
    def respond(self,code,body):
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Connection','close')
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(body)
        self.close_connection=True
    def do_GET(self):
        if self.path not in PATHS or self.requestline.split()[1]!=self.path:
            return self.respond(404,b'{"error":"not found"}')
        self.respond(200,json.dumps(snapshot(),sort_keys=True).encode())
    def denied(self):
        self.respond(405,b'{"error":"GET only"}')
    do_HEAD=do_POST=do_PUT=do_PATCH=do_DELETE=do_OPTIONS=do_TRACE=do_CONNECT=denied
if __name__=='__main__':
    server=ThreadingHTTPServer((ADDRESS,PORT),Handler)
    server.daemon_threads=True
    server.serve_forever()
