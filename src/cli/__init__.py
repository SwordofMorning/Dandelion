##
 # @file src/cli/__init__.py
 # @date 2026/08/04
 #
 # @brief Terminal frontend package exports.
 # Provides colored printing utilities and lazily exposes the interactive shell
 # without creating a prompt session merely by importing the package.
 #

from .printer import CLIPrinter

__all__ = [
    "CLIPrinter",
    "InteractiveCLI",
]

# Convenience instance for direct usage.
#
# @note Named cli_printer (not cli) on purpose: a `cli` attribute here would
#       shadow the cli.py submodule, making `import src.cli.cli as x`
#       (and `import src.cli.<submodule> as x`) fail.
cli_printer = CLIPrinter()


##
 # @brief Load the interactive shell only when explicitly requested.
 # @param name Public CLI helper name.
 # @return Interactive shell class.
 #
def __getattr__(name):
    if name == "InteractiveCLI":
        from .cli import InteractiveCLI
        globals()[name] = InteractiveCLI
        return InteractiveCLI
    # End-if
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
# End-def
