##
 # @file src/remote/storage/replies.py
 # @date 2026/10/28
 #
 # @brief Outgoing message router and transport cursor store.
 # Routes SendMessageTool and reminder messages directly to Telegram when
 # configured, and records durable replies for HTTP clients.
 #

import logging


##
 # @brief Route outgoing messages to Telegram and persist HTTP reply history.
 #
class ChannelHub:
    ##
     # @brief Create the reply and channel-state tables.
     #
     # @param store Shared durable store with its connection lock.
     #
     # @return None.
     #
    def __init__(self, store):
        self.store = store
        self.telegram = None
        self.default_telegram_session = None
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
        # End-with
    # End-def

    ##
     # @brief Attach an active Telegram transport for direct message delivery.
     #
     # @param telegram Telegram channel instance.
     # @param allowed_chats Optional iterable of authorized Telegram chat IDs.
     #
     # @return None.
     #
    def bind_telegram(self, telegram, allowed_chats=None):
        self.telegram = telegram
        if allowed_chats:
            first = next(iter(allowed_chats), None)
            if first:
                self.default_telegram_session = "telegram:" + str(first)
            # End-if
        # End-if
    # End-def

    ##
     # @brief Persist and deliver an outgoing message for one conversation.
     #
     # @param session_id Target conversation identifier (or "default").
     # @param text Message text to deliver.
     #
     # @return Resolved session_id to which the message was delivered.
     #
    def notify(self, session_id, text):
        target = str(session_id or "").strip()
        if (not target or target in ("default", "system", "sync")) and self.default_telegram_session:
            target = self.default_telegram_session
        # End-if
        body = str(text or "")
        # Persist the pending reply row first (sent=0) so an interrupted network
        # call can be retried on restart without losing the message.
        with self.store.lock, self.store.conn:
            cur = self.store.conn.execute(
                "INSERT INTO replies(session_id,text,sent) VALUES (?,?,0)",
                (target, body),
            )
            reply_id = cur.lastrowid
        # End-with

        if self.telegram is not None and target.startswith("telegram:"):
            chat_id = target.split(":", 1)[1]
            try:
                self.telegram.send_text(chat_id, body)
                with self.store.lock, self.store.conn:
                    self.store.conn.execute("UPDATE replies SET sent=1 WHERE id=?", (reply_id,))
                # End-with
            except Exception:
                logging.warning("Direct Telegram send failed; queued for retry")
            # End-try
        # End-if
        return target
    # End-def

    ##
     # @brief Read replies after a caller-owned cursor.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     # @param after Exclusive reply identifier cursor.
     #
     # @return Ordered reply dictionaries after the supplied cursor.
     #
    def replies(self, session_id, after=0):
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT id,session_id,text FROM replies "
                "WHERE session_id=? AND id>? ORDER BY id LIMIT 100",
                (session_id, after),
            ).fetchall()
        # End-with
        return [dict(row) for row in rows]
    # End-def

    ##
     # @brief Read one persistent transport cursor.
     #
     # @param name Persistent channel-state key.
     # @param default Fallback value when the requested field is absent.
     #
     # @return Stored string value, or the supplied default.
     #
    def state(self, name, default="0"):
        with self.store.lock:
            row = self.store.conn.execute(
                "SELECT value FROM channel_state WHERE name=?", (name,)
            ).fetchone()
        # End-with
        return row[0] if row else default
    # End-def

    ##
     # @brief Persist one transport cursor.
     #
     # @param name Persistent channel-state key.
     # @param value New persistent channel-state value.
     #
     # @return None.
     #
    def set_state(self, name, value):
        with self.store.lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO channel_state VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, str(value)),
            )
        # End-with
    # End-def
# End-class
