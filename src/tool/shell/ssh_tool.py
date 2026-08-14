##
 # @file src/tool/shell/ssh_tool.py
 # @date 2026/08/14
 # 
 # @brief SSH Tool.
 # Executes shell commands on remote devices (Ubuntu host / embedded board) via SSH.
 #
 # @note Security model: malicious-command semantic analysis instead of
 # blanket path blocking (embedded high-frequency dirs like /etc/init.d stay
 # open). Two tiers:
 #   P1: fast substring blacklist (".env" only - credential files).
 #   P2: command segmentation + verb extraction + rules:
 #       R1 sensitive files, R2 destructive targets (top-level critical dirs
 #          and relative traversal), R3 destructive verbs, R4 dd block-device writes.
 # Devices are declared by the user in .env/devices.yaml (alias-based; LLM
 # never sees credentials). Auth: key_path preferred, password fallback.
 # Host keys: TOFU (AutoAddPolicy + .env/known_hosts, mismatch rejected).
 #

import os
import shlex
import time

import paramiko

from ..base_tool import BaseTool

# ========================================
# @section I. Semantic Safety Rules
# ========================================

##
 # @brief P1 fast substring patterns (lower-case command check).
 #
 # @note Only ".env": credential files may exist on ANY target (deployed
 # Dandelion copies, app secrets, localhost aliases); near-zero false
 # positives. NOT "/etc" or "~/" (embedded high-frequency paths), NOT "../"
 # (handled semantically by R2 to avoid breaking "cd ..").
 #
_FAST_BLOCK_PATTERNS = [".env"]

##
 # @brief R1 sensitive files (exact or path-prefix match).
 #
_SENSITIVE_FILES = ["/etc/shadow", "/etc/gshadow"]

##
 # @brief R2 top-level critical directory names (children of "/").
 #
 # @note "/" itself is handled as exact match; direct-child globs (dir/*)
 # are blocked, deeper sub-paths are always allowed.
 #
_CRITICAL_TOP_NAMES = [
    "etc", "boot", "usr", "var", "root",
    "bin", "sbin", "lib", "dev", "proc", "sys",
]

##
 # @brief R3 destructive verbs (blocked by default; per-device
 # "security.allow" can exempt, "security.block" can add).
 #
