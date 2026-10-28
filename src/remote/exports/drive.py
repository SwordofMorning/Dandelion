##
 # @file src/remote/exports/drive.py
 # @date 2026/10/27
 #
 # @brief Explicit Google Docs daily report export from synced sessions.
 # Builds UTC daily reports from synced sessions in assets/syncs/, refreshes
 # OAuth credentials when configured, and creates or updates Drive documents.
 #

import datetime as dt
import hashlib
import json
import os
import uuid
from urllib.parse import quote

import requests
from ..storage.backups import BackupStore

DOC_TYPE = "application/vnd.google-apps.document"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"


##
 # @brief Wrap authenticated Google requests without exposing credentials.
 #
class _Google:
    ##
     # @brief Read explicitly configured access or refresh credentials.
     #
     # @return None.
     #
    def __init__(self):
        self.token = os.environ.get("DANDELION_DRIVE_ACCESS_TOKEN", "")
        self.refresh = {
            "client_id": os.environ.get("DANDELION_DRIVE_CLIENT_ID", ""),
            "client_secret": os.environ.get("DANDELION_DRIVE_CLIENT_SECRET", ""),
            "refresh_token": os.environ.get("DANDELION_DRIVE_REFRESH_TOKEN", ""),
            "grant_type": "refresh_token",
        }
        if not self.token:
            self._refresh()
        # End-if
    # End-def

    ##
     # @brief Exchange configured refresh credentials for an access token.
     #
     # @return None.
     #
    def _refresh(self):
        if not all(self.refresh.values()):
            raise ValueError("Set DANDELION_DRIVE_ACCESS_TOKEN or the Drive OAuth refresh credentials")
        # End-if
        try:
            response = requests.post(
                "https://oauth2.googleapis.com/token",
                data=self.refresh,
                timeout=(10, 60),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise RuntimeError("Google OAuth returned HTTP " + str(response.status_code))
            # End-if
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Google OAuth did not return an access token")
            # End-if
            self.token = token
        except requests.RequestException:
            raise RuntimeError("Google OAuth request failed") from None
        except (ValueError, TypeError, AttributeError):
            raise RuntimeError("Invalid Google OAuth response") from None
        # End-try
    # End-def

    ##
     # @brief Send a bounded authorized request and refresh once after HTTP 401.
     #
     # @param method HTTP verb.
     # @param url Fixed Google API endpoint.
     # @param kwargs Request parameters, headers and body.
     #
     # @return Decoded Google API response object.
     #
    def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(2):
            headers["Authorization"] = "Bearer " + self.token
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    timeout=(10, 60),
                    allow_redirects=False,
                    **kwargs,
                )
            except requests.RequestException:
                raise RuntimeError("Google Drive request failed") from None
            # End-try
            if response.status_code == 401 and attempt == 0 and all(self.refresh.values()):
                self._refresh()
                continue
            # End-if
            if not 200 <= response.status_code < 300:
                raise RuntimeError("Google Drive returned HTTP " + str(response.status_code))
            # End-if
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
                # End-if
                return result
            except (ValueError, TypeError):
                raise RuntimeError("Invalid Google Drive response") from None
            # End-try
        # End-for
        raise RuntimeError("Google Drive authorization failed")
    # End-def
# End-class


##
 # @brief Group synced sessions into deterministic UTC daily reports.
 #
 # @param sessions List of synced session metadata dictionaries from BackupStore.
 #
 # @return Iterator of (utc_day_str, report_text) pairs.
 #
def _reports(sessions):
    days = {}
    for session in sessions:
        synced_ts = float(session.get("synced_at") or 0.0)
        occurred = dt.datetime.fromtimestamp(synced_ts, tz=dt.timezone.utc)
        days.setdefault(occurred.date().isoformat(), []).append((occurred, session))
    # End-for
    for day, items in sorted(days.items()):
        parts = ["Dandelion synced session report - " + day + " (UTC)"]
        for occurred, session in sorted(items, key=lambda item: (item[0], item[1]["session_id"])):
            parts.append(
                "\n" + occurred.isoformat() + " | " + session["project"] + " | " + session["device"]
            )
            parts.append("Session: " + session["session_id"] + " (" + session.get("name", "") + ")")
            summary = session.get("summary") or ""
            task_state = session.get("task_state") or {}
            if summary:
                parts.append(summary)
            elif task_state.get("target"):
                parts.append("Target: " + str(task_state.get("target")))
                if task_state.get("completed"):
                    parts.append("Completed: " + json.dumps(task_state["completed"], ensure_ascii=False))
                # End-if
            else:
                parts.append("Synced at " + session.get("path", ""))
            # End-if
        # End-for
        yield day, "\n".join(parts) + "\n"
    # End-for
