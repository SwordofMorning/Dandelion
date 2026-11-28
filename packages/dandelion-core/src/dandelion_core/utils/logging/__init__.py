##
 # @file packages/dandelion-core/src/dandelion_core/utils/logging/__init__.py
 # @date 2026/11/28
 #
 # @brief Session persistence and turn-backup package.
 # Exports SessionManager and SessionBackup for persisted histories,
 # metadata, staged drafts, session-local state, and atomic rollbacks.
 #

from .backup import SessionBackup
from .session import SessionManager

__all__ = [
    "SessionManager",
    "SessionBackup",
]
