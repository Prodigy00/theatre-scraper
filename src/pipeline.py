import asyncio
import logging
import time
from typing import Optional
import httpx

logger = logging.getLogger('scraper.pipeline')

class AsyncPipeline:
    def __init__(self, requests_per_second: float = 1.0, max_retries: int = 3, backoff_factor: float = 2.0):
        """Manages HTTP collection with built-in rate-limiting and exponential backoff retries"""
        self.rate_delay = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._last_request_time = 0.0 
        self._lock = asyncio.Lock()
        self.headers = {
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    
    async def _throttle(self) -> None:
        """Enforces a strict configurable request rate limit across concurrent workers."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self.rate_delay:
                await asyncio.sleep(self.rate_delay - elapsed)
            self._last_request_time = time.monotonic()
    
    async def fetch_html(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        """
        Fetches raw HTML from a target URL with explicit throttling, status validations,
        and transient network exception handling.
        """
        for attempt in range(1, self.max_retries + 1): 
            await self._throttle()

            try: 
                logger.debug(f"Fetching url: {url} (Attempt {attempt}/{self.max_retries})")
                response = await client.get(url, headers=self.headers, follow_redirects=True)
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                logger.warning(f"HTTP Error {status_code} encountered for URL: {url}")

                if status_code in (401, 403, 404):
                    logger.error(f"Unrecoverable HTTP Status {status_code}. Aborting retries.")
                    return None
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                logger.warning(f"Network/Timeout exception on attempt {attempt}:{str(exc)}")
            
            if attempt < self.max_retries:
                sleep_duration = self.backoff_factor ** attempt
                logger.info(f"Sleeping for {sleep_duration}s before retry...")
                await asyncio.sleep(sleep_duration)
                
        logger.error(f"Failed to fetch content from {url} after {self.max_retries} attempts.")
        return None 