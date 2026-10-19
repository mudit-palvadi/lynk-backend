"""Keeps the shared API contract fixture honest.

`tests/fixtures/lynk_api_contract.json` is the single source of truth for the
wire format between this backend and the Flutter client. The client has its own
test that reads the same file (client/test/integration/api_contract_test.dart).

The two repos are versioned separately, so this test is what turns a silent
client break into a red test on this side: rename a field here and the
contract test fails before the client is ever shipped.

The assertions run against the generated OpenAPI schema rather than FastAPI's
internal route objects, so they describe the contract the world actually sees
and survive a FastAPI upgrade.
"""

import json
import re
from pathlib import Path

import pytest

from app.main import app

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "lynk_api_contract.json"
SERVICES_DIR = Path(__file__).parent.parent / "app" / "services"

# Loaded at import time so the endpoint count can drive parametrisation.
CONTRACT = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
ENDPOINT_COUNT = len(CONTRACT["endpoints"])


@pytest.fixture(scope="module")
def contract():
    return CONTRACT


@pytest.fixture(scope="module")
def schema():
    return app.openapi()


def resolve(schema: dict, root: dict) -> dict:
    """Follow $ref chains until a concrete schema is reached."""
    seen = 0
    while "$ref" in schema:
        # e.g. '#/components/schemas/TransferCreate' -> ['schemas', 'TransferCreate']
        parts = schema["$ref"].lstrip("#/").split("/")
        target = root
        for part in parts:
            target = target[part]
        schema = target
        seen += 1
        assert seen < 10, "cyclic $ref in the OpenAPI schema"
    return schema


def operation(schema: dict, method: str, path: str):
    """The OpenAPI operation object for a method/path, or None."""
    full_path = CONTRACT["base_path"] + path
    paths = schema["paths"]
    if full_path not in paths:
        return None
    return paths[full_path].get(method.lower())


def request_body_fields(op: dict, root: dict) -> set:
    body = op.get("requestBody")
    if not body:
        return set()
    content = body.get("content", {})
    for media in content.values():
        resolved = resolve(media.get("schema", {}), root)
        return set(resolved.get("properties", {}))
    return set()


def response_fields(op: dict, root: dict) -> set:
    for response in op.get("responses", {}).values():
        content = response.get("content")
        if not content:
            continue
        for media in content.values():
            resolved = resolve(media.get("schema", {}), root)
            properties = set(resolved.get("properties", {}))
            if properties:
                return properties
    return set()


def test_fixture_is_valid_json(contract):
    assert contract["version"] == 1
    assert contract["base_path"] == "/api/v1"
    assert contract["endpoints"], "fixture must declare at least one endpoint"


@pytest.mark.parametrize("endpoint_index", range(ENDPOINT_COUNT))
def test_endpoint_exists_in_the_openapi_schema(contract, schema, endpoint_index):
    endpoint = contract["endpoints"][endpoint_index]
    op = operation(schema, endpoint["method"], endpoint["path"])
    assert op is not None, (
        f"{endpoint['method']} {endpoint['path']} is declared in the contract "
        f"but is not served by the app"
    )


@pytest.mark.parametrize("endpoint_index", range(ENDPOINT_COUNT))
def test_response_fields_match_the_served_schema(contract, schema, endpoint_index):
    endpoint = contract["endpoints"][endpoint_index]
    expected = set(endpoint["response_fields"])
    if not expected:
        pytest.skip("endpoint returns no body")

    op = operation(schema, endpoint["method"], endpoint["path"])
    actual = response_fields(op, schema)
    assert actual == expected, (
        f"{endpoint['path']} response drifted from the contract. "
        f"served={sorted(actual)} contract={sorted(expected)}"
    )


@pytest.mark.parametrize("endpoint_index", range(ENDPOINT_COUNT))
def test_request_body_fields_match_the_served_schema(contract, schema, endpoint_index):
    endpoint = contract["endpoints"][endpoint_index]
    expected = set(endpoint["request_body"] or {})
    if not expected:
        pytest.skip("endpoint takes no request body")

    op = operation(schema, endpoint["method"], endpoint["path"])
    actual = request_body_fields(op, schema)
    assert actual == expected, (
        f"{endpoint['path']} request body drifted from the contract. "
        f"served={sorted(actual)} contract={sorted(expected)}"
    )


def test_status_values_emitted_by_services_are_declared_in_the_contract(contract):
    """A new status value the client cannot parse is a silent breakage.

    The client maps these strings onto enums and falls back to a default when
    it meets an unknown one, so nothing on either side would fail loudly.
    """
    declared = {value for values in contract["enums"].values() for value in values}

    pattern = re.compile(r'\bstatus\s*=\s*"(\w+)"')
    emitted = set()
    for path in sorted(SERVICES_DIR.glob("*.py")):
        emitted.update(pattern.findall(path.read_text(encoding="utf-8")))

    assert emitted, "no status assignments found - the scan is probably broken"
    unknown = emitted - declared
    assert not unknown, (
        f"services emit status values the contract does not declare: {sorted(unknown)}"
    )


def test_client_used_endpoints_all_have_a_client_path(contract):
    for endpoint in contract["endpoints"]:
        if endpoint["client_uses"]:
            assert endpoint["client_path"], (
                f"{endpoint['path']} is used by the client but has no client_path"
            )


def test_client_paths_are_relative_to_the_api_prefix(contract):
    """The client's ApiClient strips one leading slash and appends to the base
    url, so a client path must never start with '/'."""
    for endpoint in contract["endpoints"]:
        client_path = endpoint["client_path"]
        if client_path:
            assert not client_path.startswith("/"), (
                f"{client_path} must be relative to {contract['base_path']}"
            )
            assert contract["base_path"] not in client_path
