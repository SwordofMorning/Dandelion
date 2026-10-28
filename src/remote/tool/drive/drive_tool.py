##
 # @file src/remote/tool/drive/drive_tool.py
 # @date 2026/10/28
 #
 # @brief Sandboxed Google Drive tool for Dandelion Remote self-backup and file exchange.
 # Automatically detects or creates the top-level 'Dandelion Remote' folder on
 # Google Drive, confines all operations strictly within that folder, backs up
 # global memory, synced session data, and daily Google Docs reports, and
 # supports bidirectional file upload and download.
 #

import datetime as dt
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import uuid
from urllib.parse import quote

import requests
from src.core.tool.base_tool import BaseTool

FOLDER_MIME = "application/vnd.google-apps.folder"
DOC_MIME = "application/vnd.google-apps.document"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
ROOT_FOLDER_NAME = "Dandelion Remote"
SUBFOLDERS = ("reports", "memory", "data", "files")


##
 # @brief Escape single quotes and backslashes for Drive API query strings.
 #
 # @param text Raw name string.
 #
 # @return Escaped query literal.
 #
def _escape_q(text):
    return str(text).replace("\\", "\\\\").replace("'", "\\'")
# End-def


##
 # @brief Build a UTF-8 multipart/related request body for Google Drive uploads.
 #
 # @param metadata Drive file metadata dictionary.
 # @param raw_bytes Raw file content bytes.
 # @param content_type MIME type of the uploaded content payload.
 #
 # @return (body_bytes, headers_dict) tuple.
 #
def _multipart_bytes(metadata, raw_bytes, content_type="application/octet-stream"):
    boundary = "dandelion_" + uuid.uuid4().hex
    meta_part = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata, ensure_ascii=False)
        + f"\r\n--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    tail_part = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return meta_part + raw_bytes + tail_part, {
        "Content-Type": f"multipart/related; boundary={boundary}",
    }
# End-def


##
 # @brief Authenticated Google Drive v3 HTTP client with automatic OAuth token refresh.
 #
class _GoogleDriveClient:
    ##
     # @brief Initialize credentials from the [Drive] configuration dictionary.
     #
     # @param drive_cfg Optional [Drive] configuration dictionary from remote.cfg.
     #
     # @return None.
     #
    def __init__(self, drive_cfg=None):
        cfg = drive_cfg if isinstance(drive_cfg, dict) else {}
        self.token = (cfg.get("access_token") or os.environ.get("DANDELION_DRIVE_ACCESS_TOKEN", "")).strip()
        self.configured_root_id = (cfg.get("folder_id") or os.environ.get("DANDELION_DRIVE_FOLDER_ID", "")).strip()
        self.refresh = {
            "client_id": (cfg.get("client_id") or os.environ.get("DANDELION_DRIVE_CLIENT_ID", "")).strip(),
            "client_secret": (cfg.get("client_secret") or os.environ.get("DANDELION_DRIVE_CLIENT_SECRET", "")).strip(),
            "refresh_token": (cfg.get("refresh_token") or os.environ.get("DANDELION_DRIVE_REFRESH_TOKEN", "")).strip(),
            "grant_type": "refresh_token",
        }
        if not self.token:
            self._refresh()
        # End-if
    # End-def

    ##
     # @brief Exchange refresh credentials for a fresh access token.
     #
     # @return None.
     #
    def _refresh(self):
        if not all(self.refresh.values()):
            raise ValueError(
                "Google Drive is not configured; set ACCESS_TOKEN or (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN) in [Drive] of remote.cfg."
            )
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
     # @brief Execute an authorized Google Drive API request, refreshing once on HTTP 401.
     #
     # @param method HTTP verb.
     # @param url Target Google API URL.
     # @param raw_response True to return raw bytes instead of decoding JSON.
     # @param kwargs Additional arguments forwarded to requests.request.
     #
     # @return Decoded JSON dictionary or raw bytes.
     #
    def request(self, method, url, raw_response=False, **kwargs):
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
                raise RuntimeError("Google Drive network request failed") from None
            # End-try
            if response.status_code == 401 and attempt == 0 and all(self.refresh.values()):
                self._refresh()
                continue
            # End-if
            if not 200 <= response.status_code < 300:
                raise RuntimeError("Google Drive returned HTTP " + str(response.status_code))
            # End-if
            if raw_response:
                return response.content
            # End-if
            try:
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
                # End-if
                return result
            except (ValueError, TypeError):
                raise RuntimeError("Invalid Google Drive JSON response") from None
            # End-try
        # End-for
        raise RuntimeError("Google Drive authorization failed")
    # End-def
