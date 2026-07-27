# src/subagent/__init__.py

from .registry import TOOLSET_REGISTRY, resolve_toolset
from .result import SubAgentResult
from .i_subagent import ISubAgent
from .subagent import SubAgent
from .pool import SubAgentPool