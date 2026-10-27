"""Request-scoped protections for the public API.

Currently one mechanism lives here: a per-client-IP rate limiter backed by
Redis. The limiter is deliberately explicit about the bucket it protects —
buckets are never derived from the request path, because most routes embed a
transfer id in the path and keying on it would hand every request its own
bucket (the limit would never fire).
"""

import hashlib
import ipaddress
import secrets

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import RateLimitExceededException, ServiceUnavailableException
from app.services.redis_service import redis_service

# Parsed once at import: the list is small and static, and the limiter runs on
# every protected request.
SENDER_SECRET_HEADER = "X-Lynk-Sender-Secret"
ACCESS_TOKEN_HEADER = "X-Lynk-Access-Token"


# Hashed when a credential is missing, so the comparison is always 32 bytes
# and an empty record cannot match a presented value.
_ABSENT_SECRET = b"lynk-absent-secret"


def secrets_match(presented: str | None, expected: str | None) -> bool:
    """Constant-time check. Empty or missing values never match."""
    left = hashlib.sha256((presented or "").encode()).digest()
    right = hashlib.sha256(expected.encode() if expected else _ABSENT_SECRET).digest()
    same = secrets.compare_digest(left, right)
    return bool(presented) and bool(expected) and same


_CLOUDFLARE_NETWORKS = [
    ipaddress.ip_network(cidr) for cidr in settings.CLOUDFLARE_IP_RANGES
]


def _is_cloudflare_address(ip: str) -> bool:
    """Whether [ip] belongs to a published Cloudflare range."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False

    # An IPv4 client on a dual-stack socket appears as ::ffff:a.b.c.d.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped

    for network in _CLOUDFLARE_NETWORKS:
        if address.version == network.version and address in network:
            return True
    return False


def resolve_client_address(peer: str | None, connecting_ip: str | None) -> str:
    """Return the address to attribute a connection to.

    The API is served through Cloudflare, so the ASGI peer is a Cloudflare
    edge address and every user would otherwise share a single bucket. The
    `CF-Connecting-IP` header carries the original client address, and
    Cloudflare overwrites it on every request, so it is only trusted when the
    connection actually arrived from a Cloudflare address. A connection that
    reaches the origin directly (an exposed port) has no such guarantee, so it
    is attributed to the peer instead.
    """
    if peer and _is_cloudflare_address(peer):
        forwarded = (connecting_ip or "").strip()
        if forwarded:
            return forwarded

    return peer or "unknown"


def resolve_client_ip(request: Request) -> str:
    """Return the address to rate-limit against (see resolve_client_address)."""
    peer = request.client.host if request.client else ""
    return resolve_client_address(peer, request.headers.get("CF-Connecting-IP"))


class RateLimiter:
    """Limits how many requests one client may make to a protected endpoint.

    The Redis key is `lynk:rate:{bucket}:{client_ip}`. The bucket is an
    explicit, static name so that all requests to the same logical endpoint
    share one counter, on every worker, regardless of the ids in the path.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: int = 60,
        bucket: str = "default",
        *,
        scope: str = "client",
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.bucket = bucket
        self.scope = scope

    async def __call__(self, request: Request) -> None:
        # A client scope is one address. A global scope is one counter for
        # every address, so many addresses cannot share out a guessable code.
        identity = "global" if self.scope == "global" else resolve_client_ip(request)
        key = f"lynk:rate:{self.bucket}:{identity}"

        try:
            client = await redis_service.get_client()
            count = await client.incr(key)
            # nx=True sets the expiry only when the key has none, so the window
            # starts at the first request and a failure between INCR and EXPIRE
            # cannot leave a bucket that never expires.
            await client.expire(key, self.window_seconds, nx=True)
        except ServiceUnavailableException:
            # The Redis pool is missing: surface it as the same 503 the rest
            # of the API returns for storage problems.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise ServiceUnavailableException(
                "Rate limiter backend unavailable"
            ) from exc

        if count > self.limit:
            raise RateLimitExceededException(
                f"Rate limit exceeded. Maximum {self.limit} requests per "
                f"{self.window_seconds}s."
            )
