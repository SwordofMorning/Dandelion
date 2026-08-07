##
 # @file src/subagent/__init__.py
 # @date 2026/08/07
 # 
 # @brief Subagent Package.
 # Provides the sub-agent pool (orchestrator), virtual/derived agent classes,
 # toolset registry and result types.
 #

from .registry import TOOLSET_REGISTRY, resolve_toolset
from .result import SubAgentResult
from .i_subagent import ISubAgent
from .subagent import SubAgent
from .pool import SubAgentPool