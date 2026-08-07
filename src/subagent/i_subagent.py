##
 # @file src/subagent/i_subagent.py
 # @date 2026/08/07
 # 
 # @brief Subagent virtual base class.
 # subagent.py provided a generic subagent class derivative from virtual parent,
 # which seems unnecessary;
 # but if you need to implement a specified subagent class, this virtual might be useful.
 #

##
 # @brief Subagent virtual base class.
 #
class ISubAgent:
    ##
     # @brief COnstructor.
     #
     # @param safe_client llm_request client.
     # @param logger log management.
     # @param tools toolset.
     # @param config environment config.
     #
    def __init__(self, safe_client, logger, tools, config):
        self.client = safe_client
        self.logger = logger
        self.tools = tools
        self.config = config
        
        self.system_prompt = (
            "You are a dedicated SubAgent. "
            "Complete the sub-task using available tools, then return a concise summary. "
            "Do not delegate further."
        )
        self.tool_schemas = [
            {
                "name": t.get_name(), 
                "description": t.get_description(), 
                "input_schema": t.get_schema()
            } for t in self.tools.values()
        ]
    # End-def

    ##
     # @brief SubAgent's loop.
     #
    def run(self, task_description):
        print(f"\n[*] [SubAgent] Spawned for task: {task_description[:50]}...")
        messages = [{"role": "user", "content": task_description}]
        
        # Max loop limits.
        for turn in range(30):
            # Enforce Dictionary Key Ordering for Cache Optimization
            payload = {
                "tools": self.tool_schemas,
                "messages": messages,
                "max_tokens": self.config.get("MAX_TOKENS", 8000),
                "system": self.system_prompt
            }

            self.logger.log_api_call(f"SUBAGENT PRE-CALL (Turn {turn+1})", payload)

            # Steaming
            resp, err = self.client.safe_stream_request(payload)

            if err:
                print(f"[-] [SubAgent] API Error: {err}")
                return f"Subagent Error: {err}"

            self.logger.log_api_call(f"SUBAGENT POST-CALL (Turn {turn+1})", resp)
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                break

            # Tools iterator.
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    handler = self.tools.get(block.name)
                    if handler:
                        success, output = handler.execute(**block.input)
                    else:
                        success, output = False, f"Unknown tool: {block.name}"
                    # End-if

                    print(f"    [>] sub-tool {block.name} execution: {'Success' if success else 'Failed'}")
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                # End-if
            # End-for
            messages.append({"role": "user", "content": results})
        # End-for

        # Result check.
        result_text = self.client.extract_text(messages[-1].get("content"))
        if not result_text:
            for msg in reversed(messages):
                if msg["role"] == "assistant":
                    result_text = self.client.extract_text(msg["content"])
                    if result_text: break
                # End-if
            # End-for
        # End-if

        print("[*] [SubAgent] Finished task.")
        return result_text if result_text else "Subagent stopped without final answer."
    # End-def
# End-class