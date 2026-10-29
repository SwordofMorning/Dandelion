##
 # @file src/remote/tool/communication/message_tool.py
 # @date 2026/10/29
 #
 # @brief Agent-initiated external communication tool.
 # Serves as the sole outbound messaging gateway for Dandelion Remote to reach
 # the user over Telegram or durable remote channels, while ordinary plain-text
 # model outputs remain persisted strictly inside history.log.
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
            "This is the ONLY way the user receives your messages (ordinary plain-text "
            "responses are saved to history.log only and are NOT sent to the user). "
            "Call this tool whenever you want to reply, send a quick ACK, report completed "
            "work, or proactively share insights. Do not call it when working silently."
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
                },
                "notify": {
                    "type": "boolean",
                    "description": "True (default) to trigger an explicit popup/sound notification on the user's device; False for silent delivery (e.g. late-night notes or low-priority background logs)."
                },
                "pin": {
                    "type": "boolean",
                    "description": "True for critical alerts, completed long background tasks, or key reminders to pin the message to the top of the Telegram chat header for persistent visual visibility; False by default."
                },
                "quote": {
                    "type": "boolean",
                    "description": "True to quote-reply the user's latest Telegram message (useful when disambiguating among multiple messages); False by default for clean plain messages."
                }
            },
            "required": ["text"]
        }
    # End-def

    ##
     # @brief Deliver the message through ChannelHub.
     #
     # @param text Message text to deliver.
     # @param session_id Optional target session identifier override.
     # @param notify True to trigger a device notification; False for silent delivery.
     # @param pin True to pin the message in Telegram for persistent visual visibility.
     # @param quote True to quote-reply the user's latest Telegram message; False by default.
     #
     # @return (success_bool, status_json_string) tuple.
     #
    def execute(self, text="", session_id="", notify=True, pin=False, quote=False):
        body = str(text or "").strip()
        if not body:
            return False, "Error: 'text' must be non-empty."
        # End-if
        target = str(session_id or "").strip() or self.default_session_id
        notify_flag = True if notify is None else bool(notify)
        pin_flag = bool(pin)
        quote_flag = bool(quote)
        try:
            delivered_to = self.hub.notify(
                target,
                body,
                notify=notify_flag,
                pin=pin_flag,
                quote=quote_flag,
            )
            return True, json.dumps({
                "sent": True,
                "session_id": delivered_to,
                "chars": len(body),
                "notify": notify_flag,
                "pin": pin_flag,
                "quote": quote_flag,
            })
        except Exception as exc:
            return False, f"Error sending message: {exc}"
        # End-try
    # End-def
# End-class
