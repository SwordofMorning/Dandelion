"""Run the shared agent tool loop at safe cancellation boundaries."""


def run_agent_loop(agent, should_stop=None):
    """Return (completed, error), preserving the agent step result contract."""
    if should_stop is None:
        should_stop = agent.runtime.cancelled
    while True:
        if should_stop():
            return False, None
        cont, err = agent.step()
        if should_stop():
            return False, None
        if err is not None:
            return False, err
        if not cont:
            return True, None
