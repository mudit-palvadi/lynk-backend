import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    REDIS_URL: str = "redis://localhost:6379/0"

    R2_ACCOUNT_ID: str = "mock-account-id"
    R2_ACCESS_KEY_ID: str = "mock-access-key"
    R2_SECRET_ACCESS_KEY: str = "mock-secret-key"
    R2_BUCKET_NAME: str = "lynk-transfers"

    MAX_FILES_PER_TRANSFER: int = 10
    # Cloud/R2 tiers: free = 20 MB, rewarded-ad unlock = 50 MB.
    # Server enforces the absolute ceiling (cannot verify ad views).
    MAX_INDIVIDUAL_FILE_SIZE: int = 52428800
    MAX_TOTAL_TRANSFER_SIZE: int = 524288000
    MAX_TOTAL_R2_CAP_BYTES: int = 10200547328

    TRANSFER_LIFETIME_SECONDS: int = 600
    EXTENDED_TRANSFER_LIFETIME_SECONDS: int = 1800
    RECEIVER_SESSION_LIFETIME_SECONDS: int = 600
    UPLOAD_URL_LIFETIME_SECONDS: int = 900
    DOWNLOAD_URL_LIFETIME_SECONDS: int = 300

    # TURN relay (P2P fallback path). Empty = disabled; clients degrade to
    # STUN-only + R2 cloud fallback.
    TURN_URLS: str = ""  # comma-separated, e.g. "turn:turn.lynkshare.app:3478"
    TURN_SECRET: str = ""
    TURN_CREDENTIAL_TTL_SECONDS: int = 600

    ALLOWED_ORIGINS: list[str] = [
        "https://lynkshare.app",
        "http://localhost:8000",
        "http://localhost:3000",
    ]

    # Published Cloudflare edge ranges. The API sits behind Cloudflare, so the
    # ASGI peer is an edge address; a request from one of these ranges is
    # allowed to carry the real client address in `CF-Connecting-IP`.
    # Source: https://www.cloudflare.com/ips/ (synced manually).
    CLOUDFLARE_IP_RANGES: list[str] = [
        # IPv4
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        # IPv6
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    ]

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
