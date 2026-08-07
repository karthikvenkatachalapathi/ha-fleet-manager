from http.server import BaseHTTPRequestHandler, HTTPServer
import json, sys, time
TOKEN='mock-token'
class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code); self.send_header('content-type','application/json'); self.end_headers(); self.wfile.write(json.dumps(body).encode())
    def do_GET(self):
        if self.headers.get('Authorization') != f'Bearer {TOKEN}': return self._send(401, {'message':'unauthorized'})
        if self.path == '/api/config': return self._send(200, {'version':'2026.8.0','location_name':'Mock Lab'})
        if self.path == '/api/states': return self._send(200, [
            {'entity_id':'update.home_assistant_core_update','state':'on','attributes':{'title':'Home Assistant Core','installed_version':'2026.8.0','latest_version':'2026.8.1','release_url':'https://www.home-assistant.io/latest-release-notes/','in_progress':False}},
            {'entity_id':'update.esphome','state':'on','attributes':{'title':'ESPHome','installed_version':'2026.7.1','latest_version':'2026.8.0','release_url':'https://esphome.io/changelog/','in_progress':False}},
        ])
        return self._send(404, {'message':'not found'})
    def log_message(self, format, *args): pass
if __name__ == '__main__':
    port=int(sys.argv[1]) if len(sys.argv)>1 else 8891
    print(f'mock-ha listening {port}', flush=True)
    HTTPServer(('127.0.0.1', port), H).serve_forever()
