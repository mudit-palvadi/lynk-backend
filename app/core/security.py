"""Request-scoped protections for the public API.

Currently one mechanism lives here: a per-client-IP rate limiter backed by
Redis. The limiter is deliberately explicit about the bucket it protects —
buckets are never derived from the request path, because most routes embed a
transfer id in the path and keying on it would hand every request its own
bucket (the limit would never fire).
"""

import ipaddress

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import RateLimitExceededException, ServiceUnavailableException
from app.services.redis_service import redis_service

# Parsed once at import: the list is small and static, and the limiter runs on
# every protected request.
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


def resolve_client_ip(request: Request) -> str:
    """Return the address to rate-limit against.

    The API is served through Cloudflare, so the ASGI peer is a Cloudflare
    edge address and every user would otherwise share a single bucket. The
    `CF-Connecting-IP` header carries the original client address, and
    Cloudflare overwrites it on every request, so it is only trusted when the
    request actually arrived from a Cloudflare address. A request that reaches
    the origin directly (an exposed port) has no such guarantee, so it is
    limited by the peer address instead.
    """
    peer = request.client.host if request.client else ""

    if peer and _is_cloudflare_address(peer):
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        if forwarded:
            return forwarded

    return peer or "unknown"


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
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.bucket = bucket

    async def __call__(self, request: Request) -> None:
        client_ip = resolve_client_ip(request)
        key = f"lynk:rate:{self.bucket}:{client_ip}"

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
