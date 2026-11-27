##
 # @file packages/dandelion-core/src/dandelion_core/tool/shell/shell_guard.py
 # @date 2026/11/28
 #
 # @brief Single-pass quote-aware shell command parser and security validator.
 # Handles POSIX line continuations, single/double/ANSI-C/locale quoting,
 # heredocs, here-strings, balanced command/parameter substitutions, subshell
 # delimiters, wrapper unwrapping, brace/glob expansion checks, and path
 # canonicalization for both local BashTool and remote SSHTool.
 #

import fnmatch
import os
import posixpath
import re

SEGMENT_SEPARATORS = (";", "&&", "||", "|", "|&", "&", "(", ")", "\n")
PRINT_VERBS = {"echo", "printf"}
SEARCH_PATTERN_VERBS = {"grep", "egrep", "fgrep", "rg"}
SHELL_EVAL_VERBS = {"sh", "bash", "zsh", "dash", "fish", "ksh"}
INLINE_SCRIPT_VERBS = {
    "sh", "bash", "zsh", "dash", "fish", "ksh",
    "powershell", "pwsh", "cmd", "cmd.exe",
    "python", "python3", "py", "perl", "ruby",
    "node", "nodejs", "php", "lua", "awk", "gawk", "mawk",
    "eval", "exec", "xargs",
}

