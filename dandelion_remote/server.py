"""Small authenticated JSON API. Terminate public HTTPS at a reverse proxy."""

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import timestamp


MAX_BODY = 1024 * 1024


def validate_activity(record):
    if not isinstance(record, dict):
        raise ValueError("Each activity must be an object")
    for field in ("event_id", "device", "project", "session_id", "occurred_at", "summary"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Missing activity field: " + field)
        if len(value) > (20000 if field == "summary" else 1000):
            raise ValueError("Activity field too long: " + field)
    timestamp(record["occurred_at"])
    if not isinstance(record.get("sources"), list):
        raise ValueError("Activity sources must be an array")
    return record


def make_server(host, port, store, jobs, loop, hub, token, device_tokens=None):
    if not token:
        raise ValueError("Set DANDELION_DR_TOKEN before starting the server")
    device_tokens = device_tokens or {}
    if (not isinstance(device_tokens, dict)
            or any(not isinstance(k, str) or not k.strip()
                   or not isinstance(v, str) or not v for k, v in device_tokens.items())
            or len(set(device_tokens.values())) != len(device_tokens)
            or token in device_tokens.values()):
        raise ValueError("Device credentials must have unique nonempty names/tokens, separate from the admin token")

    class Handler(BaseHTTPRequestHandler):
        # Keep a slow client from holding a request worker forever.
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            logging.info("HTTP %s", fmt % args)

        def _reply(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self):
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if supplied and hmac.compare_digest(supplied.encode(), token.encode()):
                return "admin", None
            for device, credential in device_tokens.items():
                if supplied and hmac.compare_digest(supplied.encode(), credential.encode()):
                    return "device", device
            return None, None

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise ValueError("JSON body must be between 1 byte and 1 MiB")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            return data

        def _handle(self):
            url = urlsplit(self.path)
            path = url.path.rstrip("/") or "/"
            query = parse_qs(url.query)
            if self.command == "GET" and path == "/health":
                self._reply(200, {"status": "ok"})
                return
            role, device = self._identity()
            if role is None:
                self._reply(401, {"error": "Unauthorized"})
                return
            if role == "device" and not (self.command == "POST" and path == "/sync"):
                self._reply(403, {"error": "Device credentials only permit summary uploads"})
                return
            session = query.get("session_id", [""])[0]
            data = self._body() if self.command == "POST" else {}
            if self.command == "POST" and path == "/sync":
                records = data.get("records")
                if not isinstance(records, list) or len(records) > 100:
                    raise ValueError("Expected at most 100 activity records")
                for record in records:
                    validate_activity(record)
                    if device is not None and record["device"] != device:
                        self._reply(403, {"error": "Activity device does not match credential"})
                        return
                accepted = []
                for record in records:
                    store.ingest_activity(record)
                    accepted.append(record["event_id"])
                self._reply(200, {"accepted": accepted})
            elif self.command == "GET" and path == "/activities":
                self._reply(200, {"records": store.list_activities(limit=100)})
            elif self.command == "GET" and path == "/replies":
                if not session:
                    raise ValueError("session_id is required")
                self._reply(200, {"replies": hub.replies(session, int(query.get("after", [0])[0]))})
            elif self.command == "POST" and path == "/messages":
                session = self._session(data)
                text = data.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    raise ValueError("text must contain 1..20000 characters")
                event = loop.post("message", session, {"text": text})
                self._reply(202, {"event_id": event, "session_id": session})
            elif self.command == "POST" and path == "/cancel":
                loop.cancel(self._session(data))
                self._reply(202, {"accepted": True})
            elif self.command == "GET" and path == "/jobs":
                self._reply(200, {"jobs": store.list_jobs(session_id=session or None)})
            elif self.command == "GET" and path.startswith("/jobs/"):
                job = jobs.get(path.split("/")[-1], session_id=session or None)
                self._reply(200 if job else 404, job or {"error": "Job not found"})
            elif self.command == "DELETE" and path.startswith("/jobs/"):
                accepted = jobs.cancel(path.split("/")[-1], session_id=session or None)
                self._reply(200, {"accepted": accepted})
            elif self.command == "GET" and path == "/schedules":
                self._reply(200, {"schedules": store.list_schedules(session_id=session or None)})
            elif self.command == "POST" and path == "/schedules":
                schedule = store.create_schedule(
                    self._session(data), data["due_at"], {"text": data["text"]},
                    interval=data.get("interval"), kind=data.get("kind", "reminder"),
                )
                self._reply(201, schedule)
            elif self.command == "DELETE" and path.startswith("/schedules/"):
                self._reply(200, {"accepted": store.cancel_schedule(
                    path.split("/")[-1], session_id=session or None)})
            else:
                self._reply(404, {"error": "Unknown endpoint"})

        @staticmethod
        def _session(data):
            session = data.get("session_id")
            if not isinstance(session, str) or not session.strip() or len(session) > 200:
                raise ValueError("session_id must contain 1..200 characters")
            return session

        def _dispatch(self):
            try:
                self._handle()
            except (ValueError, KeyError, TypeError) as exc:
                self._reply(400, {"error": str(exc)})
            except (ConnectionError, OSError):
                logging.warning("HTTP connection closed")
            except Exception:
                logging.exception("Remote API request failed")
                self._reply(500, {"error": "Internal server error"})

        do_GET = do_POST = do_DELETE = _dispatch

    return ThreadingHTTPServer((host, port), Handler)
