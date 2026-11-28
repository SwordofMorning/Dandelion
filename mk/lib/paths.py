##
 # @file mk/lib/paths.py
 # @date 2026/08/07
 # 
 # @brief Runtime path resolution helpers for Dandelion (frozen/dev aware).
 #
 # @note Path priority for base_dir():
 #   1. Executable directory (frozen/Nuitka standalone build)
 #   2. Repository root (source run)
 #
 # @note The workspace root is intentionally NOT overridable at runtime
 #   (no env/config channel): an agent-modifiable override would allow
 #   escaping the sandbox by pointing the root at "/" and restarting.
 #

import os
import importlib.machinery
import sys

##
 # @brief Last-resort finder for submodules of PARTIALLY compiled stdlib packages.
 #
 # Nuitka compiles only the parts of a stdlib package that compiled code
 # imports (e.g. prompt_toolkit -> xml.dom.minidom yields a compiled "xml"
 # package that has no "xml.etree"). That compiled package shadows the full
 # copy in bin/_stdlib_fallback/xml, and its __path__ never points there, so
 # a bypassed package importing xml.etree.ElementTree (openpyxl) fails with
 # "No module named 'xml.etree'".
 #
 # This finder is APPENDED to sys.meta_path, so it is consulted only after
 # Nuitka's loader and the regular PathFinder have both given up: it can
 # never change an import that already works. It resolves "<pkg>.<sub>..."
 # for stdlib top-level names from <fallback>/<pkg>/... on disk.
 #
class StdlibFallbackFinder:
    ##
     # @brief Initialize finder with fallback root and stdlib module names.
     #
     # @param root Filesystem root of the fallback stdlib directory.
     # @param names Optional iterable of top-level stdlib module names.
     #
    def __init__(self, root, names=None):
        self.root = root
        self.names = frozenset(sys.stdlib_module_names if names is None else names)
    # End-def

    ##
     # @brief Locate a submodule spec inside the stdlib fallback directory.
     #
     # @param fullname Dotted module name to locate.
     # @param path Optional parent search path.
     # @param target Optional reload target module.
     #
     # @return ModuleSpec if found under the fallback root, else None.
     #
    def find_spec(self, fullname, path=None, target=None):
        if "." not in fullname or fullname.partition(".")[0] not in self.names:
            return None
        # End-if
        parent_dir = os.path.join(self.root, *fullname.split(".")[:-1])
        if not os.path.isdir(parent_dir):
            return None
        # End-if
        return importlib.machinery.PathFinder.find_spec(fullname, [parent_dir])
    # End-def

    def invalidate_caches(self):
        pass
    # End-def
# End-class

##
 # @brief Register the stdlib fallback dir: sys.path entry + submodule finder.
 #
 # @param fallback_dir Path to bin/_stdlib_fallback.
 # @param names        Top-level names handled by the finder (default: stdlib).
 #
 # @return True when the directory exists and was registered.
 #
def install_stdlib_fallback(fallback_dir, names=None):
    if not os.path.isdir(fallback_dir):
        return False
    # End-if
    if fallback_dir not in sys.path:
        sys.path.append(fallback_dir)
    # End-if
    if not any(isinstance(f, StdlibFallbackFinder) and f.root == fallback_dir for f in sys.meta_path):
        sys.meta_path.append(StdlibFallbackFinder(fallback_dir, names))
    # End-if
    return True
# End-def

# Stdlib fallback dir: appended LAST to sys.path (and sys.meta_path) so on-disk
# fallback copies (bin/_stdlib_fallback) never shadow Nuitka-compiled modules.
# This module is the first import in main.py, so it runs before any bypass
# package triggers on-disk stdlib imports (compiled modules > exe dir files >
# fallback top-level > fallback submodules of partially compiled packages).
_fallback_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "_stdlib_fallback")
install_stdlib_fallback(_fallback_dir)

##
 # @brief Detect frozen (compiled) runtime.
 #
 # @return True when running inside a Nuitka standalone binary.
 #
def is_compiled():
    return "__compiled__" in globals()
# End-def

##
 # @brief Resolve the runtime base directory.
 #
 # @return Absolute base directory path (str).
 #
def base_dir():
    if is_compiled():
        # Layout: <root>/bin/dandelion.exe
        # Return parent of bin/ to reach the actual workspace root
        return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    # End-if

    # Source run: <root>/mk/lib/paths.py -> <root>/mk/lib -> <root>/mk -> <root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# End-def

##
 # @brief Resolve the config directory (.env).
 #
 # @return Absolute config directory path (str).
 #
def config_dir():
    return os.path.join(base_dir(), ".env")
# End-def

##
 # @brief Resolve the log directory (.log).
 #
 # @return Absolute log directory path (str).
 #
def log_dir():
    return os.path.join(base_dir(), ".log")
# End-def

##
 # @brief Resolve the CLI config file path (cli.cfg, with fallback to legacy api.cfg).
 #
 # @return Absolute path to cli.cfg (str).
 #
def config_path():
    cli_cfg = os.path.join(config_dir(), "cli.cfg")
    legacy_cfg = os.path.join(config_dir(), "api.cfg")
    if not os.path.exists(cli_cfg) and os.path.exists(legacy_cfg):
        return legacy_cfg
    # End-if
    return cli_cfg
