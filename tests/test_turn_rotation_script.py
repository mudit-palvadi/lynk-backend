"""Contracts for ``deploy/rotate_turn_secret.sh`` (POTENTIAL_ISSUES #67).

The script rotates the TURN shared secret before it can verify anything, so a
broken probe is expensive: the operator is left with a rotated secret, a
script that reports failure, and a probe transfer holding capacity forever.

The script cannot be executed in the test suite - it rewrites
``/etc/turnserver.conf``, the real ``.env``, restarts coturn and recreates the
API container. These tests therefore read it and assert the properties that
make the probe work: the access token is sent on the credential check (#47),
the sender secret is used to cancel the probe, cleanup is registered on exit,
and neither value is ever printed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "deploy" / "rotate_turn_secret.sh"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _logical_lines(text: str) -> list[str]:
    """Join backslash-continued lines so one command is one string."""
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        lines.append(buffer + line)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return [line.strip() for line in lines if line.strip()]


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"missing rotation script at {SCRIPT}"


def test_extracts_the_access_token_and_sender_secret() -> None:
    text = _script_text()

    assert '"access_token"' in text
    assert '"sender_secret"' in text
    assert "ACCESS_TOKEN=$(printf" in text
    assert "SENDER_SECRET=$(printf" in text


def test_credentials_request_sends_the_access_token() -> None:
    """#47: the endpoint 404s without the token carried in the share link."""
    requests = [
        line
        for line in _logical_lines(_script_text())
        if "turn-credentials" in line and "rotation-check-unknown" not in line
    ]

    assert requests, "the script no longer probes turn-credentials"
    assert any(
        "X-Lynk-Access-Token: $ACCESS_TOKEN" in line and "$ACCESS_TOKEN" in line
        for line in requests
    ), "the credential probe must send the probe transfer's access token"


def test_cancel_request_sends_the_sender_secret() -> None:
    requests = [
        line
        for line in _logical_lines(_script_text())
        if "-X DELETE" in line and "/transfers/$TRANSFER_ID" in line
    ]

    assert requests, "the probe transfer is never cancelled"
    assert any("X-Lynk-Sender-Secret: $SENDER_SECRET" in line for line in requests), (
        "the cancel call must authenticate with the probe transfer's sender secret"
    )


def test_cancel_response_must_be_204() -> None:
    text = _script_text()

    assert '"204"' in text, "the cancel response is not checked"
    assert "if ! cancel_probe; then" in text, (
        "a failed cancel must fail the script instead of being ignored"
    )


def test_cleanup_is_registered_on_exit() -> None:
    text = _script_text()

    assert "cleanup()" in text
    assert "trap cleanup EXIT" in text, "the probe would leak on a failed verification"
    assert "trap - EXIT" in text, "cleanup must not run twice"
    assert 'exit "$status"' in text, (
        "cleanup must re-exit with the original status so it cannot mask a failure"
    )


def test_cancel_happens_after_the_credential_check() -> None:
    """The credential check needs the probe transfer alive; cancel comes last."""
    text = _script_text()

    check_at = text.index('[ "$CREDENTIAL" = "$EXPECTED" ]')
    cancel_at = text.index("if ! cancel_probe; then")
    assert check_at < cancel_at, (
        "the probe must not be cancelled until the credential check has read it"
    )


def test_neither_token_is_printed() -> None:
    for line in _logical_lines(_script_text()):
        if line.startswith(("echo ", "printf ")):
            assert "ACCESS_TOKEN" not in line, f"access token echoed: {line}"
            assert "SENDER_SECRET" not in line, f"sender secret echoed: {line}"


def test_rotation_order_and_up_d_are_kept() -> None:
    """#67 only changes the probe: rotate, recreate with up -d, then create."""
    text = _script_text()

    recreate = 'sudo docker compose -f "$COMPOSE_FILE" up -d'
    assert "systemctl restart coturn" in text
    assert recreate in text
    assert 'docker compose -f "$COMPOSE_FILE" restart' not in text
    assert text.index("systemctl restart coturn") < text.index(recreate)
    assert text.index(recreate) < text.index("Creating a transfer")


def _usable_bash() -> str | None:
    """The first bash that actually runs.

    On Windows ``bash`` often resolves to the WSL relay, which fails every
    invocation when no distribution is installed, so a path alone is not
    enough. Git Bash is tried as a fallback; Ubuntu CI uses the first match.
    """
    candidates = [
        shutil.which("bash"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for candidate in candidates:
        if candidate is None or not Path(candidate).is_file():
            continue
        probe = subprocess.run([candidate, "--version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return candidate
    return None


def test_script_parses_as_bash() -> None:
    bash = _usable_bash()
    if bash is None:
        pytest.skip("bash is not available here; CI runs this test on Ubuntu")

    result = subprocess.run(
        [bash, "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
