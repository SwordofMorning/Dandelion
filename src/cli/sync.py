##
 # @file src/cli/sync.py
 # @date 2026/10/27
 #
 # @brief Manual session snapshot upload client.
 # Reads history.log, meta.log and optional task_state.json from local sessions,
 # uploads bounded chunks to Dandelion Remote, and verifies commit receipt.
 #
 # @note Only explicit CLI sync commands initiate transfers. The client neither
 # changes local session files nor retries automatically in the background.
 #

import base64
import hashlib
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests

CHUNK_SIZE = 256 * 1024
MAX_REQUEST_SIZE = 1024 * 1024
REQUIRED_SESSION_FILES = ("history.log", "meta.log")
OPTIONAL_SESSION_FILES = ("task_state.json",)
ALL_SESSION_FILES = REQUIRED_SESSION_FILES + OPTIONAL_SESSION_FILES


##
 # @brief Upload session snapshots to Dandelion Remote without changing local state.
 #
class SessionSync:
    ##
     # @brief Store the backup source and explicit Remote configuration.
     #
     # @param log_dir SessionManager log directory.
     # @param workspace_dir Project workspace, used to identify the project.
     # @param remote Configured name, url, token and device_id mapping.
     #
     # @return None.
     #
    def __init__(self, log_dir, workspace_dir, remote):
        self.log_dir = Path(log_dir).resolve()
        workspace = Path(workspace_dir).resolve()
        suffix = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
        self.project = workspace.name[:160] + "-" + suffix
        self.remote = remote if isinstance(remote, dict) else {}
    # End-def

    ##
     # @brief Validate connection settings without consulting environment defaults.
     #
     # @return Remote URL and device identity, or raise ValueError.
     #
    def _connection(self):
        url = self.remote.get("url", "")
        access_key = self.remote.get("access_key") or self.remote.get("token") or ""
        device = self.remote.get("device_id", "")
        missing = [
            label for label, val in (("URL", url), ("ACCESS_KEY", access_key), ("DEVICE_ID", device))
            if not isinstance(val, str) or not val.strip()
        ]
        if missing:
            raise ValueError(
                f"Remote configuration incomplete (missing {', '.join(missing)} in [Remote])."
            )
        # End-if
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        local = host == "localhost"
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        # End-try
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or (
                not host or parsed.username is not None or parsed.password is not None
                or "?" in url or "#" in url):
            raise ValueError("Remote URL must use HTTPS, or HTTP on loopback, without embedded credentials.")
        # End-if
        parsed.port
        return url.rstrip("/"), device.strip()
    # End-def

    ##
     # @brief Read session files and derive a deterministic snapshot manifest.
     #
     # @param session_id Exact session directory ID, never a path.
     # @param device Configured device identity.
     #
     # @return Identity and manifest payload, plus fixed file bytes.
     #
    def _snapshot(self, session_id, device):
        if (not isinstance(session_id, str) or not session_id or
                Path(session_id).name != session_id or session_id in (".", "..") or
                "/" in session_id or "\\" in session_id):
            raise ValueError(f"Invalid session ID: {session_id!r}")
        # End-if
        directory = (self.log_dir / session_id).resolve()
        if directory.parent != self.log_dir or not directory.is_dir():
            raise ValueError(f"Session directory '{session_id}' does not exist.")
        # End-if

        content = {}
        files = {}
        for name in REQUIRED_SESSION_FILES:
            path = directory / name
            if not path.is_file() or path.resolve().parent != directory:
                raise ValueError(f"Session '{session_id}' is missing required file '{name}'.")
            # End-if
            raw = path.read_bytes()
            content[name] = raw
            files[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        # End-for

        for name in OPTIONAL_SESSION_FILES:
            path = directory / name
            if path.is_file() and path.resolve().parent == directory:
                raw = path.read_bytes()
                content[name] = raw
                files[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            # End-if
        # End-for

        try:
            history_obj = json.loads(content["history.log"].decode("utf-8"))
            meta_obj = json.loads(content["meta.log"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"Session '{session_id}' contains malformed JSON: {exc}") from None
        # End-try
        if not isinstance(history_obj, list) or not isinstance(meta_obj, dict) or meta_obj.get("id") != session_id:
            raise ValueError(f"Session '{session_id}' history.log or meta.log failed structure validation.")
        # End-if

        manifest = {"device": device, "project": self.project, "session_id": session_id, "files": files}
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        manifest["snapshot_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return manifest, content
    # End-def

    ##
     # @brief Send a bounded UTF-8 JSON request without following redirects.
     #
     # @param url Configured Remote base URL.
     # @param payload One file chunk or a final commit request.
     #
     # @return Decoded response object, or raise ValueError on protocol failure.
     #
    def _post(self, url, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) >= MAX_REQUEST_SIZE:
            raise ValueError("Session backup request exceeds maximum payload size.")
        # End-if
        raw_key = str(self.remote.get("access_key") or self.remote.get("token") or "").strip()
        key_md5 = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
        response = requests.post(
            url + "/sync",
            data=body,
            headers={"Authorization": "Bearer " + key_md5, "Content-Type": "application/json"},
            timeout=(5, 30),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise ValueError(f"Remote sync returned HTTP {response.status_code}; local session files are unchanged.")
        # End-if
        reply = response.json()
        if not isinstance(reply, dict):
            raise ValueError("Remote returned an invalid sync acknowledgement.")
        # End-if
        return reply
    # End-def

    ##
     # @brief Manually upload the selected session IDs to Dandelion Remote.
     #
     # @param session_ids List of exact session directory IDs to upload.
     #
     # @return Counts of committed sessions/files, skipped warnings, and any error.
     #
    def sync(self, session_ids):
        result = {"sessions": 0, "files": 0, "skipped": [], "error": None}
        try:
            url, device = self._connection()
            if not isinstance(session_ids, (list, tuple)):
                raise ValueError("Session IDs must be a list.")
            # End-if
            seen = set()
            for session_id in session_ids:
                if not isinstance(session_id, str) or session_id in seen:
                    continue
                # End-if
                seen.add(session_id)
                try:
                    manifest, content = self._snapshot(session_id, device)
                except (OSError, ValueError) as snap_err:
                    if len(session_ids) == 1:
                        raise
                    # End-if
                    result["skipped"].append(f"{session_id}: {snap_err}")
                    continue
                # End-try

                try:
                    probe = self._post(url, dict(manifest, check=True))
                except ValueError:
                    probe = {}
                # End-try
                if probe.get("unchanged") is True and probe.get("accepted") == manifest["snapshot_id"]:
                    result["skipped"].append(f"{session_id}: unchanged (skipped)")
                    continue
                # End-if

                for name, raw in content.items():
                    for offset in range(0, max(1, len(raw)), CHUNK_SIZE):
                        chunk = raw[offset:offset + CHUNK_SIZE]
                        payload = dict(
                            manifest,
                            file=name,
                            offset=offset,
                            data=base64.b64encode(chunk).decode("ascii"),
                        )
                        reply = self._post(url, payload)
                        received = reply.get("received")
                        if type(received) is not int or received != offset + len(chunk):
                            raise ValueError("Remote did not acknowledge the uploaded file chunk.")
                        # End-if
                    # End-for
                # End-for
                reply = self._post(url, dict(manifest, commit=True))
                if reply.get("accepted") != manifest["snapshot_id"]:
                    raise ValueError("Remote did not commit the session snapshot.")
                # End-if
                result["sessions"] += 1
                result["files"] += len(content)
            # End-for
        except requests.RequestException:
            result["error"] = "Remote sync failed; local session files are unchanged. Run sync again to retry."
        except OSError:
            result["error"] = "Cannot read session backup files; local session files are unchanged."
        except (ValueError, TypeError) as exc:
            result["error"] = str(exc)
        # End-try
        return result
    # End-def
# End-class
