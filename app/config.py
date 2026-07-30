"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub
    github_webhook_secret: str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")
    trigger_label: str = Field(default="devin-ready", alias="TRIGGER_LABEL")
    allowed_repos: str = Field(default="cjada/superset", alias="ALLOWED_REPOS")

    # Devin
    devin_api_base: str = Field(default="https://api.devin.ai", alias="DEVIN_API_BASE")
    devin_api_key: str = Field(default="", alias="DEVIN_API_KEY")
    devin_org_id: str = Field(default="", alias="DEVIN_ORG_ID")
    devin_create_as_user_id: str | None = Field(default=None, alias="DEVIN_CREATE_AS_USER_ID")
    devin_max_acu_limit: int | None = Field(default=None, alias="DEVIN_MAX_ACU_LIMIT")
    devin_session_tags: str = Field(default="issue-remediator", alias="DEVIN_SESSION_TAGS")

    # Behaviour
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    poll_interval_seconds: int = Field(default=60, alias="POLL_INTERVAL_SECONDS")
    database_url: str = Field(default="sqlite:///./data/remediator.db", alias="DATABASE_URL")

    @property
    def allowed_repo_set(self) -> set[str]:
        return {r.strip().lower() for r in self.allowed_repos.split(",") if r.strip()}

    @property
    def session_tag_list(self) -> list[str]:
        return [t.strip() for t in self.devin_session_tags.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
