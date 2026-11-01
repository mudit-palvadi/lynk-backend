"""The Compose stack must not ship an open or leaky Redis (#66).

These are build-configuration contracts, so they are asserted against the
files rather than against a running stack: a live test would need Docker in
CI and would only prove one host's configuration.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")
FROM_RE = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def service_block(compose_text: str, name: str) -> str:
    """Return the lines of one service, without the rest of the file.

    The services sit at two-space indentation; anything at that indentation
    or shallower starts the next block (or the top level).
    """
    lines = compose_text.splitlines()
    start = lines.index(f"  {name}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def test_redis_requires_a_password(compose_text: str) -> None:
    block = service_block(compose_text, "redis")
    assert "--requirepass" in block, "Redis must not start without a password"
    # `:?` makes compose refuse to start, rather than run unauthenticated,
    # when the operator forgot to set the variable.
    assert "${REDIS_PASSWORD:?" in block


def test_redis_runs_as_the_unprivileged_image_user(compose_text: str) -> None:
    """cap_drop: ALL removes SETUID/SETGID, so the entrypoint cannot drop root.

    The official image starts as root and switches to the `redis` user. With
    every capability dropped that switch fails and the container exits 1, so
    the service must start as `redis` directly.
    """
    block = service_block(compose_text, "redis")
    assert re.search(r"^\s*user:\s*redis\s*$", block, re.MULTILINE), (
        "redis must run as its unprivileged user when all capabilities are dropped"
    )


def test_redis_does_not_persist_snapshots(compose_text: str) -> None:
    """An RDB/AOF file would keep expired secrets and wrapped keys on disk."""
    block = service_block(compose_text, "redis")
    assert '--save ""' in block
    assert "--appendonly no" in block


def test_the_redis_data_volume_is_gone(compose_text: str) -> None:
    assert "redis_data" not in compose_text
    assert not any(
        line.split("#", 1)[0].strip() == "volumes:"
        for line in compose_text.splitlines()
    ), "a top-level volumes block would keep persisting Redis"


def test_the_web_service_authenticates_to_redis(compose_text: str) -> None:
    block = service_block(compose_text, "web")
    assert "redis://:${REDIS_PASSWORD}@redis:6379/0" in block


def test_both_services_drop_all_capabilities(compose_text: str) -> None:
    for name in ("redis", "web"):
        block = service_block(compose_text, name)
        assert "cap_drop:" in block, f"{name} keeps the default capabilities"
        assert re.search(r"cap_drop:\s*\n\s*-\s*ALL\b", block), (
            f"{name} must drop ALL capabilities"
        )


def test_both_services_set_no_new_privileges(compose_text: str) -> None:
    for name in ("redis", "web"):
        block = service_block(compose_text, name)
        assert "no-new-privileges:true" in block, f"{name} must set the flag"


def test_the_redis_image_is_pinned_by_digest(compose_text: str) -> None:
    block = service_block(compose_text, "redis")
    match = re.search(r"^\s*image:\s*(\S+)", block, re.MULTILINE)
    assert match, "the redis service has no image"
    image = match.group(1)
    assert image.startswith("redis:7-alpine@sha256:"), image
    assert DIGEST_RE.search(image), f"{image} is not pinned by a manifest digest"


def test_the_dockerfile_base_images_are_pinned_by_digest(
    dockerfile_text: str,
) -> None:
    references = FROM_RE.findall(dockerfile_text)
    assert references, "the Dockerfile has no FROM line"
    for reference in references:
        assert DIGEST_RE.search(reference), (
            f"{reference} is pinned by tag only; add its manifest-list digest"
        )


def test_the_dockerfile_still_runs_as_a_non_root_user(
    dockerfile_text: str,
) -> None:
    assert "\nUSER lynk\n" in dockerfile_text


def test_the_env_example_declares_the_redis_password(
    env_example_text: str,
) -> None:
    assert re.search(r"^REDIS_PASSWORD=\S+", env_example_text, re.MULTILINE), (
        "a fresh checkout needs a REDIS_PASSWORD placeholder"
    )


def test_the_single_worker_and_ws_cap_are_unchanged(compose_text: str) -> None:
    block = service_block(compose_text, "web")
    assert "WEB_CONCURRENCY=1" in block
    assert "--ws-max-size 65536" in block
