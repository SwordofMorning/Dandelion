##
 # @file src/core/subagent/dispatch.py
 # @date 2026/10/21
 #
 # @brief Ordered tool dispatch with parallel SubAgent segments.
 # Runs consecutive spawn calls in bounded parallel segments and executes other
 # tools as barriers, returning one outcome per call in the original order.
 #
 # @note Contract (used by MyAgent.step and SubAgent.run):
 #   - Every tool_use block gets exactly one outcome, at its own ordinal slot,
 #     whether it succeeded, failed, was unknown or crashed.
 #   - Program order is preserved: CONSECUTIVE spawn_subagent calls form one
 #     parallel segment; any other tool is a barrier that runs alone, after the
 #     previous segment has fully finished and before the next one starts.
 #   - Only spawn_subagent is parallelised. SubAgents share the workspace, so
 #     their own tool calls still race on files; see the note in sysprompt.py
 #     that asks the model to give parallel SubAgents disjoint files.
 #   - The caller converts outcomes to tool_result blocks; workers never touch
 #     the caller's history.
 #

import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

PARALLEL_TOOL = "spawn_subagent"

# Main-thread wait granularity: short enough for the SIGINT handler to run
# promptly while SubAgents are working.
_WAIT_SLICE_SECONDS = 0.2

##
 # @brief True when the caller runs on a dispatcher worker thread.
 #
 # @note SubAgents use it to stream silently (parallel token streams would
 #       interleave on the terminal).
 #
def in_worker_thread():
    return threading.current_thread().name.startswith("subagent-worker")
# End-def

##
 # @brief Execute one tool call; never raises.
 #
 # @return (success, output).
 #
def _run_one(tools, block):
    handler = tools.get(block.name)
    if handler is None:
        return False, f"Unknown tool: {block.name}"
    # End-if
    try:
        args = dict(block.input) if isinstance(block.input, dict) else {}
        return handler.execute(**args)
    except Exception as e:
        return False, f"Tool execution crashed internally: {e!s}"
    # End-try
# End-def

##
 # @brief Run every tool_use block of a response.
 #
 # @param tools Name -> tool instance mapping of the calling agent.
 # @param blocks Response content blocks (non tool_use blocks are skipped).
 # @param max_workers Upper bound of SubAgents running at once (1 = serial).
 # @param on_start Optional callback(block) invoked right before a call starts.
 #
 # @return List of (block, success, output) in original tool_use order.
 #
def dispatch_tool_calls(tools, blocks, max_workers=1, on_start=None):
    calls = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
    outcomes = [None] * len(calls)

    i = 0
    while i < len(calls):
        # Build a segment: a run of consecutive spawns, or one other call.
        j = i + 1
        if calls[i].name == PARALLEL_TOOL:
            while j < len(calls) and calls[j].name == PARALLEL_TOOL:
                j += 1
            # End-while
        # End-if
        segment = list(range(i, j))
        workers = min(max(int(max_workers or 1), 1), len(segment))

        if workers <= 1:
            for k in segment:
                if on_start:
                    on_start(calls[k])
                # End-if
                outcomes[k] = _run_one(tools, calls[k])
            # End-for
        else:
            outcomes_part = _run_parallel(tools, calls, segment, workers, on_start)
            for k, outcome in outcomes_part.items():
                outcomes[k] = outcome
            # End-for
        # End-if
        i = j
    # End-while

    return [(calls[k], outcomes[k][0], outcomes[k][1]) for k in range(len(calls))]
# End-def

##
 # @brief Run one parallel segment and wait until EVERY worker has finished.
 #
 # @note Returning only after all workers stopped is what makes a Ctrl+C
 #       rollback safe: no SubAgent can still be writing when the CLI restores
 #       the session. SubAgents check the stop flag at turn boundaries.
 #
def _run_parallel(tools, calls, segment, workers, on_start):
    results = {}
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="subagent-worker")
    try:
        futures = {}
        for k in segment:
            if on_start:
                on_start(calls[k])
            # End-if
            futures[pool.submit(_run_one, tools, calls[k])] = k
        # End-for
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=_WAIT_SLICE_SECONDS, return_when=FIRST_COMPLETED)
            for fut in done:
                k = futures[fut]
                try:
                    results[k] = fut.result()
                except Exception as e:  # _run_one never raises; defensive only
                    results[k] = (False, f"Tool execution crashed internally: {e!s}")
                # End-try
            # End-for
        # End-while
    finally:
        pool.shutdown(wait=True)
    # End-try
    return results
# End-def
