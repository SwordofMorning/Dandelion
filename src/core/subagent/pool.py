##
 # @file src/core/subagent/pool.py
 # @date 2026/08/07
 #
 # @brief Synchronous SubAgent construction and execution pool.
 # Assigns worker IDs, enforces delegation depth, validates the explicitly
 # chosen SubAgent model specification, resolves named toolsets, and converts
 # construction or execution failures into structured results for the parent LLM.
 #

import os
import time
import uuid
from src.core.runtime import Runtime
from .subagent import SubAgent
from .result import SubAgentResult
from .registry import resolve_toolset


##
 # @brief SubAgent execution pool.
 #
class SubAgentPool:
    ##
     # @brief Constructor.
     #
    def __init__(self, safe_client, logger, config, all_tools, max_depth=1, runtime=None, workspace_dir=None):
        self.safe_client = safe_client
        self.logger = logger
        self.config = config
        self.all_tools = all_tools
        self.runtime = runtime if runtime is not None else Runtime()
        if workspace_dir:
            self.workspace_dir = workspace_dir
        else:
            self.workspace_dir = next(
                (t.workspace_dir for t in (all_tools or {}).values() if getattr(t, "workspace_dir", None)),
                os.getcwd(),
            )
        # End-if
        try:
            self.max_depth = max(int(max_depth), 0)
        except (TypeError, ValueError):
            self.max_depth = 0
        # End-try
    # End-def

    ##
     # @brief Build a failed SubAgentResult without running the worker loop.
     #
    @staticmethod
    def _failed(subagent_id, task_description, depth, summary, err_msg, start_time, model_alias=""):
        return SubAgentResult(
            subagent_id=subagent_id,
            task_description=task_description,
            status="failed",
            summary=summary,
            depth_reached=depth if isinstance(depth, int) else 0,
            duration_seconds=round(time.time() - start_time, 3),
            model_alias=model_alias,
            error_message=err_msg,
        )
    # End-def

    ##
     # @brief Create and run a SubAgent bound to an explicitly chosen model.
     #
     # @param role_prompt SubAgent's custom system prompt.
     # @param toolset_name Toolset name resolved via TOOLSET_REGISTRY.
     # @param task_description SubAgent's task instructions.
     # @param model Selected SubAgent model alias from SUB_LIST.
     # @param depth Depth of the SubAgent to create (direct child of root = 1).
     # @param parent_tools Parent's logical toolset; restricts nested SubAgents.
     #
     # @return SubAgentResult.
     #
    def create_and_run(
        self,
        role_prompt: str,
        toolset_name: str,
        task_description: str,
        model: str,
        depth: int = 1,
        parent_tools: set = None,
    ) -> SubAgentResult:
        start_time = time.time()
        subagent_id = f"sa-{uuid.uuid4().hex[:8]}"
        model_alias = str(model or "").strip()

        # 1. Depth gate.
        if self.max_depth <= 0:
            return self._failed(
                subagent_id, task_description, depth,
                "SubAgents are disabled.",
                "SubAgent spawn blocked: SUB_DEPTH is 0.",
                start_time, model_alias,
            )
        # End-if
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1 or depth > self.max_depth:
            return self._failed(
                subagent_id, task_description, depth,
                "SubAgent spawn blocked.",
                f"SubAgent spawn blocked: depth {depth!r} is outside 1..{self.max_depth} "
                f"(SUB_DEPTH={self.max_depth}). Complete the task yourself.",
                start_time, model_alias,
            )
        # End-if

        # 2. Explicit model gate.
        registry = getattr(self.safe_client, "registry", None)
        model_spec = registry.get_spec(model_alias) if registry is not None else None
        if model_spec is None:
            available = registry.aliases() if registry is not None else []
            err_msg = (
                f"Unknown or missing SubAgent model: {model_alias!r}. "
                f"Available SubAgent models: {available}"
            )
            self.logger.log_api_call(f"SUBAGENT:{subagent_id} INIT ERROR", {"error": err_msg})
            return self._failed(
                subagent_id, task_description, depth,
                "Failed to initialize SubAgent. Invalid model selection.",
                err_msg, start_time, model_alias,
            )
        # End-if

        # 3. Toolset gate.
        try:
            tools = resolve_toolset(toolset_name, self.all_tools, parent_tools)
        except ValueError as e:
            err_msg = f"{e!s}"
            self.logger.log_api_call(f"SUBAGENT:{subagent_id} INIT ERROR", {"error": err_msg})
            return self._failed(
                subagent_id, task_description, depth,
                "Failed to initialize SubAgent. Invalid toolset.",
                err_msg, start_time, model_spec.alias,
            )
        # End-except

        # 4. Construct and run SubAgent.
        subagent = None
        try:
            subagent = SubAgent(
                safe_client=self.safe_client,
                logger=self.logger,
                config=self.config,
                pool=self,
                role_prompt=role_prompt,
                tools=tools,
                model_spec=model_spec,
                depth=depth,
                max_depth=self.max_depth,
                subagent_id=subagent_id,
                runtime=self.runtime,
                workspace_dir=self.workspace_dir,
            )
            return subagent.run(task_description)
        except Exception as e:
            phase = "execution" if subagent is not None else "construction"
            err_msg = f"Unexpected error during {phase}: {e!s}"
            self.logger.log_api_call(f"SUBAGENT:{subagent_id} EXEC ERROR", {"error": err_msg})
            result = self._failed(
                subagent_id, task_description, depth,
                "SubAgent execution crashed.", err_msg, start_time, model_spec.alias,
            )
            if subagent is not None:
                result.sub_results = list(subagent.sub_results)
                result.artifacts = list(subagent.artifacts)
                result.depth_reached = max([depth] + [r.depth_reached for r in subagent.sub_results])
            # End-if
            return result
        # End-try
    # End-def
# End-class
