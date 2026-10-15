from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # hosts' env editors can keep spaces around "=", which would break the Bearer header
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", str_strip_whitespace=True)

    database_url: str = "sqlite:///./metacritic.db"

    user_agent: str = "skytec-metacritic-crawler/0.1 (hourly game catalog, test project)"
    request_timeout_seconds: float = 20.0
    request_delay_seconds: float = 1.0
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    max_backoff_seconds: float = 60.0

    games_per_run: int = 20
    max_pages_per_run: int = 10
    crawl_interval_minutes: int = 60
    scheduler_enabled: bool = True
    # when set, starting a crawl by hand needs this token (header, or cookie from /monitor/unlock)
    admin_token: str = ""
    llm_daily_call_limit: int = 400

    # any OpenAI-compatible chat API; the default is a local Ollama
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""
    llm_model: str = "gemma3:4b"
    embed_model: str = "nomic-embed-text"
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 1

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, url: str) -> str:
        # hosts hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url.removeprefix(prefix)
        return url


settings = Settings()
