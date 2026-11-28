##
 # @file packages/dandelion-core/src/dandelion_core/tool/loader.py
 # @date 2026/11/28
 #
 # @brief Filesystem hot-plug tool discovery and fault-isolated loader.
 # Discovers BaseTool subclasses from operator tool directories using
 # importlib.util.spec_from_file_location so single-file plugins, category
 # folders, and multi-file packages with relative imports load identically
 # in Python source mode and Nuitka standalone binaries.
 #

from dataclasses import dataclass, field
import hashlib
import importlib
import importlib.util
import inspect
import os
import re
import sys

from .base_tool import BaseTool
from .context import ToolContext

# Synthetic sys.modules prefix for dynamically loaded hot-plug extensions.
EXT_MODULE_PREFIX = "_dandelion_ext_"

# Core orchestration, state, and communication tools that plugins may never shadow.
PROTECTED_CORE_TOOLS = frozenset({
    "plan_tool",
    "spawn_subagent",
    "submit_task",
    "task_control",
    "send_message",
    "update_state",
    "remember",
    "forget",
    "prune_context",
})


##
 # @brief Aggregated result returned by ToolLoader.discover().
 #
@dataclass
class DiscoveryResult:
    main_tools: dict = field(default_factory=dict)
    worker_tools: dict = field(default_factory=dict)
    toolset_bindings: dict = field(default_factory=dict)
    loaded_names: list = field(default_factory=list)
    errors: list = field(default_factory=list)
# End-class


##
 # @brief Dynamic filesystem loader for hot-plugged BaseTool extensions.
 #
