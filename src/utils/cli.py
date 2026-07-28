# src/utils/cli.py

import os
import shlex
import tempfile
import subprocess
import builtins

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import NestedCompleter, PathCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.formatted_text import ANSI
    HAS_PTK = True
except ImportError:
    HAS_PTK = False

class InteractiveCLI:
    # ANSI Color Codes
    C_RESET   = "\033[0m"
    C_RED     = "\033[31m"
    C_GREEN   = "\033[32m"
    C_YELLOW  = "\033[33m"
    C_BLUE    = "\033[34m"
    C_MAGENTA = "\033[35m"
    C_CYAN    = "\033[36m"
    C_GRAY    = "\033[90m"

    @staticmethod
    def CLI_Print(msg, level="info", end="\n"):
        """
        Centralized print function with color control and switch dispatch.
        Levels: info, success, error, warning, debug, agent, raw
        """
        # Handle prefix newline cleanly
        if msg.startswith("\n"):
            builtins.print("\n", end="")
            msg = msg.lstrip("\n")

        prefix = ""
        
        # Switch dispatcher
        if level == "info":
            prefix = f"{InteractiveCLI.C_CYAN}[*]{InteractiveCLI.C_RESET} "
        elif level == "success":
            prefix = f"{InteractiveCLI.C_GREEN}[+]{InteractiveCLI.C_RESET} "
        elif level == "error":
            prefix = f"{InteractiveCLI.C_RED}[-]{InteractiveCLI.C_RESET} "
        elif level == "warning":
            prefix = f"{InteractiveCLI.C_YELLOW}[!]{InteractiveCLI.C_RESET} "
        elif level == "debug":
            prefix = f"{InteractiveCLI.C_GRAY}[>]{InteractiveCLI.C_RESET} "
        elif level == "agent":
            prefix = f"{InteractiveCLI.C_MAGENTA}[Agent]{InteractiveCLI.C_RESET} "
        elif level == "raw":
            prefix = ""
            
        builtins.print(f"{prefix}{msg}", end=end)

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
        help_text = (
            f"{self.C_CYAN}\n================= REGENT WORKSPACE ================={self.C_RESET}\n"
            " Git-Style Session Management:\n"
            "   branch -a             : List all available sessions.\n"
            "   branch -d <name/id>   : Delete one session.\n"
            "   checkout <name/id>    : Switch to an existing session.\n"
            "   checkout -b <name>    : Create and switch to a new session.\n\n"
            " Vim-Style Editing:\n"
            "   vim                   : Open system editor (Vim/Notepad) to draft prompt.\n"
            "   load <filepath>       : Load a local file into the prompt buffer.\n\n"
            " Core Operations:\n"
            "   status                : View current session and staged buffer.\n"
            "   commit                : Send the staged buffer to LLM.\n"
            "   clear                 : Clear the staged buffer.\n"
            "   help / quit / exit    : System commands.\n"
            f"{self.C_CYAN}===================================================={self.C_RESET}\n"
        )
        self.CLI_Print(help_text, level="raw")

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
            self.CLI_Print("\nAvailable Sessions (Branches):", level="success")
            for s in sessions:
                mark = "*" if s["id"] == self.session.current_session_id else " "
                self.CLI_Print(f" {mark} {s['name']:<20} | {s['id']}", level="raw")
            self.CLI_Print("", level="raw")
            
        elif args[0] == '-d':
            if len(args) < 2:
                self.CLI_Print("Usage: branch -d <name/id>", level="error")
                return
            target = args[1]
            session_id = self._resolve_session_id(target)
            
            if not session_id:
                self.CLI_Print(f"Error: Session '{target}' not found.", level="error")
                return
                
            ans = input(f"{self.C_YELLOW}[!]{self.C_RESET} Are you sure you want to delete branch '{target}'? [y/N]: ").strip().lower()
            if ans in ['y', 'yes']:
                success, msg = self.session.delete_session(session_id)
                if success:
                    self.CLI_Print(msg, level="success")
                else:
                    self.CLI_Print(msg, level="error")
            else:
                self.CLI_Print("Deletion aborted.", level="error")
        else:
            self.CLI_Print(f"Unknown branch argument: {args[0]}. Try 'branch -a' or 'branch -d'.", level="error")

    def _cmd_checkout(self, args):
        if not args:
            self.CLI_Print("Usage: checkout <name> OR checkout -b <new_name>", level="error")
            return

        if args[0] == '-b':
            if len(args) < 2:
                self.CLI_Print("Error: Please provide a name for the new session.", level="error")
                return
            new_name = args[1]
            new_id = self.session.create_session(new_name)
            self.agent.reload_history()
            self.CLI_Print(f"Switched to a new session branch: '{new_name}'", level="success")
            return

        target = args[0]
        session_id = self._resolve_session_id(target)
        
        if not session_id:
            self.CLI_Print(f"Error: Session '{target}' not found.", level="error")
            return
            
        if self.session.switch_session(session_id):
            self.agent.reload_history()
            self.CLI_Print(f"Switched to session branch: '{target}'", level="success")
        else:
            self.CLI_Print(f"Error: Failed to switch to '{target}'. Directory might be corrupted.", level="error")

    def _cmd_vim(self):
        editor = os.environ.get('EDITOR')
        if not editor:
            editor = 'vim' if os.name != 'nt' else 'notepad'
            
        # Create temporary file for drafting
        fd, tmp_path = tempfile.mkstemp(suffix=".md", prefix="regent_draft_", text=True)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(self.staged_message)

            # Parse the editor command with shlex to support flags
            editor_cmd = shlex.split(editor, posix=(os.name != 'nt'))
            # Normalize the executable token on Windows to remove surrounding quotes
            if os.name == 'nt' and editor_cmd:
                editor_cmd[0] = editor_cmd[0].strip('"').strip("'")

            editor_cmd.append(tmp_path)
            subprocess.call(editor_cmd)

            # Read back user input
            with open(tmp_path, 'r', encoding='utf-8') as f:
                new_content = f.read().strip()
                
            if new_content != self.staged_message:
                self.staged_message = new_content
                self.CLI_Print("Buffer successfully updated via editor.", level="success")
            else:
                self.CLI_Print("Buffer unchanged.", level="info")
        finally:
            os.remove(tmp_path)

    def _cmd_load(self, args):
        if not args:
            self.CLI_Print("Usage: load <filepath>", level="error")
            return
            
        filepath = args[0]
        if not os.path.exists(filepath):
            self.CLI_Print(f"Error: File not found -> {filepath}", level="error")
            return
            
        if self.staged_message.strip():
            ans = input(f"{self.C_YELLOW}[!]{self.C_RESET} Warning: The buffer is not empty. Overwrite? [y/N]: ").strip().lower()
            if ans not in ['y', 'yes']:
                self.CLI_Print("Load aborted.", level="error")
                return
                
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                self.staged_message = f.read().strip()
            self.CLI_Print(f"Successfully loaded {os.path.getsize(filepath)} bytes into buffer.", level="success")
        except Exception as e:
            self.CLI_Print(f"Error loading file: {e}", level="error")

    def _cmd_status(self):
        meta = self.session.get_current_meta()
        self.CLI_Print(f"\nCurrent Branch : {meta.get('name', 'Unknown')}", level="info")
        self.CLI_Print(f"History Turns  : {len(self.agent.history)}", level="info")
        
        if not self.staged_message:
            self.CLI_Print("Staged Buffer  : (Empty)\n", level="info")
            return
            
        self.CLI_Print("Staged Buffer Preview:", level="info")
        self.CLI_Print("-" * 50, level="raw")
        preview = self.staged_message[:300]
        self.CLI_Print(preview, level="raw")
        if len(self.staged_message) > 300:
            self.CLI_Print("\n... [Truncated]", level="raw")
        self.CLI_Print("-" * 50, level="raw")
        self.CLI_Print(f"    (Total: {len(self.staged_message)} chars)\n", level="raw")

    def _cmd_commit(self):
        if not self.staged_message.strip():
            self.CLI_Print("Error: Buffer is empty. Draft a message using 'vim' or 'load' first.", level="error")
            return
            
        self.CLI_Print(f"\n{self.C_CYAN}================ COMMIT PREVIEW ================{self.C_RESET}", level="raw")
        preview = self.staged_message[:500]
        self.CLI_Print(preview + ("\n... [Truncated]" if len(self.staged_message) > 500 else ""), level="raw")
        self.CLI_Print(f"{self.C_CYAN}================================================{self.C_RESET}", level="raw")
        
        ans = input(f"{self.C_CYAN}[?]{self.C_RESET} Proceed to send to LLM? [y/N]: ").strip().lower()
        if ans in ['y', 'yes']:
            self.agent.inject_user_message(self.staged_message)
            self.staged_message = ""
            self.CLI_Print("Inference Engine Started...\n", level="info")
            while self.agent.step():
                pass
        else:
            self.CLI_Print("Send cancelled.", level="error")

    def run(self):
        self.CLI_Print(f"\n{self.C_CYAN}================ REGENT SHELL READY ================{self.C_RESET}", level="raw")
        if HAS_PTK:
            self.CLI_Print("Bash-style Tab completion enabled (Powered by prompt_toolkit).", level="success")
        else:
            self.CLI_Print("prompt_toolkit not found. Fallback to basic input. (pip install prompt_toolkit)", level="error")
        self._print_help()
        
        # Track consecutive errors to prevent infinite loop of death
        consecutive_errors = 0 
        
        while True:
            try:
                # 1. Background task check
                if self.agent.history and self.agent.history[-1]["role"] == "user":
                    content = self.agent.history[-1]["content"]
                    if isinstance(content, list) and len(content) > 0 and content[0].get("type") == "tool_result":
                        self.CLI_Print("\nProcessing pending tool returns in background...", level="info")
                        
                        initial_history_len = len(self.agent.history)
                        
                        while self.agent.step():
                            pass
                            
                        # Safeguard: If step() failed due to an API Error, the history size remains unchanged.
                        # We must pop the stuck tool_result to break the infinite 429 retry loop.
                        if len(self.agent.history) == initial_history_len:
                            self.CLI_Print("\nFATAL: Background execution blocked by an API Error.", level="error")
                            self.CLI_Print("Dropping the pending tool result to prevent infinite API retry loop.", level="error")
                            self.agent.history.pop() 
                            self.session.save_history(self.agent.history)
                            
                        continue

                # 2. UI Prompt Render
                meta = self.session.get_current_meta()
                branch_name = meta.get("name", "unknown")
                dirty_flag = "*" if self.staged_message.strip() else ""
                
                # Fetch model name and format it to be clean (e.g. "nvidia/nemotron" -> "nemotron")
                full_model_name = self.agent.config.get("MODEL_ID", "regent")
                short_model_name = full_model_name.split("/")[-1] if "/" in full_model_name else full_model_name
                
                # Linux-Style Colored Prompt Formatting
                prompt_str_ansi = (
                    f"{self.C_GREEN}{short_model_name}{self.C_RESET}:"
                    f"{self.C_BLUE}({branch_name}"
                    f"{self.C_YELLOW}{dirty_flag}"
                    f"{self.C_BLUE}){self.C_RESET}"
                    f"{self.C_GRAY}>{self.C_RESET} "
                )
                
                # 3. Read user input
                if HAS_PTK:
                    cmd_input = self.prompt_session.prompt(
                        ANSI(prompt_str_ansi),
                        completer=self._build_completer(),
                        complete_while_typing=False
                    ).strip()
                else:
                    cmd_input = input(prompt_str_ansi).strip()
                    
                # Reset error counter because we successfully reached the blocking input layer
                consecutive_errors = 0
                    
                if not cmd_input:
                    continue
                    
                # 4. Parse and Dispatch
                try:
                    parts = shlex.split(cmd_input)
                except ValueError as e:
                    self.CLI_Print(f"Shell syntax error: {e}", level="error")
                    continue
                    
                command = parts[0].lower()
                args = parts[1:]
                
                if command in ['help', '-h']:
                    self._print_help()
                elif command in ['quit', 'exit', '-q']:
                    self.CLI_Print("Terminating Regent Shell. Goodbye.", level="info")
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
                    self.CLI_Print("Buffer cleared.", level="success")
                else:
                    self.CLI_Print(f"Unknown command '{command}'. Type 'help' for available commands.", level="error")
                    
            except KeyboardInterrupt:
                self.CLI_Print("", level="raw")
                continue
            except EOFError:
                self.CLI_Print("\nTerminating Regent Shell (EOF). Goodbye.", level="info")
                break
            except Exception as e:
                consecutive_errors += 1
                self.CLI_Print(f"\nUnexpected Error: {e}", level="error")
                
                # Break out if the loop is spinning wildly without user interaction
                if consecutive_errors >= 3:
                    self.CLI_Print("FATAL: Too many consecutive errors. Terminating shell to prevent infinite loop.", level="warning")
                    break
                    
                self.CLI_Print("Shell recovered. Your staged message and session are preserved.", level="info")
                continue