##
 # @file packages/dandelion-core/src/dandelion_core/tool/shell/shell_guard.py
 # @date 2026/11/28
 #
 # @brief Quote-aware shell command segmentation and sensitive-path security guard.
 # Centralizes lexical command splitting, verb extraction, SSH semantic safety
 # rules (P1 + P2 R1-R4), and local BashTool path/boundary validation so quoted
 # literals (e.g. echo ".env") and git range expressions (e.g. git diff a..b)
 # are not falsely blocked while real .env, /etc, ~/, and ../ escapes are caught.
 #

import os
import re
import shlex

SEGMENT_SEPARATORS = (";", "&&", "||", "|")
CMD_PREFIXES = {"sudo", "nohup", "time", "env", "command"}
PRINT_VERBS = {"echo", "printf"}
INLINE_SCRIPT_VERBS = {
    "sh", "bash", "zsh", "dash", "fish", "ksh",
    "powershell", "pwsh", "cmd", "cmd.exe",
    "python", "python3", "py", "perl", "ruby",
    "node", "nodejs", "php", "lua", "awk", "gawk", "mawk",
    "eval", "exec", "xargs",
}

# SSH security rule tables (shared with SSHTool).
SSH_FAST_BLOCK_PATTERNS = [".env", "$(", "`"]
SSH_SENSITIVE_FILES = ["/etc/shadow", "/etc/gshadow"]
SSH_CRITICAL_TOP_NAMES = [
    "etc", "boot", "usr", "var", "root",
    "bin", "sbin", "lib", "dev", "proc", "sys",
]
SSH_BLOCKED_VERBS = [
    # Filesystem / partitioning
    "mke2fs", "wipefs", "blkdiscard", "shred",
    "fdisk", "sfdisk", "parted",
    # Firmware / MTD (embedded-critical)
    "flash_erase", "flashcp", "nandwrite", "ubiformat",
    "mtd", "ubirmvol", "ubimkvol",
    # Credentials
    "passwd", "chpasswd",
    # Session disruption (tunable)
    "shutdown", "poweroff", "halt", "reboot",
]
SSH_DESTRUCTIVE_TARGET_VERBS = ["rm", "mv", "cp"]
SSH_BLOCK_DEVICE_PREFIXES = (
    "/dev/mtd", "/dev/mmcblk", "/dev/sd", "/dev/hd",
    "/dev/ubiblock", "/dev/ubi",
)

# Boundary regexes used inside inline script arguments and command substitutions.
_INLINE_SENSITIVE_PATTERNS = (
    (".env", re.compile(r"(?<![A-Za-z0-9_])\.env(?=$|[/\\'\"\s,;:)\]}])", re.IGNORECASE)),
    ("/etc", re.compile(r"(?<![A-Za-z0-9_])[/\\]etc(?=$|[/\\'\"\s,;:)\]}])", re.IGNORECASE)),
    ("../", re.compile(r"(?<![A-Za-z0-9_.])\.\.[/\\]")),
    ("~/", re.compile(r"(?<![A-Za-z0-9_])~[/\\]")),
)
_SHELL_ESCAPABLE_CHARS = set(" \t'\"\\;&|<>$`*?[]{}()#~")


##
 # @brief Quote-aware shell command parser and security validator.
 #
