"""Explicit, optional daily Google Docs export using the existing requests SDK."""

import datetime as dt
import hashlib
import json
import os
import uuid
from urllib.parse import quote

import requests


DOC_TYPE = "application/vnd.google-apps.document"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"


class _Google:
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

    def _refresh(self):
        if not all(self.refresh.values()):
            raise ValueError("Set DANDELION_DRIVE_ACCESS_TOKEN or the Drive OAuth refresh credentials")
        try:
            response = requests.post("https://oauth2.googleapis.com/token",
                                     data=self.refresh, timeout=(10, 60), allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError("Google OAuth returned HTTP " + str(response.status_code))
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Google OAuth did not return an access token")
            self.token = token
        except requests.RequestException:
            raise RuntimeError("Google OAuth request failed") from None
        except (ValueError, TypeError, AttributeError):
            raise RuntimeError("Invalid Google OAuth response") from None

    def request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(2):
            headers["Authorization"] = "Bearer " + self.token
            try:
                response = requests.request(method, url, headers=headers,
                                            timeout=(10, 60), allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise RuntimeError("Google Drive request failed") from None
            if response.status_code == 401 and attempt == 0 and all(self.refresh.values()):
                self._refresh()
                continue
            if not 200 <= response.status_code < 300:
                raise RuntimeError("Google Drive returned HTTP " + str(response.status_code))
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
                return result
            except (ValueError, TypeError):
                raise RuntimeError("Invalid Google Drive response") from None
        raise RuntimeError("Google Drive authorization failed")


def _reports(records):
    days = {}
    for record in records:
        occurred = dt.datetime.fromisoformat(record["occurred_at"].replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            raise ValueError("Drive export requires activity timestamps with timezones")
        occurred = occurred.astimezone(dt.timezone.utc)
        days.setdefault(occurred.date().isoformat(), []).append((occurred, record))
    for day, items in sorted(days.items()):
        parts = ["Dandelion activity report - " + day + " (UTC)"]
        for occurred, record in sorted(items, key=lambda item: (item[0], item[1]["event_id"])):
            parts.append("\n" + occurred.isoformat() + " | " + record["project"] + " | " + record["device"])
            parts.append("Session: " + record["session_id"])
            parts.append(record["summary"])
            if record.get("sources"):
                parts.append("Sources: " + json.dumps(record["sources"], ensure_ascii=False))
        yield day, "\n".join(parts) + "\n"


def _multipart(metadata, text):
    boundary = "dandelion_" + uuid.uuid4().hex
    body = (
        "--" + boundary + "\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata, ensure_ascii=False)
        + "\r\n--" + boundary + "\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        + text + "\r\n--" + boundary + "--\r\n"
    ).encode("utf-8")
    return body, {"Content-Type": "multipart/related; boundary=" + boundary}


def export_drive(store):
    """Create or replace one Google Doc per UTC day; retain IDs in SQLite."""
    reports = list(_reports(store.list_activities(limit=None)))
    if not reports:
        return {"exported": 0}
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
        store.conn.execute("INSERT OR IGNORE INTO drive_export_state VALUES ('store_id',?)",
                           (uuid.uuid4().hex,))
        store_id = store.conn.execute(
            "SELECT value FROM drive_export_state WHERE name='store_id'").fetchone()[0]
    exported = 0
    for day, text in reports:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with store.lock:
            saved = store.conn.execute("SELECT file_id,digest FROM drive_reports WHERE day=?",
                                       (day,)).fetchone()
        if saved and saved["digest"] == digest:
            continue
        file_id = saved["file_id"] if saved else None
        if not file_id:
            # A completed upload whose response was lost can be found on retry.
            query = ("trashed=false and appProperties has { key='dandelion_store' and value='"
                     + store_id + "' } and appProperties has { key='dandelion_day' and value='"
                     + day + "' }")
            found = google.request("GET", DRIVE_API, params={
                "q": query, "fields": "files(id)", "pageSize": 1,
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            }).get("files", [])
            if found:
                file_id = found[0].get("id")
        metadata = {"name": "Dandelion " + day, "mimeType": DOC_TYPE,
                    "appProperties": {"dandelion_store": store_id, "dandelion_day": day}}
        if folder and not file_id:
            metadata["parents"] = [folder]
        body, headers = _multipart(metadata, text)
        result = google.request("PATCH" if file_id else "POST",
                                UPLOAD_API + ("/" + quote(file_id, safe="") if file_id else ""),
                                params={"uploadType": "multipart", "fields": "id",
                                        "supportsAllDrives": "true"}, headers=headers, data=body)
        file_id = result.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise RuntimeError("Google Drive did not return a document ID")
        with store.lock, store.conn:
            store.conn.execute("INSERT INTO drive_reports VALUES (?,?,?) "
                               "ON CONFLICT(day) DO UPDATE SET file_id=excluded.file_id,digest=excluded.digest",
                               (day, file_id, digest))
        exported += 1
    return {"exported": exported}
