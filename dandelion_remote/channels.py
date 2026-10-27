"""Durable replies and an optional, allowlisted Telegram transport."""

import logging
import threading

import requests


class ChannelHub:
    def __init__(self, store):
        self.store = store
        with store.lock, store.conn:
            store.conn.executescript("""
                CREATE TABLE IF NOT EXISTS replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, text TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS channel_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)

    def notify(self, session_id, text):
        with self.store.lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO replies(session_id,text) VALUES (?,?)",
                (session_id, str(text)),
            )

    def replies(self, session_id, after=0):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id,session_id,text FROM replies "
                "WHERE session_id=? AND id>? ORDER BY id LIMIT 100",
                (session_id, after),
            ).fetchall()
        return [dict(row) for row in rows]

    def state(self, name, default="0"):
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT value FROM channel_state WHERE name=?", (name,)
            ).fetchone()
        return row[0] if row else default

    def set_state(self, name, value):
        with self.store.lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO channel_state VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, str(value)),
            )


class Telegram:
    """Long polling and reply delivery run independently of Agent execution."""

    def __init__(self, token, allowed_chats, loop, hub):
        if not token or not allowed_chats:
            raise ValueError("Telegram requires a token and an explicit chat allowlist")
        self.url = "https://api.telegram.org/bot" + token
        self.allowed = {str(chat) for chat in allowed_chats}
        self.loop = loop
        self.hub = hub
        self.stopped = threading.Event()
        self.threads = []

    def _call(self, method, data):
        # Never include the URL (which contains the token) in error output.
        response = requests.post(self.url + "/" + method, json=data, timeout=(10, 40))
        if response.status_code != 200:
            raise RuntimeError("Telegram returned HTTP " + str(response.status_code))
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected the request")
        return result["result"]

    def start(self):
        for target in (self._poll, self._send):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=45)

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
                    offset = update["update_id"] + 1
                    self.hub.set_state("telegram_offset", offset)
            except Exception:
                logging.warning("Telegram polling failed; retrying")
                self.stopped.wait(5)

    def _send(self):
        while not self.stopped.is_set():
            with self.hub.store.lock:
                rows = self.hub.store.conn.execute(
                    "SELECT id,session_id,text FROM replies "
                    "WHERE sent=0 AND session_id LIKE 'telegram:%' ORDER BY id LIMIT 50"
                ).fetchall()
            for row in rows:
                chat = row["session_id"].split(":", 1)[1]
                if chat not in self.allowed:
                    continue
                try:
                    text = row["text"] or "(empty reply)"
                    # Telegram delivery is at least once across crashes.
                    # A stored chunk offset avoids resending earlier chunks.
                    key = "telegram_reply:" + str(row["id"])
                    start = int(self.hub.state(key))
                    for pos in range(start, len(text), 3500):
                        self._call("sendMessage", {"chat_id": chat, "text": text[pos:pos + 3500]})
                        self.hub.set_state(key, pos + 3500)
                    with self.hub.store.lock, self.hub.store.conn:
                        self.hub.store.conn.execute("UPDATE replies SET sent=1 WHERE id=?", (row["id"],))
                except Exception:
                    logging.warning("Telegram delivery failed; reply remains queued")
                    break
            self.stopped.wait(1)
