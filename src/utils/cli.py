# src/utils/cli.py

import os
import shlex
import tempfile
import subprocess

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import NestedCompleter, PathCompleter
    from prompt_toolkit.history import InMemoryHistory
    HAS_PTK = True
except ImportError:
    HAS_PTK = False

class InteractiveCLI:
    def __init__(self, agent_instance, session_manager):
        self.agent = agent_instance
        self.session = session_manager
        self.staged_message = ""
        
        # Initialize prompt_toolkit session with in-memory history
        if HAS_PTK:
            self.prompt_session = PromptSession(history=InMemoryHistory())
        else:
            self.prompt_session = None

    def _build_completer(self):
        """Dynamically build the context-aware completer before each prompt."""
        if not HAS_PTK:
            return None

        sessions = self.session.list_sessions()
        session_targets = {}
        for s in sessions:
            session_targets[s['name']] = None
            session_targets[s['id']] = None

        comp_dict = {
            'branch': {
                '-a': None,
                '-d': session_targets
            },
            'checkout': {
                **session_targets,
                '-b': None
            },
            'vim': None,
            'load': PathCompleter(expanduser=True),
            'status': None,
            'commit': None,
            'clear': None,
            'help': None,
            'quit': None,
            'exit': None
        }
        
        return NestedCompleter.from_nested_dict(comp_dict)
    def _print_help(self):
        print("\n================= REGENT WORKSPACE =================")
        print(" Git-Style Session Management:")
        print("   branch -a             : List all available sessions.")
        print("   branch -d <name/id>   : Delete one session.")
        print("   checkout <name/id>    : Switch to an existing session.")
        print("   checkout -b <name>    : Create and switch to a new session.")
        print("\n Vim-Style Editing:")
        print("   vim                   : Open system editor (Vim/Notepad) to draft prompt.")
        print("   load <filepath>       : Load a local file into the prompt buffer.")
        print("\n Core Operations:")
        print("   status                : View current session and staged buffer.")
        print("   commit                : Send the staged buffer to LLM.")
        print("   clear                 : Clear the staged buffer.")
        print("   help / quit / exit    : System commands.")
        print("====================================================\n")

    def _resolve_session_id(self, target):
        """Map user-friendly session names to exact session IDs"""
        sessions = self.session.list_sessions()
        for s in sessions:
            if target == s['id'] or target == s['name']:
                return s['id']
        return None

    def _cmd_branch(self, args):
        if not args or args[0] == '-a':
            sessions = self.session.list_sessions()
            print("\n[+] Available Sessions (Branches):")
            for s in sessions:
                mark = "*" if s["id"] == self.session.current_session_id else " "
                print(f" {mark} {s['name']:<20} | {s['id']}")
            print()
            
        elif args[0] == '-d':
            if len(args) < 2:
                print("[-] Usage: branch -d <name/id>")
                return
            target = args[1]
            session_id = self._resolve_session_id(target)
            
            if not session_id:
                print(f"[-] Error: Session '{target}' not found.")
                return
                
            ans = input(f"[!] Are you sure you want to delete branch '{target}'? [y/N]: ").strip().lower()
            if ans in ['y', 'yes']:
                success, msg = self.session.delete_session(session_id)
                if success:
                    print(f"[+] {msg}")
                else:
                    print(f"[-] {msg}")
            else:
                print("[-] Deletion aborted.")
        else:
            print(f"[-] Unknown branch argument: {args[0]}. Try 'branch -a' or 'branch -d'.")

    def _cmd_checkout(self, args):
        if not args:
            print("[-] Usage: checkout <name> OR checkout -b <new_name>")
            return

        if args[0] == '-b':
            if len(args) < 2:
                print("[-] Error: Please provide a name for the new session.")
                return
            new_name = args[1]
            new_id = self.session.create_session(new_name)
            self.agent.reload_history()
            print(f"[+] Switched to a new session branch: '{new_name}'")
            return

        target = args[0]
        session_id = self._resolve_session_id(target)
        
        if not session_id:
            print(f"[-] Error: Session '{target}' not found.")
            return
            
        if self.session.switch_session(session_id):
            self.agent.reload_history()
            print(f"[+] Switched to session branch: '{target}'")
        else:
            print(f"[-] Error: Failed to switch to '{target}'. Directory might be corrupted.")

