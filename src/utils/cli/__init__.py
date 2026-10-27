##
 # @file src/utils/cli/__init__.py
 # @date 2026/08/04
 # 
 # @brief CLI Package.
 # Provides interactive command-line interface and colored printing utilities.
 #

from .printer import CLIPrinter

__all__ = [
    "CLIPrinter",
    "InteractiveCLI",
]

# Convenience instance for direct usage.
#
# @note Named cli_printer (not cli) on purpose: a `cli` attribute here would
#       shadow the cli.py submodule, making `import src.utils.cli.cli as x`
#       (and `import src.utils.cli.<submodule> as x`) fail.
cli_printer = CLIPrinter()


def __getattr__(name):
    if name == "InteractiveCLI":
        from .cli import InteractiveCLI
        globals()[name] = InteractiveCLI
        return InteractiveCLI
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
