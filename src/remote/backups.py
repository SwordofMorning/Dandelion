##
 # @file src/remote/backups.py
 # @brief Receive byte-preserving session snapshots and publish complete backups.
 #

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import time

SESSION_FILES = ("history.log", "meta.log")
CHUNK_SIZE = 256 * 1024

##
 # @brief Keep uploaded session files separate from live Remote conversations.
 #
class BackupStore:
    ##
     # @brief Initialize snapshot directories and the durable publication index.
     # @param store Shared Remote Store, including data_dir and backup_lock.
     # @return None.
     #
    def __init__(self, store):
        self.store = store
        self.root = Path(store.data_dir) / "backups"
        self.staging = self.root / "staging"
        self.snapshots = self.root / "snapshots"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)
        with store.lock, store.conn:
            store.conn.execute("""
                CREATE TABLE IF NOT EXISTS session_backups (
                    snapshot_id TEXT PRIMARY KEY, device TEXT NOT NULL,
                    project TEXT NOT NULL, session_id TEXT NOT NULL,
                    manifest TEXT NOT NULL, synced_at REAL NOT NULL
                )
            """)
        # End-with
    # End-def

    ##
     # @brief Validate file identities and recompute the immutable snapshot ID.
     # @param payload A chunk or final commit request.
     # @return Canonical manifest with its verified snapshot_id.
     #
    @staticmethod
    def _manifest(payload):
        manifest = {}
        for name in ("device", "project", "session_id"):
            value = payload.get(name)
            if (not isinstance(value, str) or not value.strip() or len(value) > 200
                    or any(ord(character) < 32 or ord(character) == 127 for character in value)):
                raise ValueError("Invalid backup identity: " + name)
            # End-if
            manifest[name] = value
        # End-for
        files = payload.get("files")
        if not isinstance(files, dict) or set(files) != set(SESSION_FILES):
            raise ValueError("Backups must contain exactly history.log and meta.log")
        # End-if
        checked = {}
        for name in SESSION_FILES:
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
     # @brief Look up a committed snapshot without exposing staging uploads.
     # @param snapshot_id Snapshot identifier.
     # @return The durable snapshot row, or None.
     #
    def _row(self, snapshot_id):
        with self.store.lock:
            return self.store.conn.execute(
                "SELECT * FROM session_backups WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        # End-with
    # End-def

    ##
     # @brief Receive an idempotent chunk or commit both validated session files.
     # @param payload JSON request with a manifest and chunk or commit fields.
     # @return Received byte offset, or the committed snapshot ID.
     #
    def receive(self, payload):
        manifest = self._manifest(payload)
        snapshot_id = manifest["snapshot_id"]
        with self.store.backup_lock:
            if payload.get("commit") is True:
                self._commit(manifest)
                return {"accepted": snapshot_id}
            # End-if
            name = payload.get("file")
            offset = payload.get("offset")
            encoded = payload.get("data")
            if name not in SESSION_FILES or type(offset) is not int or offset < 0:
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
            published = self._row(snapshot_id) is not None or (self.snapshots / snapshot_id).is_dir()
            directory = (self.snapshots if published else self.staging) / snapshot_id
            directory.mkdir(exist_ok=True)
            target = directory / name
            current = target.stat().st_size if target.exists() else 0
            if offset > current:
                raise ValueError("Backup chunk leaves a gap in the file")
            # End-if
            overlap = min(len(chunk), current - offset)
            if overlap or published:
                with target.open("rb") as stream:
                    stream.seek(offset)
                    if stream.read(overlap) != chunk[:overlap] or (published and end > current):
                        raise ValueError("Backup retry differs from stored file bytes")
                    # End-if
                # End-with
            # End-if
            if not published and (end > current or not target.exists()):
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
     # @brief Validate both files and atomically publish the snapshot directory.
     # @param manifest Verified identity and file digests.
     # @return None.
     #
    def _commit(self, manifest):
        snapshot_id = manifest["snapshot_id"]
        if self._row(snapshot_id) is not None:
            # A user may intentionally sync an older local revision again.
            # Its bytes stay immutable, but it becomes the latest accepted copy.
            with self.store.lock, self.store.conn:
                self.store.conn.execute("UPDATE session_backups SET synced_at=? WHERE snapshot_id=?",
                                        (time.time(), snapshot_id))
            # End-with
            return
        # End-if
        published = self.snapshots / snapshot_id
        directory = published if published.is_dir() else self.staging / snapshot_id
        for name in SESSION_FILES:
            target = directory / name
            if not target.is_file() or target.stat().st_size != manifest["files"][name]["size"]:
                raise ValueError("Backup is incomplete: " + name)
            # End-if
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
                # End-for
            # End-with
            if digest.hexdigest() != manifest["files"][name]["sha256"]:
                raise ValueError("Backup file digest mismatch: " + name)
            # End-if
        # End-for
        with (directory / "history.log").open(encoding="utf-8") as stream:
            history = json.load(stream)
        # End-with
        with (directory / "meta.log").open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        # End-with
        if not isinstance(history, list) or not isinstance(metadata, dict) or metadata.get("id") != manifest["session_id"]:
            raise ValueError("Backup history or metadata does not match the session")
        # End-if
        if directory != published:
            os.replace(directory, published)
        # End-if
        # The directory is complete before its index becomes visible. A crash
        # between rename and INSERT can safely finish on an explicit retry.
        with self.store.lock, self.store.conn:
            self.store.conn.execute("INSERT INTO session_backups VALUES(?,?,?,?,?,?)", (
                snapshot_id, manifest["device"], manifest["project"], manifest["session_id"],
                json.dumps(manifest, ensure_ascii=False), time.time(),
            ))
        # End-with
    # End-def

    ##
     # @brief Describe a committed snapshot without returning raw history.
     # @param row Committed snapshot index row.
     # @return Identity, file digests, session name and publication timestamp.
     #
    def _metadata(self, row):
        manifest = json.loads(row["manifest"])
        with (self.snapshots / row["snapshot_id"] / "meta.log").open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        # End-with
        return dict(manifest, name=metadata.get("name", row["session_id"]), synced_at=row["synced_at"])
    # End-def

    ##
     # @brief List the most recently committed backup for each source session.
     # @param device Optional exact device filter.
     # @param project Optional exact project filter.
     # @return Snapshot metadata in publication order, newest first.
     #
    def list_sessions(self, device=None, project=None):
        query, args = "SELECT * FROM session_backups WHERE 1=1", []
        for key, value in (("device", device), ("project", project)):
            if value is not None:
                query += " AND " + key + "=?"
                args.append(value)
            # End-if
        # End-for
        with self.store.backup_lock, self.store.lock:
            rows = self.store.conn.execute(query + " ORDER BY synced_at DESC,rowid DESC", args).fetchall()
            seen, result = set(), []
            for row in rows:
                identity = (row["device"], row["project"], row["session_id"])
                if identity not in seen:
                    result.append(self._metadata(row))
                    seen.add(identity)
                # End-if
            # End-for
            return result
        # End-with
    # End-def

    ##
     # @brief Read original file bytes from an explicitly committed snapshot.
     # @param snapshot_id Published snapshot identifier.
     # @param name One allowed session filename.
     # @return Original file bytes; unknown identifiers raise KeyError.
     #
    def read_file(self, snapshot_id, name):
        if name not in SESSION_FILES or self._row(snapshot_id) is None:
            raise KeyError("Session backup was not found")
        # End-if
        return (self.snapshots / snapshot_id / name).read_bytes()
    # End-def

    ##
     # @brief Read a bounded message page and the original metadata object.
     # @param snapshot_id Published snapshot identifier.
     # @param offset First history message index.
     # @param limit Maximum messages to include, from one to one hundred.
     # @return Snapshot metadata, parsed files and history paging information.
     #
    def read_session(self, snapshot_id, offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History offset must be nonnegative and limit must be 1..100")
        # End-if
        row = self._row(snapshot_id)
        if row is None:
            raise KeyError("Session backup was not found")
        # End-if
        history = json.loads(self.read_file(snapshot_id, "history.log"))
        metadata = json.loads(self.read_file(snapshot_id, "meta.log"))
        return {"snapshot": self._metadata(row),
                "files": {"history.log": history[offset:offset + limit], "meta.log": metadata},
                "history": {"offset": offset, "limit": limit, "total": len(history)}}
    # End-def
# End-class
