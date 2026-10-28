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
    # Client tiers: Cloud/R2 free = 15 MB, rewarded-ad unlock = 30 MB.
    # P2P free = 20 MB, rewarded-ad unlock = 50 MB.
    # The server enforces the absolute ceiling (cannot verify ad views).
    MAX_INDIVIDUAL_FILE_SIZE: int = 52428800
    MAX_TOTAL_TRANSFER_SIZE: int = 524288000
    MAX_TOTAL_R2_CAP_BYTES: int = 10200547328

    TRANSFER_LIFETIME_SECONDS: int = 600
    EXTENDED_TRANSFER_LIFETIME_SECONDS: int = 1800
    # How long a transfer's declared size is reserved against the storage
    # capacity before anything has been uploaded (#34). A create that never
    # uploads must not hold capacity for the whole transfer lifetime.
    UNFILLED_RESERVATION_SECONDS: int = 120
    # How many unfinished creates one client address may hold at a time (#34).
    MAX_OPEN_RESERVATIONS_PER_CLIENT: int = 5
    RECEIVER_SESSION_LIFETIME_SECONDS: int = 600
    # Sender-owned pairing sessions: the sender shows a code, the receiver
    # types it, and the session lives long enough to upload first.
    SENDER_SESSION_LIFETIME_SECONDS: int = 600
    SENDER_CODE_LENGTH: int = 6
    UPLOAD_URL_LIFETIME_SECONDS: int = 900
    # File bytes go straight to R2, so no API request carries more than a few
    # kilobytes of JSON. A larger body is refused before it is parsed (#51).
    MAX_REQUEST_BODY_BYTES: int = 65536
    DOWNLOAD_URL_LIFETIME_SECONDS: int = 300

    # TURN relay (P2P fallback path). Empty = disabled; clients degrade to
    # STUN-only + R2 cloud fallback.
    TURN_URLS: str = ""  # comma-separated, e.g. "turn:turn.lynkshare.app:3478"
    TURN_SECRET: str = ""
    TURN_CREDENTIAL_TTL_SECONDS: int = 600

    # Signaling (WebRTC P2P) hardening.
    # - Validate that a room id belongs to a live transfer or receiver session
    #   before a peer is allowed into the room (POTENTIAL_ISSUES #28).
    # - Evict rooms that stayed idle for longer than the eviction window, so a
    #   stale room cannot linger for the lifetime of the process (#2).
    SIGNALING_VALIDATE_SESSION: bool = True
    # Require the transfer access token as the first signaling frame (#32).
    # Every client build in closed testing sends it, so the default is on: a
    # deploy that forgets the variable must not silently drop the check.
    # Leave it on; set it false only to serve a build that predates the frame.
    SIGNALING_REQUIRE_ACCESS: bool = True
    SIGNALING_IDLE_EVICTION_SECONDS: float = 60.0
    SIGNALING_SWEEPER_INTERVAL_SECONDS: float = 30.0

    ALLOWED_ORIGINS: list[str] = [
        "https://lynkshare.app",
        "http://localhost:8000",
        "http://localhost:3000",
    ]

    # Published Cloudflare edge ranges. The API sits behind Cloudflare, so the
    # ASGI peer is an edge address; a request from one of these ranges is
    # allowed to carry the real client address in `CF-Connecting-IP`.
    # Source: https://www.cloudflare.com/ips/ (synced manually).
    #
    # When this secret is set, that header is trusted only together with a
    # matching `X-Lynk-Edge-Secret`. Empty keeps the range check alone, so a
    # deploy that has not taught the Worker to inject the secret does not put
    # every user behind one edge into one rate-limit bucket.
    CLOUDFLARE_EDGE_SECRET: str = ""
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
