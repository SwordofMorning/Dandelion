"""Portable activity summaries and a small, durable local sync outbox."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
from urllib.parse import urlsplit
import uuid

import requests


def _text_blocks(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content):
        return ""
    return "\n".join(
        str(b.get("text", "")) for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def summarize_session(session):
    """Summarize final replies and task state without exporting raw tool logs."""
    finals = []
    for message in session.load_history():
        if message.get("role") == "assistant":
            text = _text_blocks(message.get("content"))
            if text:
                finals.append(text[:1800])
    sections = []
    if finals:
        sections.append("Recent completed replies:\n" + "\n\n".join(finals[-3:]))
    state_file = Path(session.current_session_dir) / "task_state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            items = {key: state.get(key) for key in ("target", "todos", "completed")
                     if state.get(key)}
            if items.get("target") == "No specific target set.":
                items.pop("target")
            if items:
                sections.append("Task state:\n" + json.dumps(items, ensure_ascii=False)[:1800])
    return "\n\n".join(sections)[:7200]


class SyncOutbox:
    """Keep independent records until the remote explicitly acknowledges them."""

    def __init__(self, log_dir, workspace_dir):
        self.directory = Path(log_dir) / "sync"
        self.pending_dir = self.directory / "pending"
        self.workspace_dir = Path(workspace_dir).resolve()
        self.receipts_file = self.directory / "receipts.jsonl"

    def _receipts(self):
        if not self.receipts_file.exists():
            return set()
        receipts = set()
        for line in self.receipts_file.read_text(encoding="utf-8").splitlines():
            # A process stopped during append may leave an incomplete last line.
            try:
                receipts.add(json.loads(line)["event_id"])
            except (ValueError, KeyError, TypeError):
                continue
        return receipts

    @staticmethod
    def _atomic_write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def enqueue(self, session, summary=None):
        text = summarize_session(session) if summary is None else str(summary).strip()
        if not text:
            return None
        if len(text) > 7200:
            raise ValueError("Activity summaries must be at most 7200 characters.")
        device = (os.environ.get("DANDELION_DEVICE_ID") or socket.gethostname())[:200]
        session_id = str(session.current_session_id)
        project = self.workspace_dir.name
        fingerprint = json.dumps([device, str(self.workspace_dir), session_id, text],
                                 ensure_ascii=False, separators=(",", ":"))
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(
            fingerprint.encode("utf-8")).hexdigest()))
        record_file = self.pending_dir / (event_id + ".json")
        if record_file.exists():
            return json.loads(record_file.read_text(encoding="utf-8"))
        record = {
            "event_id": event_id,
            "device": device,
            "project": project,
            "session_id": session_id,
            "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "summary": text,
            "sources": [{"kind": "dandelion-session", "session_id": session_id}],
        }
        if event_id not in self._receipts():
            self._atomic_write(record_file, json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def _pending(self):
        if not self.pending_dir.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.pending_dir.glob("*.json"))]

    def export(self, path):
        records = self._pending()
        self._atomic_write(Path(path).expanduser().resolve(), "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records))
        return len(records)

    def flush(self):
        records = self._pending()
        result = {"sent": 0, "pending": len(records), "error": None}
        if not records:
            return result
        base_url = (os.environ.get("DANDELION_DR_URL") or os.environ.get("DR_URL") or "").strip()
        token = os.environ.get("DANDELION_DR_TOKEN") or os.environ.get("DR_TOKEN")
        if not base_url or not token:
            result["error"] = "Set DANDELION_DR_URL and DANDELION_DR_TOKEN to upload."
            return result
        parsed = urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or (
                not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            result["error"] = "DR URL must use HTTPS (HTTP is allowed only on loopback)."
            return result
        try:
            # Batch size also bounds the amount retried after an interrupted send.
            for offset in range(0, len(records), 100):
                batch = records[offset:offset + 100]
                response = requests.post(
                    base_url.rstrip("/") + "/sync", json={"records": batch},
                    headers={"Authorization": "Bearer " + token},
                    timeout=(3, 15), allow_redirects=False,
                )
                if response.status_code != 200:
                    result["error"] = f"Remote sync returned HTTP {response.status_code}; records retained."
                    break
                accepted = response.json().get("accepted")
                if not isinstance(accepted, list) or any(not isinstance(i, str) for i in accepted):
                    result["error"] = "Remote sync did not acknowledge record IDs; records retained."
                    break
                acknowledged = set(accepted) & {record["event_id"] for record in batch}
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.receipts_file.open("a", encoding="utf-8") as stream:
                    for event_id in acknowledged:
                        stream.write(json.dumps({"event_id": event_id}) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                for event_id in acknowledged:
                    (self.pending_dir / (event_id + ".json")).unlink(missing_ok=True)
                result["sent"] += len(acknowledged)
                if len(acknowledged) != len(batch):
                    result["error"] = "Remote acknowledged only part of the batch; remaining records retained."
                    break
        except (requests.RequestException, ValueError, AttributeError):
            result["error"] = "Remote sync failed; records retained for retry or export."
        result["pending"] = len(self._pending())
        return result
