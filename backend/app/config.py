"""Runtime values shared by the Python API and worker.

The root .env configures Compose and the Python services. DATABASE_URL can
override the MySQL connection assembled from the DB and MYSQL settings.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class ConfigError(RuntimeError):
    """A required runtime setting is absent or incompatible."""


ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_ENV, extra="ignore", case_sensitive=False)

    server_port: int = 9090
    server_address: str = "127.0.0.1"
    cors_allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    database_url: str | None = None
    db_username: str = "dovideo"
    db_password: str = ""
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_database: str = "media_db"
    db_pool_max_size: int = 10

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_database: int = 0
    redis_password: str = ""

    minio_endpoint: str = "http://localhost:9000"
    minio_public_endpoint: str | None = None
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "media"
    minio_public_read: bool = False

    qdrant_enabled: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "video_chunks"

    rocketmq_endpoints: str = "127.0.0.1:8081"
    rocketmq_analysis_topic: str = "video-analysis-topic"
    rocketmq_analysis_dead_topic: str = "video-analysis-dead-topic"
    rocketmq_analysis_group: str = "video-analysis-consumer"

    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "deepseek-ai/DeepSeek-V3.2"
    llm_timeout_seconds: int = 300
    llm_input_price_per_million: float = 0
    llm_output_price_per_million: float = 0
    embedding_model: str = "BAAI/bge-m3"
    asr_model: str = "TeleAI/TeleSpeechASR"
    asr_url: str = "https://api.siliconflow.cn/v1/audio/transcriptions"
    agent_max_rounds: int = 2
    agent_max_duration_ms: int = 120_000
    agent_max_estimated_tokens: int = 50_000
    agent_max_estimated_cost: float = 0
    agent_evaluation_enabled: bool = False

    @property
    def mysql_url(self) -> str:
        if self.database_url:
            if not self.database_url.startswith("mysql+pymysql://"):
                raise ConfigError("DATABASE_URL must use mysql+pymysql://")
            return self.database_url
        if not self.db_password:
            raise ConfigError("DB_PASSWORD is required for MySQL")
        return URL.create(
            "mysql+pymysql",
            username=self.db_username,
            password=self.db_password,
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
        ).render_as_string(hide_password=False)

    @property
    def cors_origins(self) -> list[str]:
        return [part.strip() for part in self.cors_allowed_origins.split(",") if part.strip()]
