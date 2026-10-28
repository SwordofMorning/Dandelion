##
 # @file src/remote/channels/http.py
 # @date 2026/10/27
 #
 # @brief Authenticated remote HTTP interface.
 # Dispatches conversation, job, schedule and session-sync requests, enforces
 # device/admin credential scopes, and triggers sync_received interrupts.
 #
 # @note Public HTTPS termination belongs to the deployment reverse proxy.
 #

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ..storage.backups import BackupStore

MAX_BODY = 1024 * 1024


##
 # @brief Create an authenticated API with bounded chunk uploads.
 #
 # @param host Local address on which the API listens.
 # @param port TCP port, or zero to allocate a local port.
 # @param store Shared durable store with its connection lock.
 # @param jobs Background job manager used by remote delegation tools.
 # @param loop Remote conversation event loop.
 # @param hub Persistent reply and channel-state hub.
 # @param token Administrative API credential.
 # @param device_tokens Device IDs mapped to sync-only credentials.
 # @param backups Optional shared BackupStore instance.
 #
 # @return Configured threaded HTTP server.
 #
def make_server(host, port, store, jobs, loop, hub, token, device_tokens=None, backups=None):
    if not token:
        raise ValueError("Set DANDELION_DR_TOKEN before starting the server")
    # End-if
    device_tokens = device_tokens or {}
    if (not isinstance(device_tokens, dict)
            or any(not isinstance(k, str) or not k.strip()
                   or not isinstance(v, str) or not v for k, v in device_tokens.items())
            or len(set(device_tokens.values())) != len(device_tokens)
            or token in device_tokens.values()):
        raise ValueError("Device credentials must have unique nonempty names/tokens, separate from the admin token")
    # End-if

    backups = backups if backups is not None else BackupStore(store)

    ##
     # @brief Handle JSON requests and authenticated backup downloads.
     #
    class Handler(BaseHTTPRequestHandler):
        ##
         # @brief Set a timeout for each client connection.
         #
         # @return None.
         #
        def setup(self):
            super().setup()
            self.connection.settimeout(15)
        # End-def

        ##
         # @brief Record access without authorization headers or request bodies.
         #
         # @param fmt HTTP access-log format.
         # @param args Access-log interpolation values.
         #
         # @return None.
         #
        def log_message(self, fmt, *args):
            logging.info("HTTP %s", fmt % args)
        # End-def

        ##
         # @brief Send one UTF-8 JSON response.
         #
         # @param code HTTP response status.
         # @param data JSON response payload.
         #
         # @return None.
         #
        def _reply(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        # End-def

        ##
         # @brief Match administrative or device-scoped bearer credentials.
         #
         # @return Authenticated role and optional device ID.
         #
        def _identity(self):
            header = self.headers.get("Authorization", "")
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if supplied and hmac.compare_digest(supplied.encode(), token.encode()):
                return "admin", None
            # End-if
            for device, credential in device_tokens.items():
                if supplied and hmac.compare_digest(supplied.encode(), credential.encode()):
                    return "device", device
                # End-if
            # End-for
            return None, None
        # End-def

        ##
         # @brief Read one bounded JSON request.
         #
         # @return Decoded request object.
         #
        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise ValueError("JSON body must be between 1 byte and 1 MiB")
            # End-if
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
            # End-if
            return data
        # End-def

        ##
         # @brief Authorize and dispatch one supported API request.
         #
         # @return None.
         #
        def _handle(self):
            url = urlsplit(self.path)
            path = url.path.rstrip("/") or "/"
            query = parse_qs(url.query)
            if self.command == "GET" and path == "/health":
                self._reply(200, {"status": "ok"})
                return
            # End-if
            role, device = self._identity()
            if role is None:
                self._reply(401, {"error": "Unauthorized"})
                return
            # End-if
            if role == "device" and not (self.command == "POST" and path == "/sync"):
                self._reply(403, {"error": "Device credentials only permit session backup uploads"})
                return
            # End-if
            session = query.get("session_id", [""])[0]
            data = self._body() if self.command == "POST" else {}
            if self.command == "POST" and path == "/sync":
                if device is not None and data.get("device") != device:
                    self._reply(403, {"error": "Backup device does not match credential"})
                    return
                # End-if
                result = backups.receive(data)
                if data.get("commit") is True and result.get("changed"):
                    target_session = getattr(hub, "default_telegram_session", None) or "default"
                    loop.post("sync_received", target_session, {
                        "session_id": result.get("session_id", data.get("session_id", "")),
                        "device": data.get("device", ""),
                        "project": data.get("project", ""),
                        "path": result.get("path", ""),
                        "snapshot_id": result.get("accepted", ""),
                    }, event_id="sync_" + str(result.get("accepted", "")))
                # End-if
                self._reply(200, result)
            elif self.command == "GET" and path == "/sessions":
                self._reply(200, {"sessions": backups.list_sessions(
                    device=query.get("device", [None])[0], project=query.get("project", [None])[0])})
            elif self.command == "GET" and path == "/replies":
                if not session:
                    raise ValueError("session_id is required")
                # End-if
                self._reply(200, {"replies": hub.replies(session, int(query.get("after", [0])[0]))})
            elif self.command == "POST" and path == "/messages":
                session = self._session(data)
                text = data.get("text")
                if not isinstance(text, str) or not text.strip() or len(text) > 20000:
                    raise ValueError("text must contain 1..20000 characters")
                # End-if
                event = loop.post("message", session, {"text": text})
                self._reply(202, {"event_id": event, "session_id": session})
            elif self.command == "POST" and path == "/cancel":
                accepted = loop.cancel(self._session(data))
                self._reply(202, {"accepted": accepted})
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
                    self._session(data),
                    data["due_at"],
                    {"text": data["text"]},
                    interval=data.get("interval"),
                    kind=data.get("kind", "reminder"),
                )
                self._reply(201, schedule)
            elif self.command == "DELETE" and path.startswith("/schedules/"):
                self._reply(200, {"accepted": store.cancel_schedule(
                    path.split("/")[-1], session_id=session or None)})
            else:
                self._reply(404, {"error": "Unknown endpoint"})
            # End-if
        # End-def

        ##
         # @brief Validate a remote conversation identifier.
         #
         # @param data JSON request payload.
         #
         # @return Validated conversation identifier.
         #
        @staticmethod
        def _session(data):
            session = data.get("session_id")
            if not isinstance(session, str) or not session.strip() or len(session) > 200:
                raise ValueError("session_id must contain 1..200 characters")
            # End-if
            return session
        # End-def

        ##
         # @brief Translate invalid requests and connection failures into responses.
         #
         # @return None.
         #
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
            # End-try
        # End-def

        do_GET = do_POST = do_DELETE = _dispatch
    # End-class

    return ThreadingHTTPServer((host, port), Handler)
# End-def
