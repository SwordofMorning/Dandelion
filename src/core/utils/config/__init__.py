##
 # @file src/core/utils/config/__init__.py
 # @date 2026/08/05
 #
 # @brief Configuration loading package.
 # Exports cli.cfg and remote.cfg loaders used to assemble model profiles,
 # delegation settings, provider credentials, and remote server settings.
 #

from .config import load_api_config, load_remote_config

__all__ = [
    "load_api_config",
    "load_remote_config",
]
