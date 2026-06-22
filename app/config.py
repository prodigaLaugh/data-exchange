from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    feishu_table_dianshang: str = Field(..., alias="FEISHU_TABLE_DIANSHANG")

    jst_app_key: str = Field(..., alias="JST_APP_KEY")
    jst_app_secret: str = Field(..., alias="JST_APP_SECRET")
    jst_token_file: str = Field("data/jushuitan_token.json", alias="JST_TOKEN_FILE")

    submit_api_key: str = Field(..., alias="SUBMIT_API_KEY")
    debounce_seconds: int = Field(30, alias="DEBOUNCE_SECONDS")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")

    sync_status_success: str = "推送成功"
    sync_status_failed: str = "同步失败"


@lru_cache
def get_settings() -> Settings:
    return Settings()
