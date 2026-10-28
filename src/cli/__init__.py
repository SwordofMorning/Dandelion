##
 # @file src/cli/__init__.py
 # @date 2026/08/04
 #
 # @brief Terminal frontend package exports.
 # Provides colored printing utilities, the CLI prompt builder and the
 # interactive shell class.
 #

from .printer import CLIPrinter
from .cli import InteractiveCLI
from .sysprompt import CLIPromptBuilder

cli_printer = CLIPrinter()

__all__ = [
    "CLIPrinter",
    "InteractiveCLI",
    "CLIPromptBuilder",
    "cli_printer",
]
