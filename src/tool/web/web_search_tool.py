# src/tool/web/web_search_tool.py
"""
Web Search Tool for Regent Agent using Tavily API.

This tool provides robust, AI-optimized web search capabilities.
It returns clean snippets and content without the need for manual HTML scraping,
bypassing rate limits and CAPTCHAs of traditional search engines.
"""

import json
import logging
import requests
from typing import List, Dict, Any, Optional

from ..base_tool import BaseTool

_logger = logging.getLogger(__name__)

# Constants
_DEFAULT_MAX_RESULTS = 5
_MAX_ALLOWED_RESULTS = 10
_REQUEST_TIMEOUT = 15
_TAVILY_API_URL = "https://api.tavily.com/search"

class WebSearchTool(BaseTool):
    """
    Web Search Tool using Tavily API designed for LLM Agents.
    Requires TAVILY_API_KEY in the config file.
    """

    def __init__(self, workspace_dir=None, config=None):
        super().__init__(workspace_dir)
        self.config = config or {}
        self.api_key = self.config.get("TAVILY_API_KEY", "")
        self.default_max_results = _DEFAULT_MAX_RESULTS

    def get_name(self):
        return "web_search"

    def get_description(self):
        return (
            "Search the web for up-to-date information. "
            "Returns a structured list of results including titles, URLs, and clean textual snippets/content. "
            "Use this for factual verification, recent news, or finding documentation. "
            "Requires specific and clear queries."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string. Be specific."
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum number of results to return (1-{_MAX_ALLOWED_RESULTS}). Default is {_DEFAULT_MAX_RESULTS}.",
                    "minimum": 1,
                    "maximum": _MAX_ALLOWED_RESULTS
                },
                "search_depth": {
                    "type": "string",
                    "description": "The depth of the search. Options: 'basic' (faster) or 'advanced' (more comprehensive). Default is 'basic'.",
                    "enum": ["basic", "advanced"]
                }
            },
            "required": ["query"]
        }

    def execute(self, **kwargs):
        """
        Execute web search via Tavily.
        """
        if not self.api_key:
            return False, (
                "Error: TAVILY_API_KEY is missing. "
                "Please add TAVILY_API_KEY=tvly-... to the [Main] section of .env/api.cfg."
            )

        query = kwargs.get("query", "").strip()
        max_results = kwargs.get("max_results", self.default_max_results)
        search_depth = kwargs.get("search_depth", "basic")

        if not query:
            return False, "Error: No search query provided."

        if not isinstance(max_results, int) or max_results < 1:
            max_results = _DEFAULT_MAX_RESULTS
        if max_results > _MAX_ALLOWED_RESULTS:
            max_results = _MAX_ALLOWED_RESULTS
            
        if search_depth not in ["basic", "advanced"]:
            search_depth = "basic"

        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": False,
            "include_images": False,
            "include_raw_content": False
        }

        try:
            response = requests.post(
                _TAVILY_API_URL, 
                json=payload, 
                timeout=_REQUEST_TIMEOUT
            )
            
            if response.status_code == 401:
                return False, "Error: Invalid TAVILY_API_KEY."
            elif response.status_code == 429:
                return False, "Error: Rate limit exceeded or out of credits on Tavily."
                
            response.raise_for_status()
            data = response.json()
            
        except requests.RequestException as e:
            _logger.error(f"Search request failed: {e}")
            return False, f"API Request failed: {str(e)}"
        except json.JSONDecodeError:
            return False, "Failed to parse API response."

        results = data.get("results", [])
        if not results:
            return True, f"No results found for query: '{query}'"

        output = self._format_results(query, results)
        return True, output

    def _format_results(self, query: str, results: List[Dict[str, Any]]) -> str:
        lines = [
            f"Web Search Results for: '{query}'",
            f"Found {len(results)} result(s):",
            ""
        ]

        for i, result in enumerate(results, 1):
            lines.append(f"--- Result {i} ---")
            lines.append(f"Title: {result.get('title', 'No Title')}")
            lines.append(f"URL: {result.get('url', 'No URL')}")
            
            # Content is usually better than snippet if available in Tavily
            content = result.get('content') or result.get('snippet', '')
            # Explicitly mark content as untrusted to prevent prompt injection
            lines.append("Content (UNTRUSTED EXTERNAL DATA):")
            lines.append(f"<untrusted_content>\n{content}\n</untrusted_content>")
            lines.append("")

        return "\n".join(lines)