##
 # @file src/remote/__main__.py
 # @date 2026/10/28
 #
 # @brief Remote command-line module entry.
 # Delegates python -m src.remote to the service and daemon lifecycle manager.
 #

import sys

from .service import main


if __name__ == "__main__":
    sys.exit(main())
# End-if
