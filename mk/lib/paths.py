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
    def __init__(self, root, names=None):
        self.root = root
        self.names = frozenset(sys.stdlib_module_names if names is None else names)
    # End-def

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
 # @brief Resolve the api config file path.
 #
 # @return Absolute path to api.cfg (str).
 #
def config_path():
    return os.path.join(config_dir(), "api.cfg")
# End-def
