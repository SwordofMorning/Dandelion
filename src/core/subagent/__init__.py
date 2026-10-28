##
 # @file src/core/subagent/__init__.py
 # @date 2026/08/07
 #
 # @brief Synchronous SubAgent package exports.
 # Exposes the worker interface, concrete implementation, pool, named toolset
 # catalog and structured result type used by the parent agent.
 #

from .registry import TOOLSET_REGISTRY, resolve_toolset
from .result import SubAgentResult
from .i_subagent import ISubAgent
from .subagent import SubAgent
from .pool import SubAgentPool