class ShellCommandGuard:
    ##
     # @brief Split a command into plain token segments on shell separators.
     #
     # @param command Raw command string.
     #
     # @return List of token lists, or None when quotes are unbalanced (fail closed).
     #
    @staticmethod
    def split_segments(command):
        segments = []
        for raw_line in str(command or "").split("\n"):
            line = raw_line.rstrip("\r")
            if not line.strip():
                continue
            # End-if
            try:
                lex = shlex.shlex(line, posix=True, punctuation_chars=";&|")
                tokens = list(lex)
            except ValueError:
                return None
            # End-try

            cur = []
            for tok in tokens:
                if tok in SEGMENT_SEPARATORS:
                    if cur:
                        segments.append(cur)
                        cur = []
                    # End-if
                else:
                    cur.append(tok)
                # End-if
            # End-for
            if cur:
                segments.append(cur)
            # End-if
        # End-for
        return segments
    # End-def

    ##
     # @brief Tokenize a shell command into structured quote-aware segments.
     #
     # Each token dict has keys: text, kind ("word"|"redir"|"fd_dup"), quoted
     # ("unquoted"|"single"|"double"|"mixed"), and has_cmd_sub (bool).
     # Unquoted newlines split segments while quoted newlines stay inside tokens.
     #
     # @param command Raw command string.
     #
     # @return List of segment lists, or None on unbalanced quotes.
     #
    @staticmethod
    def tokenize_segments_detailed(command):
        tokens = ShellCommandGuard._lex_command(str(command or ""))
        if tokens is None:
            return None
        # End-if
        segments = []
        cur = []
        for tok in tokens:
            if tok["kind"] == "sep":
                if cur:
                    segments.append(cur)
                    cur = []
                # End-if
            else:
                cur.append(tok)
            # End-if
        # End-for
        if cur:
            segments.append(cur)
        # End-if
        return segments
    # End-def

    ##
     # @brief Quote-aware lexical scanner preserving quote style, redirs, and cmd-sub flags.
     #
     # @param command Full command string (may contain multiple lines).
     #
     # @return List of token dicts, or None on unbalanced quotes.
     #
    @staticmethod
    def _lex_command(command):
        tokens = []
        n = len(command)
        i = 0

        while i < n:
            ch = command[i]
            if ch in (" ", "\t", "\r"):
                i += 1
                continue
            # End-if
            if ch == "\n":
                tokens.append({"text": "\n", "kind": "sep", "quoted": "unquoted", "has_cmd_sub": False})
                i += 1
                continue
            # End-if
            if ch == "#":
                while i < n and command[i] != "\n":
                    i += 1
                # End-while
                continue
            # End-if

            # Two-character separators.
            two = command[i:i + 2]
            if two in ("&&", "||"):
                tokens.append({"text": two, "kind": "sep", "quoted": "unquoted", "has_cmd_sub": False})
                i += 2
                continue
            # End-if
            if ch in (";", "|"):
                tokens.append({"text": ch, "kind": "sep", "quoted": "unquoted", "has_cmd_sub": False})
                i += 1
                continue
            # End-if

            # Redirection operators (including &>, &>>, >>, <>, >|, >&N).
            if command[i:i + 3] == "&>>":
                tokens.append({"text": "&>>", "kind": "redir", "quoted": "unquoted", "has_cmd_sub": False})
                i += 3
                continue
            # End-if
            if two in (">>", "<>", ">|", "&>"):
                tokens.append({"text": two, "kind": "redir", "quoted": "unquoted", "has_cmd_sub": False})
                i += 2
                continue
            # End-if
            if two in (">&", "<&"):
                j = i + 2
                while j < n and (command[j].isdigit() or command[j] == "-"):
                    j += 1
                # End-while
                tokens.append({"text": command[i:j], "kind": "fd_dup", "quoted": "unquoted", "has_cmd_sub": False})
                i = j
                continue
            # End-if
            if ch in ("<", ">"):
                tokens.append({"text": ch, "kind": "redir", "quoted": "unquoted", "has_cmd_sub": False})
                i += 1
                continue
            # End-if

            # Word token (possibly composed of adjacent quoted/unquoted pieces).
            buf = []
            saw_single = False
            saw_double = False
            saw_unquoted = False
            has_cmd_sub = False
            word_start = i

            while i < n:
                c = command[i]
                if c in (" ", "\t", "\r", "\n", ";", "|", "<", ">"):
                    break
                # End-if
                if c == "&" and (i + 1 < n and command[i + 1] in ("&", ">")):
                    break
                # End-if

                if c == "'":
                    saw_single = True
                    i += 1
                    start = i
                    while i < n and command[i] != "'":
                        i += 1
                    # End-while
                    if i >= n:
                        return None
                    # End-if
                    buf.append(command[start:i])
                    i += 1
                    continue
                # End-if

                if c == '"':
                    saw_double = True
                    i += 1
                    while i < n and command[i] != '"':
                        dc = command[i]
                        if dc == "\\" and i + 1 < n and command[i + 1] in ('"', "\\", "$", "`"):
                            buf.append(command[i + 1])
                            i += 2
                            continue
                        # End-if
                        if dc == "$" and i + 1 < n and command[i + 1] == "(":
                            has_cmd_sub = True
                        elif dc == "`":
                            has_cmd_sub = True
                        # End-if
                        buf.append(dc)
                        i += 1
                    # End-while
                    if i >= n:
                        return None
                    # End-if
                    i += 1
                    continue
                # End-if

                saw_unquoted = True
                if c == "\\" and i + 1 < n:
                    nxt = command[i + 1]
                    if nxt in _SHELL_ESCAPABLE_CHARS:
                        buf.append(nxt)
                        i += 2
                        continue
                    # End-if
                    # Preserve non-metachar backslashes (e.g. Windows ..\ or \etc).
                    buf.append(c)
                    buf.append(nxt)
                    i += 2
                    continue
                # End-if
                if c == "$" and i + 1 < n and command[i + 1] == "(":
                    has_cmd_sub = True
                elif c == "`":
                    has_cmd_sub = True
                # End-if
                buf.append(c)
                i += 1
            # End-while

            word = "".join(buf)
            # Numeric fd prefix immediately preceding a redirection (e.g. 2>/dev/null).
            if (saw_unquoted and not saw_single and not saw_double
                    and word.isdigit() and i < n and command[i] in ("<", ">")):
                if command[i:i + 2] in (">&", "<&"):
                    j = i + 2
                    while j < n and (command[j].isdigit() or command[j] == "-"):
                        j += 1
                    # End-while
                    tokens.append({"text": command[word_start:j], "kind": "fd_dup", "quoted": "unquoted", "has_cmd_sub": False})
                    i = j
                    continue
                # End-if
                if command[i:i + 2] in (">>", "<>", ">|"):
                    tokens.append({"text": word + command[i:i + 2], "kind": "redir", "quoted": "unquoted", "has_cmd_sub": False})
                    i += 2
                    continue
                # End-if
                tokens.append({"text": word + command[i], "kind": "redir", "quoted": "unquoted", "has_cmd_sub": False})
                i += 1
                continue
            # End-if

            modes = sum(1 for flag in (saw_single, saw_double, saw_unquoted) if flag)
            if modes > 1:
                qmode = "mixed"
            elif saw_single:
                qmode = "single"
            elif saw_double:
                qmode = "double"
            else:
                qmode = "unquoted"
            # End-if
            tokens.append({"text": word, "kind": "word", "quoted": qmode, "has_cmd_sub": has_cmd_sub})
        # End-while
        return tokens
    # End-def

    ##
     # @brief Extract the command verb basename from a segment's string tokens.
     #
     # @param tokens Token list of one command segment.
     #
     # @return Verb basename string, or None when segment has no command verb.
     #
    @staticmethod
    def extract_verb(tokens):
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            is_env_assign = len(tok) > 2 and "=" in tok and not tok.startswith("=")
            is_prefix = tok in CMD_PREFIXES
            if not (is_env_assign or is_prefix):
                break
            # End-if
            i += 1
        # End-while
        if i >= len(tokens):
            return None
        # End-if
        verb = os.path.basename(tokens[i])
        if verb == "busybox" and i + 1 < len(tokens):
            return os.path.basename(tokens[i + 1])
        # End-if
        return verb
    # End-def

    ##
     # @brief Return positional (non-option) arguments of a segment.
     #
     # @param tokens Token list of one segment.
     #
     # @return List of positional argument strings.
     #
    @staticmethod
    def positional_args(tokens):
        args = []
        after_dashdash = False
        for tok in tokens[1:]:
            if after_dashdash:
                args.append(tok)
            elif tok == "--":
                after_dashdash = True
            elif tok.startswith("-"):
                continue
            else:
                args.append(tok)
            # End-if
        # End-for
        return args
    # End-def

    ##
     # @brief Inspect a path-like token for sensitive local boundary patterns.
     #
     # @param token Decoded token text.
     #
     # @return Matched sensitive pattern label (".env", "/etc", "../", "~/") or None.
     #
    @staticmethod
    def match_sensitive_path(token):
        if not token:
            return None
        # End-if
        raw = str(token).strip()
        if not raw:
            return None
        # End-if

        candidates = [raw]
        # Handle KEY=value or --flag=value assignments (e.g. if=/etc/passwd, --file=.env/cli.cfg).
        if "=" in raw and not raw.startswith("="):
            rhs = raw.split("=", 1)[1].strip().strip("\"'")
            if rhs:
                candidates.append(rhs)
            # End-if
        # End-if

        for item in candidates:
            norm = item.replace("\\", "/")
            low = norm.lower()

            # Home directory expansion (~, ~/, ~user/).
            if low == "~" or low.startswith("~/") or re.match(r"^~[a-z0-9._-]+(/|$)", low):
                return "~/"
            # End-if

            # /etc or \etc top-level system configuration path.
            stripped_leading = low.lstrip("/")
            if norm.startswith("/") and (stripped_leading == "etc" or stripped_leading.startswith("etc/")):
                return "/etc"
            # End-if

            # Path component checks: .env (or .env.*) and parent traversal (..).
            parts = [p for p in norm.split("/") if p]
            for part in parts:
                part_low = part.lower()
                if part_low == ".env" or part_low.startswith(".env."):
                    return ".env"
                # End-if
                if part == "..":
                    return "../"
                # End-if
            # End-for
        # End-for
        return None
    # End-def

    ##
     # @brief Scan inline script text or command substitution for sensitive path references.
     #
     # @param text Raw script or substitution text.
     #
     # @return Matched sensitive pattern label or None.
     #
    @staticmethod
    def match_inline_sensitive(text):
        raw = str(text or "")
        for label, pattern in _INLINE_SENSITIVE_PATTERNS:
            if pattern.search(raw):
                return label
            # End-if
        # End-for
        return None
    # End-def

    ##
     # @brief Determine whether a token looks like a filesystem path for workspace approval.
     #
     # @param token Decoded token text.
     #
     # @return Path string to check against workspace_dir, or None.
     #
    @staticmethod
    def extract_candidate_path(token):
        if not token:
            return None
        # End-if
        tok = str(token).strip()
        if not tok:
            return None
        # End-if
        # Inspect RHS of --opt=path or key=path when it looks like a path.
        if "=" in tok and not tok.startswith("="):
            rhs = tok.split("=", 1)[1].strip().strip("\"'")
            if rhs and (rhs.startswith("/") or rhs.startswith("\\") or re.match(r"^[a-zA-Z]:", rhs)):
                tok = rhs
            # End-if
        # End-if
        if re.match(r"^[a-zA-Z]:", tok):
            return tok
        # End-if
        if "\\" in tok or "/" in tok:
            if re.match(r"^/[a-zA-Z?]$", tok):
                return None
            # End-if
            return tok
        # End-if
        if tok == "..":
            return tok
        # End-if
        return None
    # End-def

    ##
     # @brief Validate a local BashTool command using quote-aware segmentation.
     #
     # @param command Raw local shell command string.
     #
     # @return (is_safe, error_message, candidate_paths) tuple.
     #
    @classmethod
    def check_local_command(cls, command):
        segments = cls.tokenize_segments_detailed(command)
        if segments is None:
            return False, (
                "CRITICAL SECURITY BLOCK: Command could not be tokenized "
                "(unbalanced quotes). Refusing to execute."
            ), []
        # End-if

        candidate_paths = []
        for seg in segments:
            word_texts = [t["text"] for t in seg if t["kind"] == "word"]
            verb = cls.extract_verb(word_texts)
            verb_low = verb.lower() if verb else ""
            is_print = verb_low in PRINT_VERBS
            is_inline = verb_low in INLINE_SCRIPT_VERBS

            expect_redir_target = False
            saw_verb = False

            for tok in seg:
                kind = tok["kind"]
                text = tok["text"]
                quoted = tok["quoted"]
                has_cmd_sub = tok["has_cmd_sub"]

                if kind == "redir":
                    expect_redir_target = True
                    continue
                # End-if
                if kind == "fd_dup":
                    continue
                # End-if

                # Identify env assignments / prefixes preceding the command verb.
                is_env_assign = (
                    not saw_verb and not expect_redir_target
                    and len(text) > 2 and "=" in text and not text.startswith("=")
                )
                is_prefix = not saw_verb and not expect_redir_target and text in CMD_PREFIXES
                if not saw_verb and not expect_redir_target and not is_env_assign and not is_prefix:
                    saw_verb = True
                # End-if

                # Command substitutions ($(...) or backticks) can hide arbitrary file reads.
                if has_cmd_sub:
                    hit = cls.match_inline_sensitive(text)
                    if hit:
                        return False, cls._local_block_message(hit), []
                    # End-if
                # End-if

                # Redirection targets are always real filesystem paths (even for echo/printf).
                if expect_redir_target:
                    expect_redir_target = False
                    hit = cls.match_sensitive_path(text)
                    if hit:
                        return False, cls._local_block_message(hit), []
                    # End-if
                    candidate_paths.append(text)
                    continue
                # End-if

                # Pure print commands (echo/printf): plain non-glob arguments are literal text.
                if is_print and saw_verb and not is_env_assign and not is_prefix:
                    is_unquoted_glob = (
                        quoted in ("unquoted", "mixed")
                        and any(g in text for g in ("*", "?", "["))
                    )
                    if is_unquoted_glob:
                        hit = cls.match_sensitive_path(text)
                        if hit:
                            return False, cls._local_block_message(hit), []
                        # End-if
                        cpath = cls.extract_candidate_path(text)
                        if cpath:
                            candidate_paths.append(cpath)
                        # End-if
                    # End-if
                    continue
                # End-if

                # Inline script runners (python -c, bash -c, awk, etc.): scan full argument text.
                if is_inline and saw_verb:
                    hit = cls.match_inline_sensitive(text)
                    if hit:
                        return False, cls._local_block_message(hit), []
                    # End-if
                # End-if

                # Standard path-boundary sensitive check for all other tokens.
                hit = cls.match_sensitive_path(text)
                if hit:
                    return False, cls._local_block_message(hit), []
                # End-if

                cpath = cls.extract_candidate_path(text)
                if cpath:
                    candidate_paths.append(cpath)
                # End-if
            # End-for
        # End-for

        return True, "", candidate_paths
    # End-def

    ##
     # @brief Format the standardized local sandbox block message.
     #
     # @param pattern Blocked sensitive pattern label.
     #
     # @return Error message string.
     #
    @staticmethod
    def _local_block_message(pattern):
        return (
            f"CRITICAL SECURITY BLOCK: Pattern '{pattern}' is forbidden. "
            f"STOP IMMEDIATELY. Do not attempt to use workarounds, PowerShell expressions, "
            f"or alternative paths to bypass this. Acknowledge the restriction to the user."
        )
    # End-def

    ##
     # @brief R2 SSH rule: check whether a destructive target resolves to a critical location.
     #
     # @param target Target path token (quotes already stripped by shlex).
     #
     # @return True when the target must be blocked.
     #
    @staticmethod
    def destructive_target_blocked(target):
        if not target or target.startswith("-"):
            return False
        # End-if

        a = target
        while a.startswith("./"):
            a = a[2:]
        # End-while

        if a.startswith("/"):
            if a in ("/", "/*"):
                return True
            # End-if
            parts = [p for p in a.split("/") if p]
            if len(parts) == 1 and parts[0] in SSH_CRITICAL_TOP_NAMES:
                return True
            # End-if
            if len(parts) == 2 and parts[0] in SSH_CRITICAL_TOP_NAMES and parts[1] == "*":
                return True
            # End-if
            return False
        # End-if

        segs = a.split("/")
        i = 0
        while i < len(segs) and segs[i] == "..":
            i += 1
        # End-while
        if i > 0:
            rest = segs[i:]
            if not rest:
                return True
            # End-if
            return rest[0] in SSH_CRITICAL_TOP_NAMES
        # End-if

        return False
    # End-def

    ##
     # @brief Full SSH semantic safety check (P1 + P2 rules R1-R4).
     #
     # @param command Raw remote shell command string.
     # @param sec_cfg Optional per-device security override {"allow": [...], "block": [...]}.
     #
     # @return (True, "") when safe, (False, reason) when blocked.
     #
    @classmethod
    def check_ssh_command(cls, command, sec_cfg=None):
        sec_cfg = sec_cfg or {}
        allow = set(sec_cfg.get("allow", []) or [])
        block = set(sec_cfg.get("block", []) or [])

        # P1. Fast substring blacklist.
        lower = str(command or "").lower()
        for pat in SSH_FAST_BLOCK_PATTERNS:
            if pat in lower:
                return False, (
                    f"CRITICAL SECURITY BLOCK [P1]: pattern '{pat}' is forbidden. "
                    f"STOP IMMEDIATELY. Do not attempt workarounds."
                )
            # End-if
        # End-for

        # P2. Semantic rules across quote-aware segments.
        segments = cls.split_segments(command)
        if segments is None:
            return False, (
                "CRITICAL SECURITY BLOCK: command could not be tokenized "
                "(unbalanced quotes). Refusing to execute."
            )
        # End-if

        for seg in segments:
            verb = cls.extract_verb(seg)
            if not verb:
                continue
            # End-if

            if verb in block:
                return False, f"CRITICAL SECURITY BLOCK [R3/device]: verb '{verb}' is blocked for this device."
            # End-if

            # R1. Sensitive files.
            for tok in seg:
                if any(tok == f or tok.startswith(f + "/") for f in SSH_SENSITIVE_FILES):
                    return False, f"CRITICAL SECURITY BLOCK [R1]: sensitive file '{tok}' is forbidden."
                # End-if
            # End-for

            # R2. Destructive targets (rm / mv / cp).
            if verb in SSH_DESTRUCTIVE_TARGET_VERBS:
                for arg in cls.positional_args(seg):
                    if cls.destructive_target_blocked(arg):
                        return False, f"CRITICAL SECURITY BLOCK [R2]: destructive target '{arg}' is forbidden."
                    # End-if
                # End-for
            # End-if

            # R3. Destructive verbs.
            if verb not in allow:
                if verb in SSH_BLOCKED_VERBS or verb.startswith("mkfs"):
                    return False, f"CRITICAL SECURITY BLOCK [R3]: verb '{verb}' is blocked."
                # End-if
                if verb == "init" and any(a in ("0", "1", "6") for a in cls.positional_args(seg)):
                    return False, "CRITICAL SECURITY BLOCK [R3]: 'init' runlevel change is blocked."
                # End-if
            # End-if

            # R4. dd writing to block devices.
            if verb == "dd":
                for tok in seg:
                    if tok.startswith("of=") and tok[3:].startswith(SSH_BLOCK_DEVICE_PREFIXES):
                        return False, f"CRITICAL SECURITY BLOCK [R4]: dd write target '{tok[3:]}' is forbidden."
                    # End-if
                # End-for
            # End-if
        # End-for

        return True, ""
    # End-def
# End-class
