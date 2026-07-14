from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        extra="ignore",
    )

    # Meta Conversions API
    meta_pixel_id: str = ""
    meta_access_token: str = ""
    meta_graph_version: str = "v21.0"
    meta_test_event_code: str = ""
    meta_business_id: str = ""

    # App
    db_path: str = "/opt/freelance/demo/adtrack/adtrack.db"
    worker_interval_sec: int = 5
    max_delivery_attempts: int = 5
    capi_timeout_sec: float = 1.5

    @property
    def capi_url(self) -> str:
        return (
            f"https://graph.facebook.com/{self.meta_graph_version}"
            f"/{self.meta_pixel_id}/events"
        )

    @property
    def live_mode(self) -> bool:
        """True when we have real credentials; otherwise the mock endpoint is used."""
        return bool(self.meta_pixel_id and self.meta_access_token)


settings = Settings()
