##
 # @file src/utils/logging/session.py
 # @date 2026/08/05
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

DEFAULT_TASK_STATE = {"target": "No specific target set.", "todos": [], "completed": []}

##
 # @brief Session class, provides all session control for CLI.
 #
class SessionManager:
    ##
     # @brief Constructor.
     # 
     # @param log_dir, path to save session file (dir), default to `.log/` subfolder.
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
     # @param session_dir, path to save session file (dir), default to `.log/sess_xxxxxx/` subfolder.
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
     # @brief Path of the current session's memory/ dir (None if no active session).
     #
    def get_session_memory_dir(self):
        if not self.current_session_dir:
            return None
        return os.path.join(self.current_session_dir, SESSION_MEMORY_DIRNAME)
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
     # @param state, state json which need to write to `task_state.json`.
     #
     # @return Success or Fail.
     # @retval True, write file success.
     # @retval False, write file fail.
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

    def migrate_legacy_task_state(self, workspace_dir):
        """One-time, idempotent migration of the legacy global task state
        (llm/task/task_state.json) into the current session's task_state.json.

        - Copies the legacy content into the session file only when the session
          file is missing OR still holds the empty default placeholder.
        - Backs up (renames) the legacy global file so old code stops seeing it.
        Safe to call on every startup.
        """
        legacy_file = os.path.join(workspace_dir, "llm", "task", "task_state.json")
        if not os.path.exists(legacy_file):
            return False

        state_file = self.get_task_state_file()
        if not state_file:
            return False

        self._ensure_session_layout(self.current_session_dir)

        # Never overwrite real session state; only fill empty placeholders.
        should_copy = not os.path.exists(state_file)
        if not should_copy:
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                should_copy = existing == DEFAULT_TASK_STATE
            except Exception:
                should_copy = False

        if should_copy:
            shutil.copy2(legacy_file, state_file)

        backup_file = legacy_file + ".legacy-backup"
        if not os.path.exists(backup_file):
            os.rename(legacy_file, backup_file)
        return True

    def _ensure_default_session(self):
        sessions = self.list_sessions()
        if not sessions:
            self.create_session("default_session")
        else:
            # Auto-load the most recent session
            self.switch_session(sessions[-1]["id"])

    def create_session(self, name=None):
        session_id = "sess_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if not name:
            name = session_id
            
        session_dir = os.path.join(self.log_dir, session_id)
        os.makedirs(session_dir, exist_ok=True)
        
        meta = {
            "id": session_id,
            "name": name,
            "created_at": datetime.datetime.now().isoformat()
        }
        
        with open(os.path.join(session_dir, "meta.log"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            
        self.save_history([], session_dir)
        self._ensure_session_layout(session_dir)
        self.switch_session(session_id)
        return session_id

    def list_sessions(self):
        sessions = []
        for d in sorted(os.listdir(self.log_dir)):
            s_dir = os.path.join(self.log_dir, d)
            meta_file = os.path.join(s_dir, "meta.log")
            if os.path.isdir(s_dir) and os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        sessions.append(meta)
                except Exception as e:
                    print(f"[-] Warning: Failed to parse session meta at {meta_file}: {e}")
        return sessions

    def switch_session(self, session_id):
        safe_session_id = os.path.basename(session_id.strip())
        if not safe_session_id or safe_session_id != session_id.strip():
            print(f"[-] Warning: Invalid session_id format: {session_id}")
            return False

        target_dir = os.path.join(self.log_dir, safe_session_id)
        meta_file = os.path.join(target_dir, "meta.log")

        if not os.path.isdir(target_dir) or not os.path.exists(meta_file):
            return False

        self.current_session_id = safe_session_id
        self.current_session_dir = target_dir
        # Sessions created before this layout existed get lazily initialized.
        self._ensure_session_layout(target_dir)
        return True

    def get_current_meta(self):
        meta_file = os.path.join(self.current_session_dir, "meta.log")
        with open(meta_file, "r", encoding="utf-8") as f:
            return json.load(f)

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

    def _default_serializer(self, obj):
        # Convert Pydantic/SDK blocks (like ThinkingBlock) to standard dicts
        if hasattr(obj, "model_dump"): return obj.model_dump()
        if hasattr(obj, "dict"): return obj.dict()
        if hasattr(obj, "__dict__"): return obj.__dict__
        return str(obj)

    def save_history(self, history, target_dir=None):
        tdir = target_dir or self.current_session_dir
        if not tdir:
            return
        hist_file = os.path.join(tdir, "history.log")
        with open(hist_file, "w", encoding="utf-8") as f:
            # default=self._default_serializer to handle ThinkingBlock objects
            json.dump(history, f, ensure_ascii=False, indent=2, default=self._default_serializer)

    def log_api_call(self, tag, payload):
        if not self.current_session_dir:
            return
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"==================== [{tag}] {now_str} ===================="
        log_file = os.path.join(self.current_session_dir, "api.log")
        
        json_str = json.dumps(payload, ensure_ascii=False, indent=2, default=self._default_serializer)
        
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{header}\n{json_str}\n\n")

    def delete_session(self, session_id):
        safe_session_id = os.path.basename(session_id.strip())
        if not safe_session_id or safe_session_id != session_id.strip():
            return False, f"Invalid session_id format: {session_id}"
            
        # Prevent deleting the currently active session
        if safe_session_id == self.current_session_id:
            return False, "Cannot delete the currently active branch."
            
        target_dir = os.path.join(self.log_dir, safe_session_id)
        if not os.path.exists(target_dir):
            return False, f"Session '{safe_session_id}' not found."
            
        try:
            shutil.rmtree(target_dir)
            return True, f"Session '{safe_session_id}' deleted successfully."
        except Exception as e:
            return False, f"Failed to delete session: {e}"
# End-class