# End-class


##
 # @brief Tool confining all Google Drive operations to the 'Dandelion Remote' folder.
 #
class GoogleDriveTool(BaseTool):
    ##
     # @brief Bind the tool to the server config, workspace and synced session store.
     #
     # @param config Loaded remote.cfg dictionary.
     # @param workspace_dir Server workspace root directory.
     # @param backups Optional BackupStore instance for reading synced sessions.
     # @param client_factory Optional callable returning a _GoogleDriveClient (for testing).
     #
     # @return None.
     #
    def __init__(self, config, workspace_dir, backups=None, client_factory=None):
        super().__init__(workspace_dir=workspace_dir)
        self.config = config if isinstance(config, dict) else {}
        self.backups = backups
        self._client_factory = client_factory
        self._cached_root_id = None
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "google_drive"
    # End-def

    ##
     # @brief Describe the tool capability and automatic backup policy to the model.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return (
            "Manage Dandelion's dedicated 'Dandelion Remote' folder on Google Drive. "
            "All operations are strictly confined inside 'Dandelion Remote/' (with subfolders: "
            "'reports/' for daily Google Docs summaries readable by Gemini App, 'memory/' for "
            "global memory self-backup, 'data/' for synced session summaries, and 'files/' for "
            "general uploads/downloads). Call action='backup' periodically or after digesting "
            "new session syncs to back up your memories, session summaries, and daily reports."
        )
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["backup", "restore_memory", "list", "upload", "download"],
                    "description": (
                        "'backup': self-backup global memory (memory/), synced session summaries (data/), "
                        "and daily Google Docs reports (reports/). "
                        "'restore_memory': restore global memory files from Drive memory/ to local llm/memory/. "
                        "'list': list files inside 'Dandelion Remote/<remote_path>'. "
                        "'upload': upload a local file to 'Dandelion Remote/<remote_path>'. "
                        "'download': download 'Dandelion Remote/<remote_path>' to a local path."
                    ),
                },
                "remote_path": {
                    "type": "string",
                    "description": "Relative path inside 'Dandelion Remote/' (e.g. 'files/notes.md', 'reports', or '' for root). '..' is forbidden.",
                },
                "local_path": {
                    "type": "string",
                    "description": "Local file path on the server for 'upload' or 'download'.",
                },
                "as_google_doc": {
                    "type": "boolean",
                    "description": "When action='upload', convert text/markdown into an online Google Doc (default: false).",
                },
            },
            "required": ["action"],
        }
    # End-def

    ##
     # @brief Obtain an authenticated Google Drive client instance.
     #
     # @return _GoogleDriveClient instance.
     #
    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        # End-if
        return _GoogleDriveClient(drive_cfg=self.config.get("DRIVE"))
    # End-def

    ##
     # @brief Validate and split a relative Drive path, rejecting any traversal attempts.
     #
     # @param remote_path Relative path under 'Dandelion Remote/'.
     #
     # @return List of validated path segment names.
     #
    @staticmethod
    def _split_safe_subpath(remote_path):
        raw = str(remote_path or "").strip().replace("\\", "/").strip("/")
        if not raw:
            return []
        # End-if
        parts = [p.strip() for p in raw.split("/")]
        for part in parts:
            if not part or part in (".", "..") or any(ord(ch) < 32 or ord(ch) == 127 for ch in part):
                raise ValueError(f"Invalid or forbidden Google Drive subpath: {remote_path!r}")
            # End-if
        # End-for
        return parts
    # End-def

    ##
     # @brief List all non-trashed children under parent_id across all result pages.
     #
     # @param client _GoogleDriveClient instance.
     # @param parent_id Parent Drive folder ID.
     # @param folder_only True to match only folders, False for non-folders, None for any.
     #
     # @return Complete list of child file metadata dictionaries.
     #
    @staticmethod
    def _list_children_all(client, parent_id, folder_only=None):
        q = f"'{_escape_q(parent_id)}' in parents and trashed = false"
        if folder_only is True:
            q += f" and mimeType = '{FOLDER_MIME}'"
        elif folder_only is False:
            q += f" and mimeType != '{FOLDER_MIME}'"
        # End-if
        items = []
        page_token = None
        while True:
            params = {
                "q": q,
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,appProperties)",
                "pageSize": 200,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            # End-if
            res = client.request("GET", DRIVE_API, params=params)
            items.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
            # End-if
        # End-while
        return items
    # End-def

    ##
     # @brief Find a child item by exact name and optional mimeType under parent_id.
     #
     # @param client _GoogleDriveClient instance.
     # @param parent_id Parent Drive folder ID.
     # @param name Exact child item name.
     # @param folder_only True to match only folders, False to match non-folders, None for any.
     #
     # @return File metadata dict (id, name, mimeType, appProperties), or None.
     #
    @staticmethod
    def _find_child(client, parent_id, name, folder_only=None):
        q = f"'{_escape_q(parent_id)}' in parents and name = '{_escape_q(name)}' and trashed = false"
        if folder_only is True:
            q += f" and mimeType = '{FOLDER_MIME}'"
        elif folder_only is False:
            q += f" and mimeType != '{FOLDER_MIME}'"
        # End-if
        res = client.request("GET", DRIVE_API, params={
            "q": q,
            "fields": "files(id,name,mimeType,size,modifiedTime,appProperties)",
            "pageSize": 10,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        })
        files = res.get("files", [])
        return files[0] if files else None
    # End-def

    ##
     # @brief Create a folder on Google Drive under an optional parent_id.
     #
     # @param client _GoogleDriveClient instance.
     # @param name Folder name.
     # @param parent_id Optional parent folder ID.
     # @param app_props Optional appProperties dictionary.
     #
     # @return Created folder ID string.
     #
    @staticmethod
    def _create_folder(client, name, parent_id=None, app_props=None):
        meta = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            meta["parents"] = [parent_id]
        # End-if
        if app_props:
            meta["appProperties"] = app_props
        # End-if
        res = client.request(
            "POST",
            DRIVE_API,
            params={"fields": "id", "supportsAllDrives": "true"},
            json=meta,
        )
        folder_id = res.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise RuntimeError(f"Failed to create Google Drive folder '{name}'")
        # End-if
        return folder_id
    # End-def

    ##
     # @brief Automatically detect or create the top-level 'Dandelion Remote' root folder.
     #
     # @param client _GoogleDriveClient instance.
     #
     # @return Folder ID of the 'Dandelion Remote' root directory.
     #
    def _ensure_root_folder(self, client):
        if self._cached_root_id:
            return self._cached_root_id
        # End-if
        if getattr(client, "configured_root_id", ""):
            self._cached_root_id = client.configured_root_id
            return self._cached_root_id
        # End-if

        # Search for an existing 'Dandelion Remote' folder on Drive.
        q = f"mimeType = '{FOLDER_MIME}' and name = '{_escape_q(ROOT_FOLDER_NAME)}' and trashed = false"
        found = client.request("GET", DRIVE_API, params={
            "q": q,
            "fields": "files(id,name)",
            "pageSize": 5,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }).get("files", [])
        if found and found[0].get("id"):
            self._cached_root_id = found[0]["id"]
            return self._cached_root_id
        # End-if

        # Create 'Dandelion Remote' automatically when absent.
        self._cached_root_id = self._create_folder(
            client,
            ROOT_FOLDER_NAME,
            parent_id=None,
            app_props={"dandelion_root": "true"},
        )
        return self._cached_root_id
    # End-def

    ##
     # @brief Ensure a folder path inside 'Dandelion Remote/' exists and return its folder ID.
     #
     # @param client _GoogleDriveClient instance.
     # @param parts List of folder names relative to 'Dandelion Remote/'.
     # @param create True to create missing intermediate folders; False to raise FileNotFoundError.
     #
     # @return Resolved folder ID.
     #
    def _resolve_folder_path(self, client, parts, create=True):
        curr_id = self._ensure_root_folder(client)
        for part in parts:
            child = self._find_child(client, curr_id, part, folder_only=True)
            if child is not None:
                curr_id = child["id"]
            elif create:
                curr_id = self._create_folder(client, part, parent_id=curr_id)
            else:
                raise FileNotFoundError(f"Drive folder not found: {'/'.join(parts)}")
            # End-if
        # End-for
        return curr_id
    # End-def

    ##
     # @brief Create or update a file inside a specific Drive parent folder if content changed.
     #
     # @param client _GoogleDriveClient instance.
     # @param parent_id Target parent folder ID (always inside 'Dandelion Remote/').
     # @param name File name on Drive.
     # @param raw_bytes File payload bytes.
     # @param content_type Payload MIME type.
     # @param target_mime Optional Drive target mimeType (e.g. DOC_MIME for Google Docs).
     #
     # @return (file_id, updated_bool) tuple.
     #
    def _upsert_file(self, client, parent_id, name, raw_bytes, content_type="text/plain; charset=UTF-8", target_mime=None):
        digest = hashlib.sha256(raw_bytes).hexdigest()
        existing = self._find_child(client, parent_id, name, folder_only=False)
        if existing is not None:
            props = existing.get("appProperties") or {}
            if props.get("sha256") == digest:
                return existing["id"], False
            # End-if
        # End-if

        meta = {
            "name": name,
            "appProperties": {"sha256": digest},
        }
        if target_mime:
            meta["mimeType"] = target_mime
        # End-if
        if existing is None:
            meta["parents"] = [parent_id]
        # End-if

        body, headers = _multipart_bytes(meta, raw_bytes, content_type=content_type)
        file_id = existing["id"] if existing else None
        res = client.request(
            "PATCH" if file_id else "POST",
            UPLOAD_API + ("/" + quote(file_id, safe="") if file_id else ""),
            params={"uploadType": "multipart", "fields": "id", "supportsAllDrives": "true"},
            headers=headers,
            data=body,
        )
        out_id = res.get("id")
        if not isinstance(out_id, str) or not out_id:
            raise RuntimeError(f"Google Drive did not return a file ID for '{name}'")
        # End-if
        return out_id, True
    # End-def

    ##
     # @brief Build deterministic UTC daily report strings from synced sessions.
     #
     # @param sessions List of synced session metadata dictionaries.
     #
     # @return List of (utc_day_str, report_text) tuples.
     #
    @staticmethod
    def _build_daily_reports(sessions):
        days = {}
        for session in sessions:
            synced_ts = float(session.get("synced_at") or 0.0)
            occurred = dt.datetime.fromtimestamp(synced_ts, tz=dt.timezone.utc)
            days.setdefault(occurred.date().isoformat(), []).append((occurred, session))
        # End-for
        reports = []
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
            reports.append((day, "\n".join(parts) + "\n"))
        # End-for
        return reports
    # End-def

    ##
     # @brief Perform full DR self-backup: memory/, data/, and reports/ inside 'Dandelion Remote/'.
     #
     # @param client _GoogleDriveClient instance.
     #
     # @return Summary dictionary of backed-up items.
     #
    def _action_backup(self, client):
        folder_ids = {
            sub: self._resolve_folder_path(client, [sub], create=True)
            for sub in SUBFOLDERS
        }
        stats = {"root_folder": ROOT_FOLDER_NAME, "memories_updated": 0, "data_updated": 0, "reports_updated": 0}

        # 1. Back up global memories (<workspace>/llm/memory/*.md -> Dandelion Remote/memory/)
        mem_dir = Path(self.workspace_dir) / "llm" / "memory"
        active_memory_files = []
        if mem_dir.is_dir():
            for md_file in sorted(mem_dir.glob("*.md")):
                if md_file.is_file():
                    active_memory_files.append(md_file.name)
                    _, changed = self._upsert_file(
                        client,
                        folder_ids["memory"],
                        md_file.name,
                        md_file.read_bytes(),
                        content_type="text/markdown; charset=UTF-8",
                    )
                    if changed:
                        stats["memories_updated"] += 1
                    # End-if
                # End-if
            # End-for
        # End-if
        # Persist the active memory manifest so forgotten memories are never restored.
        manifest_bytes = json.dumps({"active_files": active_memory_files}, ensure_ascii=True, sort_keys=True).encode("utf-8")
        self._upsert_file(
            client,
            folder_ids["memory"],
            "_manifest.json",
            manifest_bytes,
            content_type="application/json; charset=UTF-8",
        )

        # 2. Back up synced session summaries (<workspace>/assets/syncs/<session_id>/ -> Dandelion Remote/data/<session_id>/)
        syncs_dir = Path(self.workspace_dir) / "assets" / "syncs"
        if syncs_dir.is_dir():
            for sess_dir in sorted(syncs_dir.iterdir()):
                if not sess_dir.is_dir():
                    continue
                # End-if
                remote_sess_id = None
                for fname in ("summary.md", "task_state.json", "meta.log"):
                    fpath = sess_dir / fname
                    if not fpath.is_file():
                        continue
                    # End-if
                    if remote_sess_id is None:
                        remote_sess_id = self._resolve_folder_path(client, ["data", sess_dir.name], create=True)
                    # End-if
                    _, changed = self._upsert_file(
                        client,
                        remote_sess_id,
                        fname,
                        fpath.read_bytes(),
                        content_type="text/plain; charset=UTF-8",
                    )
                    if changed:
                        stats["data_updated"] += 1
                    # End-if
                # End-for
            # End-for
        # End-if

        # 3. Back up daily aggregated Google Docs reports (Dandelion Remote/reports/Dandelion YYYY-MM-DD)
        sessions = self.backups.list_sessions() if self.backups is not None else []
        for day, text in self._build_daily_reports(sessions):
            _, changed = self._upsert_file(
                client,
                folder_ids["reports"],
                "Dandelion " + day,
                text.encode("utf-8"),
                content_type="text/plain; charset=UTF-8",
                target_mime=DOC_MIME,
            )
            if changed:
                stats["reports_updated"] += 1
            # End-if
        # End-for
        return stats
    # End-def

    ##
     # @brief Restore global memory files from 'Dandelion Remote/memory/' to local llm/memory/.
     #
     # @param client _GoogleDriveClient instance.
     #
     # @return Summary dictionary of restored memory files.
     #
    def _action_restore_memory(self, client):
        mem_folder_id = self._resolve_folder_path(client, ["memory"], create=False)
        files = self._list_children_all(client, mem_folder_id, folder_only=False)
        by_name = {
            str(item.get("name", "")).strip(): item
            for item in files
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        }

        active_set = None
        if "_manifest.json" in by_name:
            try:
                m_raw = self._download_item_bytes(
                    client,
                    by_name["_manifest.json"]["id"],
                    by_name["_manifest.json"].get("mimeType", ""),
                )
                m_obj = json.loads(m_raw.decode("utf-8"))
                if isinstance(m_obj, dict) and isinstance(m_obj.get("active_files"), list):
                    active_set = {str(x) for x in m_obj["active_files"] if isinstance(x, str)}
                # End-if
            except Exception:
                active_set = None
            # End-try
        # End-if

        local_mem_dir = Path(self.workspace_dir) / "llm" / "memory"
        local_mem_dir.mkdir(parents=True, exist_ok=True)
        restored = []
        for name, item in sorted(by_name.items()):
            if not name.endswith(".md") or "/" in name or "\\" in name or name in (".", ".."):
                continue
            # End-if
            if active_set is not None and name not in active_set:
                continue
            # End-if
            raw = self._download_item_bytes(client, item["id"], item.get("mimeType", ""))
            dest = local_mem_dir / name
            tmp = dest.with_suffix(".md.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, dest)
            restored.append(name)
        # End-for

        if active_set is not None:
            for existing_md in local_mem_dir.glob("*.md"):
                if existing_md.name not in active_set:
                    try:
                        existing_md.unlink()
                    except OSError:
                        pass
                    # End-try
                # End-if
            # End-for
        # End-if
        return {"restored_files": restored, "count": len(restored)}
    # End-def

    ##
     # @brief List files and folders inside 'Dandelion Remote/<remote_path>'.
     #
     # @param client _GoogleDriveClient instance.
     # @param remote_path Relative folder path inside 'Dandelion Remote/'.
     #
     # @return Dictionary with folder path and item descriptors.
     #
    def _action_list(self, client, remote_path):
        parts = self._split_safe_subpath(remote_path)
        if not parts:
            # Ensure standard subfolders exist when listing root.
            for sub in SUBFOLDERS:
                self._resolve_folder_path(client, [sub], create=True)
            # End-for
        # End-if
        folder_id = self._resolve_folder_path(client, parts, create=False)
        files = self._list_children_all(client, folder_id, folder_only=None)
        items = [
            {
                "name": f.get("name"),
                "type": "folder" if f.get("mimeType") == FOLDER_MIME else f.get("mimeType"),
                "size": f.get("size"),
                "modifiedTime": f.get("modifiedTime"),
            }
            for f in files
        ]
        return {"path": f"{ROOT_FOLDER_NAME}/{'/'.join(parts)}".rstrip("/"), "items": items}
    # End-def

    ##
     # @brief Upload a local server file into 'Dandelion Remote/<remote_path>'.
     #
     # @param client _GoogleDriveClient instance.
     # @param local_path Source file path on the server.
     # @param remote_path Destination relative path inside 'Dandelion Remote/'.
     # @param as_google_doc Whether to convert the uploaded file into a Google Doc.
     #
     # @return Upload result dictionary.
     #
    def _action_upload(self, client, local_path, remote_path, as_google_doc=False):
        if not local_path:
            raise ValueError("'local_path' is required for action='upload'")
        # End-if
        src = Path(local_path)
        if not src.is_absolute():
            src = Path(self.workspace_dir) / src
        # End-if
        src = src.resolve()
        if not src.is_file():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        # End-if

        parts = self._split_safe_subpath(remote_path)
        if not parts:
            parts = ["files", src.name]
        elif len(parts) == 1 and parts[0] in SUBFOLDERS:
            parts = [parts[0], src.name]
        # End-if

        parent_id = self._resolve_folder_path(client, parts[:-1], create=True)
        target_name = parts[-1]
        guessed_mime, _ = mimetypes.guess_type(src.name)
        content_type = guessed_mime or "application/octet-stream"
        target_mime = DOC_MIME if as_google_doc else None
        file_id, updated = self._upsert_file(
            client,
            parent_id,
            target_name,
            src.read_bytes(),
            content_type=content_type,
            target_mime=target_mime,
        )
        return {
            "uploaded": f"{ROOT_FOLDER_NAME}/{'/'.join(parts)}",
            "file_id": file_id,
            "updated": updated,
        }
    # End-def

    ##
     # @brief Download a file or Google Doc from 'Dandelion Remote/<remote_path>' to local_path.
     #
     # @param client _GoogleDriveClient instance.
     # @param remote_path Source relative path inside 'Dandelion Remote/'.
     # @param local_path Destination file path on the server.
     #
     # @return Download result dictionary.
     #
    def _action_download(self, client, remote_path, local_path):
        parts = self._split_safe_subpath(remote_path)
        if not parts:
            raise ValueError("'remote_path' must specify a file inside 'Dandelion Remote/'")
        # End-if
        parent_id = self._resolve_folder_path(client, parts[:-1], create=False)
        item = self._find_child(client, parent_id, parts[-1], folder_only=False)
        if item is None:
            raise FileNotFoundError(f"File not found in '{ROOT_FOLDER_NAME}': {'/'.join(parts)}")
        # End-if

        raw = self._download_item_bytes(client, item["id"], item.get("mimeType", ""))
        dest = Path(local_path) if local_path else (Path(self.workspace_dir) / "assets" / "downloads" / parts[-1])
        if not dest.is_absolute():
            dest = Path(self.workspace_dir) / dest
        # End-if
        dest = dest.resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
        return {
            "downloaded": f"{ROOT_FOLDER_NAME}/{'/'.join(parts)}",
            "local_path": str(dest),
            "bytes": len(raw),
        }
    # End-def

    ##
     # @brief Download raw bytes for a Drive item, exporting Google Docs as plain text.
     #
     # @param client _GoogleDriveClient instance.
     # @param file_id Drive file ID.
     # @param mime_type Item mimeType.
     #
     # @return Downloaded bytes.
     #
    @staticmethod
    def _download_item_bytes(client, file_id, mime_type):
        safe_id = quote(str(file_id), safe="")
        if mime_type == DOC_MIME:
            return client.request(
                "GET",
                f"{DRIVE_API}/{safe_id}/export",
                raw_response=True,
                params={"mimeType": "text/plain"},
            )
        # End-if
        return client.request(
            "GET",
            f"{DRIVE_API}/{safe_id}",
            raw_response=True,
            params={"alt": "media", "supportsAllDrives": "true"},
        )
    # End-def

    ##
     # @brief Execute one validated Google Drive action within the 'Dandelion Remote' root.
     #
     # @param action One of: backup, restore_memory, list, upload, download.
     # @param remote_path Optional relative path inside 'Dandelion Remote/'.
     # @param local_path Optional local server path for upload/download.
     # @param as_google_doc Optional flag converting uploaded text into a Google Doc.
     #
     # @return (success_bool, json_or_error_string) tuple.
     #
    def execute(self, action, remote_path="", local_path="", as_google_doc=False):
        try:
            client = self._client()
            if action == "backup":
                result = self._action_backup(client)
            elif action == "restore_memory":
                result = self._action_restore_memory(client)
            elif action == "list":
                result = self._action_list(client, remote_path)
            elif action == "upload":
                result = self._action_upload(client, local_path, remote_path, bool(as_google_doc))
            elif action == "download":
                result = self._action_download(client, remote_path, local_path)
            else:
                return False, f"Unknown google_drive action: {action!r}"
            # End-if
            return True, json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return False, str(exc)
        # End-try
    # End-def
# End-class
