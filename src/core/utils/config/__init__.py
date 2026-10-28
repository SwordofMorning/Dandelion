##
 # @file src/core/utils/config/__init__.py
 # @date 2026/08/05
 #
 # @brief Configuration loading package.
 # Exports the api.cfg loader used to assemble model profiles, delegation settings,
 # provider credentials and an explicitly configured Remote destination.
 #

from .config import load_api_config

__all__ = [
    "load_api_config",
]