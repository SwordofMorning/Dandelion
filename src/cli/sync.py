##
 # @file src/cli/sync.py
 # @date 2026/10/27
 #
 # @brief Manual session snapshot upload client.
 # Reads fixed history.log and meta.log bytes from selected local sessions,
 # uploads bounded chunks, and verifies the Remote's final snapshot receipt.
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
SESSION_FILES = ("history.log", "meta.log")

##
 # @brief Upload fixed session snapshots without changing local session state.
 #
class SessionSync:
    ##
     # @brief Store the backup source and explicit Remote configuration.
     #
     # @param log_dir SessionManager log directory.
     # @param workspace_dir Project workspace, used only to identify the project.
     # @param remote Configured name, url, token and device_id mapping.
     #
     # @return None.
     #
    def __init__(self, log_dir, workspace_dir, remote):
        self.log_dir = Path(log_dir).resolve()
        workspace = Path(workspace_dir).resolve()
        suffix = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:12]
        self.project = workspace.name + "-" + suffix
        self.remote = remote if isinstance(remote, dict) else {}
    # End-def

    ##
     # @brief Validate connection settings without consulting environment defaults.
     #
     # @return Remote URL and device identity, or raise ValueError.
     #
    def _connection(self):
        url = self.remote.get("url", "")
        token = self.remote.get("token", "")
        device = self.remote.get("device_id", "")
        if not all(isinstance(value, str) and value.strip() for value in (url, token, device)):
            raise ValueError("Remote is not configured; set URL, TOKEN and DEVICE_ID in [Remote].")
        # End-if
        parsed = urlsplit(url)
        local = parsed.hostname == "localhost"
        try:
            local = local or ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            pass
        # End-try
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or (
                not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Remote URL must use HTTPS, or HTTP on loopback, without embedded credentials.")
        # End-if
        parsed.port
        return url.rstrip("/"), device
    # End-def

    ##
     # @brief Find valid session metadata without opening or switching a session.
     #
     # @return Session IDs corresponding to readable SessionManager metadata.
     #
    def _session_ids(self):
        result = []
        for path in sorted(self.log_dir.glob("*/meta.log")):
            try:
                metadata = json.loads(path.read_bytes())
                if isinstance(metadata, dict) and metadata.get("id") == path.parent.name:
                    result.append(metadata["id"])
                # End-if
            except (OSError, ValueError):
                continue
            # End-try
        # End-for
        return result
    # End-def

    ##
     # @brief Read both allowed files once and derive a stable snapshot identity.
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
            raise ValueError("Invalid session ID.")
        # End-if
        directory = (self.log_dir / session_id).resolve()
        if directory.parent != self.log_dir:
            raise ValueError("Session directory is outside the log directory.")
        # End-if
        content = {}
        files = {}
        for name in SESSION_FILES:
            path = directory / name
            if path.resolve().parent != directory:
                raise ValueError("Session file is outside its session directory.")
            # End-if
            content[name] = path.read_bytes()
            files[name] = {"size": len(content[name]), "sha256": hashlib.sha256(content[name]).hexdigest()}
        # End-for
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
            raise ValueError("Session identity is too large for a backup request.")
        # End-if
        response = requests.post(
            url + "/sync", data=body,
            headers={"Authorization": "Bearer " + self.remote["token"], "Content-Type": "application/json"},
            timeout=(5, 30), allow_redirects=False,
        )
        if response.status_code != 200:
            raise ValueError(f"Remote sync returned HTTP {response.status_code}; local session files are unchanged.")
        # End-if
        reply = response.json()
        if not isinstance(reply, dict):
            raise ValueError("Remote returned an invalid backup acknowledgement.")
        # End-if
        return reply
    # End-def

    ##
     # @brief Manually upload all listed sessions or an explicit set of IDs.
     #
     # @param session_ids Exact IDs, or None for every valid session directory.
     #
     # @return Counts of committed sessions/files and a safe error, if any.
     #
    def sync(self, session_ids=None):
        result = {"sessions": 0, "files": 0, "error": None}
        try:
            url, device = self._connection()
            selected = self._session_ids() if session_ids is None else session_ids
            if not isinstance(selected, (list, tuple)):
                raise ValueError("Session IDs must be a list.")
            # End-if
            seen = set()
            for session_id in selected:
                if not isinstance(session_id, str):
                    raise ValueError("Invalid session ID.")
                # End-if
                if session_id in seen:
                    continue
                # End-if
                seen.add(session_id)
                manifest, content = self._snapshot(session_id, device)
                for name in SESSION_FILES:
                    raw = content[name]
                    for offset in range(0, max(1, len(raw)), CHUNK_SIZE):
                        chunk = raw[offset:offset + CHUNK_SIZE]
                        payload = dict(manifest, file=name, offset=offset,
                                       data=base64.b64encode(chunk).decode("ascii"))
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
                result["files"] += len(SESSION_FILES)
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
