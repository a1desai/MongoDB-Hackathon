"""Shared test setup.

- Tests never call real LLM, Voyage or LangSmith APIs: the keys are blanked here, before
  app.config is imported, so every LLM call takes its deterministic fallback even when the
  repo-root .env holds real keys (process env beats .env in pydantic-settings).
- The database comes from MONGODB_URI / MONGODB_DB (process env first, then .env). For local
  work point them at the shared replica set, e.g.
  MONGODB_URI="mongodb://localhost:27019/?directConnection=true" MONGODB_DB=cooldown_w8 .venv/bin/pytest -q
- pytest-asyncio gives each test its own event loop, and an AsyncMongoClient is bound to the loop
  it was first used on, so the cached client is reset around every test.
"""

import os

for _key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "LANGSMITH_API_KEY"):
    os.environ[_key] = ""
os.environ["LANGSMITH_TRACING"] = "false"

import pytest  # noqa: E402

from app import db as _db  # noqa: E402
from app.config import get_settings  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "smoke: end-to-end runs; need a reachable MongoDB with the world loaded")


@pytest.fixture(autouse=True)
def _fresh_clients():
    """A new AsyncMongoClient per test (per event loop); settings re-read from the environment."""
    get_settings.cache_clear()
    _db.client.cache_clear()
    yield
    _db.client.cache_clear()
