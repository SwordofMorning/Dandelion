##
 # @file src/utils/cli/__init__.py
 # @date 2026/08/04
 # 
 # @brief CLI Package.
 # Provides interactive command-line interface and colored printing utilities.
 #

from .cli_printer import CLIPrinter
from .interactive_cli import InteractiveCLI

__all__ = [
    "CLIPrinter",
    "InteractiveCLI",
]

# Convenience instance for direct usage
cli = CLIPrinter()