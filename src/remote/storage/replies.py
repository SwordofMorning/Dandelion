##
 # @file src/remote/storage/replies.py
 # @date 2026/10/28
 #
 # @brief Durable conversation replies and transport cursors.
 # Persists outgoing messages, reads replies after a caller-owned identifier,
 # and records transport offsets using the shared lifecycle database lock.
 #
 # @note Transport delivery and acknowledgement are managed by their channel adapters.
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
     # @brief Queue a durable reply for one conversation.
     #
     # @param session_id Conversation identifier used to scope persisted records.
     # @param text Text to persist or emit.
     #
     # @return None.
     #
    def notify(self, session_id, text):
        with self.store.lock, self.store.conn:
            self.store.conn.execute(
                "INSERT INTO replies(session_id,text) VALUES (?,?)",
                (session_id, str(text)),
            )
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
