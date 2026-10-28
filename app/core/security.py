"""Request-scoped protections for the public API.

Currently one mechanism lives here: a per-client-IP rate limiter backed by
Redis. The limiter is deliberately explicit about the bucket it protects —
buckets are never derived from the request path, because most routes embed a
transfer id in the path and keying on it would hand every request its own
bucket (the limit would never fire).
"""

import hashlib
import ipaddress
import math
import secrets

from fastapi import Request

from app.core.config import settings
from app.core.exceptions import RateLimitExceededException, ServiceUnavailableException
from app.services.redis_service import redis_service

# Redis executes the counter and its fixed-window expiry as one operation.
# Saturating at limit + 1 bounds the stored value even during sustained abuse.
_RATE_LIMIT_SCRIPT = """
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count < limit + 1 then
    count = redis.call('INCR', KEYS[1])
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 or ttl > window then
    redis.call('PEXPIRE', KEYS[1], window)
    ttl = window
end
return {count, ttl}
"""

# Parsed once at import: the list is small and static, and the limiter runs on
# every protected request.
SENDER_SECRET_HEADER = "X-Lynk-Sender-Secret"
ACCESS_TOKEN_HEADER = "X-Lynk-Access-Token"
RECEIVER_SECRET_HEADER = "X-Lynk-Receiver-Secret"
EDGE_SECRET_HEADER = "X-Lynk-Edge-Secret"


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


def _canonical_address(value: str, *, allow_scope: bool = False) -> str | None:
    """Normalize aliases without trusting caller-supplied IPv6 zone identifiers."""
    if "%" in value:
        if not allow_scope:
            return None
        value = value.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped if mapped is not None else address)


def resolve_client_address(
    peer: str | None,
    connecting_ip: str | None,
    edge_secret: str | None = None,
) -> str:
    """Return the address to attribute a connection to.

    The API is served through Cloudflare, so the ASGI peer is a Cloudflare
    edge address and every user would otherwise share a single bucket. The
    `CF-Connecting-IP` header carries the original client address, and
    Cloudflare overwrites it on every request, so it is only trusted when the
    connection actually arrived from a Cloudflare address and the value is an
    IP. When `CLOUDFLARE_EDGE_SECRET` is set, the request must also present
    that secret. A connection that reaches the origin directly (an exposed
    port) has no such guarantee, so it is attributed to the peer instead.
    """
    if peer and _is_cloudflare_address(peer):
        forwarded = _canonical_address((connecting_ip or "").strip())
        if forwarded is not None:
            configured = settings.CLOUDFLARE_EDGE_SECRET
            if not configured or secrets_match(edge_secret, configured):
                return forwarded

    if not peer:
        return "unknown"
    return _canonical_address(peer, allow_scope=True) or peer


def resolve_client_ip(request: Request) -> str:
    """Return the address to rate-limit against (see resolve_client_address)."""
    peer = request.client.host if request.client else ""
    return resolve_client_address(
        peer,
        request.headers.get("CF-Connecting-IP"),
        request.headers.get(EDGE_SECRET_HEADER),
    )


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
        if type(limit) is not int or limit < 1:
            raise ValueError("Rate limit must be a positive integer")
        if type(window_seconds) is not int or window_seconds < 1:
            raise ValueError("Rate limit window must be a positive integer")

    async def __call__(self, request: Request) -> None:
        # A client scope is one address. A global scope is one counter for
        # every address, so many addresses cannot share out a guessable code.
        identity = "global" if self.scope == "global" else resolve_client_ip(request)
        if self.scope == "ipv6-prefix":
            try:
                address = ipaddress.ip_address(identity)
            except ValueError:
                return
            if address.version != 6:
                return
            network = ipaddress.ip_network(
                (address, settings.RATE_LIMIT_IPV6_PREFIX_LENGTH), strict=False
            )
            identity = f"ipv6:{network.with_prefixlen}"
        key = f"lynk:rate:{self.bucket}:{identity}"

        try:
            client = await redis_service.get_client()
            window_ms = self.window_seconds * 1000
            result = await client.eval(
                _RATE_LIMIT_SCRIPT, 1, key, window_ms, self.limit
            )
            if (
                not isinstance(result, (list, tuple))
                or len(result) != 2
                or any(type(value) is not int for value in result)
            ):
                raise ValueError("Invalid rate limiter response")
            count, remaining_ms = result
            if not 1 <= count <= self.limit + 1 or not 0 <= remaining_ms <= window_ms:
                raise ValueError("Invalid rate limiter response")
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
                f"{self.window_seconds}s.",
                retry_after=max(1, math.ceil(remaining_ms / 1000)),
            )
