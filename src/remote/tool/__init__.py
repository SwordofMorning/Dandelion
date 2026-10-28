##
 # @file src/remote/tool/__init__.py
 # @date 2026/10/28
 #
 # @brief Remote-specific Agent tool extensions.
 # Exports communication tools that let Dandelion Remote proactively or
 # reactively message the user across configured channels.
 #

from .communication import SendMessageTool

__all__ = [
    "SendMessageTool",
]
