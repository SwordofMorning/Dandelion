# src/utils/rate_limiter.py

import time
import threading
from collections import deque
from dataclasses import dataclass, field

@dataclass
class RateLimitConfig:
    tpm: int = 0      
    rpm: int = 0      
    rpd: int = 0      

@dataclass
class UsageWindow:
    minute_tokens: deque = field(default_factory=deque)
    minute_requests: deque = field(default_factory=deque)
    day_requests: deque = field(default_factory=deque)

class RateLimiter:
    def __init__(self):
        self._windows = {}
        self._lock = threading.RLock()
        self._configs = {}

    def register(self, alias: str, config: RateLimitConfig):
        with self._lock:
            self._configs[alias] = config
            self._windows[alias] = UsageWindow()

    def _cleanup(self, window: UsageWindow):
        now = time.time()
        while window.minute_requests and window.minute_requests[0] < now - 60:
            window.minute_requests.popleft()
        while window.minute_tokens and window.minute_tokens[0][0] < now - 60:
            window.minute_tokens.popleft()
        while window.day_requests and window.day_requests[0] < now - 86400:
            window.day_requests.popleft()

    def acquire(self, alias: str, estimated_tokens: int = 2000) -> bool:
        with self._lock:
            if alias not in self._configs:
                return True 

            config = self._configs[alias]
            window = self._windows[alias]
            self._cleanup(window)
            now = time.time()

            if config.rpm > 0 and len(window.minute_requests) >= config.rpm:
                return False

            if config.tpm > 0:
                current_tokens = sum(t for _, t in window.minute_tokens)
                if current_tokens + estimated_tokens > config.tpm:
                    return False

            if config.rpd > 0 and len(window.day_requests) >= config.rpd:
                return False

            window.minute_requests.append(now)
            window.minute_tokens.append((now, estimated_tokens))
            window.day_requests.append(now)
            return True