##
 # @file src/utils/cli_printer.py
 # @date 2026/08/04
 # 
 # @brief CLI printing and formatting utility.
 # A wrapper function, string -> cli_printer -> string(colorful) -> terminal
 #

import builtins

##
 # @brief Centralized print class with color control and level-based formatting.
 # 
 # Provides consistent colored output for CLI applications with support for
 # different message levels: info, success, error, warning, debug, agent, raw.
 #
class CLIPrinter:    
    # ANSI Color Codes
    C_RESET   = "\033[0m"
    C_RED     = "\033[31m"
    C_GREEN   = "\033[32m"
    C_YELLOW  = "\033[33m"
    C_BLUE    = "\033[34m"
    C_MAGENTA = "\033[35m"
    C_CYAN    = "\033[36m"
    C_GRAY    = "\033[90m"

    ##
     # @brief Centralized print function with color control and level dispatch.
     # 
     # @param msg The message to print.
     # @param level Output level - one of: info, success, error, warning, debug, agent, raw.
     # @param end String appended after the message (default: newline).
     #
     # @return None.
     #
    @staticmethod
    def print(msg: str, level: str = "info", end: str = "\n") -> None:
        # Handle prefix newline cleanly
        if msg.startswith("\n"):
            builtins.print("\n", end="")
            msg = msg.lstrip("\n")
        # End-if

        prefix = ""
        
        # Level-based prefix dispatcher
        if level == "info":
            prefix = f"{CLIPrinter.C_CYAN}[*]{CLIPrinter.C_RESET} "
        elif level == "success":
            prefix = f"{CLIPrinter.C_GREEN}[+]{CLIPrinter.C_RESET} "
        elif level == "error":
            prefix = f"{CLIPrinter.C_RED}[-]{CLIPrinter.C_RESET} "
        elif level == "warning":
            prefix = f"{CLIPrinter.C_YELLOW}[!]{CLIPrinter.C_RESET} "
        elif level == "debug":
            prefix = f"{CLIPrinter.C_GRAY}[>]{CLIPrinter.C_RESET} "
        elif level == "agent":
            prefix = f"{CLIPrinter.C_MAGENTA}[Agent]{CLIPrinter.C_RESET} "
        elif level == "raw":
            prefix = ""
        else:
            # Default to info for unknown levels
            prefix = f"{CLIPrinter.C_CYAN}[*]{CLIPrinter.C_RESET} "
        # End-if
            
        builtins.print(f"{prefix}{msg}", end=end)

    ##
     # @brief Print an informational message (cyan [*]).
     #
    @staticmethod
    def info(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="info", end=end)
    # End-def

    ##
     # @brief Print a success message (green [+]).
     #
    @staticmethod
    def success(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="success", end=end)
    # End-def

    ##
     # @brief Print an error message (red [-]).
     #
    @staticmethod
    def error(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="error", end=end)
    # End-def

    ##
     # @brief Print a warning message (yellow [!]).
     #
    @staticmethod
    def warning(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="warning", end=end)
    # End-def

    ##
     # @brief Print a debug message (gray [>]).
     #
    @staticmethod
    def debug(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="debug", end=end)
    # End-def

    ##
     # @brief Print an agent message (magenta [Agent]).
     #
    @staticmethod
    def agent(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="agent", end=end)
    # End-def

    ##
     # @brief Print a raw message without any prefix or formatting.
     #
    @staticmethod
    def raw(msg: str, end: str = "\n") -> None:
        CLIPrinter.print(msg, level="raw", end=end)
    # End-def

    ##
     # @brief Print a divider line.
     # 
     # @param char Character to repeat for the divider.
     # @param length Length of the divider line.
     # @param color Optional color constant (e.g., CLIPrinter.C_CYAN).
     # 
     # @return None.
     #
    @staticmethod
    def divider(char: str = "=", length: int = 50, color: str = None) -> None:
        line = char * length
        if color:
            line = f"{color}{line}{CLIPrinter.C_RESET}"
        CLIPrinter.raw(line)
    # End-def

    ##
     # @brief Print a formatted header with title centered between dividers.
     # 
     # @param title The header title text.
     # @param char Divider character.
     # @param length Total width of the header.
     # @param color Optional color for the dividers.
     # 
     # @return None.
     #
    @staticmethod
    def header(title: str, char: str = "=", length: int = 50, color: str = None) -> None:
        if color is None:
            color = CLIPrinter.C_CYAN
        CLIPrinter.divider(char, length, color)
        # Center the title
        padding = (length - len(title)) // 2
        CLIPrinter.raw(f"{' ' * padding}{title}")
        CLIPrinter.divider(char, length, color)
    # End-def

    ##
     # @brief Print a key-value pair with optional colors.
     # 
     # @param key The key/label.
     # @param value The value.
     # @param key_color Optional color for the key.
     # @param value_color Optional color for the value.
     # 
     # @return None.
     #
    @staticmethod
    def key_value(key: str, value: str, key_color: str = None, value_color: str = None) -> None:
        formatted_key = f"{key_color}{key}{CLIPrinter.C_RESET}" if key_color else key
        formatted_value = f"{value_color}{value}{CLIPrinter.C_RESET}" if value_color else value
        CLIPrinter.raw(f"{formatted_key}: {formatted_value}")
    # End-def
# End-class

# @note Convenience instance for direct usage
cli = CLIPrinter()

if __name__ == "__main__":
    # Demo usage
    print("=== CLIPrinter Demo ===\n")
    
    cli.info("This is an info message")
    cli.success("Operation completed successfully")
    cli.error("Something went wrong")
    cli.warning("This is a warning")
    cli.debug("Debug information")
    cli.agent("Agent response here")
    cli.raw("Raw output without prefix")
    
    print()
    cli.header("SECTION HEADER")
    print()
    
    cli.key_value("Model", "nemotron-3-ultra", cli.C_CYAN, cli.C_GREEN)
    cli.key_value("Status", "Running", cli.C_CYAN, cli.C_YELLOW)
    cli.key_value("Tokens", "1,234", cli.C_CYAN, cli.C_GRAY)
    
    print()
    cli.divider("-", 40, cli.C_BLUE)
    cli.raw("End of demo")