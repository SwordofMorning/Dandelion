##
 # @file src/core/tool/agent/plan_tool.py
 # @date 2026/08/13
 #
 # @brief Strict structured task-planning and pre-flight validation tool.
 # The calling Agent LLM decomposes the task directly using its full live
 # context and submits a structured TaskPlan; PlanTool validates SubAgent model
 # aliases, toolsets, DAG dependencies (cycle-free), and parallel target-file
 # write-conflict safety without making a separate context-blind LLM request.
 #

import heapq
import json
import os
from collections import deque

from ..base_tool import BaseTool
from ...subagent.registry import TOOLSET_REGISTRY

VALID_STRATEGIES = ("parallel", "sequential", "mixed")
MAX_PLAN_SUBTASKS = 20


##
 # @brief Compute topological waves and transitive ancestors via Kahn's algorithm.
 #
 # @param ordered_ids Subtask IDs in declaration order.
 # @param deps_map Mapping of subtask_id -> list of dependency subtask_ids.
 #
 # @return (waves, ancestors, cyclic_ids) tuple.
 #
def _analyze_dag(ordered_ids, deps_map):
    in_degree = {sid: 0 for sid in ordered_ids}
    children = {sid: [] for sid in ordered_ids}
    ancestors = {sid: set() for sid in ordered_ids}

    for sid in ordered_ids:
        for dep in deps_map[sid]:
            in_degree[sid] += 1
            children[dep].append(sid)
        # End-for
    # End-for

    current = deque([sid for sid in ordered_ids if in_degree[sid] == 0])
    waves = []
    visited = 0

    while current:
        wave = list(current)
        current.clear()
        waves.append(wave)
        visited += len(wave)
        for u in wave:
            for v in children[u]:
                ancestors[v].add(u)
                ancestors[v].update(ancestors[u])
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    current.append(v)
                # End-if
            # End-for
        # End-for
    # End-while

    if visited < len(ordered_ids):
        cyclic = [sid for sid in ordered_ids if in_degree[sid] > 0]
        return [], ancestors, cyclic
    # End-if
    return waves, ancestors, []
# End-def


##
 # @brief Build single-subtask topological waves preserving declaration order.
 #
