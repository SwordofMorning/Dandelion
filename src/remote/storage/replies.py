##
 # @file src/remote/storage/replies.py
 # @date 2026/10/28
 #
 # @brief Outgoing message router and transport cursor store.
 # Enqueues outgoing messages with exclusive in-flight ownership (sent=-1)
 # before releasing the database lock to perform Telegram network delivery.
 #

import logging


##
 # @brief Route outgoing messages to Telegram and persist HTTP reply history.
 #
class ChannelHub:
    ##
     # @brief Create the reply and channel-state tables and recover in-flight rows.
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
                    sent INTEGER NOT NULL DEFAULT 0,
                    notify INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS channel_state (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                UPDATE replies SET sent=0 WHERE sent=-1;
            """)
            try:
                store.conn.execute("ALTER TABLE replies ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")
            except Exception:
                pass
            # End-try
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
     # @brief Enqueue a reply row with in-flight status (sent=-1) inside a short DB lock.
     #
     # @param session_id Target conversation identifier (or "default").
     # @param text Message text to persist.
     # @param notify True to trigger a device notification; False for silent delivery.
     #
     # @return (resolved_session_id, reply_id, body_text) tuple.
     #
    def enqueue(self, session_id, text, notify=True):
        target = str(session_id or "").strip()
        if (not target or target in ("default", "system", "sync")) and self.default_telegram_session:
            target = self.default_telegram_session
        # End-if
        body = str(text or "")
        notify_int = 1 if (True if notify is None else bool(notify)) else 0
        with self.store.lock, self.store.conn:
            cur = self.store.conn.execute(
                "INSERT INTO replies(session_id,text,sent,notify) VALUES (?,?,-1,?)",
                (target, body, notify_int),
            )
            reply_id = int(cur.lastrowid)
        # End-with
        return target, reply_id, body
    # End-def

    ##
     # @brief Perform network delivery outside the DB lock and finalize row state.
     #
     # @param target Resolved conversation identifier.
     # @param reply_id Primary key of the enqueued reply row.
     # @param body Message text to send.
     # @param notify True to trigger a device notification; False for silent delivery.
     #
     # @return Resolved session_id.
     #
    def deliver(self, target, reply_id, body, notify=True):
        if self.telegram is not None and target.startswith("telegram:"):
            chat_id = target.split(":", 1)[1]
            try:
                self.telegram.send_text(
                    chat_id,
                    body,
                    disable_notification=not bool(notify),
                )
                self.mark_sent(reply_id)
            except Exception:
                logging.warning("Direct Telegram send failed; queued for retry")
                with self.store.lock, self.store.conn:
                    self.store.conn.execute("UPDATE replies SET sent=0 WHERE id=?", (int(reply_id),))
                # End-with
            # End-try
        else:
            with self.store.lock, self.store.conn:
                self.store.conn.execute("UPDATE replies SET sent=0 WHERE id=?", (int(reply_id),))
            # End-with
        # End-if
        return target
    # End-def

    ##
     # @brief Persist and deliver an outgoing message without holding the DB lock across I/O.
     #
     # @param session_id Target conversation identifier (or "default").
     # @param text Message text to deliver.
     # @param notify True to trigger a device notification; False for silent delivery.
     #
     # @return Resolved session_id to which the message was delivered.
     #
    def notify(self, session_id, text, notify=True):
        target, reply_id, body = self.enqueue(session_id, text, notify=notify)
        return self.deliver(target, reply_id, body, notify=notify)
    # End-def

    ##
     # @brief Claim unsent retryable replies (sent=0) atomically as in-flight (sent=-1).
     #
     # @param allowed_chats Iterable of authorized Telegram chat IDs.
     #
     # @return Ordered list of claimed reply rows.
     #
    def unsent_replies(self, allowed_chats):
        rows = []
        with self.store.lock, self.store.conn:
            for chat in allowed_chats:
                rows.extend(self.store.conn.execute(
                    "SELECT id,session_id,text,notify FROM replies "
                    "WHERE sent=0 AND session_id=? ORDER BY id LIMIT 20",
                    ("telegram:" + str(chat),),
                ).fetchall())
            # End-for
            claimed = [dict(r) for r in sorted(rows, key=lambda r: r["id"])]
            for item in claimed:
                self.store.conn.execute("UPDATE replies SET sent=-1 WHERE id=?", (item["id"],))
            # End-for
        # End-with
        return claimed
    # End-def

    ##
     # @brief Mark a queued reply row as sent (sent=1).
     #
     # @param reply_id Primary key of the delivered reply row.
     #
     # @return None.
     #
    def mark_sent(self, reply_id):
        with self.store.lock, self.store.conn:
            self.store.conn.execute("UPDATE replies SET sent=1 WHERE id=?", (int(reply_id),))
        # End-with
    # End-def

    ##
     # @brief Release an in-flight reply row back to retryable state (sent=0) on failure.
     #
     # @param reply_id Primary key of the failed reply row.
     #
     # @return None.
     #
    def mark_retry(self, reply_id):
        with self.store.lock, self.store.conn:
            self.store.conn.execute("UPDATE replies SET sent=0 WHERE id=?", (int(reply_id),))
        # End-with
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
