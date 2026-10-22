"""The production image must not be a copy of the working tree.

POTENTIAL_ISSUES #38: the Dockerfile ran as root, copied the whole working
tree with ``COPY . .`` - which put the local ``.env`` into a layer where it
can still be read out of the image - and installed the test framework into
the runtime environment.

These are build-configuration contracts, so they are asserted against the
files themselves rather than against a running container: a container test
would need Docker in the test environment and would only prove one build.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerignore_lines() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.is_file()


def test_the_websocket_message_size_is_capped(dockerfile_text: str) -> None:
    """#31: the relay refuses a frame over 16 KB itself.

    The transport cap underneath it is the backstop: uvicorn buffers a
    message before the app sees it, so its default of 16 MiB would let one
    client make the process hold a huge frame.
    """
    assert '"--ws-max-size", "65536"' in dockerfile_text


def test_dockerignore_exists() -> None:
    assert DOCKERIGNORE.is_file()


def test_the_image_does_not_run_as_root(dockerfile_text: str) -> None:
    assert "\nUSER " in dockerfile_text, "the runtime stage must drop to USER"
    assert "USER root" not in dockerfile_text


def test_the_image_never_copies_the_whole_working_tree(
    dockerfile_text: str,
) -> None:
    # `COPY . .` puts the local .env, the tests and the deployment scripts
    # into a layer, where they can still be extracted from the image.
    assert "COPY . ." not in dockerfile_text
    assert "COPY . /" not in dockerfile_text


def test_only_the_application_package_is_copied(dockerfile_text: str) -> None:
    assert "COPY --chown=lynk:lynk app/ ./app/" in dockerfile_text


def test_the_image_has_no_test_framework(dockerfile_text: str) -> None:
    assert "pytest" not in dockerfile_text


def test_the_runtime_requirements_do_not_contain_pytest() -> None:
    runtime = REQUIREMENTS.read_text(encoding="utf-8").lower()
    assert "pytest" not in runtime


def test_the_dev_requirements_contain_pytest() -> None:
    dev = REQUIREMENTS_DEV.read_text(encoding="utf-8").lower()
    assert "pytest" in dev


def test_the_dockerignore_excludes_the_secrets(dockerignore_lines: list[str]) -> None:
    assert ".env" in dockerignore_lines


def test_the_dockerignore_excludes_the_tests(dockerignore_lines: list[str]) -> None:
    assert "tests/" in dockerignore_lines


def test_the_dockerignore_excludes_the_development_artifacts(
    dockerignore_lines: list[str],
) -> None:
    for pattern in (".venv/", "__pycache__/", ".git", ".pytest_cache/"):
        assert pattern in dockerignore_lines, f"{pattern} is not ignored"


def test_the_dockerignore_keeps_the_env_example(dockerignore_lines: list[str]) -> None:
    # `.env.*` is ignored but the template must stay in the context.
    assert "!.env.example" in dockerignore_lines
