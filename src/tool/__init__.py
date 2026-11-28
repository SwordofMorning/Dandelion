##
 # @file src/tool/__init__.py
 # @date 2026/11/28
 #
 # @brief Shared drop-in directory for zero-rebuild hot-plugged tools.
 #
 # Drop any single-file module (e.g. imap_tool.py) or multi-file plugin package
 # defining concrete BaseTool subclasses into this directory to expose them
 # automatically to both Dandelion CLI and Dandelion Remote without modifying
 # packages/dandelion-core or rebuilding native extensions / Nuitka binaries.
 #
 # Host-specific drop-ins may also be placed under src/cli/tool/ (CLI only),
 # src/remote/tool/ (Remote only), or <install_root>/tool/ (runtime drop-in
 # directory alongside dandelion.sh / dandelion.cmd in standalone builds).
 #
