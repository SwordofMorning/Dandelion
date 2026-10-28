##
 # @file src/remote/__main__.py
 # @date 2026/10/28
 #
 # @brief Remote command-line module entry.
 # Delegates python -m src.remote to the service command dispatcher, keeping
 # argument parsing and process lifecycle assembly in the service module.
 #
 # @note Commands execute only through the module entry guard.
 #

from .service import main


if __name__ == "__main__":
    main()
# End-if
