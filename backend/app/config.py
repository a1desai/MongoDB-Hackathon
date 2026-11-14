"""Settings from the environment, with the repo-root .env as a fallback."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    mongodb_uri: str = ""
    mongodb_db: str = "cooldown"

    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    llm_model_smart: str = "claude-sonnet-5"
    llm_model_fast: str = "claude-haiku-4-5"
    llm_concurrency: int = 10
    llm_mode: str = "live"  # live | cache_first | cache_only

    # auto = Atlas Automated Embedding (autoEmbed index, $vectorSearch with `query`).
    # client = Voyage API from Python, vectors stored in `embedding`, $vectorSearch with `queryVector`.
    embed_mode: str = "auto"
    voyage_api_key: str = ""
    voyage_api_url: str = "https://api.voyageai.com/v1/embeddings"
    voyage_model: str = "voyage-4"
    voyage_dim: int = 1024

    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "cooldown"

    sim_households: int = 2000
    sim_seed: int = 42
    tick_sim_minutes: int = 5
    tick_real_seconds: float = 1.5

    # The Executor calls the mock systems over HTTP at this base URL.
    api_base_url: str = "http://localhost:8000"

    data_dir: Path = ROOT / "data"
    scenarios_dir: Path = ROOT / "scenarios"


@lru_cache
def get_settings() -> Settings:
    return Settings()