# End-def


##
 # @brief Build a UTF-8 multipart body for text-to-Docs conversion.
 #
 # @param metadata Drive file metadata.
 # @param text Complete daily report text.
 #
 # @return Encoded request body and content-type headers.
 #
def _multipart(metadata, text):
    boundary = "dandelion_" + uuid.uuid4().hex
    body = (
        "--" + boundary + "\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata, ensure_ascii=False)
        + "\r\n--" + boundary + "\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        + text + "\r\n--" + boundary + "--\r\n"
    ).encode("utf-8")
    return body, {"Content-Type": "multipart/related; boundary=" + boundary}
# End-def


##
 # @brief Explicitly create or update Google Docs for synced sessions in assets/syncs/.
 #
 # @param store Remote Store with persistent export mappings.
 # @param workspace_dir Optional workspace root containing assets/syncs/.
 #
 # @return Mapping containing the number of changed documents exported.
 #
def export_drive(store, workspace_dir=None):
    backups = BackupStore(store, workspace_dir=workspace_dir)
    reports = list(_reports(backups.list_sessions()))
    if not reports:
        return {"exported": 0}
    # End-if
    google = _Google()
    folder = os.environ.get("DANDELION_DRIVE_FOLDER_ID", "")
    with store.lock, store.conn:
        store.conn.executescript("""
            CREATE TABLE IF NOT EXISTS drive_reports (
                day TEXT PRIMARY KEY, file_id TEXT NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drive_export_state (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)
        store.conn.execute(
            "INSERT OR IGNORE INTO drive_export_state VALUES ('store_id',?)",
            (uuid.uuid4().hex,),
        )
        store_id = store.conn.execute(
            "SELECT value FROM drive_export_state WHERE name='store_id'"
        ).fetchone()[0]
    # End-with
    exported = 0
    for day, text in reports:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with store.lock:
            saved = store.conn.execute(
                "SELECT file_id,digest FROM drive_reports WHERE day=?",
                (day,),
            ).fetchone()
        # End-with
        if saved and saved["digest"] == digest:
            continue
        # End-if
        file_id = saved["file_id"] if saved else None
        if not file_id:
            query = (
                "trashed=false and appProperties has { key='dandelion_store' and value='"
                + store_id + "' } and appProperties has { key='dandelion_day' and value='"
                + day + "' }"
            )
            found = google.request("GET", DRIVE_API, params={
                "q": query,
                "fields": "files(id)",
                "pageSize": 1,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }).get("files", [])
            if found:
                file_id = found[0].get("id")
            # End-if
        # End-if
        metadata = {
            "name": "Dandelion " + day,
            "mimeType": DOC_TYPE,
            "appProperties": {"dandelion_store": store_id, "dandelion_day": day},
        }
        if folder and not file_id:
            metadata["parents"] = [folder]
        # End-if
        body, headers = _multipart(metadata, text)
        result = google.request(
            "PATCH" if file_id else "POST",
            UPLOAD_API + ("/" + quote(file_id, safe="") if file_id else ""),
            params={"uploadType": "multipart", "fields": "id", "supportsAllDrives": "true"},
            headers=headers,
            data=body,
        )
        file_id = result.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise RuntimeError("Google Drive did not return a document ID")
        # End-if
        with store.lock, store.conn:
            store.conn.execute(
                "INSERT INTO drive_reports VALUES (?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET file_id=excluded.file_id,digest=excluded.digest",
                (day, file_id, digest),
            )
        # End-with
        exported += 1
    # End-for
    return {"exported": exported}
# End-def
