"""Serves the face (jansky/face/) and streams Jansky's state to it.

Server-Sent Events over the standard library: the page opens /events and receives one JSON object per
line. Touches come back as POSTs: /tap, /quit (the Close button), /mute (a long press), and /todo/done,
/todo/show, /todo/hide for the list (JSON body). No extra dependencies.
"""
import json
import queue
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FACE_DIR = Path(__file__).parent / "face"


class EventBus:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.last = {}  # latest event per type, replayed to a page that (re)connects

    def publish(self, event_type, **data):
        # Named event_type, not kind: card payloads carry their own "kind" field.
        event = {"type": event_type, **data}
        if event_type in ("hello", "state", "timers", "todo", "muted"):
            self.last[event_type] = event
        line = json.dumps(event)
        with self.lock:
            for q in self.clients:
                q.put(line)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        for event in list(self.last.values()):
            q.put(json.dumps(event))
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


class Handler(SimpleHTTPRequestHandler):
    bus = None
    routes = {}  # POST path -> callable(body dict)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path != "/events":
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        q = self.bus.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    line = None
                self.wfile.write(f"data: {line}\n\n".encode() if line else b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.bus.unsubscribe(q)

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(size) or b"{}") if size else {}
        except ValueError:
            body = {}
        fn = self.routes.get(self.path)
        if fn:
            fn(body)
        self.send_response(204 if fn else 404)
        self.end_headers()


def serve(bus, routes, port):
    handler = type("FaceHandler", (Handler,), {"bus": bus, "routes": routes})
    server = ThreadingHTTPServer(("0.0.0.0", port), partial(handler, directory=str(FACE_DIR)))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