def _sequential_waves(ordered_ids, deps_map):
    index_of = {sid: idx for idx, sid in enumerate(ordered_ids)}
    in_degree = {sid: len(deps_map[sid]) for sid in ordered_ids}
    children = {sid: [] for sid in ordered_ids}
    for sid in ordered_ids:
        for dep in deps_map[sid]:
            children[dep].append(sid)
        # End-for
    # End-for

    ready = [index_of[sid] for sid in ordered_ids if in_degree[sid] == 0]
    heapq.heapify(ready)
    waves = []
    while ready:
        idx = heapq.heappop(ready)
        u = ordered_ids[idx]
        waves.append([u])
        for v in children[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                heapq.heappush(ready, index_of[v])
            # End-if
        # End-for
    # End-while
    return waves
# End-def


##
 # @brief Structured TaskPlan validation tool ("Let LLM Decide" + strict invariants).
 #
class PlanTool(BaseTool):
    ##
     # @brief Constructor.
     #
     # @param registry ModelRegistry providing allowed SUB_LIST aliases.
     # @param workspace_dir Workspace root directory used to canonicalize target_files.
     # @param parent_tools Optional set of logical tool names bounding nested SubAgents.
     # @param caller_label Diagnostic label for runtime progress output.
     #
    def __init__(self, registry, workspace_dir=None, parent_tools=None, caller_label="MainAgent"):
        super().__init__(workspace_dir=workspace_dir)
        self.registry = registry
        self.parent_tools = set(parent_tools) if parent_tools is not None else None
        self.caller_label = caller_label
    # End-def

    ##
     # @brief Resolve a declared target file path to its canonical realpath key and display path.
     #
    def _resolve_target_file(self, raw_path: str):
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return "", ""
        # End-if
        ws_real = os.path.realpath(self.workspace_dir) if self.workspace_dir else os.path.realpath(os.getcwd())
        candidate = cleaned if os.path.isabs(cleaned) else os.path.join(ws_real, cleaned)
        real_abs = os.path.realpath(candidate)
        canon_key = os.path.normcase(real_abs)
        ws_key = os.path.normcase(ws_real)
        if canon_key == ws_key:
            return "", ""
        # End-if
        try:
            if os.path.commonpath([ws_key, canon_key]) == ws_key:
                shown = os.path.relpath(real_abs, ws_real).replace("\\", "/")
            else:
                shown = real_abs.replace("\\", "/")
            # End-if
        except ValueError:
            shown = real_abs.replace("\\", "/")
        # End-try
        return canon_key, shown
    # End-def

    ##
     # @brief Return tool's name.
     #
    def get_name(self):
        return "plan_tool"
    # End-def

    ##
     # @brief Return tool's description.
     #
    def get_description(self):
        return (
            "Submit and strictly validate a structured TaskPlan BEFORE spawning multiple "
            "or parallel SubAgents. Decompose the complex task yourself using your full "
            "conversation context, select each subtask's 'model' according to the SubAgent "
            "model descriptions, assign 'toolset', 'depends_on', and 'target_files', and "
            "receive validated parallel execution waves."
        )
    # End-def

    ##
     # @brief Return tool's JSON input schema.
     #
    def get_schema(self):
        model_aliases = self.registry.aliases() if self.registry else []
        model_prop = {
            "type": "string",
            "description": "Target SubAgent model alias from SUB_LIST for this subtask.",
        }
        if model_aliases:
            model_prop["enum"] = model_aliases
        # End-if

        return {
            "type": "object",
            "properties": {
                "overall_goal": {
                    "type": "string",
                    "description": "Concise summary of the overall complex task objective.",
                },
                "execution_strategy": {
                    "type": "string",
                    "enum": list(VALID_STRATEGIES),
                    "description": "Execution topology across subtasks: 'parallel', 'sequential', or 'mixed'.",
                },
                "subtasks": {
                    "type": "array",
                    "description": f"Non-empty list of subtasks (1..{MAX_PLAN_SUBTASKS}).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Unique subtask ID (e.g., 'task-1')."},
                            "model": model_prop,
                            "toolset": {
                                "type": "string",
                                "enum": list(TOOLSET_REGISTRY.keys()),
                                "description": "Pre-defined worker toolset for this subtask.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Detailed, self-contained instructions for the SubAgent.",
                            },
                            "role_prompt": {
                                "type": "string",
                                "description": "System role and behavioral boundaries for the SubAgent.",
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "IDs of prerequisite subtasks ([] if independent).",
                            },
                            "target_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Files this subtask may create or modify ([] if read-only).",
                            },
                        },
                        "required": ["id", "model", "toolset", "description", "role_prompt"],
                    },
                },
            },
            "required": ["overall_goal", "execution_strategy", "subtasks"],
        }
    # End-def

    ##
     # @brief Validate the submitted TaskPlan and compute topological execution waves.
     #
     # @param kwargs Schema properties: overall_goal, execution_strategy, subtasks.
     #
     # @return (success_bool, json_or_error_string) tuple.
     #
    def execute(self, **kwargs):
        overall_goal = str(kwargs.get("overall_goal") or "").strip()
        strategy = str(kwargs.get("execution_strategy") or "").strip().lower()
        raw_subtasks = kwargs.get("subtasks")

        if not overall_goal:
            return False, "Error: 'overall_goal' must be a non-empty string."
        # End-if
        if strategy not in VALID_STRATEGIES:
            return False, (
                f"Error: 'execution_strategy' must be one of {list(VALID_STRATEGIES)}, "
                f"got {strategy!r}."
            )
        # End-if
        if not isinstance(raw_subtasks, list) or not raw_subtasks:
            return False, "Error: 'subtasks' must be a non-empty list."
        # End-if
        if len(raw_subtasks) > MAX_PLAN_SUBTASKS:
            return False, (
                f"Error: 'subtasks' has {len(raw_subtasks)} items, exceeding limit {MAX_PLAN_SUBTASKS}."
            )
        # End-if

        valid_models = self.registry.aliases() if self.registry else []
        ordered_ids = []
        id_set = set()
        deps_map = {}
        files_map = {}
        display_by_canon = {}
        normalized_subtasks = []

        for idx, item in enumerate(raw_subtasks):
            if not isinstance(item, dict):
                return False, f"Error: subtasks[{idx}] must be an object."
            # End-if
            sid = str(item.get("id") or "").strip()
            if not sid:
                return False, f"Error: subtasks[{idx}] is missing a non-empty 'id'."
            # End-if
            if sid in id_set:
                return False, f"Error: duplicate subtask id '{sid}'."
            # End-if

            model = str(item.get("model") or "").strip()
            if not model or (self.registry is not None and self.registry.get_spec(model) is None):
                return False, (
                    f"Error: subtask '{sid}' specifies invalid model {model!r}. "
                    f"Available SubAgent models: {valid_models}"
                )
            # End-if

            toolset = str(item.get("toolset") or "").strip()
            if toolset not in TOOLSET_REGISTRY:
                return False, (
                    f"Error: subtask '{sid}' specifies unknown toolset {toolset!r}. "
                    f"Available toolsets: {list(TOOLSET_REGISTRY.keys())}"
                )
            # End-if
            if self.parent_tools is not None:
                effective = [t for t in TOOLSET_REGISTRY[toolset] if t in self.parent_tools]
                if not effective:
                    return False, (
                        f"Error: subtask '{sid}' toolset '{toolset}' has no overlap with "
                        f"parent SubAgent capabilities ({sorted(self.parent_tools)})."
                    )
                # End-if
            # End-if

            desc = str(item.get("description") or "").strip()
            role = str(item.get("role_prompt") or "").strip()
            if not desc or not role:
                return False, f"Error: subtask '{sid}' requires non-empty 'description' and 'role_prompt'."
            # End-if

            raw_deps = item.get("depends_on") if item.get("depends_on") is not None else []
            if not isinstance(raw_deps, list) or not all(isinstance(d, str) for d in raw_deps):
                return False, f"Error: subtask '{sid}' field 'depends_on' must be a list of subtask IDs."
            # End-if
            deps = []
            for d in raw_deps:
                dep_id = d.strip()
                if dep_id and dep_id not in deps:
                    deps.append(dep_id)
                # End-if
            # End-for

            raw_files = item.get("target_files") if item.get("target_files") is not None else []
            if not isinstance(raw_files, list) or not all(isinstance(f, str) for f in raw_files):
                return False, f"Error: subtask '{sid}' field 'target_files' must be a list of file paths."
            # End-if
            canon_files = []
            shown_files = []
            for fp in raw_files:
                canon_key, shown = self._resolve_target_file(fp)
                if canon_key and canon_key not in canon_files:
                    canon_files.append(canon_key)
                    shown_files.append(shown)
                    display_by_canon.setdefault(canon_key, shown)
                # End-if
            # End-for

            ordered_ids.append(sid)
            id_set.add(sid)
            deps_map[sid] = deps
            files_map[sid] = set(canon_files)
            normalized_subtasks.append({
                "id": sid,
                "model": model,
                "toolset": toolset,
                "depends_on": deps,
                "target_files": shown_files,
            })
        # End-for

        for sid in ordered_ids:
            for dep in deps_map[sid]:
                if dep == sid:
                    return False, f"Error: subtask '{sid}' cannot depend on itself."
                # End-if
                if dep not in id_set:
                    return False, (
                        f"Error: subtask '{sid}' depends on unknown subtask '{dep}'. "
                        f"Valid subtask IDs: {ordered_ids}"
                    )
                # End-if
            # End-for
        # End-for

        dag_waves, ancestors, cyclic = _analyze_dag(ordered_ids, deps_map)
        if cyclic:
            return False, f"Error: dependency cycle detected among subtasks: {cyclic}."
        # End-if

        if strategy == "sequential":
            waves = _sequential_waves(ordered_ids, deps_map)
        else:
            if strategy == "parallel" and len(dag_waves) > 1:
                return False, (
                    "Error: 'execution_strategy' is 'parallel' but 'depends_on' defines multi-wave "
                    "dependencies. Use 'mixed' or 'sequential', or remove dependencies."
                )
            # End-if
            waves = dag_waves

            # Check parallel shared-workspace file write conflicts across unordered subtask pairs.
            for i in range(len(ordered_ids)):
                for j in range(i + 1, len(ordered_ids)):
                    a_id, b_id = ordered_ids[i], ordered_ids[j]
                    ordered_pair = (a_id in ancestors[b_id]) or (b_id in ancestors[a_id])
                    if not ordered_pair:
                        overlap_keys = sorted(files_map[a_id] & files_map[b_id])
                        if overlap_keys:
                            overlap_shown = [display_by_canon.get(k, k) for k in overlap_keys]
                            return False, (
                                f"Error: subtasks '{a_id}' and '{b_id}' have no dependency ordering "
                                f"(may run in parallel) but both target file(s): {overlap_shown}. "
                                f"Add 'depends_on' to serialize them, use 'sequential' strategy, "
                                f"or assign disjoint 'target_files'."
                            )
                        # End-if
                    # End-if
                # End-for
            # End-for
        # End-if

        self._emit(
            f"\n[+] [PlanTool:{self.caller_label}] Validated TaskPlan: "
            f"{len(normalized_subtasks)} subtask(s) across {len(waves)} wave(s)."
        )
        result = {
            "status": "validated",
            "overall_goal": overall_goal,
            "execution_strategy": strategy,
            "execution_waves": waves,
            "subtasks": normalized_subtasks,
        }
        return True, json.dumps(result, ensure_ascii=False, indent=2)
    # End-def
# End-class
