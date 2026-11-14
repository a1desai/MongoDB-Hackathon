"""Voyage AI embeddings over HTTP (used only when EMBED_MODE=client).

In the default EMBED_MODE=auto, Atlas Automated Embedding embeds documents at index time and
queries at query time, so nothing here is called.
"""

import asyncio
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

BATCH = 64
_query_cache: dict[str, list[float]] = {}


class EmbeddingUnavailable(RuntimeError):
    """No Voyage key is configured, or the API kept failing."""


def available() -> bool:
    return bool(get_settings().voyage_api_key)


async def embed(texts: list[str], input_type: str) -> list[list[float]]:
    """Embed texts with Voyage ("document" or "query"), in batches of 64. Raises EmbeddingUnavailable."""
    s = get_settings()
    if not s.voyage_api_key:
        raise EmbeddingUnavailable("VOYAGE_API_KEY is not set")
    if not texts:
        return []
    headers = {"Authorization": f"Bearer {s.voyage_api_key}", "Content-Type": "application/json"}
    out: list[list[float]] = []
    async with httpx.AsyncClient(timeout=30) as http:
        for i in range(0, len(texts), BATCH):
            body = {
                "input": texts[i : i + BATCH],
                "model": s.voyage_model,
                "input_type": input_type,
                "output_dimension": s.voyage_dim,
            }
            out.extend(await _post(http, s.voyage_api_url, headers, body))
    return out


async def embed_query(text: str) -> list[float]:
    """Embed one query string, memoized for the process."""
    if text not in _query_cache:
        _query_cache[text] = (await embed([text], "query"))[0]
    return _query_cache[text]


async def _post(http: httpx.AsyncClient, url: str, headers: dict, body: dict) -> list[list[float]]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = await http.post(url, headers=headers, json=body)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"voyage {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            return [d["embedding"] for d in data]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            last = e
            log.warning("voyage embed attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(0.5 * 2**attempt)
    raise EmbeddingUnavailable(f"voyage embed failed: {last}")
