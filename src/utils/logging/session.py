##
 # @file src/utils/logging/session.py
 # @date 2026/08/11
 # 
 # @brief Session Management.
 # Provides session interface include target and memory.
 #

import os
import json
import datetime
import shutil

##
 # ========================================
 # @section I. Task State (target) and memory const value.
 # ========================================
 #

TASK_STATE_FILENAME = "task_state.json"
SESSION_MEMORY_DIRNAME = "memory"
STAGED_FILENAME = "staged.md"

DEFAULT_TASK_STATE = {"target": "No specific target set.", "todos": [], "completed": []}

##
 # @brief Parse an ISO-8601 string into a naive local datetime.
 #
 # Normalizes timezone-aware values to naive local time so that all
 # recency keys stay comparable across platforms.
 #
 # @param value ISO string (e.g. datetime.now().isoformat()).
 #
 # @return naive datetime, or None if unparseable.
 #
def _parse_iso_time(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None
# End-def

##
 # @brief Parse the creation timestamp embedded in a session ID.
 #
 # Session IDs look like `sess_YYYYMMDD_HHMMSS_ffffff`.
 #
 # @param session_id Session ID string.
 #
 # @return naive datetime, or None if the ID has no parseable stamp.
 #
def _parse_session_id_time(session_id):
    if not isinstance(session_id, str) or not session_id.startswith("sess_"):
        return None
    try:
        return datetime.datetime.strptime(session_id[len("sess_"):], "%Y%m%d_%H%M%S_%f")
    except (ValueError, TypeError):
        return None
# End-def

##
 # @brief Session class, provides all session control for CLI.
 #
class SessionManager:
    ##
     # @brief Constructor.
     # 
     # @param log_dir path to save session file (dir), default to `.log/` subfolder.
     #
    def __init__(self, log_dir=".log"):
        self.log_dir = log_dir
        self.current_session_id = None
        self.current_session_dir = None

        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

        self._ensure_default_session()
    # End-def

    ##
     # ========================================
     # @section II. Session Memory and Target Management
     # ========================================
     #

    ##
     # @brief Ensure a session directory has `task_state.json` and `memory/`.
     #
     # @param session_dir path to save session file (dir), default to `.log/sess_xxxxxx/` subfolder.
     # 
     # @note Session-scoped runtime layout: 
     # task_state.json + memory/ live inside the session directory;
     # so each branch has its own task state and session-local memory.
     #
     # @note Global memory is still in `./llm` subfolder, while NO global task state (target).
     #
    def _ensure_session_layout(self, session_dir):
        os.makedirs(session_dir, exist_ok=True)

        # `memory/`
        memory_dir = os.path.join(session_dir, SESSION_MEMORY_DIRNAME)
        os.makedirs(memory_dir, exist_ok=True)

        # `task_state.json`
        state_file = os.path.join(session_dir, TASK_STATE_FILENAME)
        if not os.path.exists(state_file):
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_TASK_STATE, f, indent=2, ensure_ascii=False)
        # End-if
    # End-def

    ##
     # @brief Path of the current session's task_state.json (None if no active session).
     #
    def get_task_state_file(self):
        if not self.current_session_dir:
            return None
        return os.path.join(self.current_session_dir, TASK_STATE_FILENAME)
    # End-def

    ##
     # @brief Ensure the current session's task_state.json exists.
     #
     # Creates a blank task state (DEFAULT_TASK_STATE) via _ensure_session_layout
     # when the file is missing. Session-scoped only: there is no global task
     # state fallback anymore.
     #
     # @return Absolute path of task_state.json; None if no active session.
     #
    def ensure_task_state_file(self):
        if not self.current_session_dir:
            return None
        self._ensure_session_layout(self.current_session_dir)
        return self.get_task_state_file()
    # End-def

    ##
     # @brief Path of the current session's memory/ dir (None if no active session).
     #
    def get_session_memory_dir(self):
        if not self.current_session_dir:
            return None
        return os.path.join(self.current_session_dir, SESSION_MEMORY_DIRNAME)
    # End-def

    ##
     # @brief Path of the current session's staged draft file (None if no active session).
     #
     # @note The staged area (edited message) lives under `.log/sess_xx/staged.md`,
     #       managed together with history.log / api.log / task_state.json / memory/.
     #       It is session-scoped: `checkout` switches the buffer, `exit` loses nothing.
     #
     # @return Absolute path of `.log/sess_xx/staged.md`, or None.
     #
    def get_staged_file(self):
        if not self.current_session_dir:
            return None
        return os.path.join(self.current_session_dir, STAGED_FILENAME)
    # End-def

    ##
     # @brief Load the staged draft (edited message) of the current session.
     #
     # @return Staged draft text (stripped), or "" when absent/unreadable.
     #
     # @note Missing or corrupt files degrade to "" (same tolerance as
     #       load_history), never blocking startup or commit.
     #
    def load_staged(self):
        staged_file = self.get_staged_file()
        if not staged_file or not os.path.exists(staged_file):
            return ""
        try:
            with open(staged_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            print(f"[-] Warning: Failed to load staged draft from {staged_file}: {e}")
            return ""
    # End-def

    ##
     # @brief Save the staged draft (edited message) of the current session.
     #
     # @param content Staged draft text.
     #
     # @return Success or Fail.
     # @retval True write file success.
     # @retval False write file fail (no active session).
     #
     # @note Atomic write (tmp + os.replace): a crash mid-write must never leave
     #       a half-written draft that would silently truncate the buffer.
     #
    def save_staged(self, content):
        staged_file = self.get_staged_file()
        if not staged_file:
            return False
        tmp_path = staged_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, staged_file)
        return True
    # End-def

    ##
     # @brief Clear the staged draft of the current session.
     #
     # @note Missing file is OK (silent success); a deletion failure only warns.
     #
    def clear_staged(self):
        staged_file = self.get_staged_file()
        if not staged_file or not os.path.exists(staged_file):
            return
        try:
            os.remove(staged_file)
        except OSError as e:
            print(f"[-] Warning: Failed to clear staged draft at {staged_file}: {e}")
    # End-def

    ##
     # @brief Load `task_state.json`.
     #
     # @return `task_state.json` if success.
     #
    def load_task_state(self):
        state_file = self.get_task_state_file()
        if not state_file or not os.path.exists(state_file):
            return None
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[-] Warning: Failed to parse task state at {state_file}: {e}")
            return None
    # End-def

    ##
     # @brief Save `task_state.json`.
     #
     # @param state state json which need to write to `task_state.json`.
     #
     # @return Success or Fail.
     # @retval True write file success.
     # @retval False write file fail.
     #
    def save_task_state(self, state):
        state_file = self.get_task_state_file()
        if not state_file:
            return False
        self._ensure_session_layout(self.current_session_dir)
        # Atomic write (tmp + os.replace): a crash mid-write must never leave a
        # half-written task_state.json that would silently reset the attention
        # anchor to defaults on the next turn.
        tmp_path = state_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, state_file)
        return True
    # End-def

    ##
     # ========================================
     # @section III. Session Control
     # ========================================
     #

    ##
     # @brief If no session, create default_session.
     #
    def _ensure_default_session(self):
        # Auto-load the most recently used (last checked-out) session.
        latest = self.get_most_recent_session()
        latest_id = latest.get("id") if latest else None
        if not latest_id:
            self.create_session("default_session")
        else:
            self.switch_session(latest_id)
    # End-def

    ##
     # @brief Atomically write a session's meta.log.
     #
     # `tmp + os.replace`: a crash mid-write must never leave a truncated
     # meta.log, because `list_sessions()` skips sessions whose meta fails
     # to parse (a truncated meta would hide the session from listing).
     #
     # @param session_dir Session directory.
     # @param meta Session metadata dict.
     #
    def _write_meta(self, session_dir, meta):
        meta_file = os.path.join(session_dir, "meta.log")
        tmp_path = meta_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp_path, meta_file)
    # End-def

    ##
     # @brief Get the most recently used session's metadata.
     #
     # Recency key (git-style "last checkout"):
     #  1. meta.last_used_at (stamped on every switch_session);
     #  2. meta.created_at (legacy fallback, preserves old behavior);
     #  3. creation timestamp embedded in the session ID;
     #  4. epoch minimum (malformed data sorts last).
     # Ties are broken by session ID descending for determinism.
     #
     # @return Most recent session meta, or None if there are no sessions.
     #
    def get_most_recent_session(self):
        # Only metadata that is a dict with a non-empty string id is a valid
        # session. meta.log is normally written by us, but a hand-edited or
        # corrupt file may still parse as valid JSON (e.g. a JSON array, or a
        # dict missing `id`): _recency_key() calls .get() and compares the id,
        # so such entries must be filtered out BEFORE max() to avoid
        # AttributeError/TypeError crashing startup.
        sessions = [
            s for s in self.list_sessions()
            if isinstance(s, dict)
            and isinstance(s.get("id"), str)
            and s.get("id")
        ]
        if not sessions:
            return None
        return max(sessions, key=self._recency_key)
    # End-def

    ##
     # @brief Recency key (datetime, session_id) of one session meta.
     #
     # @param meta Session metadata dict.
     #
     # @return Tuple (datetime, session_id), always comparable.
     #
    def _recency_key(self, meta):
        # 1. last_used_at (primary).
        dt = _parse_iso_time(meta.get("last_used_at"))
        # 2. created_at (legacy fallback).
        if dt is None:
            dt = _parse_iso_time(meta.get("created_at"))
        # 3. Creation timestamp embedded in the session ID.
        if dt is None:
            dt = _parse_session_id_time(meta.get("id"))
        # 4. Malformed data sorts last.
        if dt is None:
            dt = datetime.datetime.min
        return (dt, meta.get("id", ""))
    # End-def

    ##
     # @brief Create one session.
     #
     # @param name Session name.
     #
     # @return Session ID.
     #
    def create_session(self, name=None):
        # Append suffix with time.
        session_id = "sess_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # session_id as name.
        if not name:
            name = session_id

        # Make dir.
        session_dir = os.path.join(self.log_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        # Session meta.log. `last_used_at` = git-style checkout stamp:
        # a fresh session counts as just-used at creation.
        now_iso = datetime.datetime.now().isoformat()
        meta = {
            "id": session_id,
            "name": name,
            "created_at": now_iso,
            "last_used_at": now_iso
        }
        # Write metadata (atomic).
        self._write_meta(session_dir, meta)

        # Post check and switch.
        self.save_history([], session_dir)
        self._ensure_session_layout(session_dir)
        self.switch_session(session_id)
        return session_id
    # End-def

    ##
     # @brief List all sessions.
     #
     # @return All sessions metadata.
     #
    def list_sessions(self):
        sessions = []
        # Iterate log subfolder to get all `sess_xxxx/`.
        for d in sorted(os.listdir(self.log_dir)):
            # Get `sess_xxxx/meta.log` to gather metadata.
            s_dir = os.path.join(self.log_dir, d)
            meta_file = os.path.join(s_dir, "meta.log")
            # Load meta_file, and try to append session metadata to sessions.
            if os.path.isdir(s_dir) and os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        sessions.append(meta)
                except Exception as e:
                    print(f"[-] Warning: Failed to parse session meta at {meta_file}: {e}")
            # End-if
        # End-for
        return sessions
    # End-def

    ##
     # @brief Switch to another session. 
     #
     # @param session_id target session ID.
     #
     # @return Success or fail. 
     # @retval True switch session success.
     # @retval False switch session fail.
     #
    def switch_session(self, session_id):
        safe_session_id = os.path.basename(session_id.strip())
        if not safe_session_id or safe_session_id != session_id.strip():
            print(f"[-] Warning: Invalid session_id format: {session_id}")
            return False
        # End-if

        target_dir = os.path.join(self.log_dir, safe_session_id)
        meta_file = os.path.join(target_dir, "meta.log")

        if not os.path.isdir(target_dir) or not os.path.exists(meta_file):
            return False

        self.current_session_id = safe_session_id
        self.current_session_dir = target_dir
        # Sessions created before this layout existed get lazily initialized.
        self._ensure_session_layout(target_dir)

        # Stamp "last used" (git-style checkout). Every path that loads a
        # session goes through here: startup auto-load, `checkout -b`, and
        # manual `checkout`; opening the app counts as a default checkout.
        # Best-effort: a corrupt meta must not block the switch itself.
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                meta = json.load(f)
            meta["last_used_at"] = datetime.datetime.now().isoformat()
            self._write_meta(target_dir, meta)
        except Exception as e:
            print(f"[-] Warning: Failed to stamp last_used_at for {safe_session_id}: {e}")
        return True
    # End-def

    ##
     # @brief Get current session's metadata.
     #
     # @return metadata.
     #
    def get_current_meta(self):
        meta_file = os.path.join(self.current_session_dir, "meta.log")
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)
    # End-def

    ##
     # @brief Load chat history (used to construct payload).
     # 
     # @return history.log (json format), or empty one.
     #
    def load_history(self):
        if not self.current_session_dir:
            return []
        hist_file = os.path.join(self.current_session_dir, "history.log")
        if not os.path.exists(hist_file):
            return []
        try:
            with open(hist_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[-] Warning: Failed to load history from {hist_file}: {e}")
            return []
    # End-def

    ##
     # @brief Convert Pydantic/SDK blocks (like ThinkingBlock) to standard dicts.
     #
     # @return OBJ's metadata.
     #
    def _default_serializer(self, obj):
        if hasattr(obj, "model_dump"): return obj.model_dump()
        if hasattr(obj, "dict"): return obj.dict()
        if hasattr(obj, "__dict__"): return obj.__dict__
        return str(obj)
    # End-def

    ##
     # @brief Save chat history to session folder.
     #
     # @param history chat history (json format).
     # @param target_dir session dir.
     #
    def save_history(self, history, target_dir=None):
        tdir = target_dir or self.current_session_dir
        if not tdir:
            return
        hist_file = os.path.join(tdir, "history.log")
        with open(hist_file, "w", encoding="utf-8") as f:
            # default=self._default_serializer to handle ThinkingBlock objects
            json.dump(history, f, ensure_ascii=False, indent=2, default=self._default_serializer)
    # End-def

    ##
     # @brief Save request logs.
     # 
     # @param tag MainAgent or SubAgent with serial number (sa-xxxx).
     # @param payload original data (payload, which is sent to LLM) to save.
     #
    def log_api_call(self, tag, payload):
        if not self.current_session_dir:
            return
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"==================== [{tag}] {now_str} ===================="
        log_file = os.path.join(self.current_session_dir, "api.log")

        json_str = json.dumps(payload, ensure_ascii=False, indent=2, default=self._default_serializer)

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{header}\n{json_str}\n\n")
    # End-def

    ##
     # @brief Delete session.
     #
     # @param session_id which session will be deleted.
     #
     # @return Success or fail. 
     # @retval True delete session success.
     # @retval False delete session fail.
     #
    def delete_session(self, session_id):
        # Check existed.
        safe_session_id = os.path.basename(session_id.strip())
        if not safe_session_id or safe_session_id != session_id.strip():
            return False, f"Invalid session_id format: {session_id}"

        # Prevent deleting the currently active session.
        if safe_session_id == self.current_session_id:
            return False, "Cannot delete the currently active branch."

        # Find target session.
        target_dir = os.path.join(self.log_dir, safe_session_id)
        if not os.path.exists(target_dir):
            return False, f"Session '{safe_session_id}' not found."

        # Delete action.
        try:
            shutil.rmtree(target_dir)
            return True, f"Session '{safe_session_id}' deleted successfully."
        except Exception as e:
            return False, f"Failed to delete session: {e}"
    # End-def
# End-class