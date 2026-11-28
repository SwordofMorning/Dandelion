##
 # @file packages/dandelion-core/src/dandelion_core/agent/runner.py
 # @date 2026/11/28
 #
 # @brief Shared main-agent turn runner.
 # Repeats complete agent steps until a response finishes, an API error occurs,
 # or the caller requests cancellation at a boundary before or after a step.
 # Resumes seamlessly when a mid-stream RequestCancelledError is cleared by
 # should_stop() (for example, CLI user selecting [C]ontinue).
 #
 # @note Host callbacks own any rollback policy. The runner only returns the
 # completion/error tuple and does not modify history or session files itself.
 #

from ..utils.llm_provider.blocks import RequestCancelledError


##
 # @brief Run agent steps while preserving completion and error tuple semantics.
 #
 # @param agent Agent whose runtime, session and history are used.
 # @param should_stop Optional cancellation checkpoint; defaults to agent.runtime.cancelled.
 #
 # @return (completed, error); cancellation returns (False, None).
 #
def run_agent_loop(agent, should_stop=None):
    if should_stop is None:
        should_stop = agent.runtime.cancelled
    # End-if
    while True:
        if should_stop():
            return False, None
        # End-if
        cont, err = agent.step()
        if should_stop():
            return False, None
        # End-if
        if isinstance(err, RequestCancelledError):
            # Interrupt was cleared by should_stop() (e.g. CLI user chose [C]ontinue);
            # resume the interrupted LLM step immediately instead of aborting.
            continue
        # End-if
        if err is not None:
            return False, err
        # End-if
        if not cont:
            return True, None
        # End-if
    # End-while
# End-def
