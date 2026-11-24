"""应用配置。全部通过环境变量注入，容器内由 docker-compose 提供。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "XING"
    app_env: str = "development"
    app_debug: bool = True
    app_secret_key: str = "dev-only-change-me"
    log_level: str = "INFO"

    # 容器内主机名为 db / redis；本机直跑时可用环境变量覆盖为 localhost
    #
    # 应用连接必须使用非超级用户、非表 owner、NOBYPASSRLS 的角色，
    # 否则 PostgreSQL 会整体绕过 RLS（见 scripts/app_role.sql）。
    database_url: str = "postgresql+asyncpg://xing_app:xing_app_dev_pwd@localhost:5432/xing"

    # 迁移专用连接：建表 / 改表需要表 owner 权限。
    # 留空则复用 database_url（此时该角色必须有 DDL 权限）。
    database_admin_url: str = ""

    sql_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    redis_url: str = "redis://localhost:6379/0"

    # 逗号分隔，例如 "http://localhost:3000,http://localhost:5173"
    cors_origins: str = "*"

    openai_api_key: str = ""
    openai_base_url: str = ""

    @property
    def is_dev(self) -> bool:
        return self.app_env in {"development", "dev", "local"}

    @property
    def migration_database_url(self) -> str:
        """Alembic 使用。业务运行时永远走 database_url。"""
        return self.database_admin_url or self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_origins.strip()
        if not raw or raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