def _cmd_vim(self):
        editor = os.environ.get('EDITOR')
        if not editor:
            editor = 'vim' if os.name != 'nt' else 'notepad'
            
        # Create temporary file for drafting
        fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="regent_draft_", text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(self.staged_message)

            # Use subprocess.call instead of os.system for safety against spaces in paths
            subprocess.call([editor, tmp_path])

            # Read back user input
            with open(tmp_path, 'r', encoding='utf-8') as f:
                new_content = f.read().strip()
                
            if new_content != self.staged_message:
                self.staged_message = new_content
                print("[+] Buffer successfully updated via editor.")
            else:
                print("[*] Buffer unchanged.")
        finally:
            os.remove(tmp_path)

    def _cmd_load(self, args):
        if not args:
            print("[-] Usage: load <filepath>")
            return
            
        filepath = args[0]
        if not os.path.exists(filepath):
            print(f"[-] Error: File not found -> {filepath}")
            return
            
        if self.staged_message.strip():
            ans = input("[!] Warning: The buffer is not empty. Overwrite? [y/N]: ").strip().lower()
            if ans not in ['y', 'yes']:
                print("[-] Load aborted.")
                return
                
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self.staged_message = f.read().strip()
            print(f"[+] Successfully loaded {os.path.getsize(filepath)} bytes into buffer.")
        except Exception as e:
            print(f"[-] Error loading file: {e}")

    def _cmd_status(self):
        meta = self.session.get_current_meta()
        print(f"\n[*] Current Branch : {meta.get('name', 'Unknown')}")
        print(f"[*] History Turns  : {len(self.agent.history)}")
        
        if not self.staged_message:
            print("[*] Staged Buffer  : (Empty)\n")
            return
            
        print("[*] Staged Buffer Preview:")
        print("--------------------------------------------------")
        preview = self.staged_message[:300]
        print(preview)
        if len(self.staged_message) > 300:
            print("\n... [Truncated]")
        print("--------------------------------------------------")
        print(f"    (Total: {len(self.staged_message)} chars)\n")

    def _cmd_commit(self):
        if not self.staged_message.strip():
            print("[-] Error: Buffer is empty. Draft a message using 'vim' or 'load' first.")
            return
            
        print("\n================ COMMIT PREVIEW ================")
        preview = self.staged_message[:500]
        print(preview + ("\n... [Truncated]" if len(self.staged_message) > 500 else ""))
        print("================================================")
        
        ans = input("Proceed to send to LLM? [y/N]: ").strip().lower()
        if ans in ['y', 'yes']:
            self.agent.inject_user_message(self.staged_message)
            self.staged_message = ""
            print("[*] Inference Engine Started...\n")
            while self.agent.step():
                pass
        else:
            print("[-] Send cancelled.")

    def run(self):
        print("\n================ REGENT SHELL READY ================")
        if HAS_PTK:
            print("[+] Bash-style Tab completion enabled (Powered by prompt_toolkit).")
        else:
            print("[-] prompt_toolkit not found. Fallback to basic input. (pip install prompt_toolkit)")
        self._print_help()
        
        while True:
            try:
                # 1. Background task check
                if self.agent.history and self.agent.history[-1]["role"] == "user":
                    content = self.agent.history[-1]["content"]
                    if isinstance(content, list) and len(content) > 0 and content[0].get("type") == "tool_result":
                        print("\n[*] Processing pending tool returns in background...")
                        while self.agent.step():
                            pass
                        continue

                # 2. UI Prompt Render
                meta = self.session.get_current_meta()
                branch_name = meta.get("name", "unknown")
                dirty_flag = "*" if self.staged_message.strip() else ""
                prompt_str = f"regent({branch_name}{dirty_flag})> "
                
                # 3. Read user input with prompt_toolkit
                if HAS_PTK:
                    cmd_input = self.prompt_session.prompt(
                        prompt_str,
                        completer=self._build_completer(),
                        complete_while_typing=False
                    ).strip()
                else:
                    cmd_input = input(prompt_str).strip()
                    
                if not cmd_input:
                    continue
                    
                # 4. Parse and Dispatch
                try:
                    parts = shlex.split(cmd_input)
                except ValueError as e:
                    print(f"[-] Shell syntax error: {e}")
                    continue
                    
                command = parts[0].lower()
                args = parts[1:]
                
                if command in ['help', '-h']:
                    self._print_help()
                elif command in ['quit', 'exit', '-q']:
                    print("[*] Terminating Regent Shell. Goodbye.")
                    break
                elif command == 'branch':
                    self._cmd_branch(args)
                elif command == 'checkout':
                    self._cmd_checkout(args)
                elif command == 'vim':
                    self._cmd_vim()
                elif command == 'load':
                    self._cmd_load(args)
                elif command == 'status':
                    self._cmd_status()
                elif command == 'commit':
                    self._cmd_commit()
                elif command == 'clear':
                    self.staged_message = ""
                    print("[+] Buffer cleared.")
                else:
                    print(f"[-] Unknown command '{command}'. Type 'help' for available commands.")
                    
            except KeyboardInterrupt:
                # Handle Ctrl+C gracefully
                print()
                continue
            except EOFError:
                # Handle Ctrl+D gracefully
                print("\n[*] Terminating Regent Shell (EOF). Goodbye.")
                break