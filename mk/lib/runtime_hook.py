##
 # @file mk/lib/runtime_hook.py
 # @date 2026/08/10
 # 
 # @brief Nuitka runtime hook: append the stdlib fallback dir LAST to sys.path.
 #
 # Stdlib fallback copies live in <exe_dir>/_stdlib_fallback and must never
 # shadow Nuitka-compiled modules, so they are appended after every existing
 # search path entry (compiled modules > exe dir files > fallback dir).
 #

import os
import sys

_fallback_dir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "_stdlib_fallback")
if os.path.isdir(_fallback_dir) and _fallback_dir not in sys.path:
    sys.path.append(_fallback_dir)
# End-if