class ToolLoader:
    ##
     # @brief Purge all dynamically loaded extension modules from sys.modules.
     #
     # Called before MyAgent.reload_tools() so edited or newly dropped plugin
     # files are cleanly re-imported without restarting the process.
     #
     # @return Number of purged module entries.
     #
    @classmethod
    def purge_loaded_modules(cls) -> int:
        to_remove = [
            name for name in list(sys.modules.keys())
            if name.startswith(EXT_MODULE_PREFIX)
        ]
        for name in to_remove:
            sys.modules.pop(name, None)
        # End-for
        importlib.invalidate_caches()
        return len(to_remove)
    # End-def

    ##
     # @brief Emit a warning through context.runtime when available.
     #
     # @param context ToolContext instance.
     # @param message Diagnostic warning text.
     #
     # @return None.
     #
    @staticmethod
    def _warn(context, message: str) -> None:
        runtime = getattr(context, "runtime", None)
        if runtime is not None and hasattr(runtime, "emit"):
            runtime.emit(f"[-] Warning [ToolLoader]: {message}")
        # End-if
    # End-def

    ##
     # @brief Build a deterministic synthetic module name for a plugin path.
     #
     # @param real_path Canonical filesystem path of the plugin file or package dir.
     # @param label Human-readable stem or package directory name.
     #
     # @return Synthetic module identifier under EXT_MODULE_PREFIX.
     #
    @staticmethod
    def _synthetic_module_name(real_path: str, label: str) -> str:
        digest = hashlib.sha1(real_path.encode("utf-8", errors="replace")).hexdigest()[:12]
        safe_label = re.sub(r"[^A-Za-z0-9_]", "_", str(label or "plugin")).strip("_") or "plugin"
        return f"{EXT_MODULE_PREFIX}{digest}_{safe_label}"
    # End-def

    ##
     # @brief Compute a lightweight file signature (mtime_ns, size) for change detection.
     #
     # @param file_path Path to Python source file.
     #
     # @return (mtime_ns, size) tuple, or None when stat fails.
     #
    @staticmethod
    def _file_sig(file_path: str):
        try:
            st = os.stat(file_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        # End-try
    # End-def

    ##
     # @brief Evict a synthetic module and any submodules under its namespace.
     #
     # @param mod_name Synthetic module or package name.
     #
     # @return None.
     #
    @staticmethod
    def _evict_module_tree(mod_name: str) -> None:
        prefix = mod_name + "."
        for key in list(sys.modules.keys()):
            if key == mod_name or key.startswith(prefix):
                sys.modules.pop(key, None)
            # End-if
        # End-for
    # End-def

    ##
     # @brief Load one Python source file or package __init__.py via spec_from_file_location.
     #
     # @param mod_name Synthetic module name in sys.modules.
     # @param file_path Path to the .py source file.
     # @param search_locations Optional package submodule_search_locations list.
     #
     # @return Loaded module object.
     #
    @classmethod
    def _load_module(cls, mod_name: str, file_path: str, search_locations=None):
        sig = cls._file_sig(file_path)
        existing = sys.modules.get(mod_name)
        if existing is not None and sig is not None and getattr(existing, "_dandelion_ext_sig", None) == sig:
            return existing
        # End-if
        if existing is not None:
            cls._evict_module_tree(mod_name)
        # End-if

        spec = importlib.util.spec_from_file_location(
            mod_name,
            file_path,
            submodule_search_locations=search_locations,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for '{file_path}'")
        # End-if

        mod = importlib.util.module_from_spec(spec)
        mod._dandelion_ext_sig = sig
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            cls._evict_module_tree(mod_name)
            raise
        # End-try
        return mod
    # End-def

    ##
     # @brief Normalize an iterable or string class metadata attribute to lowercase strings.
     #
     # @param raw Raw class attribute value.
     # @param default Default tuple used when raw is None or empty.
     #
     # @return Tuple of normalized non-empty strings.
     #
    @staticmethod
    def _normalize_tags(raw, default):
        if raw is None:
            return tuple(default)
        # End-if
        if isinstance(raw, str):
            items = [raw]
        else:
            items = list(raw)
        # End-if
        cleaned = []
        for item in items:
            val = str(item or "").strip().lower()
            if val and val not in cleaned:
                cleaned.append(val)
            # End-if
        # End-for
        return tuple(cleaned) if cleaned else tuple(default)
    # End-def

    ##
     # @brief Check whether a BaseTool subclass targets the requested host.
     #
     # @param tool_cls Concrete BaseTool subclass.
     # @param host Active host tag ("cli", "remote", "core", or "*").
     #
     # @return True if tool_cls should load on host.
     #
    @classmethod
    def _host_matches(cls, tool_cls, host: str) -> bool:
        target = str(host or "core").strip().lower() or "core"
        if target == "*":
            return True
        # End-if
        host_tags = cls._normalize_tags(getattr(tool_cls, "HOSTS", BaseTool.HOSTS), BaseTool.HOSTS)
        if "*" in host_tags or target in host_tags:
            return True
        # End-if
        # Standalone "core" host accepts default universal ("cli", "remote") tools
        # while still respecting host-restricted ("cli",) or ("remote",) tools.
        if target == "core" and "cli" in host_tags and "remote" in host_tags:
            return True
        # End-if
        return False
    # End-def

    ##
     # @brief Collect eligible BaseTool subclasses defined directly inside one module.
     #
     # @param mod Loaded Python module object.
     #
     # @return List of concrete BaseTool subclasses whose __module__ == mod.__name__.
     #
    @staticmethod
    def _extract_tool_classes(mod) -> list:
        found = []
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is BaseTool or not issubclass(obj, BaseTool):
                continue
            # End-if
            if inspect.isabstract(obj):
                continue
            # End-if
            if getattr(obj, "AUTO_REGISTER", True) is not True:
                continue
            # End-if
            if obj.__module__ != mod.__name__:
                continue
            # End-if
            found.append(obj)
        # End-for
        return found
    # End-def

    ##
     # @brief Instantiate and register tool classes extracted from one loaded module.
     #
     # @param tool_classes List of concrete BaseTool subclasses.
     # @param source_path Filesystem path for diagnostics.
     # @param host Target host identifier.
     # @param context ToolContext passed to create_instance().
     # @param existing_set Set of already-registered tool names (mutated on accept).
     # @param result DiscoveryResult accumulator (mutated in place).
     #
     # @return None.
     #
    @classmethod
    def _register_classes(cls, tool_classes, source_path, host, context, existing_set, result) -> None:
        for tool_cls in tool_classes:
            if not cls._host_matches(tool_cls, host):
                continue
            # End-if

            scopes = cls._normalize_tags(getattr(tool_cls, "SCOPES", BaseTool.SCOPES), BaseTool.SCOPES)
            for_main = "*" in scopes or "main" in scopes
            for_worker = "*" in scopes or "subagent" in scopes
            if not for_main and not for_worker:
                continue
            # End-if

            try:
                instance = tool_cls.create_instance(context)
            except Exception as exc:
                msg = f"Failed to instantiate {tool_cls.__name__} from '{source_path}': {exc}"
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-try

            if instance is None:
                continue
            # End-if

            if not isinstance(instance, BaseTool):
                msg = (
                    f"{tool_cls.__name__}.create_instance() from '{source_path}' "
                    f"returned non-BaseTool {type(instance).__name__}; skipping."
                )
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-if

            try:
                tool_name = str(instance.get_name() or "").strip()
            except Exception as exc:
                msg = f"Failed to read get_name() on {tool_cls.__name__} from '{source_path}': {exc}"
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-try

            if not tool_name:
                msg = f"Tool class {tool_cls.__name__} from '{source_path}' returned empty get_name(); skipping."
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-if

            if tool_name in PROTECTED_CORE_TOOLS:
                msg = f"Refused to shadow protected core tool '{tool_name}' from '{source_path}'."
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-if

            allow_override = bool(getattr(tool_cls, "ALLOW_OVERRIDE", False))
            if tool_name in existing_set and not allow_override:
                msg = (
                    f"Skipped duplicate tool '{tool_name}' from '{source_path}' "
                    "(set ALLOW_OVERRIDE = True to override)."
                )
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-if

            if for_main:
                result.main_tools[tool_name] = instance
            # End-if
            if for_worker:
                result.worker_tools[tool_name] = instance
                raw_ts = getattr(tool_cls, "TOOLSETS", BaseTool.TOOLSETS)
                if isinstance(raw_ts, str):
                    ts_items = [raw_ts]
                else:
                    ts_items = list(raw_ts) if raw_ts else list(BaseTool.TOOLSETS)
                # End-if
                clean_ts = []
                for ts in ts_items:
                    ts_name = str(ts or "").strip()
                    if ts_name and ts_name not in clean_ts:
                        clean_ts.append(ts_name)
                    # End-if
                # End-for
                result.toolset_bindings[tool_name] = tuple(clean_ts) if clean_ts else ("full",)
            # End-if

            existing_set.add(tool_name)
            if tool_name not in result.loaded_names:
                result.loaded_names.append(tool_name)
            # End-if
        # End-for
    # End-def

    ##
     # @brief Load a multi-file plugin package directory containing __init__.py.
     #
     # @param pkg_dir Package directory path.
     # @param host Target host identifier.
     # @param context ToolContext instance.
     # @param existing_set Set of already-registered tool names.
     # @param result DiscoveryResult accumulator.
     #
     # @return None.
     #
    @classmethod
    def _load_package_dir(cls, pkg_dir: str, host: str, context, existing_set, result) -> None:
        real_pkg = os.path.realpath(pkg_dir)
        init_py = os.path.join(real_pkg, "__init__.py")
        pkg_name = cls._synthetic_module_name(real_pkg, os.path.basename(real_pkg))

        try:
            pkg_mod = cls._load_module(pkg_name, init_py, search_locations=[real_pkg])
        except Exception as exc:
            msg = f"Failed to load plugin package '{pkg_dir}': {exc}"
            result.errors.append(msg)
            cls._warn(context, msg)
            return
        # End-try

        pkg_classes = cls._extract_tool_classes(pkg_mod)
        if pkg_classes:
            cls._register_classes(pkg_classes, init_py, host, context, existing_set, result)
            return
        # End-if

        # If __init__.py defines no BaseTool directly, scan child .py modules under the package.
        try:
            entries = sorted(os.listdir(real_pkg))
        except OSError as exc:
            msg = f"Failed to list plugin package '{pkg_dir}': {exc}"
            result.errors.append(msg)
            cls._warn(context, msg)
            return
        # End-try

        for entry in entries:
            if entry.startswith(".") or entry.startswith("_") or not entry.endswith(".py"):
                continue
            # End-if
            child_path = os.path.join(real_pkg, entry)
            if not os.path.isfile(child_path):
                continue
            # End-if
            stem = os.path.splitext(entry)[0]
            safe_stem = re.sub(r"[^A-Za-z0-9_]", "_", stem)
            child_mod_name = f"{pkg_name}.{safe_stem}"
            try:
                child_mod = cls._load_module(child_mod_name, child_path)
            except Exception as exc:
                msg = f"Failed to load plugin module '{child_path}': {exc}"
                result.errors.append(msg)
                cls._warn(context, msg)
                continue
            # End-try
            child_classes = cls._extract_tool_classes(child_mod)
            if child_classes:
                cls._register_classes(child_classes, child_path, host, context, existing_set, result)
            # End-if
        # End-for
    # End-def

    ##
     # @brief Recursively walk a tool directory discovering single files and packages.
     #
     # @param current_dir Directory path to scan.
     # @param host Target host identifier.
     # @param context ToolContext instance.
     # @param existing_set Set of already-registered tool names.
     # @param result DiscoveryResult accumulator.
     #
     # @return None.
     #
    @classmethod
    def _walk_directory(cls, current_dir: str, host: str, context, existing_set, result) -> None:
        try:
            entries = sorted(os.listdir(current_dir))
        except OSError as exc:
            msg = f"Failed to scan tool directory '{current_dir}': {exc}"
            result.errors.append(msg)
            cls._warn(context, msg)
            return
        # End-try

        for entry in entries:
            if entry.startswith(".") or entry.startswith("_") or entry == "__pycache__":
                continue
            # End-if
            full_path = os.path.join(current_dir, entry)
            if os.path.isdir(full_path):
                init_py = os.path.join(full_path, "__init__.py")
                if os.path.isfile(init_py):
                    cls._load_package_dir(full_path, host, context, existing_set, result)
                else:
                    cls._walk_directory(full_path, host, context, existing_set, result)
                # End-if
                continue
            # End-if

            if os.path.isfile(full_path) and entry.endswith(".py"):
                real_file = os.path.realpath(full_path)
                stem = os.path.splitext(entry)[0]
                mod_name = cls._synthetic_module_name(real_file, stem)
                try:
                    mod = cls._load_module(mod_name, real_file)
                except Exception as exc:
                    msg = f"Failed to load tool file '{full_path}': {exc}"
                    result.errors.append(msg)
                    cls._warn(context, msg)
                    continue
                # End-try
                tool_classes = cls._extract_tool_classes(mod)
                if tool_classes:
                    cls._register_classes(tool_classes, full_path, host, context, existing_set, result)
                # End-if
            # End-if
        # End-for
    # End-def

    ##
     # @brief Discover, instantiate, and validate hot-plugged tools across tool_dirs.
     #
     # @param tool_dirs Ordered iterable of filesystem directories to scan.
     # @param host Active host tag ("cli", "remote", "core", or "*").
     # @param context Optional ToolContext bundle passed to BaseTool.create_instance().
     # @param existing_tools Optional iterable of already-mounted built-in tool names.
     #
     # @return DiscoveryResult containing main_tools, worker_tools, toolset_bindings,
     #         loaded_names, and errors.
     #
    @classmethod
    def discover(cls, tool_dirs, host="core", context=None, existing_tools=None) -> DiscoveryResult:
        result = DiscoveryResult()
        if not tool_dirs:
            return result
        # End-if

        ctx = context if context is not None else ToolContext(host=str(host or "core"))
        existing_set = set(existing_tools) if existing_tools else set()
        seen_dirs = set()

        for raw_dir in tool_dirs:
            if not raw_dir:
                continue
            # End-if
            real_dir = os.path.realpath(os.path.abspath(str(raw_dir)))
            norm_key = os.path.normcase(real_dir)
            if norm_key in seen_dirs or not os.path.isdir(real_dir):
                continue
            # End-if
            seen_dirs.add(norm_key)

            # Optional vendored third-party dependency directory (<dir>/_deps).
            deps_dir = os.path.join(real_dir, "_deps")
            if os.path.isdir(deps_dir):
                real_deps = os.path.realpath(deps_dir)
                if real_deps not in sys.path:
                    sys.path.append(real_deps)
                # End-if
            # End-if

            cls._walk_directory(real_dir, host, ctx, existing_set, result)
        # End-for

        return result
    # End-def
# End-class