# Wrapper commands unwrapped by extract_verb_and_args():
# verb -> (flags_taking_value_set, extra_positional_args_before_cmd)
_WRAPPER_SPECS = {
    "sudo": (
        {
            "-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U",
            "--user", "--group", "--chdir", "--host", "--prompt",
            "--role", "--type", "--command-timeout", "--other-user",
        },
        0,
    ),
    "env": (
        {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
        0,
    ),
    "nohup": (set(), 0),
    "time": ({"-f", "-o", "--format", "--output"}, 0),
    "command": (set(), 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "timeout": ({"-k", "-s", "--kill-after", "--signal"}, 1),
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, 0),
    "ionice": ({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"}, 0),
    "chrt": ({"-p", "--pid"}, 1),
    "setsid": (set(), 0),
    "exec": ({"-a"}, 0),
    "xargs": ({"-I", "-i", "-n", "-P", "-L", "-l", "-d", "-E", "-e", "-s", "-a"}, 0),
}

# grep/rg options that consume the following argument (pattern or config, not file path).
_GREP_PATTERN_OPTS = {"-e", "--regexp"}
_GREP_FILE_OPTS = {"-f", "--file"}
_GREP_VALUE_OPTS = {
    "-m", "--max-count", "-A", "--after-context", "-B", "--before-context",
    "-C", "--context", "--include", "--exclude", "--exclude-dir",
    "--label", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not",
    "--color", "--colour", "-M", "--max-columns",
}

# SSH security rule tables (shared with SSHTool).
SSH_FAST_BLOCK_PATTERNS = [".env", "$(", "`", "${"]
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
    "/dev/nvme", "/dev/vd", "/dev/xvd", "/dev/loop", "/dev/md", "/dev/dm-",
    "/dev/ubiblock", "/dev/ubi",
)

# Sentinel names used to test whether a glob segment could match .env or parent traversal.
_DOTENV_SENTINELS = (".env", ".env.local", ".env.production", ".env.example")

# Boundary regexes used inside inline scripts, variable expressions, and heredoc code.
_INLINE_SENSITIVE_PATTERNS = (
    (".env", re.compile(r"(?<![A-Za-z0-9_])\.env(?=$|[/\\'\"\s,;:)\]}&|])", re.IGNORECASE)),
    ("/etc", re.compile(r"(?<![A-Za-z0-9_])[/\\]etc(?=$|[/\\'\"\s,;:)\]}&|])", re.IGNORECASE)),
    ("../", re.compile(r"(?<![A-Za-z0-9_.])\.\.(?=$|[/\\'\"\s,;:)\]}&|])")),
    ("~/", re.compile(r"(?<![A-Za-z0-9_])~[/\\]")),
)

# Maximum recursion depth for command substitutions and -c/eval unwrapping.
_MAX_RECURSION_DEPTH = 8
# Maximum brace expansion fan-out before failing closed.
_MAX_BRACE_EXPANSIONS = 64

# Simple ANSI-C single-char escape map ($'...').
_ANSI_C_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b",
    "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    "\\": "\\", "'": "'", '"': '"', "?": "?",
}


##
 # @brief Quote-aware shell command parser and security validator.
 #
class ShellCommandGuard:
    ##
     # @brief Split a command into plain token segments using the single-pass lexer.
     #
     # Multiline quoted strings stay intact in one segment; unquoted newlines,
     # semicolons, pipes, logical operators, single-&, and subshell parens split
     # segments. Redirection target words are omitted from the plain command list.
     #
     # @param command Raw command string.
     #
     # @return List of token string lists, or None when quotes/heredocs are invalid.
     #
    @classmethod
    def split_segments(cls, command):
        detailed = cls.tokenize_segments_detailed(command)
        if detailed is None:
            return None
        # End-if
        segments = []
        for seg in detailed:
            words = [
                t["text"] for t in seg["tokens"]
                if t["kind"] == "word"
                and not t.get("is_redir_target")
                and not t.get("is_heredoc_delim")
                and not t.get("is_herestring_word")
            ]
            if words:
                segments.append(words)
            # End-if
        # End-for
        return segments
    # End-def

    ##
     # @brief Tokenize a shell command into structured segments with heredoc bodies.
     #
     # Each segment dict has keys:
     #   - "tokens": list of token dicts (text, raw, kind, quoted, cmd_subs,
     #               has_var_exp, has_unquoted_glob, has_unquoted_brace,
     #               is_redir_target, is_heredoc_delim, is_herestring_word)
     #   - "heredocs": list of {"delim": str, "quoted": bool, "body": str}
     #
     # @param command Raw command string.
     #
     # @return List of segment dicts, or None on unbalanced quotes/substitutions/heredocs.
     #
    @classmethod
    def tokenize_segments_detailed(cls, command):
        raw_cmd = str(command or "")
        return cls._lex_command_segments(raw_cmd)
    # End-def

    ##
     # @brief Decode an ANSI-C $'...' string starting after "$'".
     #
     # @param text Full command string.
     # @param start Index of the first character inside $'...'.
     #
     # @return (decoded_str, next_index_after_closing_quote) or (None, -1) if unclosed.
     #
    @staticmethod
    def _parse_ansi_c_quote(text, start):
        n = len(text)
        i = start
        out = []
        while i < n:
            c = text[i]
            if c == "'":
                return "".join(out), i + 1
            # End-if
            if c != "\\":
                out.append(c)
                i += 1
                continue
            # End-if
            if i + 1 >= n:
                return None, -1
            # End-if
            nxt = text[i + 1]
            if nxt in _ANSI_C_ESCAPES:
                out.append(_ANSI_C_ESCAPES[nxt])
                i += 2
                continue
            # End-if
            if nxt == "x":
                j = i + 2
                while j < n and j < i + 4 and text[j] in "0123456789abcdefABCDEF":
                    j += 1
                # End-while
                if j > i + 2:
                    out.append(chr(int(text[i + 2:j], 16)))
                    i = j
                    continue
                # End-if
            # End-if
            if nxt in ("u", "U"):
                max_digits = 4 if nxt == "u" else 8
                j = i + 2
                while j < n and j < i + 2 + max_digits and text[j] in "0123456789abcdefABCDEF":
                    j += 1
                # End-while
                if j > i + 2:
                    codepoint = int(text[i + 2:j], 16)
                    if 0 <= codepoint <= 0x10FFFF:
                        out.append(chr(codepoint))
                    # End-if
                    i = j
                    continue
                # End-if
            # End-if
            if nxt in "01234567":
                j = i + 1
                while j < n and j < i + 4 and text[j] in "01234567":
                    j += 1
                # End-while
                out.append(chr(int(text[i + 1:j], 8) & 0xFF))
                i = j
                continue
            # End-if
            out.append(nxt)
            i += 2
        # End-while
        return None, -1
    # End-def

    ##
     # @brief Consume a balanced $(...) command substitution starting at '$'.
     #
     # @param text Full command string.
     # @param start Index of '$'.
     #
     # @return (inner_command_str, next_index_after_rparen) or (None, -1) if unclosed.
     #
    @classmethod
    def _parse_dollar_paren(cls, text, start):
        n = len(text)
        # Skip "$("
        i = start + 2
        depth = 1
        inner = []
        while i < n:
            c = text[i]
            # Line continuation inside $(...)
            if c == "\\" and i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            # End-if
            if c == "\\" and i + 2 < n and text[i + 1] == "\r" and text[i + 2] == "\n":
                i += 3
                continue
            # End-if
            if c == "\\":
                if i + 1 < n:
                    inner.append(c)
                    inner.append(text[i + 1])
                    i += 2
                    continue
                # End-if
                return None, -1
            # End-if
            if c == "'":
                j = i + 1
                while j < n and text[j] != "'":
                    j += 1
                # End-while
                if j >= n:
                    return None, -1
                # End-if
                inner.append(text[i:j + 1])
                i = j + 1
                continue
            # End-if
            if c == '"':
                j = i + 1
                while j < n and text[j] != '"':
                    if text[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    # End-if
                    j += 1
                # End-while
                if j >= n:
                    return None, -1
                # End-if
                inner.append(text[i:j + 1])
                i = j + 1
                continue
            # End-if
            if c == "(":
                depth += 1
                inner.append(c)
                i += 1
                continue
            # End-if
            if c == ")":
                depth -= 1
                if depth == 0:
                    return "".join(inner), i + 1
                # End-if
                inner.append(c)
                i += 1
                continue
            # End-if
            inner.append(c)
            i += 1
        # End-while
        return None, -1
    # End-def

    ##
     # @brief Consume a balanced ${...} parameter expansion starting at '$'.
     #
     # @param text Full command string.
     # @param start Index of '$'.
     #
     # @return (full_expr_str, nested_cmd_subs, next_index) or (None, [], -1) if unclosed.
     #
    @classmethod
    def _parse_dollar_brace(cls, text, start):
        n = len(text)
        i = start + 2
        depth = 1
        buf = ["${"]
        nested_subs = []
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            # End-if
            if c == "\\" and i + 2 < n and text[i + 1] == "\r" and text[i + 2] == "\n":
                i += 3
                continue
            # End-if
            if c == "\\" and i + 1 < n:
                buf.append(c)
                buf.append(text[i + 1])
                i += 2
                continue
            # End-if
            if c == "$" and i + 1 < n and text[i + 1] == "(":
                inner, nxt = cls._parse_dollar_paren(text, i)
                if inner is None:
                    return None, [], -1
                # End-if
                nested_subs.append(inner)
                buf.append("$(" + inner + ")")
                i = nxt
                continue
            # End-if
            if c == "`":
                inner, nxt = cls._parse_backtick(text, i)
                if inner is None:
                    return None, [], -1
                # End-if
                nested_subs.append(inner)
                buf.append("`" + inner + "`")
                i = nxt
                continue
            # End-if
            if c == "'":
                j = i + 1
                while j < n and text[j] != "'":
                    j += 1
                # End-while
                if j >= n:
                    return None, [], -1
                # End-if
                buf.append(text[i:j + 1])
                i = j + 1
                continue
            # End-if
            if c == '"':
                j = i + 1
                while j < n and text[j] != '"':
                    if text[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    # End-if
                    j += 1
                # End-while
                if j >= n:
                    return None, [], -1
                # End-if
                buf.append(text[i:j + 1])
                i = j + 1
                continue
            # End-if
            if c == "{":
                depth += 1
                buf.append(c)
                i += 1
                continue
            # End-if
            if c == "}":
                depth -= 1
                buf.append(c)
                i += 1
                if depth == 0:
                    return "".join(buf), nested_subs, i
                # End-if
                continue
            # End-if
            buf.append(c)
            i += 1
        # End-while
        return None, [], -1
    # End-def

    ##
     # @brief Consume a backtick `...` command substitution starting at '`'.
     #
     # @param text Full command string.
     # @param start Index of opening '`'.
     #
     # @return (inner_command_str, next_index_after_backtick) or (None, -1) if unclosed.
     #
    @staticmethod
    def _parse_backtick(text, start):
        n = len(text)
        i = start + 1
        inner = []
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            # End-if
            if c == "\\" and i + 2 < n and text[i + 1] == "\r" and text[i + 2] == "\n":
                i += 3
                continue
            # End-if
            if c == "\\" and i + 1 < n and text[i + 1] in ("$", "`", "\\"):
                inner.append(text[i + 1])
                i += 2
                continue
            # End-if
            if c == "`":
                return "".join(inner), i + 1
            # End-if
            inner.append(c)
            i += 1
        # End-while
        return None, -1
    # End-def

    ##
     # @brief Helper checking for backslash-newline continuation at index i.
     #
     # @param text Command string.
     # @param i Current character index.
     #
     # @return Number of chars to skip (2 for \<LF>, 3 for \<CR><LF>, else 0).
     #
    @staticmethod
    def _line_continuation_len(text, i):
        n = len(text)
        if i + 1 < n and text[i] == "\\":
            if text[i + 1] == "\n":
                return 2
            # End-if
            if i + 2 < n and text[i + 1] == "\r" and text[i + 2] == "\n":
                return 3
            # End-if
        # End-if
        return 0
    # End-def

    ##
     # @brief Peek ahead past any \<LF> line continuations to find next effective char.
     #
     # @param text Command string.
     # @param i Starting index.
     #
     # @return Index of next non-continuation character (or len(text)).
     #
    @classmethod
    def _skip_continuations(cls, text, i):
        while i < len(text):
            skip = cls._line_continuation_len(text, i)
            if skip == 0:
                break
            # End-if
            i += skip
        # End-while
        return i
    # End-def

    ##
     # @brief Single-pass POSIX shell lexer producing structured segments and heredocs.
     #
     # @param command Full command string.
     #
     # @return List of segment dicts, or None on syntax/quoting/heredoc error.
     #
    @classmethod
    def _lex_command_segments(cls, command):
        n = len(command)
        i = 0
        segments = []
        cur_seg = {"tokens": [], "heredocs": []}
        pending_heredocs = []
        expect_kind = None  # None | "redir" | "heredoc" | "herestring"
        heredoc_strip_tabs = False

        def flush_segment():
            nonlocal cur_seg, expect_kind
            if expect_kind is not None:
                return False
            # End-if
            if cur_seg["tokens"] or cur_seg["heredocs"]:
                segments.append(cur_seg)
                cur_seg = {"tokens": [], "heredocs": []}
            # End-if
            return True
        # End-def

        def consume_heredocs(pos):
            nonlocal pending_heredocs
            for spec in pending_heredocs:
                delim = spec["delim"]
                strip_tabs = spec["strip_tabs"]
                body_lines = []
                closed = False
                while pos <= n:
                    nl = command.find("\n", pos)
                    if nl == -1:
                        line = command[pos:]
                        pos = n + 1
                    else:
                        line = command[pos:nl]
                        pos = nl + 1
                    # End-if
                    clean_line = line.rstrip("\r")
                    cmp_line = clean_line.lstrip("\t") if strip_tabs else clean_line
                    if cmp_line == delim:
                        closed = True
                        break
                    # End-if
                    body_lines.append(clean_line)
                # End-while
                if not closed:
                    return -1
                # End-if
                spec["seg"]["heredocs"].append({
                    "delim": delim,
                    "quoted": spec["quoted"],
                    "body": "\n".join(body_lines),
                })
            # End-for
            pending_heredocs = []
            return min(pos, n)
        # End-def

        while i < n:
            skip = cls._line_continuation_len(command, i)
            if skip:
                i += skip
                continue
            # End-if

            ch = command[i]
            if ch in (" ", "\t", "\r"):
                i += 1
                continue
            # End-if

            if ch == "\n":
                if not flush_segment():
                    return None
                # End-if
                i += 1
                if pending_heredocs:
                    i = consume_heredocs(i)
                    if i < 0:
                        return None
                    # End-if
                # End-if
                continue
            # End-if

            if ch == "#":
                while i < n and command[i] != "\n":
                    i += 1
                # End-while
                continue
            # End-if

            # Multi-char operators & separators.
            three = command[i:i + 3]
            two = command[i:i + 2]

            if three in ("<<<", "<<-"):
                if expect_kind is not None:
                    return None
                # End-if
                if three == "<<<":
                    cur_seg["tokens"].append({
                        "text": "<<<", "raw": "<<<", "kind": "herestring",
                        "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                        "has_unquoted_glob": False, "has_unquoted_brace": False,
                    })
                    expect_kind = "herestring"
                else:
                    cur_seg["tokens"].append({
                        "text": "<<-", "raw": "<<-", "kind": "heredoc",
                        "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                        "has_unquoted_glob": False, "has_unquoted_brace": False,
                    })
                    expect_kind = "heredoc"
                    heredoc_strip_tabs = True
                # End-if
                i += 3
                continue
            # End-if

            if three == "&>>":
                if expect_kind is not None:
                    return None
                # End-if
                cur_seg["tokens"].append({
                    "text": "&>>", "raw": "&>>", "kind": "redir",
                    "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                    "has_unquoted_glob": False, "has_unquoted_brace": False,
                })
                expect_kind = "redir"
                i += 3
                continue
            # End-if

            if two in ("&&", "||", "|&"):
                if not flush_segment():
                    return None
                # End-if
                i += 2
                continue
            # End-if

            if ch in (";", "|", "&", "(", ")"):
                if two in ("&>",):
                    if expect_kind is not None:
                        return None
                    # End-if
                    cur_seg["tokens"].append({
                        "text": "&>", "raw": "&>", "kind": "redir",
                        "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                        "has_unquoted_glob": False, "has_unquoted_brace": False,
                    })
                    expect_kind = "redir"
                    i += 2
                    continue
                # End-if
                if not flush_segment():
                    return None
                # End-if
                i += 1
                continue
            # End-if

            if two == "<<":
                if expect_kind is not None:
                    return None
                # End-if
                cur_seg["tokens"].append({
                    "text": "<<", "raw": "<<", "kind": "heredoc",
                    "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                    "has_unquoted_glob": False, "has_unquoted_brace": False,
                })
                expect_kind = "heredoc"
                heredoc_strip_tabs = False
                i += 2
                continue
            # End-if

            if two in (">>", "<>", ">|"):
                if expect_kind is not None:
                    return None
                # End-if
                cur_seg["tokens"].append({
                    "text": two, "raw": two, "kind": "redir",
                    "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                    "has_unquoted_glob": False, "has_unquoted_brace": False,
                })
                expect_kind = "redir"
                i += 2
                continue
            # End-if

            if two in (">&", "<&"):
                j = i + 2
                while j < n and (command[j].isdigit() or command[j] == "-"):
                    j += 1
                # End-while
                cur_seg["tokens"].append({
                    "text": command[i:j], "raw": command[i:j], "kind": "fd_dup",
                    "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                    "has_unquoted_glob": False, "has_unquoted_brace": False,
                })
                i = j
                continue
            # End-if

            if ch in ("<", ">"):
                if expect_kind is not None:
                    return None
                # End-if
                cur_seg["tokens"].append({
                    "text": ch, "raw": ch, "kind": "redir",
                    "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                    "has_unquoted_glob": False, "has_unquoted_brace": False,
                })
                expect_kind = "redir"
                i += 1
                continue
            # End-if

            # Parse word token.
            buf = []
            raw_buf = []
            cmd_subs = []
            saw_single = False
            saw_double = False
            saw_unquoted = False
            saw_escape = False
            has_var_exp = False
            has_unquoted_glob = False
            has_unquoted_brace = False
            word_start = i

            while i < n:
                skip = cls._line_continuation_len(command, i)
                if skip:
                    i += skip
                    continue
                # End-if

                c = command[i]
                if c in (" ", "\t", "\r", "\n", ";", "|", "&", "<", ">", "(", ")"):
                    break
                # End-if

                # ANSI-C $'...' or locale $"..."
                if c == "$":
                    nxt_idx = cls._skip_continuations(command, i + 1)
                    if nxt_idx < n and command[nxt_idx] == "'":
                        saw_single = True
                        decoded, after = cls._parse_ansi_c_quote(command, nxt_idx + 1)
                        if decoded is None:
                            return None
                        # End-if
                        buf.append(decoded)
                        raw_buf.append(decoded)
                        i = after
                        continue
                    # End-if
                    if nxt_idx < n and command[nxt_idx] == '"':
                        saw_double = True
                        i = nxt_idx + 1
                        while i < n and command[i] != '"':
                            dskip = cls._line_continuation_len(command, i)
                            if dskip:
                                i += dskip
                                continue
                            # End-if
                            dc = command[i]
                            if dc == "\\" and i + 1 < n and command[i + 1] in ('"', "\\", "$", "`"):
                                buf.append(command[i + 1])
                                raw_buf.append(command[i + 1])
                                i += 2
                                continue
                            # End-if
                            if dc == "$":
                                d_nxt = cls._skip_continuations(command, i + 1)
                                if d_nxt < n and command[d_nxt] == "(":
                                    inner, after = cls._parse_dollar_paren(command, d_nxt - 1)
                                    if inner is None:
                                        return None
                                    # End-if
                                    cmd_subs.append(inner)
                                    buf.append("$(" + inner + ")")
                                    raw_buf.append("$(" + inner + ")")
                                    i = after
                                    continue
                                # End-if
                                if d_nxt < n and command[d_nxt] == "{":
                                    expr, nsubs, after = cls._parse_dollar_brace(command, d_nxt - 1)
                                    if expr is None:
                                        return None
                                    # End-if
                                    has_var_exp = True
                                    cmd_subs.extend(nsubs)
                                    buf.append(expr)
                                    raw_buf.append(expr)
                                    i = after
                                    continue
                                # End-if
                                if d_nxt < n and (command[d_nxt].isalnum() or command[d_nxt] in "_@*?#$!-"):
                                    has_var_exp = True
                                # End-if
                            # End-if
                            if dc == "`":
                                inner, after = cls._parse_backtick(command, i)
                                if inner is None:
                                    return None
                                # End-if
                                cmd_subs.append(inner)
                                buf.append("`" + inner + "`")
                                raw_buf.append("`" + inner + "`")
                                i = after
                                continue
                            # End-if
                            buf.append(dc)
                            raw_buf.append(dc)
                            i += 1
                        # End-while
                        if i >= n:
                            return None
                        # End-if
                        i += 1
                        continue
                    # End-if
                    if nxt_idx < n and command[nxt_idx] == "(":
                        saw_unquoted = True
                        inner, after = cls._parse_dollar_paren(command, nxt_idx - 1)
                        if inner is None:
                            return None
                        # End-if
                        cmd_subs.append(inner)
                        buf.append("$(" + inner + ")")
                        raw_buf.append("$(" + inner + ")")
                        i = after
                        continue
                    # End-if
                    if nxt_idx < n and command[nxt_idx] == "{":
                        saw_unquoted = True
                        expr, nsubs, after = cls._parse_dollar_brace(command, nxt_idx - 1)
                        if expr is None:
                            return None
                        # End-if
                        has_var_exp = True
                        cmd_subs.extend(nsubs)
                        buf.append(expr)
                        raw_buf.append(expr)
                        i = after
                        continue
                    # End-if
                    if nxt_idx < n and (command[nxt_idx].isalnum() or command[nxt_idx] in "_@*?#$!-"):
                        has_var_exp = True
                    # End-if
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
                    piece = command[start:i]
                    buf.append(piece)
                    raw_buf.append(piece)
                    i += 1
                    continue
                # End-if

                if c == '"':
                    saw_double = True
                    i += 1
                    while i < n and command[i] != '"':
                        dskip = cls._line_continuation_len(command, i)
                        if dskip:
                            i += dskip
                            continue
                        # End-if
                        dc = command[i]
                        if dc == "\\" and i + 1 < n and command[i + 1] in ('"', "\\", "$", "`"):
                            buf.append(command[i + 1])
                            raw_buf.append(command[i + 1])
                            i += 2
                            continue
                        # End-if
                        if dc == "$":
                            d_nxt = cls._skip_continuations(command, i + 1)
                            if d_nxt < n and command[d_nxt] == "(":
                                inner, after = cls._parse_dollar_paren(command, d_nxt - 1)
                                if inner is None:
                                    return None
                                # End-if
                                cmd_subs.append(inner)
                                buf.append("$(" + inner + ")")
                                raw_buf.append("$(" + inner + ")")
                                i = after
                                continue
                            # End-if
                            if d_nxt < n and command[d_nxt] == "{":
                                expr, nsubs, after = cls._parse_dollar_brace(command, d_nxt - 1)
                                if expr is None:
                                    return None
                                # End-if
                                has_var_exp = True
                                cmd_subs.extend(nsubs)
                                buf.append(expr)
                                raw_buf.append(expr)
                                i = after
                                continue
                            # End-if
                            if d_nxt < n and (command[d_nxt].isalnum() or command[d_nxt] in "_@*?#$!-"):
                                has_var_exp = True
                            # End-if
                        # End-if
                        if dc == "`":
                            inner, after = cls._parse_backtick(command, i)
                            if inner is None:
                                return None
                            # End-if
                            cmd_subs.append(inner)
                            buf.append("`" + inner + "`")
                            raw_buf.append("`" + inner + "`")
                            i = after
                            continue
                        # End-if
                        buf.append(dc)
                        raw_buf.append(dc)
                        i += 1
                    # End-while
                    if i >= n:
                        return None
                    # End-if
                    i += 1
                    continue
                # End-if

                if c == "`":
                    saw_unquoted = True
                    inner, after = cls._parse_backtick(command, i)
                    if inner is None:
                        return None
                    # End-if
                    cmd_subs.append(inner)
                    buf.append("`" + inner + "`")
                    raw_buf.append("`" + inner + "`")
                    i = after
                    continue
                # End-if

                saw_unquoted = True
                if c == "\\":
                    if i + 1 >= n:
                        return None
                    # End-if
                    saw_escape = True
                    nxt = command[i + 1]
                    # POSIX shell unescapes ANY escaped char (e.g. .en\v -> .env, .\./ -> ../).
                    buf.append(nxt)
                    # Preserve raw backslash sequence so Windows ..\ and \etc are also caught.
                    raw_buf.append("\\")
                    raw_buf.append(nxt)
                    i += 2
                    continue
                # End-if

                if c in ("*", "?", "["):
                    has_unquoted_glob = True
                elif c in ("{", "}"):
                    has_unquoted_brace = True
                # End-if
                buf.append(c)
                raw_buf.append(c)
                i += 1
            # End-while

            word = "".join(buf)
            raw_word = "".join(raw_buf)

            # Numeric fd prefix immediately preceding a redirection (e.g. 2>/dev/null, 0<<<foo).
            if (saw_unquoted and not saw_single and not saw_double and not saw_escape
                    and word.isdigit() and i < n and command[i] in ("<", ">")):
                if command[i:i + 3] in ("<<<", "<<-"):
                    op = command[i:i + 3]
                    if op == "<<<":
                        cur_seg["tokens"].append({
                            "text": word + op, "raw": word + op, "kind": "herestring",
                            "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                            "has_unquoted_glob": False, "has_unquoted_brace": False,
                        })
                        expect_kind = "herestring"
                    else:
                        cur_seg["tokens"].append({
                            "text": word + op, "raw": word + op, "kind": "heredoc",
                            "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                            "has_unquoted_glob": False, "has_unquoted_brace": False,
                        })
                        expect_kind = "heredoc"
                        heredoc_strip_tabs = True
                    # End-if
                    i += 3
                    continue
                # End-if
                if command[i:i + 2] == "<<":
                    cur_seg["tokens"].append({
                        "text": word + "<<", "raw": word + "<<", "kind": "heredoc",
                        "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                        "has_unquoted_glob": False, "has_unquoted_brace": False,
                    })
                    expect_kind = "heredoc"
                    heredoc_strip_tabs = False
                    i += 2
                    continue
                # End-if
                if command[i:i + 2] in (">&", "<&"):
                    j = i + 2
                    while j < n and (command[j].isdigit() or command[j] == "-"):
                        j += 1
                    # End-while
                    cur_seg["tokens"].append({
                        "text": command[word_start:j], "raw": command[word_start:j], "kind": "fd_dup",
                        "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                        "has_unquoted_glob": False, "has_unquoted_brace": False,
                    })
                    i = j
                    continue
                # End-if
                if command[i:i + 2] in (">>", "<>", ">|"):
                    op = command[i:i + 2]
                    cur_seg["tokens"].append({
                        "text": word + op, "raw": word + op, "kind": "redir",
                        "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                        "has_unquoted_glob": False, "has_unquoted_brace": False,
                    })
                    expect_kind = "redir"
                    i += 2
                    continue
                # End-if
                cur_seg["tokens"].append({
                    "text": word + command[i], "raw": word + command[i], "kind": "redir",
                    "quoted": "unquoted", "cmd_subs": [], "has_var_exp": False,
                    "has_unquoted_glob": False, "has_unquoted_brace": False,
                })
                expect_kind = "redir"
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

            tok_obj = {
                "text": word,
                "raw": raw_word,
                "kind": "word",
                "quoted": qmode,
                "cmd_subs": cmd_subs,
                "has_var_exp": has_var_exp,
                "has_unquoted_glob": has_unquoted_glob,
                "has_unquoted_brace": has_unquoted_brace,
                "is_redir_target": expect_kind == "redir",
                "is_heredoc_delim": expect_kind == "heredoc",
                "is_herestring_word": expect_kind == "herestring",
            }
            cur_seg["tokens"].append(tok_obj)

            if expect_kind == "heredoc":
                delim_quoted = saw_single or saw_double or saw_escape
                pending_heredocs.append({
                    "delim": word,
                    "strip_tabs": heredoc_strip_tabs,
                    "quoted": delim_quoted,
                    "seg": cur_seg,
                })
                expect_kind = None
            elif expect_kind in ("redir", "herestring"):
                expect_kind = None
            # End-if
        # End-while

        if not flush_segment():
            return None
        # End-if
        if pending_heredocs:
            return None
        # End-if
        return segments
    # End-def

    ##
     # @brief Unwrap env assignments and wrapper verbs (with flags/args) to find real verb + args.
     #
     # Handles sudo -E / -u root, env -i, command -p, timeout 5, nice -n 10,
     # chrt, ionice, stdbuf, nohup, time, setsid, exec, xargs, and busybox --.
     #
     # @param tokens Command word strings (excluding redirection targets).
     #
     # @return (verb_basename_or_None, remaining_tokens_starting_at_verb) tuple.
     #
    @classmethod
    def extract_verb_and_args(cls, tokens):
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            # Skip leading env assignments (KEY=VAL).
            if len(tok) > 1 and "=" in tok and not tok.startswith("=") and not tok.startswith("-"):
                i += 1
                continue
            # End-if

            base = os.path.basename(tok)

            if base == "busybox":
                i += 1
                while i < n and tokens[i].startswith("-"):
                    if tokens[i] == "--":
                        i += 1
                        break
                    # End-if
                    i += 1
                # End-while
                continue
            # End-if

            if base in _WRAPPER_SPECS:
                val_opts, extra_pos = _WRAPPER_SPECS[base]
                i += 1
                saw_chrt_pid = False
                # Skip options and option-arguments for this wrapper.
                while i < n:
                    opt = tokens[i]
                    if opt == "--":
                        i += 1
                        break
                    # End-if
                    if base == "env" and opt == "-":
                        i += 1
                        continue
                    # End-if
                    if base == "env" and "=" in opt and not opt.startswith("=") and not opt.startswith("-"):
                        i += 1
                        continue
                    # End-if
                    if not opt.startswith("-") or opt == "-":
                        break
                    # End-if
                    if base == "chrt" and opt in ("-p", "--pid"):
                        saw_chrt_pid = True
                    # End-if
                    if "=" in opt:
                        i += 1
                        continue
                    # End-if
                    if opt in val_opts:
                        i += 2
                        continue
                    # End-if
                    # Combined short option with attached arg or flags (e.g. -u0, -iE).
                    i += 1
                # End-while
                if base == "chrt" and saw_chrt_pid:
                    extra_pos = 0
                # End-if
                # Skip mandatory wrapper positional arguments (e.g. timeout DURATION).
                for _ in range(extra_pos):
                    if i < n:
                        i += 1
                    # End-if
                # End-for
                # Skip any trailing KEY=VAL assignments after env.
                if base == "env":
                    while i < n and "=" in tokens[i] and not tokens[i].startswith("=") and not tokens[i].startswith("-"):
                        i += 1
                    # End-while
                # End-if
                continue
            # End-if

            return base, tokens[i:]
        # End-while
        return None, []
    # End-def

    ##
     # @brief Extract the effective command verb basename from a segment's tokens.
     #
     # @param tokens Token list of one command segment.
     #
     # @return Verb basename string, or None when segment has no command verb.
     #
    @classmethod
    def extract_verb(cls, tokens):
        verb, _ = cls.extract_verb_and_args(tokens)
        return verb
    # End-def

    ##
     # @brief Return positional (non-option) arguments of a command token list.
     #
     # @param tokens Token list starting at the effective verb (or full segment).
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
     # @brief Expand simple shell brace expressions {a,b} with bounded fan-out.
     #
     # @param text Token text containing unquoted braces.
     #
     # @return Expanded list of strings, or None when expansion exceeds safety cap.
     #
    @classmethod
    def expand_braces(cls, text):
        results = [text]
        while True:
            next_results = []
            changed = False
            for item in results:
                open_idx = -1
                depth = 0
                close_idx = -1
                has_comma = False
                for idx, ch in enumerate(item):
                    if ch == "{":
                        if depth == 0:
                            open_idx = idx
                            has_comma = False
                        # End-if
                        depth += 1
                    elif ch == "}" and depth > 0:
                        depth -= 1
                        if depth == 0 and open_idx != -1:
                            inner = item[open_idx + 1:idx]
                            if has_comma or ".." in inner:
                                close_idx = idx
                                break
                            # End-if
                            open_idx = -1
                        # End-if
                    elif ch == "," and depth == 1:
                        has_comma = True
                    # End-if
                # End-for

                if open_idx == -1 or close_idx == -1:
                    next_results.append(item)
                    continue
                # End-if

                changed = True
                prefix = item[:open_idx]
                suffix = item[close_idx + 1:]
                inner = item[open_idx + 1:close_idx]

                # Split top-level commas inside {...}
                alts = []
                cur = []
                d = 0
                for ch in inner:
                    if ch == "{":
                        d += 1
                        cur.append(ch)
                    elif ch == "}":
                        d -= 1
                        cur.append(ch)
                    elif ch == "," and d == 0:
                        alts.append("".join(cur))
                        cur = []
                    else:
                        cur.append(ch)
                    # End-if
                # End-for
                alts.append("".join(cur))

                if len(alts) == 1 and ".." in inner:
                    # Range brace expansion {a..z} or {1..10}
                    parts = inner.split("..")
                    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                        lo, hi = int(parts[0]), int(parts[1])
                        step = 1 if lo <= hi else -1
                        if abs(hi - lo) + 1 > _MAX_BRACE_EXPANSIONS:
                            return None
                        # End-if
                        alts = [str(v) for v in range(lo, hi + step, step)]
                    elif len(parts) >= 2 and len(parts[0]) == 1 and len(parts[1]) == 1:
                        lo, hi = ord(parts[0]), ord(parts[1])
                        step = 1 if lo <= hi else -1
                        if abs(hi - lo) + 1 > _MAX_BRACE_EXPANSIONS:
                            return None
                        # End-if
                        alts = [chr(v) for v in range(lo, hi + step, step)]
                    else:
                        return None
                    # End-if
                # End-if

                for alt in alts:
                    next_results.append(prefix + alt + suffix)
                    if len(next_results) > _MAX_BRACE_EXPANSIONS:
                        return None
                    # End-if
                # End-for
            # End-for
            results = next_results
            if not changed:
                break
            # End-if
        # End-while
        return results
    # End-def

    ##
     # @brief Check whether a glob path segment could match .env*, .., or /etc.
     #
     # @param token Decoded token text with unquoted glob metacharacters.
     #
     # @return Matched sensitive label or None.
     #
    @staticmethod
    def match_sensitive_glob(token):
        if not token:
            return None
        # End-if
        norm = str(token).strip()
        if not norm:
            return None
        # End-if
        if norm.startswith("/"):
            parts = [p for p in norm.split("/") if p]
            if parts and fnmatch.fnmatchcase("etc", parts[0].lower()):
                return "/etc"
            # End-if
        # End-if
        parts = [p for p in norm.split("/") if p]
        for part in parts:
            low = part.lower()
            # Bash globs only match dotfiles when pattern starts with '.' or bracket/extglob.
            if low.startswith(".") or low.startswith("[") or low.startswith("?"):
                for sentinel in _DOTENV_SENTINELS:
                    if fnmatch.fnmatchcase(sentinel, low):
                        return ".env"
                    # End-if
                # End-for
                if fnmatch.fnmatchcase("..", low):
                    return "../"
                # End-if
            # End-if
        # End-for
        return None
    # End-def

    ##
     # @brief Inspect a path-like token for sensitive local boundary patterns.
     #
     # Does NOT blindly normalize POSIX backslash escapes to slashes; checks
     # POSIX-unescaped text on '/' AND raw text for Windows-style '..\', '\etc', '~\'.
     #
     # @param token Decoded (POSIX-unescaped) token text.
     # @param raw_token Optional raw token preserving unquoted backslashes.
     #
     # @return Matched sensitive pattern label (".env", "/etc", "../", "~/") or None.
     #
    @staticmethod
    def match_sensitive_path(token, raw_token=None):
        if not token and not raw_token:
            return None
        # End-if
        raw = str(token or "").strip()
        candidates = []
        if raw:
            candidates.append(raw)
            if "=" in raw and not raw.startswith("="):
                rhs = raw.split("=", 1)[1].strip().strip("\"'")
                if rhs:
                    candidates.append(rhs)
                # End-if
            # End-if
        # End-if

        for item in candidates:
            low = item.lower()

            # Home directory expansion (~, ~/, ~user/).
            if low == "~" or low.startswith("~/") or low.startswith("~\\") or re.match(r"^~[a-z0-9._-]+(/|\\|$)", low):
                return "~/"
            # End-if

            # /etc or \etc top-level system configuration path.
            if item.startswith("/"):
                canon = posixpath.normpath(item)
                canon_low = canon.lower()
                if canon_low == "/etc" or canon_low.startswith("/etc/"):
                    return "/etc"
                # End-if
            # End-if
            if low == "\\etc" or low.startswith("\\etc\\"):
                return "/etc"
            # End-if

            # Path component checks on POSIX '/' (plus explicit Windows backslash traversal).
            parts = [p for p in item.split("/") if p]
            for part in parts:
                part_low = part.lower()
                if part_low == ".env" or part_low.startswith(".env.") or part_low.startswith(".env\\"):
                    return ".env"
                # End-if
                if part == ".." or part.startswith("..\\") or part.endswith("\\..") or "\\..\\" in part:
                    return "../"
                # End-if
            # End-for
        # End-for

        # Also catch explicit Windows backslash patterns in raw unescaped input (e.g. cat ..\secret).
        if raw_token:
            rt = str(raw_token).strip()
            rt_low = rt.lower()
            if "..\\" in rt or rt_low.endswith("\\.."):
                return "../"
            # End-if
            if rt_low == "\\etc" or rt_low.startswith("\\etc\\"):
                return "/etc"
            # End-if
            if rt_low.startswith("~\\"):
                return "~/"
            # End-if
            if rt_low.startswith(".env\\") or "\\.env\\" in rt_low or rt_low.endswith("\\.env"):
                return ".env"
            # End-if
        # End-if

        return None
    # End-def

    ##
     # @brief Scan inline script text or parameter expansion for sensitive path references.
     #
     # @param text Raw script or expansion text.
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
            return tok
        # End-if
        if tok == "..":
            return tok
        # End-if
        return None
    # End-def

    ##
     # @brief Identify indices of command word tokens that are pattern arguments to grep/rg.
     #
     # @param cmd_tokens List of token dicts starting at the effective verb (grep/rg).
     #
     # @return Set of token indices inside cmd_tokens that represent search patterns.
     #
    @staticmethod
    def _grep_pattern_indices(cmd_tokens):
        pattern_indices = set()
        saw_explicit_pattern = False
        after_dashdash = False
        i = 1
        n = len(cmd_tokens)
        while i < n:
            text = cmd_tokens[i]["text"]
            if after_dashdash:
                if not saw_explicit_pattern:
                    pattern_indices.add(i)
                    saw_explicit_pattern = True
                # End-if
                i += 1
                continue
            # End-if
            if text == "--":
                after_dashdash = True
                i += 1
                continue
            # End-if
            if text in _GREP_PATTERN_OPTS:
                saw_explicit_pattern = True
                if i + 1 < n:
                    pattern_indices.add(i + 1)
                    i += 2
                    continue
                # End-if
            # End-if
            if any(text.startswith(p + "=") for p in _GREP_PATTERN_OPTS):
                saw_explicit_pattern = True
                pattern_indices.add(i)
                i += 1
                continue
            # End-if
            if text in _GREP_FILE_OPTS:
                saw_explicit_pattern = True
                i += 2
                continue
            # End-if
            if any(text.startswith(p + "=") for p in _GREP_FILE_OPTS):
                saw_explicit_pattern = True
                i += 1
                continue
            # End-if
            if text in _GREP_VALUE_OPTS:
                i += 2
                continue
            # End-if
            if text.startswith("-") and text != "-":
                i += 1
                continue
            # End-if
            if not saw_explicit_pattern:
                pattern_indices.add(i)
                saw_explicit_pattern = True
            # End-if
            i += 1
        # End-while
        return pattern_indices
    # End-def

    ##
     # @brief Extract inline shell command strings passed via sh/bash -c or eval.
     #
     # @param verb Effective command verb basename.
     # @param cmd_words Command words starting at verb.
     #
     # @return List of nested command strings to validate recursively.
     #
    @staticmethod
    def _extract_nested_shell_commands(verb, cmd_words):
        nested = []
        v = str(verb or "").lower()
        if v == "eval" and len(cmd_words) > 1:
            args = [w for w in cmd_words[1:] if w != "--"]
            if args:
                nested.append(" ".join(args))
            # End-if
            return nested
        # End-if
        if v in SHELL_EVAL_VERBS:
            i = 1
            while i < len(cmd_words):
                arg = cmd_words[i]
                if arg == "--":
                    break
                # End-if
                if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                    if i + 1 < len(cmd_words):
                        nested.append(cmd_words[i + 1])
                    # End-if
                    break
                # End-if
                if not arg.startswith("-"):
                    break
                # End-if
                i += 1
            # End-while
        # End-if
        return nested
    # End-def

    ##
     # @brief Validate one candidate path/operand token for local BashTool execution.
     #
     # @param tok Structured token dict.
     # @param candidate_paths Output list receiving recognized paths for workspace checks.
     #
     # @return (True, "") or (False, block_reason).
     #
    @classmethod
    def _check_local_operand(cls, tok, candidate_paths):
        text = tok["text"]
        raw = tok["raw"]

        # Fail closed on dynamic command substitutions used directly as file operands/redirs
        # (e.g. cat $(printf '.env/cli.cfg')). Benign print uses like echo $(date) skip this.
        if tok.get("cmd_subs"):
            return False, cls._local_block_message("command substitution in path")
        # End-if

        # Fail closed on parameter expansions (${...}) or $HOME / dynamic path variables.
        if tok.get("has_var_exp"):
            hit = cls.match_inline_sensitive(text)
            if hit:
                return False, cls._local_block_message(hit)
            # End-if
            if re.search(r"\$(?:\{[^}]*\}|HOME\b|OLDPWD\b|[A-Za-z_][A-Za-z0-9_]*)", text):
                return False, cls._local_block_message("shell variable expansion in path")
            # End-if
        # End-if

        # Expand unquoted brace expressions and check every expansion.
        if tok.get("has_unquoted_brace") and "{" in text and "}" in text:
            expanded = cls.expand_braces(text)
            if expanded is None:
                return False, cls._local_block_message("brace expansion")
            # End-if
            for item in expanded:
                hit = cls.match_sensitive_path(item)
                if hit:
                    return False, cls._local_block_message(hit)
                # End-if
                if tok.get("has_unquoted_glob"):
                    ghit = cls.match_sensitive_glob(item)
                    if ghit:
                        return False, cls._local_block_message(ghit)
                    # End-if
                # End-if
                cpath = cls.extract_candidate_path(item)
                if cpath:
                    candidate_paths.append(cpath)
                # End-if
            # End-for
            return True, ""
        # End-if

        # Unquoted glob check against .env*, .., /etc.
        if tok.get("has_unquoted_glob"):
            ghit = cls.match_sensitive_glob(text)
            if ghit:
                return False, cls._local_block_message(ghit)
            # End-if
        # End-if

        hit = cls.match_sensitive_path(text, raw_token=raw)
        if hit:
            return False, cls._local_block_message(hit)
        # End-if

        cpath = cls.extract_candidate_path(text)
        if cpath:
            candidate_paths.append(cpath)
        # End-if
        return True, ""
    # End-def

    ##
     # @brief Validate a local BashTool command using single-pass quote-aware analysis.
     #
     # @param command Raw local shell command string.
     # @param _depth Internal recursion counter for nested command substitutions.
     #
     # @return (is_safe, error_message, candidate_paths) tuple.
     #
    @classmethod
    def check_local_command(cls, command, _depth=0):
        if _depth > _MAX_RECURSION_DEPTH:
            return False, cls._local_block_message("nested command depth exceeded"), []
        # End-if

        segments = cls.tokenize_segments_detailed(command)
        if segments is None:
            return False, (
                "CRITICAL SECURITY BLOCK: Command could not be tokenized "
                "(unbalanced quotes or unterminated heredoc). Refusing to execute."
            ), []
        # End-if

        candidate_paths = []

        for seg in segments:
            all_tokens = seg["tokens"]
            heredocs = seg["heredocs"]

            # 1. Always recursively validate every command substitution ($(...) / `...`)
            #    in every token (including inside echo/printf, redirs, and here-strings).
            for tok in all_tokens:
                for sub_cmd in tok.get("cmd_subs", []):
                    ok, err, sub_paths = cls.check_local_command(sub_cmd, _depth=_depth + 1)
                    if not ok:
                        return False, err, []
                    # End-if
                    candidate_paths.extend(sub_paths)
                # End-for
            # End-for

            # 2. Separate command words from redirection targets and heredoc/here-string words.
            cmd_token_objs = [
                t for t in all_tokens
                if t["kind"] == "word"
                and not t.get("is_redir_target")
                and not t.get("is_heredoc_delim")
                and not t.get("is_herestring_word")
            ]
            cmd_word_texts = [t["text"] for t in cmd_token_objs]
            verb, unwrapped_words = cls.extract_verb_and_args(cmd_word_texts)
            verb_low = verb.lower() if verb else ""
            verb_offset = len(cmd_word_texts) - len(unwrapped_words)
            unwrapped_tokens = cmd_token_objs[verb_offset:]

            is_print = verb_low in PRINT_VERBS
            is_inline = verb_low in INLINE_SCRIPT_VERBS
            is_grep = verb_low in SEARCH_PATTERN_VERBS

            # 3. Validate all redirection targets as real filesystem paths (even on echo/printf).
            for tok in all_tokens:
                if tok.get("is_redir_target"):
                    ok, err = cls._check_local_operand(tok, candidate_paths)
                    if not ok:
                        return False, err, []
                    # End-if
                # End-if
            # End-for

            # 4. Validate env assignments / wrapper args before the effective verb.
            for tok in cmd_token_objs[:verb_offset]:
                text = tok["text"]
                if "=" in text and not text.startswith("=") and not text.startswith("-"):
                    ok, err = cls._check_local_operand(tok, candidate_paths)
                    if not ok:
                        return False, err, []
                    # End-if
                # End-if
            # End-for

            # 5. Validate heredoc bodies:
            #    - Inline script interpreters execute heredoc bodies as code -> scan always!
            #    - Unquoted heredocs expand $(...), `...`, ${...} -> scan for expansions!
            #    - Quoted heredocs passed to non-script commands (e.g. cat <<'EOF') -> literal data!
            for hd in heredocs:
                body = hd["body"]
                if is_inline:
                    hit = cls.match_inline_sensitive(body)
                    if hit:
                        return False, cls._local_block_message(hit), []
                    # End-if
                    if verb_low in SHELL_EVAL_VERBS:
                        ok, err, sub_paths = cls.check_local_command(body, _depth=_depth + 1)
                        if not ok:
                            return False, err, []
                        # End-if
                        candidate_paths.extend(sub_paths)
                    # End-if
                elif not hd["quoted"]:
                    ok, err, sub_paths = cls._scan_unquoted_heredoc_local(body, _depth=_depth + 1)
                    if not ok:
                        return False, err, []
                    # End-if
                    candidate_paths.extend(sub_paths)
                # End-if
            # End-for

            # 6. Validate here-string data words for inline script runners.
            for tok in all_tokens:
                if tok.get("is_herestring_word") and is_inline:
                    hit = cls.match_inline_sensitive(tok["text"])
                    if hit:
                        return False, cls._local_block_message(hit), []
                    # End-if
                    if verb_low in SHELL_EVAL_VERBS:
                        ok, err, sub_paths = cls.check_local_command(tok["text"], _depth=_depth + 1)
                        if not ok:
                            return False, err, []
                        # End-if
                        candidate_paths.extend(sub_paths)
                    # End-if
                # End-if
            # End-for

            # 7. Recursively check sh/bash -c '...' and eval '...'.
            for nested_cmd in cls._extract_nested_shell_commands(verb_low, unwrapped_words):
                ok, err, sub_paths = cls.check_local_command(nested_cmd, _depth=_depth + 1)
                if not ok:
                    return False, err, []
                # End-if
                candidate_paths.extend(sub_paths)
            # End-for

            # 8. Validate remaining command tokens according to verb semantics.
            grep_patterns = cls._grep_pattern_indices(unwrapped_tokens) if is_grep else set()

            for idx, tok in enumerate(unwrapped_tokens):
                # Pure print builtins (echo/printf): literal args allowed unless unquoted glob/brace.
                if is_print and idx > 0:
                    if tok.get("has_unquoted_glob") or (tok.get("has_unquoted_brace") and "{" in tok["text"] and "}" in tok["text"]):
                        ok, err = cls._check_local_operand(tok, candidate_paths)
                        if not ok:
                            return False, err, []
                        # End-if
                    # End-if
                    continue
                # End-if

                # grep/rg search pattern argument (not a file path).
                if is_grep and idx in grep_patterns:
                    continue
                # End-if

                # Inline script runners (python -c, awk, perl, etc.): scan full argument text.
                if is_inline and idx > 0:
                    hit = cls.match_inline_sensitive(tok["text"])
                    if hit:
                        return False, cls._local_block_message(hit), []
                    # End-if
                # End-if

                ok, err = cls._check_local_operand(tok, candidate_paths)
                if not ok:
                    return False, err, []
                # End-if
            # End-for
        # End-for

        return True, "", candidate_paths
    # End-def

    ##
     # @brief Scan an unquoted heredoc body for $(...), `...`, or ${...} expansions.
     #
     # @param body Raw heredoc body string.
     # @param _depth Current recursion depth.
     #
     # @return (is_safe, error_message, candidate_paths) tuple.
     #
    @classmethod
    def _scan_unquoted_heredoc_local(cls, body, _depth=0):
        candidate_paths = []
        n = len(body)
        i = 0
        while i < n:
            c = body[i]
            if c == "\\" and i + 1 < n and body[i + 1] in ("$", "`", "\\", "\n"):
                i += 2
                continue
            # End-if
            if c == "$" and i + 1 < n and body[i + 1] == "(":
                inner, nxt = cls._parse_dollar_paren(body, i)
                if inner is None:
                    return False, cls._local_block_message("unterminated $(...) in heredoc"), []
                # End-if
                ok, err, sub_paths = cls.check_local_command(inner, _depth=_depth)
                if not ok:
                    return False, err, []
                # End-if
                candidate_paths.extend(sub_paths)
                i = nxt
                continue
            # End-if
            if c == "$" and i + 1 < n and body[i + 1] == "{":
                expr, nsubs, nxt = cls._parse_dollar_brace(body, i)
                if expr is None:
                    return False, cls._local_block_message("unterminated ${...} in heredoc"), []
                # End-if
                hit = cls.match_inline_sensitive(expr)
                if hit:
                    return False, cls._local_block_message(hit), []
                # End-if
                for sub_cmd in nsubs:
                    ok, err, sub_paths = cls.check_local_command(sub_cmd, _depth=_depth)
                    if not ok:
                        return False, err, []
                    # End-if
                    candidate_paths.extend(sub_paths)
                # End-for
                i = nxt
                continue
            # End-if
            if c == "`":
                inner, nxt = cls._parse_backtick(body, i)
                if inner is None:
                    return False, cls._local_block_message("unterminated backtick in heredoc"), []
                # End-if
                ok, err, sub_paths = cls.check_local_command(inner, _depth=_depth)
                if not ok:
                    return False, err, []
                # End-if
                candidate_paths.extend(sub_paths)
                i = nxt
                continue
            # End-if
            i += 1
        # End-while
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
     # Canonicalizes // and /. via posixpath.normpath and blocks glob patterns
     # matching top-level critical directories (e.g. /et?, /etc/*).
     #
     # @param target Target path token (quotes already stripped).
     #
     # @return True when the target must be blocked.
     #
    @staticmethod
    def destructive_target_blocked(target):
        if not target or target.startswith("-"):
            return False
        # End-if

        a = str(target).strip()
        if not a:
            return False
        # End-if

        if a.startswith("/"):
            # Collapse multiple leading slashes (// -> /) before normpath (POSIX // special case).
            collapsed = "/" + a.lstrip("/")
            canon = posixpath.normpath(collapsed)
            if canon in ("/", "/*"):
                return True
            # End-if
            parts = [p for p in canon.split("/") if p]
            if not parts:
                return True
            # End-if
            top = parts[0]
            top_is_crit = (
                top in SSH_CRITICAL_TOP_NAMES
                or any(fnmatch.fnmatchcase(c, top) for c in SSH_CRITICAL_TOP_NAMES)
            )
            if len(parts) == 1 and top_is_crit:
                return True
            # End-if
            if len(parts) == 2 and top_is_crit and parts[1] in ("*", "**"):
                return True
            # End-if
            return False
        # End-if

        canon = posixpath.normpath(a)
        segs = [s for s in canon.split("/") if s]
        i = 0
        while i < len(segs) and segs[i] == "..":
            i += 1
        # End-while
        if i > 0:
            rest = segs[i:]
            if not rest:
                return True
            # End-if
            first = rest[0]
            return (
                first in SSH_CRITICAL_TOP_NAMES
                or any(fnmatch.fnmatchcase(c, first) for c in SSH_CRITICAL_TOP_NAMES)
            )
        # End-if

        return False
    # End-def

    ##
     # @brief Check whether a path matches an SSH sensitive file (with normpath + glob).
     #
     # @param tok Candidate path token.
     #
     # @return True when token targets /etc/shadow, /etc/gshadow, or .env.
     #
    @staticmethod
    def _ssh_sensitive_path(tok):
        if not tok:
            return False
        # End-if
        s = str(tok).strip()
        if not s:
            return False
        # End-if
        if ".env" in s.lower():
            return True
        # End-if
        if s.startswith("/"):
            canon = posixpath.normpath("/" + s.lstrip("/"))
            for sf in SSH_SENSITIVE_FILES:
                if canon == sf or canon.startswith(sf + "/") or fnmatch.fnmatchcase(sf, canon):
                    return True
                # End-if
            # End-for
        # End-if
        return False
    # End-def

    ##
     # @brief Check whether an SSH dd of= target hits a protected block device.
     #
     # @param target Path after "of=".
     #
     # @return True when target resolves to a protected block device prefix.
     #
    @staticmethod
    def _ssh_block_device_target(target):
        if not target:
            return False
        # End-if
        s = str(target).strip()
        if s.startswith("/"):
            s = posixpath.normpath("/" + s.lstrip("/"))
        # End-if
        if s.startswith(SSH_BLOCK_DEVICE_PREFIXES):
            return True
        # End-if
        if any(g in s for g in ("*", "?", "[")) and s.startswith("/dev/"):
            for pref in SSH_BLOCK_DEVICE_PREFIXES:
                if fnmatch.fnmatchcase(pref + "0", s) or fnmatch.fnmatchcase(pref + "a", s):
                    return True
                # End-if
            # End-for
        # End-if
        return False
    # End-def

    ##
     # @brief Scan inline interpreter code (python/perl/ruby/node) for blocked SSH operations.
     #
     # @param code Script body string.
     # @param allow Per-device allowed verb set.
     # @param block Per-device blocked verb set.
     #
     # @return (True, "") or (False, reason).
     #
    @staticmethod
    def _check_ssh_inline_code(code, allow, block):
        text = str(code or "")
        low = text.lower()
        for sf in SSH_SENSITIVE_FILES:
            if sf in low:
                return False, f"CRITICAL SECURITY BLOCK [R1/inline]: sensitive file '{sf}' is forbidden."
            # End-if
        # End-for
        if ".env" in low:
            return False, "CRITICAL SECURITY BLOCK [P1/inline]: pattern '.env' is forbidden."
        # End-if
        for v in block:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(v)}(?![A-Za-z0-9_])", text):
                return False, f"CRITICAL SECURITY BLOCK [R3/inline]: verb '{v}' is blocked for this device."
            # End-if
        # End-for
        for v in SSH_BLOCKED_VERBS:
            if v not in allow and re.search(rf"(?<![A-Za-z0-9_]){re.escape(v)}(?![A-Za-z0-9_])", text):
                return False, f"CRITICAL SECURITY BLOCK [R3/inline]: verb '{v}' is blocked."
            # End-if
        # End-for
        if re.search(r"(?<![A-Za-z0-9_])mkfs(?:\.[A-Za-z0-9_]+)?(?![A-Za-z0-9_])", text):
            return False, "CRITICAL SECURITY BLOCK [R3/inline]: 'mkfs' is blocked."
        # End-if
        if re.search(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+/", text):
            return False, "CRITICAL SECURITY BLOCK [R2/inline]: destructive rm on root path is forbidden."
        # End-if
        return True, ""
    # End-def

    ##
     # @brief Full SSH semantic safety check (P1 + P2 rules R1-R4).
     #
     # Uses the single-pass quote-aware lexer so multiline quoted SSH commands
     # (e.g. python3 -c "import os\nprint(1)") are accepted while wrapper flags,
     # globs, path canonicalization tricks, destructive find, and nested shells
     # are blocked.
     #
     # @param command Raw remote shell command string.
     # @param sec_cfg Optional per-device security override {"allow": [...], "block": [...]}.
     # @param _depth Internal recursion counter for sh -c / eval unwrapping.
     #
     # @return (True, "") when safe, (False, reason) when blocked.
     #
    @classmethod
    def check_ssh_command(cls, command, sec_cfg=None, _depth=0):
        if _depth > _MAX_RECURSION_DEPTH:
            return False, "CRITICAL SECURITY BLOCK: nested shell command depth exceeded."
        # End-if

        sec_cfg = sec_cfg or {}
        allow = set(sec_cfg.get("allow", []) or [])
        block = set(sec_cfg.get("block", []) or [])

        # P1. Fast substring blacklist.
        raw_cmd = str(command or "")
        lower = raw_cmd.lower()
        for pat in SSH_FAST_BLOCK_PATTERNS:
            if pat in lower:
                return False, (
                    f"CRITICAL SECURITY BLOCK [P1]: pattern '{pat}' is forbidden. "
                    f"STOP IMMEDIATELY. Do not attempt workarounds."
                )
            # End-if
        # End-for

        # P2. Single-pass quote-aware segmentation (supports multiline quoted commands).
        segments = cls.tokenize_segments_detailed(raw_cmd)
        if segments is None:
            return False, (
                "CRITICAL SECURITY BLOCK: command could not be tokenized "
                "(unbalanced quotes or unterminated heredoc). Refusing to execute."
            )
        # End-if

        for seg in segments:
            all_tokens = seg["tokens"]
            heredocs = seg["heredocs"]

            # R1. Sensitive files & escaped .env across ALL tokens (including redirs).
            for tok in all_tokens:
                if tok["kind"] != "word":
                    continue
                # End-if
                if cls._ssh_sensitive_path(tok["text"]) or cls._ssh_sensitive_path(tok["raw"]):
                    return False, f"CRITICAL SECURITY BLOCK [R1]: sensitive file '{tok['text']}' is forbidden."
                # End-if
            # End-for

            cmd_token_objs = [
                t for t in all_tokens
                if t["kind"] == "word"
                and not t.get("is_redir_target")
                and not t.get("is_heredoc_delim")
                and not t.get("is_herestring_word")
            ]
            cmd_words = [t["text"] for t in cmd_token_objs]
            verb, unwrapped = cls.extract_verb_and_args(cmd_words)
            if not verb:
                continue
            # End-if

            # Block glob metacharacters in command verb (e.g. /sbin/reb??t).
            if any(g in verb for g in ("*", "?", "[")):
                return False, f"CRITICAL SECURITY BLOCK [R3]: globbed command verb '{verb}' is forbidden."
            # End-if

            # Per-device block list applies to every verb.
            if verb in block:
                return False, f"CRITICAL SECURITY BLOCK [R3/device]: verb '{verb}' is blocked for this device."
            # End-if

            # Recursively check nested shell commands (sh -c '...', eval '...').
            for nested_cmd in cls._extract_nested_shell_commands(verb, unwrapped):
                ok, reason = cls.check_ssh_command(nested_cmd, sec_cfg=sec_cfg, _depth=_depth + 1)
                if not ok:
                    return False, reason
                # End-if
            # End-for

            # Check inline interpreter scripts (python/perl/ruby/node -c or heredocs).
            verb_low = verb.lower()
            if verb_low in ("python", "python3", "py", "perl", "ruby", "node", "nodejs", "php"):
                for arg in unwrapped[1:]:
                    ok, reason = cls._check_ssh_inline_code(arg, allow, block)
                    if not ok:
                        return False, reason
                    # End-if
                # End-for
                for hd in heredocs:
                    ok, reason = cls._check_ssh_inline_code(hd["body"], allow, block)
                    if not ok:
                        return False, reason
                    # End-if
                # End-for
            elif verb_low in SHELL_EVAL_VERBS:
                for hd in heredocs:
                    ok, reason = cls.check_ssh_command(hd["body"], sec_cfg=sec_cfg, _depth=_depth + 1)
                    if not ok:
                        return False, reason
                    # End-if
                # End-for
            # End-if

            # R2. Destructive targets (rm / mv / cp).
            if verb in SSH_DESTRUCTIVE_TARGET_VERBS:
                for arg in cls.positional_args(unwrapped):
                    if cls.destructive_target_blocked(arg):
                        return False, f"CRITICAL SECURITY BLOCK [R2]: destructive target '{arg}' is forbidden."
                    # End-if
                # End-for
            # End-if

            # R2-find. Destructive find on critical roots (find /etc -delete / -exec rm).
            if verb == "find":
                args_tail = unwrapped[1:]
                has_delete = "-delete" in args_tail
                has_exec = "-exec" in args_tail or "-execdir" in args_tail
                if has_delete or has_exec:
                    roots = []
                    for tok in args_tail:
                        if tok.startswith("-") or tok in ("(", "!", ","):
                            break
                        # End-if
                        roots.append(tok)
                    # End-for
                    for r in roots:
                        if cls.destructive_target_blocked(r):
                            return False, f"CRITICAL SECURITY BLOCK [R2]: destructive find on '{r}' is forbidden."
                        # End-if
                    # End-for
                # End-if
                if has_exec:
                    for idx, tok in enumerate(args_tail):
                        if tok in ("-exec", "-execdir") and idx + 1 < len(args_tail):
                            exec_verb = os.path.basename(args_tail[idx + 1])
                            if exec_verb in block or (exec_verb not in allow and (exec_verb in SSH_BLOCKED_VERBS or exec_verb.startswith("mkfs"))):
                                return False, f"CRITICAL SECURITY BLOCK [R3]: find -exec verb '{exec_verb}' is blocked."
                            # End-if
                        # End-if
                    # End-for
                # End-if
            # End-if

            # R3. Destructive verbs.
            if verb not in allow:
                if verb in SSH_BLOCKED_VERBS or verb.startswith("mkfs"):
                    return False, f"CRITICAL SECURITY BLOCK [R3]: verb '{verb}' is blocked."
                # End-if
                if verb == "init" and any(a in ("0", "1", "6") for a in cls.positional_args(unwrapped)):
                    return False, "CRITICAL SECURITY BLOCK [R3]: 'init' runlevel change is blocked."
                # End-if
            # End-if

            # R4. dd writing to block devices.
            if verb == "dd":
                for tok in unwrapped[1:]:
                    if tok.startswith("of=") and cls._ssh_block_device_target(tok[3:]):
                        return False, f"CRITICAL SECURITY BLOCK [R4]: dd write target '{tok[3:]}' is forbidden."
                    # End-if
                # End-for
            # End-if
        # End-for

        return True, ""
    # End-def
# End-class
