##
 # @file packages/dandelion-core/src/dandelion_core/tool/base_tool.py
 # @date 2026/11/28
 #
 # @brief Abstract base tool interface, hot-plug metadata, and sandbox helpers.
 # Defines BaseTool(ABC) enforcing get_name, get_description, get_schema, and
 # execute on concrete tools, declarative discovery metadata and signature-bound
 # create_instance(cls, context), plus shared output forwarding, workspace and
 # protected-directory sandbox checks, and symlink-safe file-opening helpers.
 #

from abc import ABC, abstractmethod
import inspect
import os


##
 # @brief Abstract Base Tool Class.
 #
class BaseTool(ABC):
    # Hot-plug discovery defaults (optional to override in subclasses).
    AUTO_REGISTER = True
    HOSTS = ("cli", "remote")
    SCOPES = ("main", "subagent")
    TOOLSETS = ("full",)
    ALLOW_OVERRIDE = False
    ARTIFACT_PATH_ARG = None

    ##
     # @brief Constructor.
     #
     # @param workspace_dir Default to current directory if not explicitly provided.
     # @param protected_dirs Optional iterable of sandbox-protected directory paths
     #        that always require runtime.approve() when sandbox=True.
     #
     # @return None.
     #
    def __init__(self, workspace_dir=None, protected_dirs=None):
        self.workspace_dir = workspace_dir if workspace_dir else os.getcwd()
        self.protected_dirs = tuple(protected_dirs) if protected_dirs else ()
    # End-def

    ##
     # @brief Instantiate cls by binding only the parameters its __init__ declares.
     #
     # Returns a BaseTool instance, or None to opt out cleanly when a required
     # main-only runtime dependency (agent, session_manager, memory_manager,
     # skill_manager) is absent in the provided ToolContext.
     #
     # @param context ToolContext dependency bundle supplied by the host or loader.
     #
     # @return BaseTool instance, or None to opt out gracefully.
     #
    @classmethod
    def create_instance(cls, context):
        sig = inspect.signature(cls.__init__)
        params = list(sig.parameters.values())[1:]
        protected = tuple(getattr(context, "protected_dirs", ()) or ())
        pool = {
            "context": context,
            "host": getattr(context, "host", "core"),
            "workspace_dir": getattr(context, "workspace_dir", "."),
            "config": getattr(context, "config", {}),
            "config_dir": getattr(context, "config_dir", None),
            "runtime": getattr(context, "runtime", None),
            "session_manager": getattr(context, "session_manager", None),
            "skill_manager": getattr(context, "skill_manager", None),
            "memory_manager": getattr(context, "memory_manager", None),
            "agent": getattr(context, "agent", None),
            "protected_dirs": protected,
        }
        extras = getattr(context, "extras", None)
        if extras:
            for k, v in extras.items():
                pool.setdefault(k, v)
            # End-for
        # End-if

        kwargs = {}
        accepts_var_kw = False
        for p in params:
            if p.kind == inspect.Parameter.VAR_KEYWORD:
                accepts_var_kw = True
                continue
            # End-if
            if p.kind == inspect.Parameter.VAR_POSITIONAL:
                continue
            # End-if
            if p.name in pool:
                val = pool[p.name]
                if (
                    val is None
                    and p.default is inspect.Parameter.empty
                    and p.name in ("agent", "session_manager", "memory_manager", "skill_manager")
                ):
                    return None
                # End-if
                kwargs[p.name] = val
            elif p.default is inspect.Parameter.empty:
                raise TypeError(
                    f"{cls.__name__}.__init__ requires unknown parameter '{p.name}'; "
                    "override create_instance(cls, context) for custom wiring."
                )
            # End-if
        # End-for

        if accepts_var_kw:
            for key in ("workspace_dir", "config", "config_dir", "runtime", "host", "protected_dirs"):
                kwargs.setdefault(key, pool[key])
            # End-for
        # End-if

        instance = cls(**kwargs)
        if pool["runtime"] is not None and getattr(instance, "runtime", None) is None:
            instance.runtime = pool["runtime"]
        # End-if
        if protected:
            existing = tuple(getattr(instance, "protected_dirs", ()) or ())
            if not existing:
                instance.protected_dirs = protected
            # End-if
        elif not hasattr(instance, "protected_dirs"):
            instance.protected_dirs = ()
        # End-if
        return instance
    # End-def

    ##
     # @brief Emit a tool message through the host output adapter.
     #
     # @param text Message text.
     #
     # @return None.
     #
    def _emit(self, text):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.emit(text)
        else:
            print(text)
        # End-if
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    @abstractmethod
    def get_name(self):
        pass
    # End-def

    ##
     # @brief Return the model-facing tool description.
     #
     # @return Human-readable description string.
     #
    @abstractmethod
    def get_description(self):
        pass
    # End-def

    ##
     # @brief Return the JSON input schema dictionary for this tool.
     #
     # @return Schema dictionary containing type, properties, and required keys.
     #
    @abstractmethod
    def get_schema(self):
        pass
    # End-def

    ##
     # @brief Check whether a resolved target path equals or lies inside protected_dirs.
     #
     # @param target_path Candidate filesystem path.
     #
     # @return True when target_path hits any directory in self.protected_dirs.
     #
    def _is_protected_path(self, target_path):
        pdirs = getattr(self, "protected_dirs", None)
        if not pdirs:
            return False
        # End-if
        abs_target = os.path.normcase(os.path.realpath(target_path))
        for raw_dir in pdirs:
            if not raw_dir:
                continue
            # End-if
            cand = str(raw_dir)
            if not os.path.isabs(cand) and self.workspace_dir:
                cand = os.path.join(self.workspace_dir, cand)
            # End-if
            abs_pdir = os.path.normcase(os.path.realpath(cand))
            try:
                if os.path.commonpath([abs_pdir, abs_target]) == abs_pdir:
                    return True
                # End-if
            except ValueError:
                continue
            # End-try
        # End-for
        return False
    # End-def

    ##
     # @brief Sandbox protect: verify workspace containment and protected_dirs gates.
     #
     # @param target_path File path the tool wants to access.
     # @param action_desc Human-readable action description for the approval prompt.
     #
     # @note When target_path escapes workspace_dir OR hits any directory listed in
     # self.protected_dirs under sandbox=True, delegates to runtime.approve() for
     # host-controlled authorization and never auto-allows.
     #
     # @return True if allowed, False if denied.
     #
    def check_workspace_permission(self, target_path, action_desc="File Access"):
        runtime = getattr(self, "runtime", None)
        if runtime is not None and not getattr(runtime, "sandbox", True):
            return True
        # End-if

        abs_target, is_inside = self._resolved_within_workspace(target_path)
        is_protected = self._is_protected_path(abs_target)
        abs_workspace = (
            os.path.realpath(self.workspace_dir)
            if self.workspace_dir
            else os.path.realpath(os.getcwd())
        )

        if not self.workspace_dir and not is_protected:
            return True
        # End-if

        if is_inside and not is_protected:
            return True
        # End-if

        if runtime is not None:
            tool_name = self.get_name()
            desc = action_desc if action_desc.startswith("[") else f"[{tool_name}] {action_desc}"
            if is_protected and "protected" not in desc.lower():
                desc = f"{desc} (protected directory)"
            # End-if
            return bool(runtime.approve(desc, abs_target, abs_workspace))
        # End-if

        return False
    # End-def

    ##
     # @brief Resolve symlinks and test workspace containment without prompting.
     #
     # @param target_path File path to test.
     #
     # @return (resolved_path, is_inside) tuple.
     #
    def _resolved_within_workspace(self, target_path):
        if not self.workspace_dir:
            return os.path.realpath(target_path), True
        # End-if
        abs_workspace = os.path.realpath(self.workspace_dir)
        abs_target = os.path.realpath(target_path)
        try:
            # os.path.commonpath prevents prefix trickery like /work vs /workspace.
            return abs_target, os.path.commonpath([abs_workspace, abs_target]) == abs_workspace
        except ValueError:
            # On Windows, ValueError is raised when paths are on different drives.
            return abs_target, False
        # End-try
    # End-def

    ##
     # @brief Prepare a target path with sandbox boundary, protected_dirs, and approval checks.
     #
     # @param file_path File path from the tool schema.
     # @param action_desc Human-readable action description for the prompt.
     #
     # @return (resolved_path, None) on success, or (None, error_message).
     #
    def _prepare_path(self, file_path, action_desc):
        runtime = getattr(self, "runtime", None)
        if runtime is not None and not getattr(runtime, "sandbox", True):
            return os.path.realpath(file_path), None
        # End-if

        resolved, inside = self._resolved_within_workspace(file_path)
        if inside and not self._is_protected_path(resolved):
            return resolved, None
        # End-if

        if not self.check_workspace_permission(resolved, action_desc=action_desc):
            return None, (
                f"CRITICAL SECURITY BLOCK: The human user explicitly DENIED permission "
                f"to access '{file_path}'. STOP IMMEDIATELY. "
                f"Do not attempt any workarounds. Acknowledge this restriction to the user."
            )
        # End-if
        return resolved, None
    # End-def

    ##
     # @brief Open a file with the final component protected against symlink swaps.
     #
     # @param path Resolved (realpath) file path that already passed permission checks.
     # @param mode 'r', 'w' or 'a', optionally suffixed with 'b' for binary mode.
     # @param encoding Text encoding (default utf-8; ignored in binary mode).
     # @param errors Text error handling policy passed to os.fdopen.
     #
     # @return Opened file object on success; raises OSError otherwise.
     #
    def _open_secure(self, path, mode, encoding="utf-8", errors=None):
        binary = "b" in mode
        flags = {
            "r": os.O_RDONLY,
            "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        }[mode.replace("b", "")]
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | nofollow)
        if binary:
            return os.fdopen(fd, mode)
        # End-if
        return os.fdopen(fd, mode, encoding=encoding, errors=errors)
    # End-def

    ##
     # @brief Execute the tool with validated keyword arguments.
     #
     # @param kwargs Tool input parameters matching get_schema().
     #
     # @return (success_bool, result_string_or_payload) tuple.
     #
    @abstractmethod
    def execute(self, **kwargs):
        pass
    # End-def
# End-class
