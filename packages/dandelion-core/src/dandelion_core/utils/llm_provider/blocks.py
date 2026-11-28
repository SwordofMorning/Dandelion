##
 # @file packages/dandelion-core/src/dandelion_core/utils/llm_provider/blocks.py
 # @date 2026/11/28
 #
 # @brief Python containers and error string subclasses created by the native provider module.
 # Response content blocks are plain dicts that also allow attribute access, so
 # callers reading `block.name` and callers reading `block["name"]` (history
 # reloaded from disk) see the same object, and json.dump needs no hook.
 #


##
 # @brief Content block: a dict whose keys are also readable as attributes.
 #
class Block(dict):
    __slots__ = ()

    ##
     # @brief Resolve a missing attribute from the dict keys.
     #
     # @param name Attribute name.
     #
     # @return Stored value.
     #
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None
        # End-try
    # End-def
# End-class


##
 # @brief json.dumps default hook for request bodies (mirrors SessionManager).
 #
 # @param obj Object json cannot encode natively.
 #
 # @return JSON-encodable replacement.
 #
def json_default(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    # End-if
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    # End-if
    return str(obj)
# End-def


##
 # @brief Request error raised after user-visible output has already been streamed.
 # Replaying the request would duplicate visible terminal output.
 #
class PartialStreamError(str):
    __slots__ = ()
# End-class


##
 # @brief Request error indicating cooperative cancellation by a runtime interrupt.
 # Callers should break out of retry loops immediately without sleeping or replaying.
 #
class RequestCancelledError(str):
    __slots__ = ()
# End-class
