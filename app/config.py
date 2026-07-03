from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _abs_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((_PROJECT_ROOT / path).resolve())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    feishu_app_id: str = Field(..., alias="FEISHU_APP_ID")
    feishu_app_secret: str = Field(..., alias="FEISHU_APP_SECRET")
    feishu_app_token: str = Field(..., alias="FEISHU_APP_TOKEN")
    feishu_table_wenchuang: str = Field(..., alias="FEISHU_TABLE_WENCHUANG")
    # 关联子表 table_id；不填则从父表「关联文创营收」字段元数据自动解析
    feishu_table_wenchuang_items: str = Field("", alias="FEISHU_TABLE_WENCHUANG_ITEMS")
    feishu_table_dianshang: str = Field(..., alias="FEISHU_TABLE_DIANSHANG")

    jst_app_key: str = Field(..., alias="JST_APP_KEY")
    jst_app_secret: str = Field(..., alias="JST_APP_SECRET")
    jst_token_file: str = Field("data/jushuitan_token.json", alias="JST_TOKEN_FILE")
    push_state_file: str = Field("data/push_state.json", alias="PUSH_STATE_FILE")

    submit_api_key: str = Field(..., alias="SUBMIT_API_KEY")
    debounce_seconds: int = Field(30, alias="DEBOUNCE_SECONDS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")

    sync_status_success: str = Field("同步成功", alias="SYNC_STATUS_SUCCESS")
    sync_status_failed: str = Field("同步失败", alias="SYNC_STATUS_FAILED")
    sync_status_pending: str = Field("待同步", alias="SYNC_STATUS_PENDING")
    # True=飞书「同步时间」为日期字段(写毫秒)；False=文本字段(写 YYYY-MM-DD HH:mm:ss)
    sync_time_use_ms: bool = Field(False, alias="SYNC_TIME_USE_MS")
    # 飞书父表失败原因列名（表里没有「同步原因」时用「失败原因」）
    feishu_col_sync_reason: str = Field("失败原因", alias="FEISHU_COL_SYNC_REASON")

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        object.__setattr__(self, "log_dir", _abs_path(self.log_dir))
        object.__setattr__(self, "jst_token_file", _abs_path(self.jst_token_file))
        object.__setattr__(self, "push_state_file", _abs_path(self.push_state_file))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
