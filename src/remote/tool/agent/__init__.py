##
 # @file src/remote/tool/agent/__init__.py
 # @date 2026/10/28
 #
 # @brief Remote agent delegation and schedule control tools package.
 # Exports SubmitTaskTool and TaskControlTool.
 #

from .task_tool import SubmitTaskTool, TaskControlTool

__all__ = [
    "SubmitTaskTool",
    "TaskControlTool",
]
