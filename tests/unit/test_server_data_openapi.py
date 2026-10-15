"""The OpenAPI fragment of the data endpoints: it is the contract a frontend codes against."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FRAGMENT = ROOT / "docs" / "develop" / "data-api.openapi.json"

# Frozen with the HTTP API of the data-first build: a change here is a change of the contract.
OPERATIONS = {
    ("post", "/v1/uploads"),
    ("get", "/v1/uploads/{upload_id}"),
    ("delete", "/v1/uploads/{upload_id}"),
    ("post", "/v1/data/profile"),
    ("post", "/v1/builds"),
    ("get", "/v1/builds"),
    ("get", "/v1/builds/{build_id}"),
    ("delete", "/v1/builds/{build_id}"),
    ("get", "/v1/builds/{build_id}/events"),
    ("post", "/v1/builds/{build_id}/answers"),
    ("get", "/v1/builds/{build_id}/project.zip"),
    ("get", "/v1/builds/{build_id}/files/{path}"),
    ("post", "/v1/builds/{build_id}/runs"),
    ("get", "/v1/builds/{build_id}/threads/{thread_id}/interrupts"),
    ("post", "/v1/builds/{build_id}/threads/{thread_id}/resume"),
}


def generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_data_openapi", ROOT / "scripts" / "gen_data_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_checked_in_fragment_is_what_the_code_produces() -> None:
    current = json.dumps(generator().build(), indent=2, ensure_ascii=False) + "\n"

    assert FRAGMENT.read_text(encoding="utf-8") == current, (
        "docs/develop/data-api.openapi.json is out of date: run python scripts/gen_data_openapi.py"
    )


def test_the_fragment_holds_exactly_the_data_operations() -> None:
    document = json.loads(FRAGMENT.read_text(encoding="utf-8"))

    found = {(method, path) for path, item in document["paths"].items() for method in item}

    assert found == OPERATIONS


def test_the_shapes_a_frontend_relies_on_are_in_the_fragment() -> None:
    document = json.loads(FRAGMENT.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    paths = document["paths"]

    assert (
        paths["/v1/uploads"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"][
            "properties"
        ]
    ) == {"files": {"type": "array", "items": {"type": "string", "format": "binary"}}}
    assert set(schemas["BuildCreate"]["properties"]) == {
        "description",
        "upload_id",
        "notes",
        "options",
    }
    assert set(schemas["BuildOptionsIn"]["properties"]) == {
        "model",
        "rounds",
        "target",
        "max_requests",
        "mask",
        "guidance",
    }
    assert schemas["BuildState"]["properties"]["status"]["enum"] == [
        "queued",
        "profiling",
        "needs_input",
        "designing",
        "evaluating",
        "repairing",
        "writing",
        "completed",
        "failed",
        "cancelled",
    ]
    assert set(schemas["UploadedFileOut"]["properties"]) == {
        "id",
        "name",
        "size",
        "kind",
        "sniffed_type",
        "sha256",
        "status",
    }
    assert set(schemas["ProfileResponse"]["properties"]) == {"upload_id", "profile", "model_view"}
    assert (
        "text/event-stream"
        in paths["/v1/builds/{build_id}/events"]["get"]["responses"]["200"]["content"]
    )
    assert paths["/v1/builds"]["post"]["responses"]["202"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/BuildAccepted"}


def test_every_operation_answers_errors_in_the_servers_error_shape() -> None:
    document = json.loads(FRAGMENT.read_text(encoding="utf-8"))

    for path, item in document["paths"].items():
        for method, operation in item.items():
            errors = {
                status: response
                for status, response in operation["responses"].items()
                if status.startswith(("4", "5")) and status != "422"
            }
            assert "401" in errors, (method, path)
            for status, response in errors.items():
                reference = response["content"]["application/json"]["schema"]["$ref"]
                assert reference == "#/components/schemas/ErrorResponse", (method, path, status)
