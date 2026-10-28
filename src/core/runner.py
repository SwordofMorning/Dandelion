##
 # @file src/core/runner.py
 # @brief Run the shared agent tool loop at safe cancellation boundaries.
 #


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
        if err is not None:
            return False, err
        # End-if
        if not cont:
            return True, None
        # End-if
    # End-while
# End-def
