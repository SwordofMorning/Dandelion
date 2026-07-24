import os
import json
import datetime

class SessionManager:
    def __init__(self, log_dir=".log"):
        self.log_dir = log_dir
        self.current_session_id = None
        self.current_session_dir = None
        
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
            
        self._ensure_default_session()

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