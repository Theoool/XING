"""应用配置。全部通过环境变量注入，容器内由 docker-compose 提供。"""

from functools import lru_cache

from pydantic import field_validator
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

    # 后台 worker 专用连接。
    # 必须与 database_url 分开：worker 的领取查询要跨租户扫 tasks，
    # 而 xing_app 是 NOBYPASSRLS 且每条 policy 都要求 app.workspace_id 匹配，
    # 用应用角色连接会导致队列只能看到「当前上下文租户」的任务。
    # 见 scripts/worker_role.sql 与 docs/AGENT_DESIGN.md §8.0。
    worker_database_url: str = (
        "postgresql+asyncpg://xing_worker:xing_worker_dev_pwd@localhost:5432/xing"
    )

    sql_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    redis_url: str = "redis://localhost:6379/0"

    # 逗号分隔，例如 "http://localhost:3000,http://localhost:5173"
    cors_origins: str = "*"

    openai_api_key: str = ""
    openai_base_url: str = ""

    # stub = 确定性、不联网（默认）。openai = 真实调用，需要 key。
    # 默认 stub 的理由：没有 key 也要能把「预算 → 调用 → 记账 → 回放」跑通，
    # 否则整条 Agent 链路在本地无法验证。
    llm_provider: str = "stub"
    llm_model: str = "stub-1"

    # 思考型模型（qwen3 系等）的思维链计入 completion_tokens，既花钱又占
    # max_output_tokens —— 给 800 上限时正文会被思考挤到截断。
    # None = 不发送该字段（OpenAI 等不支持此参数，发了会 400）；
    # False = 通过 extra_body 显式关闭思考。这是供应商相关开关，不是通用语义。
    llm_enable_thinking: bool | None = None

    @field_validator("llm_enable_thinking", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """compose 用 ``${VAR:-}`` 注入，未设时是空串而不是「未提供」。

        空串必须映射成 None（不发送该字段），否则会被当成 False，
        语义上等价于「显式关闭思考」—— 对不支持该参数的服务商就是 400。
        """
        return None if value == "" else value

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
