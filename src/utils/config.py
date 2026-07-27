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
        
    # Read the master switch
    active_profile = config.get("API", "API_ENABLE", fallback=None)
    
    # Fallback for backward compatibility if API_ENABLE is missing or section does not exist
    if not active_profile or not config.has_section(active_profile):
        active_profile = "API"
        
    try:
        max_tokens = int(config.get(active_profile, "MAX_TOKENS", fallback="8000"))
    except ValueError:
        max_tokens = 8000

    return {
        "ACTIVE_PROFILE": active_profile,
        "ANTHROPIC_BASE_URL": config.get(active_profile, "ANTHROPIC_BASE_URL", fallback=""),
        "ANTHROPIC_API_KEY": config.get(active_profile, "ANTHROPIC_API_KEY", fallback=""),
        "MODEL_ID": config.get(active_profile, "MODEL_ID", fallback=""),
        "MAX_TOKENS": max_tokens
    }