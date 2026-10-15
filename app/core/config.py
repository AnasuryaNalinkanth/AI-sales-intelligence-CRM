from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


class Settings(BaseSettings):
    app_name: str = "AI Sales CRM"
    environment: str = "development"
    api_prefix: str = "/api"
    cors_allowed_origins: list[str] = [
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ]
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "ai_sales_crm"
    db_user: str | None = None
    db_password: SecretStr = SecretStr("")
    # When set, replaces the DB_* settings above. Secret because it embeds the password.
    database_url_override: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")
    # "serverless" opens a fresh connection per checkout (NullPool) for an external pooler
    # such as Supabase Supavisor; "pooled" keeps the in-process pool used locally.
    database_pool_mode: Literal["pooled", "serverless"] = "pooled"
    # Applied to PostgreSQL URLs that do not set sslmode themselves.
    database_ssl_mode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "require"
    ai_provider: Literal["gemini", "ollama"] = "gemini"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    gemini_max_retries: int = Field(default=2, ge=0, le=10)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:4b"
    # Local models on CPU can be slow; allow a longer ceiling than Gemini.
    ollama_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    # Ollama's runtime default (often 4096) silently truncates the agent prompt and tools.
    ollama_num_ctx: int = Field(default=16384, ge=2048, le=262144)
    # Keep the legacy key available only for redacting secrets from an untouched .env.
    openai_api_key: SecretStr | None = None
    # Overall deadline for interpreting one analytics question. Provider timeouts are per
    # request or per streamed read, so they do not bound the total on their own.
    analytics_ask_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    agent_recursion_limit: int = Field(default=12, ge=2, le=100)
    # Earlier chat messages sent to the model per request; each is also capped in length.
    agent_history_max_messages: int = Field(default=12, ge=0, le=50)
    agent_action_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    pending_action_max_capacity: int = Field(default=250, ge=1, le=10000)
    scheduling_meeting_default_duration_minutes: int = Field(default=60, ge=1, le=1440)
    scheduling_call_default_duration_minutes: int = Field(default=30, ge=1, le=1440)
    scheduling_followup_default_duration_minutes: int = Field(default=15, ge=1, le=1440)
    scheduling_business_start_hour: int = Field(default=9, ge=0, le=23)
    scheduling_business_end_hour: int = Field(default=17, ge=1, le=24)
    scheduling_increment_minutes: int = Field(default=15, ge=1, le=120)
    scheduling_suggestion_count: int = Field(default=5, ge=1, le=5)
    scheduling_horizon_days: int = Field(default=14, ge=1, le=90)
    ai_rate_limit_requests: int = Field(default=20, ge=1, le=1000)
    ai_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    jwt_secret_key: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(default=60, ge=1, le=1440)
    allow_public_registration: bool = True
    allow_public_registration_in_production: bool = False
    allow_localhost_cors_in_production: bool = False
    # Google Calendar sync is disabled until all four values are configured.
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_redirect_uri: str | None = None
    # A Fernet key (base64, 32 bytes) used to encrypt OAuth tokens at rest.
    google_token_encryption_key: SecretStr | None = None
    google_calendar_scope: str = "https://www.googleapis.com/auth/calendar.events.owned"
    google_api_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("ai_provider", mode="before")
    @classmethod
    def normalize_ai_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.scheduling_business_start_hour >= self.scheduling_business_end_hour:
            raise ValueError("Scheduling business start must be before business end.")
        if self.environment.strip().lower() == "production":
            if len(self.jwt_secret_key.get_secret_value().strip()) < 32:
                raise ValueError(
                    "JWT_SECRET_KEY must be configured with at least 32 characters in production."
                )
            if (
                self.allow_public_registration
                and not self.allow_public_registration_in_production
            ):
                raise ValueError(
                    "ALLOW_PUBLIC_REGISTRATION cannot be enabled in production unless "
                    "ALLOW_PUBLIC_REGISTRATION_IN_PRODUCTION=true is explicitly set."
                )
            if not self.cors_allowed_origins:
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must specify at least one explicit origin in production."
                )
            for origin in self.cors_allowed_origins:
                cleaned = origin.strip()
                if cleaned == "*":
                    raise ValueError(
                        "Wildcard CORS_ALLOWED_ORIGINS ('*') is not permitted in production."
                    )
                hostname = (urlparse(cleaned).hostname or "").lower()
                if hostname in _LOCAL_HOSTS and not self.allow_localhost_cors_in_production:
                    raise ValueError(
                        "CORS_ALLOWED_ORIGINS cannot use localhost/loopback origins in production "
                        "unless ALLOW_LOCALHOST_CORS_IN_PRODUCTION=true is explicitly set."
                    )
        return self

    @property
    def database_url(self) -> URL:
        raw = (
            self.database_url_override.get_secret_value().strip()
            if self.database_url_override is not None
            else ""
        )
        if raw:
            url = make_url(raw)
            # Providers hand out postgres:// or postgresql:// URLs; pick the installed driver.
            if url.drivername in {"postgres", "postgresql"}:
                url = url.set(drivername="postgresql+psycopg")
            return url
        password = self.db_password.get_secret_value()
        return URL.create(
            drivername="mysql+pymysql",
            username=self.db_user or None,
            password=password or None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


settings = Settings()