_BLOCKED_VERBS = [
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

##
 # @brief Verbs whose positional targets are checked by R2.
 #
_DESTRUCTIVE_TARGET_VERBS = ["rm", "mv", "cp"]

##
 # @brief R4 dd: block-device write prefixes (of= target).
 #
_BLOCK_DEVICE_PREFIXES = (
    "/dev/mtd", "/dev/mmcblk", "/dev/sd", "/dev/hd",
    "/dev/ubiblock", "/dev/ubi",
)

##
 # @brief Prefix commands stripped before verb extraction.
 #
_CMD_PREFIXES = {"sudo", "nohup", "time", "env", "command"}

##
 # @brief Command separators for segmentation (quote-aware).
 #
_SEGMENT_SEPARATORS = (";", "&&", "||", "|")

##
 # @brief Output truncation limit (aligned with bash tool).
 #
_MAX_OUTPUT_CHARS = 50000

##
 # @brief Split a command into segments on shell separators.
 #
 # @param command Raw command string.
 #
 # @return List of token-lists (one per sub-command segment).
 #
def _split_segments(command):
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return [shlex.split(command, posix=False)]
    # End-try

    segments = []
    cur = []
    for tok in tokens:
        if tok in _SEGMENT_SEPARATORS:
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
 # @brief Extract the command verb from a segment's tokens.
 #
 # @param tokens Token list of one segment.
 #
 # @return Verb basename (str) or None.
 #
 # @note Strips leading env assignments (FOO=1) and prefix commands
 # (sudo/nohup/time/env/command); "/bin/rm" resolves to "rm".
 #
def _extract_verb(tokens):
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        is_env_assign = len(tok) > 2 and "=" in tok and not tok.startswith("=")
        is_prefix = tok in _CMD_PREFIXES
        if not (is_env_assign or is_prefix):
            break
        # End-if
        i += 1
    # End-while
    if i >= len(tokens):
        return None
    # End-if
    verb = os.path.basename(tokens[i])
    # busybox applet: "busybox rm -rf /" must resolve to "rm" (bypass guard).
    if verb == "busybox" and i + 1 < len(tokens):
        return os.path.basename(tokens[i + 1])
    # End-if
    return verb
# End-def

##
 # @brief Get positional (non-option) tokens of a segment.
 #
 # @param tokens Token list of one segment.
 #
 # @return List of positional args (options like -rf, --force skipped).
 #
def _positional_args(tokens):
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
 # @brief R2: check whether a target resolves to a critical location.
 #
 # @param target Target path token (quotes already stripped by shlex).
 #
 # @return True when the target must be blocked.
 #
 # @note Blocked: absolute top-level critical dir ("/", "/etc"), its
 # direct-child glob ("/etc/*"), and relative traversal that reaches a
 # critical dir ("../../etc") or the root (pure ".." run, "../../..").
 # Allowed: deeper sub-paths ("/etc/init.d/foo"), ordinary relatives
 # ("../backup/old.tgz"), non-critical tops ("/home/...").
 #
def _destructive_target_blocked(target):
    if not target or target.startswith("-"):
        return False
    # End-if

    a = target
    while a.startswith("./"):
        a = a[2:]
    # End-while

    # Absolute path.
    if a.startswith("/"):
        if a in ("/", "/*"):
            return True
        # End-if
        parts = [p for p in a.split("/") if p]
        if len(parts) == 1 and parts[0] in _CRITICAL_TOP_NAMES:
            return True
        # End-if
        if len(parts) == 2 and parts[0] in _CRITICAL_TOP_NAMES and parts[1] == "*":
            return True
        # End-if
        return False
    # End-if

    # Relative traversal.
    segs = a.split("/")
    i = 0
    while i < len(segs) and segs[i] == "..":
        i += 1
    # End-while
    if i > 0:
        rest = segs[i:]
        if not rest:
            return True  # pure ".." run: reaches root (or unknown critical parent)
        # End-if
        return rest[0] in _CRITICAL_TOP_NAMES
    # End-if

    return False
# End-def

##
 # @brief Full semantic safety check (P1 + P2 rules R1-R4).
 #
 # @param command Raw command string.
 # @param sec_cfg Optional per-device security override
 #        {"allow": [verbs], "block": [verbs]}.
 #
 # @return (True, "") when safe, (False, reason) when blocked.
 #
def check_command_safety(command, sec_cfg=None):
    sec_cfg = sec_cfg or {}
    allow = set(sec_cfg.get("allow", []) or [])
    block = set(sec_cfg.get("block", []) or [])

    # ----- @par P1. Fast substring blacklist -----
    lower = command.lower()
    for pat in _FAST_BLOCK_PATTERNS:
        if pat in lower:
            return False, (
                f"CRITICAL SECURITY BLOCK [P1]: pattern '{pat}' is forbidden. "
                f"STOP IMMEDIATELY. Do not attempt workarounds."
            )
        # End-if
    # End-for

    # ----- @par P2. Semantic rules -----
    for seg in _split_segments(command):
        verb = _extract_verb(seg)
        if not verb:
            continue
        # End-if

        # Per-device overrides.
        if verb in block:
            return False, f"CRITICAL SECURITY BLOCK [R3/device]: verb '{verb}' is blocked for this device."
        # End-if
        if verb in allow:
            continue
        # End-if

        # R1. Sensitive files.
        for tok in seg:
            if any(tok == f or tok.startswith(f + "/") for f in _SENSITIVE_FILES):
                return False, f"CRITICAL SECURITY BLOCK [R1]: sensitive file '{tok}' is forbidden."
            # End-if
        # End-for

        # R2. Destructive targets (rm / mv / cp).
        if verb in _DESTRUCTIVE_TARGET_VERBS:
            for arg in _positional_args(seg):
                if _destructive_target_blocked(arg):
                    return False, f"CRITICAL SECURITY BLOCK [R2]: destructive target '{arg}' is forbidden."
                # End-if
            # End-for
        # End-if

        # R3. Destructive verbs.
        if verb in _BLOCKED_VERBS or verb.startswith("mkfs"):
            return False, f"CRITICAL SECURITY BLOCK [R3]: verb '{verb}' is blocked."
        # End-if
        if verb == "init" and any(a in ("0", "1", "6") for a in _positional_args(seg)):
            return False, "CRITICAL SECURITY BLOCK [R3]: 'init' runlevel change is blocked."
        # End-if

        # R4. dd writing to block devices.
        if verb == "dd":
            for tok in seg:
                if tok.startswith("of=") and tok[3:].startswith(_BLOCK_DEVICE_PREFIXES):
                    return False, f"CRITICAL SECURITY BLOCK [R4]: dd write target '{tok[3:]}' is forbidden."
                # End-if
            # End-for
        # End-if
    # End-for

    return True, ""
# End-def


# ========================================
# @section II. SSH Tool
# ========================================

##
 # @brief SSH Tool Class.
 #
class SSHTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not provided.
     # @param devices_path Path to devices.yaml; default: config_dir()/devices.yaml.
     #
    def __init__(self, workspace_dir=None, devices_path=None):
        super().__init__(workspace_dir)
        self.devices_path = devices_path
        # alias -> paramiko.SSHClient (lazy connect, reused across calls).
        self._clients = {}
        self._known_hosts_path = None
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "ssh"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return "Execute a shell command on a remote device via SSH. Strict security rules apply."
    # End-def

    ##
     # @brief Return tool's schema.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device alias registered in devices.yaml"},
                "command": {"type": "string", "description": "Shell command to execute on the remote device"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default from device config (120)"}
            },
            "required": ["device", "command"]
        }
    # End-def

    ##
     # @brief Resolve known_hosts path (lazy).
     #
     # @return Absolute path string.
     #
    def _known_hosts_file(self):
        if self._known_hosts_path is None:
            try:
                from mk.lib.paths import config_dir
                base = config_dir()
            except Exception:
                base = os.getcwd()
            # End-try
            self._known_hosts_path = os.path.join(base, "known_hosts")
        # End-if
        return self._known_hosts_path
    # End-def

    ##
     # @brief Load devices.yaml.
     #
     # @return (devices_dict, errors_list).
     #
    def _load_devices(self):
        from ...utils.config.config import load_devices_config
        return load_devices_config(self.devices_path)
    # End-def

    ##
     # @brief Resolve a device entry by alias.
     #
     # @param alias Device alias.
     #
     # @return (cfg_dict, None) or (None, error_string).
     #
    def _device_config(self, alias):
        devices, _ = self._load_devices()
        if alias not in devices:
            names = ", ".join(sorted(devices.keys())) or "(none)"
            return None, (
                f"Error: Device alias '{alias}' not found in devices.yaml. "
                f"Available aliases: {names}"
            )
        # End-if
        cfg = devices[alias]
        if cfg.get("type") != "ssh":
            return None, f"Error: Device '{alias}' has type '{cfg.get('type')}', not 'ssh'."
        # End-if
        return cfg, None
    # End-def

    ##
     # @brief Get or create a reusable SSH client for a device.
     #
     # @param cfg Device config dict.
     #
     # @return paramiko.SSHClient (live transport).
     #
     # @note Auth: key_path preferred, password fallback. Host keys: TOFU
     # (AutoAddPolicy + known_hosts persistence; mismatch raises -> caller error).
     #
    def _get_client(self, cfg):
        alias = cfg["alias"]
        client = self._clients.get(alias)
        if client is not None:
            transport = client.get_transport()
            if transport is not None and transport.is_active():
                return client
            # End-if
        # End-if

        new_client = paramiko.SSHClient()

        # TOFU host-key policy.
        kh_file = self._known_hosts_file()
        os.makedirs(os.path.dirname(kh_file), exist_ok=True)
        if not os.path.exists(kh_file):
            with open(kh_file, "w", encoding="utf-8"):
                pass
            # End-with
        # End-if
        new_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        new_client.load_host_keys(kh_file)

        host = cfg["host"]
        port = int(cfg.get("port", 22))
        user = cfg["user"]
        timeout = int(cfg.get("timeout", 120))
        kwargs = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if cfg.get("key_path"):
            kwargs["key_filename"] = cfg["key_path"]
        else:
            kwargs["password"] = cfg["password"]
        # End-if

        new_client.connect(**kwargs)
        new_client.set_keepalive(30)
        self._clients[alias] = new_client
        return new_client
    # End-def

    ##
     # @brief Execute one command over a channel and collect output.
     #
     # @param client paramiko SSHClient.
     # @param cmd Command string.
     # @param timeout Seconds.
     #
     # @return (exit_status, output_string) or (None, "Timeout") on timeout.
     #
    def _exec_one_shot(self, client, cmd, timeout):
        channel = client.get_transport().open_session()
        channel.settimeout(5)
        channel.exec_command(cmd)

        out = b""
        err = b""
        deadline = time.monotonic() + float(timeout)
        while True:
            if time.monotonic() > deadline:
                channel.close()
                return None, "Timeout"
            # End-if
            if channel.recv_ready():
                out += channel.recv(8192)
            # End-if
            if channel.recv_stderr_ready():
                err += channel.recv_stderr(8192)
            # End-if
            if channel.exit_status_ready():
                while channel.recv_ready():
                    out += channel.recv(8192)
                # End-while
                while channel.recv_stderr_ready():
                    err += channel.recv_stderr(8192)
                # End-while
                break
            # End-if
            time.sleep(0.05)
        # End-while

        exit_status = channel.recv_exit_status()
        channel.close()
        text = (out + err).decode("utf-8", errors="replace")
        return exit_status, text
    # End-def

    ##
     # @brief Execute shell command on a remote device.
     #
     # @param kwargs schema properties.
     #
     # @return (success_bool, result_string)
     #
    def execute(self, **kwargs):
        device = kwargs.get("device", "")
        command = kwargs.get("command", "")
        if not device or not command:
            return False, "Error: device and command are required."
        # End-if

        # Resolve device config.
        cfg, err = self._device_config(device)
        if err:
            return False, err
        # End-if

        # Semantic safety check (P1 + P2, no interactive approval).
        safe, reason = check_command_safety(command, cfg.get("security") or {})
        if not safe:
            return False, reason
        # End-if

        timeout = kwargs.get("timeout") or cfg.get("timeout", 120)

        # Connect (lazy, reused).
        try:
            client = self._get_client(cfg)
        except Exception as e:
            return False, f"Error: SSH connect to '{device}' failed: {str(e)}"
        # End-try

        # Execute.
        try:
            status, output = self._exec_one_shot(client, command, timeout)
        except Exception as e:
            return False, f"Error: SSH exec on '{device}' failed: {str(e)}"
        # End-try

        if status is None:
            return False, f"Error: Timeout ({timeout}s)"
        # End-if

        out_str = output.strip()[:_MAX_OUTPUT_CHARS] if output.strip() else "(no output)"
        if status == 0:
            return True, out_str
        # End-if
        return False, f"[Command Failed with code {status}]\n{out_str}"
    # End-def
# End-class
