##
 # @file src/remote/storage/backups.py
 # @date 2026/10/28
 #
 # @brief Pure-filesystem session sync storage under assets/syncs/<session_id>/.
 # Validates chunk offsets, file sizes and SHA-256 manifests, atomically writes
 # synced session files (history.log, meta.log, task_state.json) into readable
 # session directories, and provides summary digests for DR context and Drive export.
 #

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time

REQUIRED_FILES = ("history.log", "meta.log")
OPTIONAL_FILES = ("task_state.json",)
ALLOWED_FILES = REQUIRED_FILES + OPTIONAL_FILES
CHUNK_SIZE = 256 * 1024


##
 # @brief Store synced CLI sessions directly on disk under assets/syncs/<session_id>/.
 #
class BackupStore:
    ##
     # @brief Initialize staging and assets/syncs directories.
     #
     # @param data_dir Private remote-service data directory (or Store owning data_dir).
     # @param workspace_dir Optional workspace root where assets/syncs/ is placed.
     #
     # @return None.
     #
    def __init__(self, data_dir, workspace_dir=None):
        raw_data_dir = getattr(data_dir, "data_dir", data_dir)
        resolved_data = Path(raw_data_dir).resolve()
        base = Path(workspace_dir).resolve() if workspace_dir else resolved_data
        self.lock = threading.RLock()
        self.syncs_dir = base / "assets" / "syncs"
        self.staging_dir = resolved_data / "staging_syncs"
        self.syncs_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
    # End-def

    ##
     # @brief Validate file identities and recompute the snapshot digest.
     #
     # @param payload A chunk or final commit request dictionary.
     #
     # @return Canonical manifest dictionary with verified snapshot_id.
     #
    @staticmethod
    def _manifest(payload):
        manifest = {}
        for name in ("device", "project", "session_id"):
            value = payload.get(name)
            if (not isinstance(value, str) or not value.strip() or len(value) > 200
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
                raise ValueError("Invalid backup identity: " + name)
            # End-if
            manifest[name] = value
        # End-for
        session_id = manifest["session_id"]
        if (Path(session_id).name != session_id or session_id in (".", "..")
                or "/" in session_id or "\\" in session_id):
            raise ValueError("Invalid session_id path component")
        # End-if

        files = payload.get("files")
        if (not isinstance(files, dict)
                or not set(REQUIRED_FILES).issubset(files.keys())
                or not set(files.keys()).issubset(ALLOWED_FILES)):
            raise ValueError("Backups must contain history.log and meta.log (and optional task_state.json)")
        # End-if

        checked = {}
        for name in sorted(files.keys()):
            entry = files[name]
            if not isinstance(entry, dict) or set(entry) != {"size", "sha256"}:
                raise ValueError("Invalid backup file manifest")
            # End-if
            size, digest = entry["size"], entry["sha256"]
            if (type(size) is not int or size < 0 or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("Invalid backup size or digest")
            # End-if
            checked[name] = {"size": size, "sha256": digest}
        # End-for
        manifest["files"] = checked
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        snapshot_id = hashlib.sha256(encoded).hexdigest()
        if payload.get("snapshot_id") != snapshot_id:
            raise ValueError("Backup snapshot digest does not match its manifest")
        # End-if
        manifest["snapshot_id"] = snapshot_id
        return manifest
    # End-def

    ##
     # @brief Resolve the target directory for a session_id or snapshot_id.
     #
     # @param identifier Session ID (e.g. sess_...) or snapshot_id SHA-256.
     #
     # @return Path to the session directory in assets/syncs/, or None.
     #
    def _find_session_dir(self, identifier):
        if not isinstance(identifier, str) or not identifier.strip():
            return None
        # End-if
        identifier = identifier.strip()
        if (Path(identifier).name == identifier
                and identifier not in (".", "..")
                and "/" not in identifier and "\\" not in identifier):
            direct = (self.syncs_dir / identifier).resolve()
            if direct.parent == self.syncs_dir and (direct / "meta.log").is_file():
                return direct
            # End-if
        # End-if
        for sync_meta_path in self.syncs_dir.glob("*/sync_meta.json"):
            try:
                meta = json.loads(sync_meta_path.read_text(encoding="utf-8"))
                if meta.get("snapshot_id") == identifier or meta.get("session_id") == identifier:
                    return sync_meta_path.parent
                # End-if
            except (OSError, ValueError):
                continue
            # End-try
        # End-for
        return None
    # End-def

    ##
     # @brief Receive an idempotent chunk or commit validated files to assets/syncs/<session_id>/.
     #
     # @param payload JSON request with manifest and chunk or commit fields.
     #
     # @return Received byte offset, or committed snapshot metadata dictionary.
     #
    def receive(self, payload):
        manifest = self._manifest(payload)
        snapshot_id = manifest["snapshot_id"]
        session_id = manifest["session_id"]
        with self.lock:
            if payload.get("commit") is True:
                return self._commit(manifest)
            # End-if
            name = payload.get("file")
            offset = payload.get("offset")
            encoded = payload.get("data")
            if name not in manifest["files"] or type(offset) is not int or offset < 0:
                raise ValueError("Invalid backup filename or offset")
            # End-if
            if not isinstance(encoded, str) or len(encoded) > ((CHUNK_SIZE + 2) // 3) * 4:
                raise ValueError("Backup chunk is too large")
            # End-if
            try:
                chunk = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid backup chunk encoding") from None
            # End-try
            expected = manifest["files"][name]["size"]
            end = offset + len(chunk)
            if len(chunk) > CHUNK_SIZE or end > expected or (not chunk and expected != 0):
                raise ValueError("Backup chunk exceeds the declared file size")
            # End-if

            existing_dir = self._find_session_dir(snapshot_id)
            if existing_dir is not None:
                target = existing_dir / name
                if target.is_file() and target.stat().st_size == expected:
                    with target.open("rb") as stream:
                        stream.seek(offset)
                        if stream.read(len(chunk)) != chunk:
                            raise ValueError("Backup retry differs from stored file bytes")
                        # End-if
                    # End-with
                    return {"received": end}
                # End-if
            # End-if

            stage_dir = self.staging_dir / snapshot_id
            stage_dir.mkdir(parents=True, exist_ok=True)
            target = stage_dir / name
            current = target.stat().st_size if target.exists() else 0
            if offset > current:
                raise ValueError("Backup chunk leaves a gap in the file")
            # End-if
            overlap = min(len(chunk), current - offset)
            if overlap:
                with target.open("rb") as stream:
                    stream.seek(offset)
                    if stream.read(overlap) != chunk[:overlap]:
                        raise ValueError("Backup retry differs from staged file bytes")
                    # End-if
                # End-with
            # End-if
            if end > current or not target.exists():
                with target.open("ab") as stream:
                    stream.write(chunk[overlap:])
                    stream.flush()
                    os.fsync(stream.fileno())
                # End-with
            # End-if
            return {"received": end}
        # End-with
    # End-def

    ##
     # @brief Validate staged files and atomically publish into assets/syncs/<session_id>/.
     #
     # @param manifest Verified identity and file digests.
     #
     # @return Dictionary with accepted snapshot_id, session_id, path, and changed flag.
     #
    def _commit(self, manifest):
        snapshot_id = manifest["snapshot_id"]
        session_id = manifest["session_id"]
        dest_dir = (self.syncs_dir / session_id).resolve()
        if dest_dir.parent != self.syncs_dir:
            raise ValueError("Invalid session destination directory")
        # End-if

        prev_meta_file = dest_dir / "sync_meta.json"
        prev_meta = {}
        if prev_meta_file.is_file():
            try:
                prev_meta = json.loads(prev_meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prev_meta = {}
            # End-try
        # End-if

        stage_dir = self.staging_dir / snapshot_id
        source_dir = stage_dir if stage_dir.is_dir() else dest_dir
        for name, spec in manifest["files"].items():
            target = source_dir / name
            if not target.is_file() or target.stat().st_size != spec["size"]:
                raise ValueError("Backup is incomplete: " + name)
            # End-if
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
                # End-for
            # End-with
            if digest.hexdigest() != spec["sha256"]:
                raise ValueError("Backup file digest mismatch: " + name)
            # End-if
        # End-for

        with (source_dir / "history.log").open(encoding="utf-8") as stream:
            history = json.load(stream)
        # End-with
        with (source_dir / "meta.log").open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        # End-with
        if not isinstance(history, list) or not isinstance(metadata, dict) or metadata.get("id") != session_id:
            raise ValueError("Backup history or metadata does not match the session")
        # End-if

        dest_dir.mkdir(parents=True, exist_ok=True)
        if source_dir != dest_dir:
            for name in manifest["files"]:
                src_file = source_dir / name
                tmp_file = dest_dir / (name + ".tmp")
                shutil.copyfile(src_file, tmp_file)
                os.replace(tmp_file, dest_dir / name)
            # End-for
            shutil.rmtree(stage_dir, ignore_errors=True)
        # End-if

        new_hist_sha = manifest["files"]["history.log"]["sha256"]
        prev_hist_sha = (
            prev_meta.get("files", {}).get("history.log", {}).get("sha256")
            if isinstance(prev_meta.get("files"), dict) else None
        )
        changed = (new_hist_sha != prev_hist_sha)
        has_summary = (dest_dir / "summary.md").is_file()

        sync_meta = dict(
            manifest,
            name=metadata.get("name", session_id),
            created_at=metadata.get("created_at", ""),
            last_used_at=metadata.get("last_used_at", ""),
            turns=len(history),
            synced_at=time.time(),
            path=str(dest_dir),
        )
        tmp_meta = prev_meta_file.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(sync_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_meta, prev_meta_file)

        return {
            "accepted": snapshot_id,
            "session_id": session_id,
            "changed": changed,
            "needs_digest": bool(changed or not has_summary),
            "path": f"assets/syncs/{session_id}",
        }
    # End-def

    ##
     # @brief Load metadata for one synced session directory.
     #
     # @param session_dir Path to a session folder under assets/syncs/.
     #
     # @return Session metadata dictionary, or None when invalid.
     #
    @staticmethod
    def _load_dir_meta(session_dir):
        meta_log = session_dir / "meta.log"
        sync_meta_file = session_dir / "sync_meta.json"
        if not meta_log.is_file():
            return None
        # End-if
        try:
            metadata = json.loads(meta_log.read_text(encoding="utf-8"))
            sync_meta = (
                json.loads(sync_meta_file.read_text(encoding="utf-8"))
                if sync_meta_file.is_file() else {}
            )
            summary_file = session_dir / "summary.md"
            summary = summary_file.read_text(encoding="utf-8").strip() if summary_file.is_file() else ""
            task_state_file = session_dir / "task_state.json"
            task_state = (
                json.loads(task_state_file.read_text(encoding="utf-8"))
                if task_state_file.is_file() else {}
            )
            session_id = metadata.get("id", session_dir.name)
            return {
                "snapshot_id": sync_meta.get("snapshot_id", session_id),
                "session_id": session_id,
                "name": metadata.get("name", session_id),
                "device": sync_meta.get("device", "local"),
                "project": sync_meta.get("project", "default"),
                "synced_at": float(sync_meta.get("synced_at") or meta_log.stat().st_mtime),
                "last_used_at": metadata.get("last_used_at") or metadata.get("created_at") or "",
                "files": sync_meta.get("files", {}),
                "path": f"assets/syncs/{session_id}",
                "summary": summary,
                "task_state": task_state if isinstance(task_state, dict) else {},
            }
        except (OSError, ValueError):
            return None
        # End-try
    # End-def

    ##
     # @brief List synced sessions under assets/syncs/, newest first.
     #
     # @param device Optional exact device filter.
     # @param project Optional exact project filter.
     #
     # @return List of synced session metadata dictionaries.
     #
    def list_sessions(self, device=None, project=None):
        with self.lock:
            items = []
            for entry in self.syncs_dir.iterdir():
                if not entry.is_dir():
                    continue
                # End-if
                info = self._load_dir_meta(entry)
                if info is None:
                    continue
                # End-if
                if device is not None and info["device"] != device:
                    continue
                # End-if
                if project is not None and info["project"] != project:
                    continue
                # End-if
                items.append(info)
            # End-for
            items.sort(key=lambda x: x["synced_at"], reverse=True)
            return items
        # End-with
    # End-def

    ##
     # @brief Render a compact overview of recently synced sessions for [Dandelion Context].
     #
     # @param limit Maximum number of recent sessions to include.
     #
     # @return Formatted Markdown overview string, or empty string.
     #
    def render_sync_digest(self, limit=10):
        sessions = self.list_sessions()[:max(1, int(limit))]
        if not sessions:
            return ""
        # End-if
        lines = ["## Recently Synced CLI Sessions (assets/syncs/)"]
        for s in sessions:
            target = s.get("task_state", {}).get("target") or "No target set"
            summary_line = (s.get("summary") or "").splitlines()[0] if s.get("summary") else ""
            detail = summary_line or f"Target: {target}"
            lines.append(
                f"- [{s['session_id']}] name='{s['name']}' | device={s['device']} | "
                f"project={s['project']} | path={s['path']} | {detail[:180]}"
            )
        # End-for
        return "\n".join(lines)
    # End-def
# End-class