# End-def

##
 # @brief Resolve the Remote server config file path (remote.cfg).
 #
 # @return Absolute path to remote.cfg (str).
 #
def remote_config_path():
    return os.path.join(config_dir(), "remote.cfg")
# End-def


##
 # @brief Resolve symlinks in existing ancestor components while preserving non-existent leaves.
 #
 # @param path Target filesystem path (may not exist yet).
 #
 # @return Canonical absolute path string.
 #
def _resolve_stable_path(path):
    abs_path = os.path.abspath(str(path))
    tail = []
    curr = abs_path
    while not os.path.exists(curr):
        parent, name = os.path.split(curr)
        if parent == curr:
            break
        # End-if
        if name:
            tail.append(name)
        # End-if
        curr = parent
    # End-while
    resolved = os.path.realpath(curr)
    if tail:
        resolved = os.path.abspath(os.path.join(resolved, *reversed(tail)))
    # End-if
    return resolved
# End-def


##
 # @brief Normalize extra operator-configured tool directories anchored to root_dir/config_dir.
 #
 # Never resolves relative entries against os.getcwd(), preventing untrusted CWD execution.
 #
 # @param extra_dirs Optional iterable or comma/semicolon-separated string of extra dirs.
 # @param root       Anchored root directory (base_dir() or explicit root_dir).
 #
 # @return List of resolved path strings.
 #
def _resolve_extra_tool_dirs(extra_dirs, root):
    if not extra_dirs:
        return []
    # End-if
    if isinstance(extra_dirs, (str, os.PathLike)):
        raw_items = [p.strip() for p in str(extra_dirs).replace(";", ",").split(",") if p.strip()]
    else:
        raw_items = [str(p).strip() for p in extra_dirs if p is not None and str(p).strip()]
    # End-if

    cfg_root = os.path.join(root, ".env")
    resolved = []
    for raw in raw_items:
        expanded = os.path.expanduser(raw)
        if os.path.isabs(expanded):
            candidate = expanded
        else:
            cfg_candidate = os.path.join(cfg_root, expanded)
            root_candidate = os.path.join(root, expanded)
            if os.path.isdir(cfg_candidate) and not os.path.isdir(root_candidate):
                candidate = cfg_candidate
            else:
                candidate = root_candidate
            # End-if
        # End-if
        resolved.append(_resolve_stable_path(candidate))
    # End-for
    return resolved
# End-def


##
 # @brief Resolve deduplicated hot-plug tool scan directories anchored to root_dir/base_dir().
 #
 # Never scans os.getcwd(). Returns ordered precedence directories:
 #   1. <root>/src/tool
 #   2. <root>/src/<host>/tool (when host is non-empty)
 #   3. <root>/tool
 #   4. Resolved extra_dirs (optional operator config [Main] TOOL_DIRS)
 #
 # @param host       Host identifier ("cli" or "remote").
 # @param extra_dirs Optional extra directories from config.
 # @param root_dir   Optional explicit root directory (defaults to base_dir()).
 #
 # @return Deduplicated list of resolved tool directory paths.
 #
def tool_dirs(host="cli", extra_dirs=None, root_dir=None):
    root = _resolve_stable_path(root_dir if root_dir is not None else base_dir())
    host_name = str(host or "").strip()

    candidates = [os.path.join(root, "src", "tool")]
    if host_name:
        candidates.append(os.path.join(root, "src", host_name, "tool"))
    # End-if
    candidates.append(os.path.join(root, "tool"))
    candidates.extend(_resolve_extra_tool_dirs(extra_dirs, root))

    seen = set()
    result = []
    for path in candidates:
        canonical = _resolve_stable_path(path)
        key = os.path.normcase(canonical)
        if key not in seen:
            seen.add(key)
            result.append(canonical)
        # End-if
    # End-for
    return result
# End-def


##
 # @brief Resolve deduplicated sandbox-protected config and plugin directories.
 #
 # Always includes <root>/.env, <root>/tool, <root>/src/tool, <root>/src/cli/tool,
 # <root>/src/remote/tool, plus any resolved extra_dirs, even when leaf directories
 # do not yet exist on disk.
 #
 # @param extra_dirs Optional extra directories from config ([Main] TOOL_DIRS).
 # @param root_dir   Optional explicit root directory (defaults to base_dir()).
 #
 # @return Deduplicated list of canonical protected directory paths.
 #
def protected_plugin_dirs(extra_dirs=None, root_dir=None):
    root = _resolve_stable_path(root_dir if root_dir is not None else base_dir())
    candidates = [
        os.path.join(root, ".env"),
        os.path.join(root, "tool"),
        os.path.join(root, "src", "tool"),
        os.path.join(root, "src", "cli", "tool"),
        os.path.join(root, "src", "remote", "tool"),
    ]
    candidates.extend(_resolve_extra_tool_dirs(extra_dirs, root))

    seen = set()
    result = []
    for path in candidates:
        canonical = _resolve_stable_path(path)
        key = os.path.normcase(canonical)
        if key not in seen:
            seen.add(key)
            result.append(canonical)
        # End-if
    # End-for
    return result
# End-def
