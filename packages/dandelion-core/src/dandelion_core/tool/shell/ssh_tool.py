##
 # @file packages/dandelion-core/src/dandelion_core/tool/shell/ssh_tool.py
 # @date 2026/11/28
 #
 # @brief Configured-device SSH execution tool.
 # Resolves device aliases from config_dir/devices.yaml (defaulting to
 # <workspace_dir>/.env/ without coupling to mk.lib.paths), validates remote
 # commands through ShellCommandGuard, reuses locked Paramiko clients, and
 # bounds captured output per stream.
 #

import os
import threading
import time

import paramiko

from ..base_tool import BaseTool
from .shell_guard import (
    ShellCommandGuard,
    SSH_BLOCK_DEVICE_PREFIXES as _BLOCK_DEVICE_PREFIXES,
    SSH_BLOCKED_VERBS as _BLOCKED_VERBS,
    SSH_CRITICAL_TOP_NAMES as _CRITICAL_TOP_NAMES,
    SSH_DESTRUCTIVE_TARGET_VERBS as _DESTRUCTIVE_TARGET_VERBS,
    SSH_FAST_BLOCK_PATTERNS as _FAST_BLOCK_PATTERNS,
    SSH_SENSITIVE_FILES as _SENSITIVE_FILES,
)

# Output truncation limit (aligned with bash tool).
_MAX_OUTPUT_CHARS = 50000

# Shared helper aliases retained for direct callers and tests.
_split_segments = ShellCommandGuard.split_segments
_extract_verb = ShellCommandGuard.extract_verb
_positional_args = ShellCommandGuard.positional_args
_destructive_target_blocked = ShellCommandGuard.destructive_target_blocked
check_command_safety = ShellCommandGuard.check_ssh_command


##
 # @brief SSH Tool Class.
 #
class SSHTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not provided.
     # @param devices_path Optional explicit path to devices.yaml.
     # @param config_dir Optional config directory; defaults to <workspace_dir>/.env.
     #
     # @return None.
     #
    def __init__(self, workspace_dir=None, devices_path=None, config_dir=None):
        super().__init__(workspace_dir)
        self.devices_path = devices_path
        self.config_dir = config_dir if config_dir else os.path.join(self.workspace_dir, ".env")
        # alias -> paramiko.SSHClient (lazy connect, reused across calls).
        self._clients = {}
        # Parallel SubAgents share this tool: guard the check-then-connect.
        self._clients_lock = threading.Lock()
        self._known_hosts_path = None
    # End-def

    ##
     # @brief Return tool's name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "ssh"
    # End-def

    ##
     # @brief Return tool's description.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return "Execute a shell command on a remote device via SSH. Strict security rules apply."
    # End-def

    ##
     # @brief Return tool's schema.
     #
     # @return JSON Schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "device": {"type": "string", "description": "Device alias registered in devices.yaml"},
                "command": {"type": "string", "description": "Shell command to execute on the remote device"},
                "timeout": {"type": "integer", "description": "Timeout in seconds, default from device config (120)"},
            },
            "required": ["device", "command"],
        }
    # End-def

    ##
     # @brief Resolve known_hosts path under config_dir (lazy).
     #
     # @return Absolute path string to known_hosts.
     #
    def _known_hosts_file(self):
        if self._known_hosts_path is None:
            self._known_hosts_path = os.path.join(self.config_dir, "known_hosts")
        # End-if
        return self._known_hosts_path
    # End-def

    ##
     # @brief Load devices.yaml using configured paths without mk.lib.paths coupling.
     #
     # @return (devices_dict, errors_list) tuple.
     #
    def _load_devices(self):
        from ...utils.config.config import load_devices_config
        return load_devices_config(
            self.devices_path,
            config_dir=self.config_dir,
            workspace_dir=self.workspace_dir,
        )
    # End-def

    ##
     # @brief Resolve a device entry by alias.
     #
     # @param alias Device alias.
     #
     # @return (cfg_dict, None) or (None, error_string).
     #
    def _device_config(self, alias):
        devices, errors = self._load_devices()
        if alias not in devices:
            for err in errors:
                if err.startswith(f"Device '{alias}'"):
                    return None, f"Error: {err}"
                # End-if
            # End-for
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
    def _get_client(self, cfg):
        with self._clients_lock:
            return self._get_client_locked(cfg)
        # End-with
    # End-def

    ##
     # @brief _get_client() body; caller holds _clients_lock.
     #
     # @param cfg Device config dict.
     #
     # @return paramiko.SSHClient (live transport).
     #
    def _get_client_locked(self, cfg):
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
        new_client.get_transport().set_keepalive(30)
        self._clients[alias] = new_client
        return new_client
    # End-def

    ##
     # @brief Execute one command over a channel and collect bounded output.
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
        out_capped = False
        err_capped = False
        deadline = time.monotonic() + float(timeout)
        while True:
            if time.monotonic() > deadline:
                channel.close()
                return None, "Timeout"
            # End-if

            if not out_capped and channel.recv_ready():
                chunk = channel.recv(8192)
                if chunk:
                    room = _MAX_OUTPUT_CHARS - len(out)
                    if len(chunk) > room:
                        out += chunk[:room]
                        out_capped = True
                    else:
                        out += chunk
                    # End-if
                # End-if
            # End-if
            if not err_capped and channel.recv_stderr_ready():
                chunk = channel.recv_stderr(8192)
                if chunk:
                    room = _MAX_OUTPUT_CHARS - len(err)
                    if len(chunk) > room:
                        err += chunk[:room]
                        err_capped = True
                    else:
                        err += chunk
                    # End-if
                # End-if
            # End-if

            if channel.exit_status_ready():
                while not out_capped and channel.recv_ready():
                    chunk = channel.recv(8192)
                    room = _MAX_OUTPUT_CHARS - len(out)
                    if len(chunk) > room:
                        out += chunk[:room]
                        out_capped = True
                        break
                    # End-if
                    out += chunk
                # End-while
                while not err_capped and channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(8192)
                    room = _MAX_OUTPUT_CHARS - len(err)
                    if len(chunk) > room:
                        err += chunk[:room]
                        err_capped = True
                        break
                    # End-if
                    err += chunk
                # End-while
                break
            # End-if
            time.sleep(0.05)
        # End-while

        exit_status = channel.recv_exit_status()
        channel.close()
        marker = ""
        if out_capped or err_capped:
            marker = f"[TRUNCATED: output exceeded {_MAX_OUTPUT_CHARS} chars]\n"
        # End-if
        text = marker + (out + err).decode("utf-8", errors="replace")
        return exit_status, text
    # End-def

    ##
     # @brief Execute shell command on a remote device.
     #
     # @param kwargs Schema properties: device, command, optional timeout.
     #
     # @return (success_bool, result_string) tuple.
     #
    def execute(self, **kwargs):
        device = kwargs.get("device", "")
        command = kwargs.get("command", "")
        if not device or not command:
            return False, "Error: device and command are required."
        # End-if

        cfg, err = self._device_config(device)
        if err:
            return False, err
        # End-if

        safe, reason = ShellCommandGuard.check_ssh_command(command, cfg.get("security") or {})
        if not safe:
            return False, reason
        # End-if

        timeout = kwargs.get("timeout") or cfg.get("timeout", 120)

        try:
            client = self._get_client(cfg)
        except Exception as e:
            return False, f"Error: SSH connect to '{device}' failed: {str(e)}"
        # End-try

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
