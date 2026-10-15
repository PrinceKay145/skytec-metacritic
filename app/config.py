from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    # any OpenAI-compatible chat API; the default is a local Ollama
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = ""
    llm_model: str = "gemma3:4b"
    llm_timeout_seconds: float = 120.0


settings = Settings()
