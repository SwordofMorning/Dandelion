##
 # @file src/remote/channels/telegram.py
 # @date 2026/10/29
 #
 # @brief Allowlisted Telegram polling, attachment saving, and message delivery.
 # Accepts private text, photo, and document messages from authorized chats,
 # saves incoming attachments into <workspace>/assets/telegram/, posts interrupt
 # events to the blocking RemoteLoop, and delivers outgoing messages with
 # typing indicators, reply quotes, notification controls, and chat pinning.
 #

import logging
import os
from pathlib import Path
import re
import threading

import requests

MAX_MEDIA_BYTES = 20 * 1024 * 1024


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
     # @brief Emit a best-effort 'typing' chat action indicator to an authorized chat.
     #
     # @param chat_id Target Telegram chat identifier.
     #
     # @return None.
     #
    def send_typing(self, chat_id):
        chat_str = str(chat_id)
        if chat_str not in self.allowed:
            return
        # End-if
        try:
            self._call("sendChatAction", {"chat_id": chat_str, "action": "typing"})
        except Exception:
            pass
        # End-try
    # End-def

    ##
     # @brief Send a text message directly to an authorized Telegram chat in 3500-char chunks.
     #
     # @param chat_id Target Telegram chat identifier.
     # @param text Message text to send.
     # @param reply_to_message_id Optional user message ID to quote-reply.
     # @param disable_notification True for silent delivery; False for popup alert.
     # @param pin True to pin the sent message to the chat header.
     #
     # @return None.
     #
    def send_text(self, chat_id, text, reply_to_message_id=None, disable_notification=False, pin=False):
        chat_str = str(chat_id)
        if chat_str not in self.allowed:
            raise ValueError("Chat ID is not in DANDELION_TELEGRAM_CHATS allowlist")
        # End-if
        if reply_to_message_id is None:
            last_id = self.hub.state("telegram_last_msg:" + chat_str, default="")
            if str(last_id).isdigit():
                reply_to_message_id = int(last_id)
            # End-if
        # End-if
        body = str(text or "").strip() or "(empty reply)"
        for idx, pos in enumerate(range(0, len(body), 3500)):
            payload = {
                "chat_id": chat_str,
                "text": body[pos:pos + 3500],
                "disable_notification": bool(disable_notification),
            }
            if idx == 0 and reply_to_message_id:
                payload["reply_parameters"] = {
                    "message_id": int(reply_to_message_id),
                    "allow_sending_without_reply": True,
                }
            # End-if
            sent = self._call("sendMessage", payload)
            if pin and idx == 0 and isinstance(sent, dict) and sent.get("message_id"):
                try:
                    self._call("pinChatMessage", {
                        "chat_id": chat_str,
                        "message_id": int(sent["message_id"]),
                        "disable_notification": bool(disable_notification),
                    })
                except Exception:
                    logging.warning("Telegram pinChatMessage failed; message was still delivered")
                # End-try
            # End-if
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
                    reply_to_message_id=row.get("reply_to"),
                    disable_notification=not bool(row.get("notify", 1)),
                    pin=bool(row.get("pin", 0)),
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
                            msg_id = message.get("message_id")
                            if msg_id is not None:
                                self.hub.set_state("telegram_last_msg:" + chat, msg_id)
                            # End-if
                            self.send_typing(chat)
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
                                {"text": body, "message_id": msg_id},
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
