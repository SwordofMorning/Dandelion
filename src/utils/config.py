# src/utils/config.py
import os
import configparser

def load_api_config(file_path):
    if not os.path.exists(file_path):
        return None
    
    config = configparser.ConfigParser()
    config.read(file_path)
    
    if not config.has_section("API"):
        return None
        
    try:
        max_tokens = int(config.get("API", "MAX_TOKENS", fallback="8000"))
    except ValueError:
        max_tokens = 8000

    return {
        "ANTHROPIC_BASE_URL": config.get("API", "ANTHROPIC_BASE_URL", fallback=""),
        "ANTHROPIC_API_KEY": config.get("API", "ANTHROPIC_API_KEY", fallback=""),
        "MODEL_ID": config.get("API", "MODEL_ID", fallback=""),
        "MAX_TOKENS": max_tokens
    }