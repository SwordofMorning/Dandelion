##
 # @file src/remote/__main__.py
 # @date 2026/10/28
 #
 # @brief Remote command-line module entry.
 # Delegates python -m src.remote to the Remote CLI and lifecycle dispatcher.
 #
 # @note Commands execute only through the module entry guard.
 #

import sys

from .cli import main


if __name__ == "__main__":
    sys.exit(main())
# End-if
