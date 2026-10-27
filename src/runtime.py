"""Small host callbacks shared by terminal and headless agent runtimes."""

from dataclasses import dataclass
from typing import Callable


def _not_cancelled():
    return False


def _deny_approval(action_desc, abs_target, abs_workspace):
    return False


@dataclass
class Runtime:
    """Host-owned output, cancellation and approval without terminal imports."""

    output: Callable[[str], None] = print
    cancelled: Callable[[], bool] = _not_cancelled
    approve: Callable[[str, str, str], bool] = _deny_approval
    echo: bool = False

    def emit(self, text):
        self.output(str(text))
