##
 # @file src/remote/channels/telegram.py
 # @date 2026/10/29
 #
 # @brief Allowlisted Telegram polling, attachment saving, and message delivery.
 # Accepts private text, photo, and document messages from authorized chats,
 # saves incoming attachments into <workspace>/assets/telegram/, posts interrupt
 # events to the blocking RemoteLoop, and delivers outgoing messages with
 # typing indicators and explicit/silent notification control.
 #

import logging
import os
from pathlib import Path
import re
import threading
import time

import requests

MAX_MEDIA_BYTES = 20 * 1024 * 1024
PROGRESS_MIN_INTERVAL = 1.5


##
 # @brief Poll authorized private Telegram chats and send outgoing messages.
 #
class Telegram:
    ##
     # @brief Bind an explicit chat allowlist to the remote loop, channel hub, and workspace.
     #
     # @param token Telegram bot token.
     # @param allowed_chats Explicit collection of authorized private chat identifiers.
     # @param loop Remote conversation event loop.
     # @param hub Persistent reply and channel-state hub.
     # @param workspace_dir Optional server workspace root for saving incoming attachments.
     #
     # @return None.
     #
    def __init__(self, token, allowed_chats, loop, hub, workspace_dir=None):
        if not token or not allowed_chats:
            raise ValueError("Telegram requires a token and an explicit chat allowlist")
        # End-if
        self._token = str(token)
        self.url = "https://api.telegram.org/bot" + self._token
        self.file_url = "https://api.telegram.org/file/bot" + self._token
        self.allowed = {str(chat) for chat in allowed_chats}
        self.loop = loop
        self.hub = hub
        self.workspace_dir = Path(workspace_dir or "./.data/workspace").resolve()
        self.media_dir = self.workspace_dir / "assets" / "telegram"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.stopped = threading.Event()
        self.threads = []
        self._progress = {}
        self._progress_lock = threading.Lock()
        self.hub.bind_telegram(self, allowed_chats)
    # End-def

    ##
     # @brief Call Telegram Bot API without exposing the token in error messages.
     #
     # @param method Telegram API method name.
     # @param data JSON request body.
     #
     # @return Decoded Telegram result payload.
     #
    def _call(self, method, data):
        try:
            response = requests.post(self.url + "/" + method, json=data, timeout=(10, 40))
        except requests.RequestException:
            raise RuntimeError("Telegram API network request failed") from None
        # End-try
        if response.status_code != 200:
            raise RuntimeError("Telegram returned HTTP " + str(response.status_code))
        # End-if
        try:
            result = response.json()
        except ValueError:
            raise RuntimeError("Telegram returned invalid JSON") from None
        # End-try
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the request")
        # End-if
        return result["result"]
    # End-def

    ##
     # @brief Download a Telegram photo or document into <workspace>/assets/telegram/.
     #
     # @param file_id Telegram file_id to fetch via getFile.
     # @param update_id Telegram update_id used to prefix the saved file name.
     # @param file_name Optional original file name hint.
     #
     # @return Workspace-relative POSIX path to the saved file.
     #
    def _download_media(self, file_id, update_id, file_name=""):
        info = self._call("getFile", {"file_id": str(file_id)})
        remote_path = str(info.get("file_path") or "").strip()
        if not remote_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        # End-if
        raw_name = Path(file_name or Path(remote_path).name or "attachment").name
        raw_suffix = Path(raw_name).suffix
        ext = (raw_suffix or Path(remote_path).suffix).lower()
        ext = re.sub(r"[^.a-z0-9]+", "", ext)[:16]
        if ext and not ext.startswith("."):
            ext = "." + ext
        # End-if
        stem_raw = raw_name[:-len(raw_suffix)] if raw_suffix else raw_name
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem_raw).strip("._")[:100] or "attachment"
        safe_name = stem + ext
        target = self.media_dir / f"tg_{update_id}_{safe_name}"
        try:
            response = requests.get(self.file_url + "/" + remote_path.lstrip("/"), timeout=(10, 60))
        except requests.RequestException:
            raise RuntimeError("Telegram file download network request failed") from None
        # End-try
        if response.status_code != 200:
            raise RuntimeError("Telegram file download returned HTTP " + str(response.status_code))
        # End-if
        payload = response.content
        if len(payload) > MAX_MEDIA_BYTES:
            raise ValueError("Telegram attachment exceeds 20 MB limit")
        # End-if
        self.media_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            # End-if
        # End-try
        return target.relative_to(self.workspace_dir).as_posix()
    # End-def

    ##
     # @brief Create an initial silent progress bubble at the start of a user turn.
     #
     # @param chat_id Target Telegram chat identifier.
     #
     # @return None.
     #
    def start_progress(self, chat_id):
        chat_str = str(chat_id)
        if chat_str not in self.allowed:
            return
        # End-if
        self.end_progress(chat_str, keep_active=False)
        initial_text = "[Step 1] Thinking..."
        msg_id = None
        try:
            sent = self._call("sendMessage", {
                "chat_id": chat_str,
                "text": initial_text,
                "disable_notification": True,
            })
            if isinstance(sent, dict) and sent.get("message_id"):
                msg_id = int(sent["message_id"])
            # End-if
        except Exception:
            pass
        # End-try
        with self._progress_lock:
            prev = self._progress.get(chat_str)
            stale = list(prev.get("stale_ids", [])) if prev else []
            old_timer = prev.get("timer") if prev else None
            if old_timer is not None:
                old_timer.cancel()
            # End-if
            self._progress[chat_str] = {
                "active": True,
                "editing": False,
                "message_id": msg_id,
                "stale_ids": stale,
                "text": initial_text if msg_id is not None else "",
                "desired_text": initial_text,
                "last_ts": 0.0,
                "step": 0,
                "tools": [],
                "timer": None,
            }
        # End-with
    # End-def

    ##
     # @brief Perform a serialized editMessageText and schedule a follow-up timer if desired_text advanced.
     #
     # @param chat_str Target Telegram chat identifier string.
     # @param msg_id Bubble message identifier to edit.
     # @param sent_text Text being sent in this edit call.
     #
     # @return None.
     #
    def _do_edit_progress(self, chat_str, msg_id, sent_text):
        edit_ok = False
        try:
            self._call("editMessageText", {
                "chat_id": chat_str,
                "message_id": int(msg_id),
                "text": sent_text,
            })
            edit_ok = True
        except Exception:
            pass
        # End-try
        with self._progress_lock:
            cur = self._progress.get(chat_str)
            if cur is None:
                return
            # End-if
            cur["editing"] = False
            if not cur.get("active") or cur.get("message_id") != msg_id:
                return
            # End-if
            if edit_ok:
                cur["text"] = sent_text
                cur["last_ts"] = time.monotonic()
            # End-if
            desired = cur.get("desired_text", "")
            if desired and desired != cur.get("text", "") and cur.get("timer") is None:
                timer = threading.Timer(PROGRESS_MIN_INTERVAL, self._flush_progress, args=(chat_str,))
                timer.daemon = True
                cur["timer"] = timer
                timer.start()
            # End-if
        # End-with
    # End-def

    ##
     # @brief Flush a deferred progress bubble update when the 1.5s cooldown expires.
     #
     # @param chat_str Target Telegram chat identifier string.
     #
     # @return None.
     #
    def _flush_progress(self, chat_str):
        with self._progress_lock:
            state = self._progress.get(chat_str)
            if state is None:
                return
            # End-if
            state["timer"] = None
            if not state.get("active") or state.get("editing"):
                return
            # End-if
            msg_id = state.get("message_id")
            desired = state.get("desired_text", "")
            last_text = state.get("text", "")
            if msg_id is None or not desired or desired == last_text:
                return
            # End-if
            state["editing"] = True
        # End-with
        self._do_edit_progress(chat_str, msg_id, desired)
    # End-def

    ##
     # @brief Update the step-based progress bubble with a 1.5s timestamp guard and deferred flush.
     #
     # @param chat_id Target Telegram chat identifier.
     # @param text Runtime status line emitted by MyAgent.step().
     #
     # @return None.
     #
    def on_progress(self, chat_id, text):
        chat_str = str(chat_id)
        stripped = str(text or "").strip()
        if not stripped:
            return
        # End-if
        first_tool_in_step = False
        now = time.monotonic()
        with self._progress_lock:
            state = self._progress.get(chat_str)
            if state is None or not state.get("active"):
                return
            # End-if
            if stripped == "Thinking...":
                state["step"] += 1
                state["tools"] = []
            elif stripped.startswith("Tool requested:"):
                tname = stripped.split(":", 1)[1].strip()
                if tname and tname != "send_message":
                    first_tool_in_step = (len(state["tools"]) == 0)
                    state["tools"].append(tname)
                else:
                    return
                # End-if
            else:
                return
            # End-if
            step_num = max(int(state["step"]), 1)
            tools = list(state["tools"][-4:])
            status_text = (
                f"[Step {step_num}] Running: {', '.join(tools)} ..."
                if tools else f"[Step {step_num}] Thinking..."
            )
            state["desired_text"] = status_text
            if state.get("editing"):
                return
            # End-if
            msg_id = state.get("message_id")
            last_text = state.get("text", "")
            last_ts = float(state.get("last_ts", 0.0))
            if msg_id is not None:
                if status_text == last_text:
                    return
                # End-if
                if not first_tool_in_step and (now - last_ts) < PROGRESS_MIN_INTERVAL:
                    if state.get("timer") is None:
                        delay = max(0.05, PROGRESS_MIN_INTERVAL - (now - last_ts))
                        timer = threading.Timer(delay, self._flush_progress, args=(chat_str,))
                        timer.daemon = True
                        state["timer"] = timer
                        timer.start()
                    # End-if
                    return
                # End-if
                old_timer = state.get("timer")
                if old_timer is not None:
                    old_timer.cancel()
                    state["timer"] = None
                # End-if
                state["editing"] = True
            else:
                state["editing"] = True
            # End-if
        # End-with

        if msg_id is None:
            new_mid = None
            try:
                sent = self._call("sendMessage", {
                    "chat_id": chat_str,
                    "text": status_text,
                    "disable_notification": True,
                })
                if isinstance(sent, dict) and sent.get("message_id"):
                    new_mid = int(sent["message_id"])
                # End-if
            except Exception:
                pass
            # End-try
            with self._progress_lock:
                cur = self._progress.get(chat_str)
                if cur is not None:
                    cur["editing"] = False
                    if cur.get("active") and new_mid is not None:
                        cur["message_id"] = new_mid
                        cur["text"] = status_text
                        cur["last_ts"] = time.monotonic() if tools else 0.0
                        desired = cur.get("desired_text", "")
                        if desired and desired != status_text and cur.get("timer") is None:
                            timer = threading.Timer(PROGRESS_MIN_INTERVAL, self._flush_progress, args=(chat_str,))
                            timer.daemon = True
                            cur["timer"] = timer
                            timer.start()
                        # End-if
                    # End-if
                # End-if
            # End-with
            return
        # End-if

        self._do_edit_progress(chat_str, msg_id, status_text)
    # End-def

    ##
     # @brief Delete the temporary progress bubble before formal delivery or at turn end.
     #
     # @param chat_id Target Telegram chat identifier.
     # @param keep_active True to keep turn tracking alive for subsequent steps after a mid-turn send.
     #
     # @return None.
     #
    def end_progress(self, chat_id, keep_active=False):
        chat_str = str(chat_id)
        to_delete = []
        with self._progress_lock:
            state = self._progress.get(chat_str)
            if state is not None:
                old_timer = state.get("timer")
                if old_timer is not None:
                    old_timer.cancel()
                    state["timer"] = None
                # End-if
                for sid in state.get("stale_ids", []):
                    if sid is not None and sid not in to_delete:
                        to_delete.append(sid)
                    # End-if
                # End-for
                mid = state.get("message_id")
                if mid is not None and mid not in to_delete:
                    to_delete.append(mid)
                # End-if
                if keep_active:
                    state["editing"] = False
                    state["message_id"] = None
                    state["stale_ids"] = []
                    state["text"] = ""
                    state["desired_text"] = ""
                    state["tools"] = []
                else:
                    self._progress.pop(chat_str, None)
                # End-if
            # End-if
        # End-with
        failed = []
        for mid in to_delete:
            try:
                self._call("deleteMessage", {"chat_id": chat_str, "message_id": int(mid)})
            except Exception:
                failed.append(mid)
            # End-try
        # End-for
        if failed:
            with self._progress_lock:
                cur = self._progress.get(chat_str)
                if cur is not None:
                    existing = cur.setdefault("stale_ids", [])
                    for mid in failed:
                        if mid not in existing:
                            existing.append(mid)
                        # End-if
                    # End-for
                else:
                    self._progress[chat_str] = {
                        "active": False,
                        "editing": False,
                        "message_id": None,
                        "stale_ids": failed,
                        "text": "",
                        "desired_text": "",
                        "last_ts": 0.0,
                        "step": 0,
                        "tools": [],
                        "timer": None,
                    }
                # End-if
            # End-with
        # End-if
    # End-def

    ##
     # @brief Send a text message directly to an authorized Telegram chat in 3500-char chunks.
     #
     # @param chat_id Target Telegram chat identifier.
     # @param text Message text to send.
     # @param disable_notification True for silent delivery; False for standard notification.
     #
     # @return None.
     #
    def send_text(self, chat_id, text, disable_notification=False):
        chat_str = str(chat_id)
        if chat_str not in self.allowed:
            raise ValueError("Chat ID is not in DANDELION_TELEGRAM_CHATS allowlist")
        # End-if
        self.end_progress(chat_str, keep_active=True)
        body = str(text or "").strip() or "(empty reply)"
        for pos in range(0, len(body), 3500):
            self._call("sendMessage", {
                "chat_id": chat_str,
                "text": body[pos:pos + 3500],
                "disable_notification": bool(disable_notification),
            })
        # End-for
    # End-def

    ##
     # @brief Start the incoming message polling thread and retry flusher.
     #
     # @return None.
     #
    def start(self):
        thread = threading.Thread(target=self._poll, name="telegram-poll", daemon=True)
        thread.start()
        self.threads.append(thread)
    # End-def

    ##
     # @brief Stop and join the polling thread.
     #
     # @return None.
     #
    def close(self):
        self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=45)
        # End-for
    # End-def

    ##
     # @brief Flush any unsent replies that failed during a transient network error.
     #
     # @return None.
     #
    def _flush_unsent(self):
        rows = self.hub.unsent_replies(self.allowed)
        for idx, row in enumerate(rows):
            chat = row["session_id"].split(":", 1)[1]
            try:
                self.send_text(
                    chat,
                    row["text"],
                    disable_notification=not bool(row.get("notify", 1)),
                )
                self.hub.mark_sent(row["id"])
            except Exception:
                for remaining in rows[idx:]:
                    self.hub.mark_retry(remaining["id"])
                # End-for
                break
            # End-try
        # End-for
    # End-def

    ##
     # @brief Poll private messages from allowed chats and post interrupts to RemoteLoop.
     #
     # @return None.
     #
    def _poll(self):
        offset = int(self.hub.state("telegram_offset"))
        while not self.stopped.is_set():
            try:
                self._flush_unsent()
                updates = self._call("getUpdates", {
                    "offset": offset, "timeout": 25, "allowed_updates": ["message"],
                })
                for update in updates:
                    message = update.get("message", {})
                    chat = str(message.get("chat", {}).get("id", ""))
                    text = str(message.get("text") or message.get("caption") or "").strip()
                    photos = message.get("photo") or []
                    document = message.get("document") or {}
                    if (chat in self.allowed
                            and message.get("chat", {}).get("type") == "private"
                            and (text or photos or document)):
                        session = "telegram:" + chat
                        if text == "/cancel" and not photos and not document:
                            self.loop.cancel(session)
                            self.hub.notify(session, "Cancellation requested.")
                        else:
                            notes = []
                            best_photo = max(
                                (p for p in photos if isinstance(p, dict) and p.get("file_id")),
                                key=lambda p: (
                                    int(p.get("width") or 0) * int(p.get("height") or 0),
                                    int(p.get("file_size") or 0),
                                ),
                                default=None,
                            )
                            if best_photo is not None:
                                try:
                                    rel = self._download_media(
                                        best_photo["file_id"], update["update_id"]
                                    )
                                    notes.append(
                                        f"[Telegram Attachment saved at: {rel} - call 'read_image' "
                                        "(or 'read_pdf' / 'read_file') on this path to inspect it.]"
                                    )
                                except Exception as exc:
                                    err = str(exc).replace(self._token, "<redacted>")
                                    notes.append(f"[Telegram photo download failed: {err}]")
                                # End-try
                            # End-if
                            if isinstance(document, dict) and document.get("file_id"):
                                try:
                                    rel = self._download_media(
                                        document["file_id"],
                                        update["update_id"],
                                        str(document.get("file_name") or "document"),
                                    )
                                    notes.append(
                                        f"[Telegram Attachment saved at: {rel} - call 'read_image' "
                                        "(or 'read_pdf' / 'read_file') on this path to inspect it.]"
                                    )
                                except Exception as exc:
                                    err = str(exc).replace(self._token, "<redacted>")
                                    notes.append(f"[Telegram document download failed: {err}]")
                                # End-try
                            # End-if
                            body = text or "User sent an attachment via Telegram."
                            if notes:
                                body = body + "\n" + "\n".join(notes)
                            # End-if
                            self.loop.post(
                                "message",
                                session,
                                {"text": body},
                                event_id="telegram:" + str(update["update_id"]),
                            )
                        # End-if
                    # End-if
                    offset = update["update_id"] + 1
                    self.hub.set_state("telegram_offset", offset)
                # End-for
            except Exception:
                logging.warning("Telegram polling failed; retrying")
                self.stopped.wait(5)
            # End-try
        # End-while
    # End-def
# End-class
