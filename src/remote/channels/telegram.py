##
 # @file src/remote/channels/telegram.py
 # @date 2026/10/27
 #
 # @brief Allowlisted Telegram polling and reply delivery.
 # Accepts messages from authorized private chats, submits durable events, and
 # sends queued replies independently of model and background-worker execution.
 #
 # @note Delivery cursors are persisted; crash windows can still produce repeated sends.
 #

import logging
import threading

import requests


##
 # @brief Poll private messages and deliver replies independently of the agent.
 #
class Telegram:

    ##
     # @brief Bind an explicit chat allowlist to the remote loop.
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
    # End-def

    ##
     # @brief Call Telegram without exposing the token in errors.
     #
     # @param method Telegram API method name.
     # @param data JSON request body.
     #
     # @return Decoded Telegram result payload.
     #
    def _call(self, method, data):
        # Never include the URL (which contains the token) in error output.
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
     # @brief Start independent polling and delivery threads.
     #
     # @return None.
     #
    def start(self):
        for target in (self._poll, self._send):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        # End-for
    # End-def

    ##
     # @brief Stop and join the transport threads.
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
     # @brief Accept private messages from allowed chats.
     #
     # @return None.
     #
    def _poll(self):
        offset = int(self.hub.state("telegram_offset"))
        while not self.stopped.is_set():
            try:
                updates = self._call("getUpdates", {
                    "offset": offset, "timeout": 25, "allowed_updates": ["message"],
                })
                for update in updates:
                    message = update.get("message", {})
                    chat = str(message.get("chat", {}).get("id", ""))
                    text = message.get("text", "")
                    # Restrict to private chats: a group allowlist alone would
                    # otherwise authorize every group member to run tools.
                    if (chat in self.allowed and text
                            and message.get("chat", {}).get("type") == "private"):
                        session = "telegram:" + chat
                        if text.strip() == "/cancel":
                            self.loop.cancel(session)
                            self.hub.notify(session, "Cancellation requested at the next safe boundary.")
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

    ##
     # @brief Select allowed sessions before applying the delivery limit.
     #
     # @return At most 50 allowed unsent replies ordered by identifier.
     #
    def _pending_replies(self):
        rows = []
        with self.hub.store.lock:
            # Filter before limiting so removed chats cannot block delivery.
            # Individual chat queries also avoid SQLite's parameter limit.
            for chat in self.allowed:
                rows.extend(self.hub.store.conn.execute(
                    "SELECT id,session_id,text FROM replies "
                    "WHERE sent=0 AND session_id=? ORDER BY id LIMIT 50",
                    ("telegram:" + chat,),
                ).fetchall())
            # End-for
        # End-with
        return sorted(rows, key=lambda row: row["id"])[:50]
    # End-def

    ##
     # @brief Deliver queued reply chunks and persist progress.
     #
     # @return None.
     #
    def _send(self):
        while not self.stopped.is_set():
            rows = self._pending_replies()
            for row in rows:
                chat = row["session_id"].split(":", 1)[1]
                try:
                    text = row["text"] or "(empty reply)"
                    # Telegram delivery is at least once across crashes.
                    # A stored chunk offset avoids resending earlier chunks.
                    key = "telegram_reply:" + str(row["id"])
                    start = int(self.hub.state(key))
                    for pos in range(start, len(text), 3500):
                        self._call("sendMessage", {"chat_id": chat, "text": text[pos:pos + 3500]})
                        self.hub.set_state(key, pos + 3500)
                    # End-for
                    with self.hub.store.lock, self.hub.store.conn:
                        self.hub.store.conn.execute("UPDATE replies SET sent=1 WHERE id=?", (row["id"],))
                    # End-with
                except Exception:
                    logging.warning("Telegram delivery failed; reply remains queued")
                    break
                # End-try
            # End-for
            self.stopped.wait(1)
        # End-while
    # End-def
# End-class
