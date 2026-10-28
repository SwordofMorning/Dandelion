##
 # @file src/remote/tool/communication/message_tool.py
 # @date 2026/10/28
 #
 # @brief Agent-initiated external communication tool.
 # Allows Dandelion Remote to decide autonomously when to send a message to
 # the user over Telegram or the durable reply channel.
 #

import json
from src.core.tool.base_tool import BaseTool


##
 # @brief Tool enabling Dandelion Remote to send messages to the user on demand.
 #
class SendMessageTool(BaseTool):
    ##
     # @brief Bind the communication tool to a conversation session and channel hub.
     #
     # @param hub ChannelHub instance routing messages to Telegram and HTTP clients.
     # @param default_session_id Default conversation identifier for this agent.
     #
     # @return None.
     #
    def __init__(self, hub, default_session_id):
        super().__init__()
        self.hub = hub
        self.default_session_id = str(default_session_id)
        self.sent_in_turn = 0
    # End-def

    ##
     # @brief Reset the per-turn send counter before a new event turn starts.
     #
     # @return None.
     #
    def reset_turn(self):
        self.sent_in_turn = 0
    # End-def

    ##
     # @brief Return the registered tool name.
     #
     # @return Tool identifier string.
     #
    def get_name(self):
        return "send_message"
    # End-def

    ##
     # @brief Describe the tool capability to the model.
     #
     # @return Human-readable tool description.
     #
    def get_description(self):
        return (
            "Send a message to the user via Telegram or the active remote chat channel. "
            "Use this tool whenever you want to reply to the user, proactively report "
            "completed background work, or share important insights from synced sessions. "
            "If you are processing a background sync or schedule and do not need to disturb "
            "the user, simply do not call this tool."
        )
    # End-def

    ##
     # @brief Return the model-visible JSON input schema.
     #
     # @return JSON schema dictionary.
     #
    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Message text to send to the user."
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional target session/chat identifier (e.g. 'telegram:123456'). Defaults to the current conversation or primary Telegram chat."
                }
            },
            "required": ["text"]
        }
    # End-def

    ##
     # @brief Deliver the message through ChannelHub and record that a message was sent.
     #
     # @param text Message text to deliver.
     # @param session_id Optional target session identifier override.
     #
     # @return (success_bool, status_json_string) tuple.
     #
    def execute(self, text="", session_id=""):
        body = str(text or "").strip()
        if not body:
            return False, "Error: 'text' must be non-empty."
        # End-if
        target = str(session_id or "").strip() or self.default_session_id
        try:
            delivered_to = self.hub.notify(target, body)
            self.sent_in_turn += 1
            return True, json.dumps({"sent": True, "session_id": delivered_to, "chars": len(body)})
        except Exception as exc:
            return False, f"Error sending message: {exc}"
        # End-try
    # End-def
# End-class
