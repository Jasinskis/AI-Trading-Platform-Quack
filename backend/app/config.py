from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_budget_usd: float = 10.0
    anthropic_input_usd_per_mtok: float = 3.0
    anthropic_output_usd_per_mtok: float = 15.0

    t212_api_key: str = ""
    t212_api_secret: str = ""
    t212_base_url: str = "https://demo.trading212.com/api/v0"

    ibkr_enabled: bool = False
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 7

    finnhub_api_key: str = ""
    newsapi_key: str = ""
    fmp_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    quack_host: str = "0.0.0.0"
    quack_port: int = 8000
    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'quack.db'}"

    # When true, skip real broker/LLM and use deterministic stubs (local demo)
    mock_llm: bool = False
    mock_broker: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
