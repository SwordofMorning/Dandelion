##
 # @file src/utils/logging/logger.py
 # @date 2026/08/05
 # 
 # @brief Agent Logger Component.
 # Provides logging utilities for the agent.
 #

import os
import json
import datetime

##
 # @brief Agent's log interface.
 #
class AgentLogger:

    ##
     # @brief Constructor.
     #
     # @param log_dir: Default to `.log/` subfolder.
     #
    def __init__(self, log_dir="./.log"):
        self.log_dir = log_dir
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
    # End-def

    ##
     # @brief Get date.
     #
     # @return log dir append date suffix.
     #
    def _get_daily_log_file(self):
        now_str = datetime.datetime.now().strftime("%Y_%m_%d")
        return os.path.join(self.log_dir, f"{now_str}.log")
    # End-def

    ##
     # @brief Serializer OBJ.
     #
     # @return OBJ's metadata.
     #
    def _default_serializer(self, obj):
        if hasattr(obj, "model_dump"): return obj.model_dump()
        if hasattr(obj, "dict"): return obj.dict()
        if hasattr(obj, "__dict__"): return obj.__dict__
        return str(obj)
    # End-def

    ##
     # @brief Write payload to log file.
     #
     # @param tag, MainAgent or SubAgent with serial number (sa-xxxx).
     # @param payload, original data (payload, which is sent to LLM) to save.
     #
    def log_api_call(self, tag, payload):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"==================== [{tag}] {now_str} ===================="
        log_file = self._get_daily_log_file()
        
        json_str = json.dumps(payload, ensure_ascii=False, indent=2, default=self._default_serializer)
        
        # C-style direct write, check path beforehand
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"{header}\n{json_str}\n\n")
    # End-def
# End-class