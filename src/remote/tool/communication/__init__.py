##
 # @file src/remote/tool/communication/__init__.py
 # @date 2026/10/28
 #
 # @brief External communication tool package for Dandelion Remote.
 # Exposes SendMessageTool for Telegram and HTTP channel delivery.
 #

from .message_tool import SendMessageTool

__all__ = [
    "SendMessageTool",
]
