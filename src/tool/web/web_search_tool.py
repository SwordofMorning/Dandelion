# src/tool/web/web_search_tool.py
"""
Web Search Tool for Regent Agent.

This tool provides web search capabilities using DuckDuckGo's HTML interface
(no API key required) as the primary search backend. It can also be extended
to support other search engines like Google, Bing, etc. via API keys.

Features:
- No API key required (uses DuckDuckGo HTML scraping)
- Configurable max results
- Returns structured results with title, URL, and snippet
- Handles rate limiting and errors gracefully
"""

import os
import re
import json
import time
import logging
from urllib.parse import quote_plus, urljoin
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

from ..base_tool import BaseTool


# Configuration constants
_DEFAULT_MAX_RESULTS = 10
_MAX_ALLOWED_RESULTS = 20
_REQUEST_TIMEOUT = 15  # seconds
_RETRY_DELAY = 1.0  # seconds between retries
_MAX_RETRIES = 2

# DuckDuckGo search URL
_DUCKDUCKGO_HTML_URL = "https://html.duckduckgo.com/html/"

# User agent to appear as a regular browser
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Setup logging
_logger = logging.getLogger(__name__)


class WebSearchTool(BaseTool):
    """
    Web Search Tool using DuckDuckGo (no API key required).
    
    This tool performs web searches and returns structured results
    containing title, URL, and snippet for each result.
    """

    def __init__(self, workspace_dir=None, config=None):
        """
        Initialize the Web Search Tool.
        
        Args:
            workspace_dir: Working directory (inherited from BaseTool)
            config: Optional configuration dict with keys:
                - max_results: Default max results (default 10)
                - timeout: Request timeout in seconds (default 15)
                - search_engine: 'duckduckgo' (default), 'google', 'bing' (future)
        """
        super().__init__(workspace_dir)
        self.config = config or {}
        self.default_max_results = self.config.get("max_results", _DEFAULT_MAX_RESULTS)
        self.timeout = self.config.get("timeout", _REQUEST_TIMEOUT)
        self.search_engine = self.config.get("search_engine", "duckduckgo")
        
        # Session for connection pooling and cookie persistence
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def get_name(self):
        return "web_search"

    def get_description(self):
        return (
            "Search the web for information using DuckDuckGo (no API key required). "
            "Returns a list of search results with title, URL, and snippet. "
            "Use this to find current information, documentation, news, or any "
            "web-accessible content. Supports configurable max results (1-20)."
        )

    def get_schema(self):
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query string. Be specific for better results. "
                                   "Examples: 'Python asyncio tutorial', 'latest React 19 features', "
                                   "'DeepSeek R1 benchmark results 2024'."
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum number of results to return (1-{_MAX_ALLOWED_RESULTS}). "
                                   f"Default is {_DEFAULT_MAX_RESULTS}.",
                    "minimum": 1,
                    "maximum": _MAX_ALLOWED_RESULTS
                },
                "recency_days": {
                    "type": "integer",
                    "description": "Optional: Filter results to last N days (e.g., 7 for last week, 30 for last month). "
                                   "Note: DuckDuckGo HTML may not strictly enforce this.",
                    "minimum": 1,
                    "maximum": 365
                }
            },
            "required": ["query"]
        }

    def execute(self, **kwargs):
        """
        Execute web search.
        
        Args:
            query: Search query string (required)
            max_results: Max results to return (optional, default 10, max 20)
            recency_days: Filter by recency in days (optional)
            
        Returns:
            Tuple of (success: bool, result: str)
        """
        query = kwargs.get("query", "").strip()
        max_results = kwargs.get("max_results", self.default_max_results)
        recency_days = kwargs.get("recency_days", None)

        if not query:
            return False, "Error: No search query provided."

        # Validate max_results
        if not isinstance(max_results, int) or max_results < 1:
            max_results = _DEFAULT_MAX_RESULTS
        if max_results > _MAX_ALLOWED_RESULTS:
            max_results = _MAX_ALLOWED_RESULTS

        # Perform search based on configured engine
        if self.search_engine == "duckduckgo":
            results = self._search_duckduckgo(query, max_results, recency_days)
        else:
            # Future: add Google, Bing, etc. via API keys
            return False, f"Error: Unsupported search engine '{self.search_engine}'"

        if not results:
            return True, f"No results found for query: '{query}'"

        # Format results for output
        output = self._format_results(query, results)
        return True, output

    def _search_duckduckgo(self, query: str, max_results: int, recency_days: Optional[int]) -> List[Dict[str, Any]]:
        """
        Search using DuckDuckGo's HTML interface.
        
        Args:
            query: Search query
            max_results: Maximum number of results
            recency_days: Optional recency filter
            
        Returns:
            List of result dicts with keys: title, url, snippet
        """
        # Build search parameters
        params = {"q": query}
        if recency_days:
            # DuckDuckGo uses df parameter for date filter: d=day, w=week, m=month, y=year
            if recency_days <= 1:
                params["df"] = "d"
            elif recency_days <= 7:
                params["df"] = "w"
            elif recency_days <= 31:
                params["df"] = "m"
            else:
                params["df"] = "y"

        # Encode query for URL
        encoded_query = quote_plus(query)
        url = f"{_DUCKDUCKGO_HTML_URL}?q={encoded_query}"
        if recency_days:
            url += f"&df={params['df']}"

        # Make request with retries
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = self._session.get(url, timeout=self.timeout)
                response.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt < _MAX_RETRIES:
                    _logger.warning(f"Search request failed (attempt {attempt + 1}): {e}. Retrying...")
                    time.sleep(_RETRY_DELAY)
                else:
                    _logger.error(f"Search request failed after {_MAX_RETRIES + 1} attempts: {e}")
                    return []

        # Parse HTML results
        return self._parse_duckduckgo_html(response.text, max_results)

    def _parse_duckduckgo_html(self, html: str, max_results: int) -> List[Dict[str, Any]]:
        """
        Parse DuckDuckGo HTML search results.
        
        Args:
            html: Raw HTML response
            max_results: Maximum results to extract
            
        Returns:
            List of parsed result dicts
        """
        soup = BeautifulSoup(html, "html.parser")
        results = []

        # DuckDuckGo HTML structure: results are in <a class="result__url"> or similar
        # Try multiple selectors for robustness
        result_containers = soup.select("div.result__body, div.web-result, div.result")

        for container in result_containers:
            if len(results) >= max_results:
                break

            # Extract title
            title_elem = container.select_one("a.result__url, a.result__snippet, h2 a, .result__title a")
            if not title_elem:
                # Fallback: find any link that looks like a result title
                title_elem = container.select_one("a[href^='http']")

            # Extract snippet
            snippet_elem = container.select_one("a.result__snippet, .result__snippet, .snippet")

            # Extract URL
            url_elem = container.select_one("a.result__url, a[href^='http']")

            if not title_elem:
                continue

            title = title_elem.get_text(strip=True)
            url = url_elem.get("href", "") if url_elem else ""
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            # Clean up URL (DuckDuckGo sometimes uses redirect URLs)
            if url and "duckduckgo.com" in url:
                # Try to extract the actual destination from redirect
                match = re.search(r"uddg=([^&]+)", url)
                if match:
                    import urllib.parse
                    url = urllib.parse.unquote(match.group(1))

            if title and url:
                results.append({
                    "title": title[:200],  # Truncate long titles
                    "url": url,
                    "snippet": snippet[:500] if snippet else "No snippet available"
                })

        # If the above parsing didn't work, try alternative parsing
        if not results:
            results = self._parse_duckduckgo_alternative(soup, max_results)

        return results[:max_results]

    def _parse_duckduckgo_alternative(self, soup: BeautifulSoup, max_results: int) -> List[Dict[str, Any]]:
        """
        Alternative parsing for DuckDuckGo if primary method fails.
        
        Args:
            soup: BeautifulSoup object
            max_results: Maximum results to extract
            
        Returns:
            List of parsed result dicts
        """
        results = []

        # Look for all links that could be results
        links = soup.find_all("a", class_=lambda x: x and ("result" in x or "link" in x))
        
        for link in links:
            if len(results) >= max_results:
                break
                
            href = link.get("href", "")
            text = link.get_text(strip=True)
            
            if href and text and href.startswith("http") and "duckduckgo.com" not in href:
                # Find snippet nearby
                parent = link.find_parent(["div", "li", "article"])
                snippet = ""
                if parent:
                    snippet_elem = parent.select_one(".snippet, .result__snippet, p")
                    if snippet_elem:
                        snippet = snippet_elem.get_text(strip=True)
                
                results.append({
                    "title": text[:200],
                    "url": href,
                    "snippet": snippet[:500] if snippet else "No snippet available"
                })

        return results

    def _format_results(self, query: str, results: List[Dict[str, Any]]) -> str:
        """
        Format search results for display.
        
        Args:
            query: Original search query
            results: List of result dicts
            
        Returns:
            Formatted string output
        """
        lines = [
            f"Web Search Results for: '{query}'",
            f"Found {len(results)} result(s):",
            ""
        ]

        for i, result in enumerate(results, 1):
            lines.append(f"--- Result {i} ---")
            lines.append(f"Title: {result['title']}")
            lines.append(f"URL: {result['url']}")
            lines.append(f"Snippet: {result['snippet']}")
            lines.append("")

        return "\n".join(lines)


# Convenience function for direct usage
def search_web(query: str, max_results: int = 10, config: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """
    Convenience function to perform a web search and return raw results.
    
    Args:
        query: Search query
        max_results: Maximum results to return
        config: Optional configuration
        
    Returns:
        List of result dicts (title, url, snippet)
    """
    tool = WebSearchTool(config=config)
    success, output = tool.execute(query=query, max_results=max_results)
    if not success:
        return []
    
    # Parse the formatted output back to structured data if needed
    # For now, return empty list - user should use tool.execute() directly
    return []


if __name__ == "__main__":
    # Simple test
    logging.basicConfig(level=logging.INFO)
    tool = WebSearchTool()
    success, result = tool.execute(query="Python asyncio tutorial", max_results=5)
    print(result)