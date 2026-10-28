##
 # @file src/remote/channels/telegram.py
 # @date 2026/10/27
 #
 # @brief Allowlisted Telegram polling and direct message delivery.
 # Accepts private messages from authorized chats, posts interrupt events to
 # the blocking RemoteLoop, and delivers outgoing messages on demand.
 #

import logging
import threading

import requests


##
 # @brief Poll authorized private Telegram chats and send outgoing messages.
 #
class Telegram:
    ##
     # @brief Bind an explicit chat allowlist to the remote loop and channel hub.
     #
     # @param token Telegram bot token.
     # @param allowed_chats Explicit collection of authorized private chat identifiers.
     # @param loop Remote conversation event loop.
     # @param hub Persistent reply and channel-state hub.
     #
     # @return None.
     #
    def __init__(self, token, allowed_chats, loop, hub):
        if not token or not allowed_chats:
            raise ValueError("Telegram requires a token and an explicit chat allowlist")
        # End-if
        self.url = "https://api.telegram.org/bot" + token
        self.allowed = {str(chat) for chat in allowed_chats}
        self.loop = loop
        self.hub = hub
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
        response = requests.post(self.url + "/" + method, json=data, timeout=(10, 40))
        if response.status_code != 200:
            raise RuntimeError("Telegram returned HTTP " + str(response.status_code))
        # End-if
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the request")
        # End-if
        return result["result"]
    # End-def

    ##
     # @brief Send a text message directly to an authorized Telegram chat in 3500-char chunks.
     #
     # @param chat_id Target Telegram chat identifier.
     # @param text Message text to send.
     #
     # @return None.
     #
    def send_text(self, chat_id, text):
        chat_str = str(chat_id)
        if chat_str not in self.allowed:
            raise ValueError("Chat ID is not in DANDELION_TELEGRAM_CHATS allowlist")
        # End-if
        body = str(text or "").strip() or "(empty reply)"
        for pos in range(0, len(body), 3500):
            self._call("sendMessage", {"chat_id": chat_str, "text": body[pos:pos + 3500]})
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
        rows = []
        with self.hub.store.lock:
            for chat in self.allowed:
                rows.extend(self.hub.store.conn.execute(
                    "SELECT id,session_id,text FROM replies "
                    "WHERE sent=0 AND session_id=? ORDER BY id LIMIT 20",
                    ("telegram:" + chat,),
                ).fetchall())
            # End-for
        # End-with
        for row in sorted(rows, key=lambda r: r["id"]):
            chat = row["session_id"].split(":", 1)[1]
            try:
                self.send_text(chat, row["text"])
                with self.hub.store.lock, self.hub.store.conn:
                    self.hub.store.conn.execute("UPDATE replies SET sent=1 WHERE id=?", (row["id"],))
                # End-with
            except Exception:
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
                    text = message.get("text", "")
                    if (chat in self.allowed and text
                            and message.get("chat", {}).get("type") == "private"):
                        session = "telegram:" + chat
                        if text.strip() == "/cancel":
                            self.loop.cancel(session)
                            self.hub.notify(session, "Cancellation requested.")
                        else:
                            self.loop.post("message", session, {"text": text},
                                           event_id="telegram:" + str(update["update_id"]))
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
