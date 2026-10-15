"""The generic connectors, configured from options alone: REST, OData, GraphQL, OpenAPI."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from awa.connectors import (
    MockTransport,
    auth_from_config,
    check_connector,
    connector_tools,
    mock_response,
    openapi_connector,
    openapi_operations,
    operation_from_config,
)
from awa.connectors._catalog import connector_context
from awa.connectors._generic import _kinds, describe_odata
from awa.errors import ConfigurationError
from awa.tools.approval import APPROVAL_METADATA_KEY


def content(result: str) -> Any:
    assert result.startswith("<tool_output"), result
    text = json.loads(result.split(">", 1)[1].rsplit("</tool_output>", 1)[0])["content"]
    try:
        return json.loads(text)
    except ValueError:
        return text


def by_name(tools: list[Any]) -> dict[str, Any]:
    return {tool.name: tool for tool in tools}


# ---------------------------------------------------------------------------- REST

BILLING = {
    "use": "rest",
    "auth": {"type": "api_key", "header": "X-Api-Key", "token_env": "BILLING_KEY"},
    "operations": [
        {
            "name": "list_invoices",
            "description": "List invoices.",
            "path": "/invoices",
            "params": [{"name": "status", "description": "Status.", "enum": ["open", "paid"]}],
            "items": "data",
            "pages": {"kind": "cursor", "next": "next", "param": "cursor"},
        },
        {
            "name": "void_invoice",
            "description": "Void an invoice.",
            "method": "POST",
            "path": "/invoices/{id}/void",
            "params": [{"name": "id", "description": "Invoice id.", "in": "path"}],
        },
    ],
    "health": "list_invoices",
}
BILLING_ENV = {"BILLING_URL": "https://billing.example.test/v1", "BILLING_KEY": "key-0123456789"}


def test_rest_operations_come_from_the_configuration() -> None:
    transport = MockTransport(
        {
            "GET /v1/invoices": [
                mock_response(json={"data": [{"id": 1}], "next": "n2"}),
                mock_response(json={"data": [{"id": 2}]}),
            ],
            "POST /v1/invoices/7/void": mock_response(json={"id": 7, "status": "void"}),
        }
    )
    tools = by_name(
        connector_tools("billing", env=BILLING_ENV, config=BILLING, transport=transport)
    )
    assert set(tools) == {"billing_list_invoices", "billing_void_invoice"}
    assert tools["billing_void_invoice"].metadata[APPROVAL_METADATA_KEY] is True
    listed = content(tools["billing_list_invoices"].invoke({"status": "open"}))
    assert listed["items"] == [{"id": 1}, {"id": 2}]
    assert transport.requests[0].header("X-Api-Key") == "key-0123456789"
    assert transport.requests[1].url.endswith("status=open&cursor=n2")
    ungated = by_name(
        connector_tools(
            "billing",
            env=BILLING_ENV,
            config={**BILLING, "requires_approval": False},
            transport=transport,
        )
    )
    assert content(ungated["billing_void_invoice"].invoke({"id": "7"}))["status"] == "void"
    assert check_connector("billing", env=BILLING_ENV, config=BILLING, transport=transport).ok


def test_rest_without_operations_offers_get_and_send() -> None:
    transport = MockTransport({"DELETE /api/customers/9": mock_response(204, text="")})
    env = {"CRM_URL": "https://crm.example.test/api", "CRM_TOKEN": "tok-0123456"}
    tools = by_name(
        connector_tools(
            "crm", env=env, config={"use": "rest", "requires_approval": False}, transport=transport
        )
    )
    assert set(tools) == {"crm_get", "crm_send"}
    result = tools["crm_send"].invoke({"path": "customers/9", "method": "DELETE"})
    assert content(result) == {"ok": True, "status": 204}
    request = transport.requests[0]
    assert request.method == "DELETE" and request.body is None
    assert request.header("Authorization") == "Bearer tok-0123456"
    assert "must not contain" in tools["crm_get"].invoke({"path": "../admin"})


def test_api_keys_in_a_query_parameter_never_reach_the_model() -> None:
    key = "qk-0123456789abcdef"
    config = {
        "use": "rest",
        "auth": {"type": "api_key", "query": "api_key", "token_env": "WEATHER_KEY"},
    }
    env = {"WEATHER_URL": "https://weather.example.test/v1", "WEATHER_KEY": key}
    transport = MockTransport(
        {
            "GET /v1/stations": mock_response(
                json={
                    "stations": ["a"],
                    "next": f"https://weather.example.test/v1/stations?page=2&api_key={key}",
                }
            )
        }
    )
    tools = by_name(connector_tools("weather", env=env, config=config, transport=transport))
    answer = tools["weather_get"].invoke(
        {"path": "stations", "query": {"api_key": "x", "q": "a b"}}
    )
    request = transport.requests[0]
    assert request.url == f"https://weather.example.test/v1/stations?q=a%20b&api_key={key}"
    assert request.header("X-API-Key") is None
    assert key not in answer
    token = content(answer)["next_page"]
    assert key.encode() not in base64.urlsafe_b64decode(token + "==")
    tools["weather_get"].invoke({"path": "stations", "page_token": token})
    assert (
        transport.requests[1].url
        == f"https://weather.example.test/v1/stations?page=2&api_key={key}"
    )
    with pytest.raises(ConfigurationError, match="header or query"):
        auth_from_config({"type": "api_key", "header": "X", "query": "k"}, "W")


def test_the_generic_get_follows_link_headers_and_next_urls() -> None:
    env = {"GH_URL": "https://api.example.test", "GH_TOKEN": "tok-0123456"}
    page_two = "https://api.example.test/repos/o/r/issues?per_page=2&page=2"
    transport = MockTransport(
        {
            "GET /repos/o/r/issues": [
                mock_response(
                    json=[{"n": 1}, {"n": 2}], headers={"Link": f'<{page_two}>; rel="next"'}
                ),
                mock_response(json=[{"n": 3}]),
            ],
            "GET /customers": mock_response(
                json={"data": [1], "links": {"next": "/customers?cursor=c2"}}
            ),
            "GET /plain": mock_response(json={"next": "a-cursor-not-a-url"}),
        }
    )
    tools = by_name(connector_tools("gh", env=env, config={"use": "rest"}, transport=transport))
    get = tools["gh_get"]
    first = content(get.invoke({"path": "repos/o/r/issues", "query": {"per_page": 2}}))
    assert first["data"] == [{"n": 1}, {"n": 2}]
    second = content(get.invoke({"path": "repos/o/r/issues", "page_token": first["next_page"]}))
    assert second == [{"n": 3}]  # the last page comes as it is
    assert transport.requests[1].url == page_two
    relative = content(get.invoke({"path": "customers"}))
    get.invoke({"path": "customers", "page_token": relative["next_page"]})
    assert transport.requests[-1].url == "https://api.example.test/customers?cursor=c2"
    assert content(get.invoke({"path": "plain"})) == {"next": "a-cursor-not-a-url"}
    assert "page_token" in get.description
    foreign = base64.urlsafe_b64encode(b'["url","https://evil.example.test/x",0]').decode()
    assert "not a next_page" in get.invoke({"path": "x", "page_token": foreign})  # unsigned
    signed = base64.urlsafe_b64decode(first["next_page"] + "==")
    edited = signed.replace(b"api.example.test", b"evil.example.test")
    assert edited != signed
    forged = base64.urlsafe_b64encode(edited).decode()
    sent = len(transport.requests)
    assert "not a next_page" in get.invoke({"path": "x", "page_token": forged})  # tampered
    assert len(transport.requests) == sent


def test_rest_variables_follow_the_instance_name() -> None:
    ctx = connector_context("orders_api", env={}, config={"use": "rest"})
    assert ctx.missing() == ("ORDERS_API_URL",)
    assert [v.name for v in ctx.spec.variables()] == [
        "ORDERS_API_URL",
        "ORDERS_API_TOKEN",
        "ORDERS_API_USERNAME",
        "ORDERS_API_PASSWORD",
    ]
    explicit = connector_context("orders_api", env={}, config={"use": "rest", "auth": "bearer"})
    assert explicit.missing() == ("ORDERS_API_URL", "ORDERS_API_TOKEN")


@pytest.mark.parametrize(
    ("kind", "wording"),
    [
        ("rest", "Base address of the REST API."),
        ("odata", "Base address of the OData service."),
        ("graphql", "Base address of the GraphQL API."),
    ],
)
def test_the_base_address_is_described_in_plain_words(kind: str, wording: str) -> None:
    ctx = connector_context("orders", env={}, config={"use": kind})
    [base] = [v for v in ctx.spec.variables() if v.name == "ORDERS_URL"]
    assert base.description == wording


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"name": "x"}, "needs a name and a description"),
        ({"name": "x", "description": "d", "method": "FETCH"}, "unknown method"),
        ({"name": "x", "description": "d", "path": "/a/{id}"}, "no path parameter for id"),
        ({"name": "x", "description": "d", "colour": 1}, "unknown key"),
        ({"name": "x", "description": "d", "pages": {"kind": "magic"}}, "pages.kind"),
    ],
)
def test_invalid_operation_configuration(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        operation_from_config(config)


def test_auth_configuration() -> None:
    [client] = auth_from_config(
        {
            "type": "oauth2_client_credentials",
            "token_url": "https://id.example.test/token",
            "scope": "api",
        },
        "ERP",
    )
    assert [v.name for v in client.variables()] == ["ERP_CLIENT_ID", "ERP_CLIENT_SECRET"]
    assert auth_from_config("none", "ERP") == ()
    assert len(auth_from_config(["basic", "bearer"], "ERP")) == 2
    with pytest.raises(ConfigurationError, match="needs a token_url"):
        auth_from_config({"type": "oauth2_refresh_token"}, "ERP")
    with pytest.raises(ConfigurationError, match="unknown auth type"):
        auth_from_config("magic", "ERP")


# ---------------------------------------------------------------------------- OData

ERP_ENV = {
    "ERP_URL": "https://erp.example.test/odata/sales",
    "ERP_USERNAME": "svc",
    "ERP_PASSWORD": "pw-0123456",
}
ERP = {"use": "odata", "auth": "basic"}
METADATA = """<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">
 <edmx:DataServices><Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="S">
  <EntityType Name="Order"><Key><PropertyRef Name="OrderID"/></Key>
   <Property Name="OrderID" Type="Edm.Int32"/><Property Name="Customer" Type="Edm.String"/>
   <NavigationProperty Name="Items" Type="Collection(S.Item)"/></EntityType>
  <EntityContainer Name="C"><EntitySet Name="Orders" EntityType="S.Order"/></EntityContainer>
 </Schema></edmx:DataServices></edmx:Edmx>"""


def test_odata_query_follows_next_links_in_v4_and_v2() -> None:
    transport = MockTransport(
        {
            "GET /odata/sales/Orders": mock_response(
                json={
                    "value": [{"OrderID": 1}],
                    "@odata.nextLink": "https://erp.example.test/odata/sales/Orders?$skiptoken=2",
                }
            ),
            "GET /odata/sales/Lines": mock_response(json={"d": {"results": [{"Id": 1}]}}),
        }
    )
    tools = by_name(connector_tools("erp", env=ERP_ENV, config=ERP, transport=transport))
    first = content(
        tools["erp_query"].invoke(
            {
                "entity_set": "Orders",
                "filter": "Customer eq 'ACME'",
                "select": "OrderID",
                "limit": 2,
            }
        )
    )
    assert first["count"] == 2
    assert transport.requests[0].url == (
        "https://erp.example.test/odata/sales/Orders?%24filter=Customer%20eq%20%27ACME%27"
        "&%24select=OrderID&%24top=2"
    )
    assert transport.requests[1].url.endswith("Orders?$skiptoken=2")
    assert content(tools["erp_query"].invoke({"entity_set": "Lines"}))["items"] == [{"Id": 1}]
    assert "should match pattern" in tools["erp_query"].invoke({"entity_set": "../x"})


def test_odata_get_describe_and_sap_csrf_writes() -> None:
    transport = MockTransport(
        {
            "GET /odata/sales/Orders(42)": mock_response(json={"d": {"OrderID": 42}}),
            "GET /odata/sales/$metadata": mock_response(
                text=METADATA, content_type="application/xml"
            ),
            "GET /odata/sales/": mock_response(
                headers={"X-CSRF-Token": "tok-csrf-1"}, json={"value": [{"name": "Orders"}]}
            ),
            "PATCH /odata/sales/Orders(42)": mock_response(204, text=""),
        }
    )
    config = {**ERP, "csrf": True, "requires_approval": False}
    tools = by_name(connector_tools("erp", env=ERP_ENV, config=config, transport=transport))
    assert content(tools["erp_get"].invoke({"entity_set": "Orders", "key": "42"})) == {
        "OrderID": 42
    }
    described = content(tools["erp_describe"].invoke({"entity_set": "Orders"}))
    assert described == [
        {
            "entity_set": "Orders",
            "entity_type": "Order",
            "key": ["OrderID"],
            "properties": [
                {"name": "OrderID", "type": "Edm.Int32"},
                {"name": "Customer", "type": "Edm.String"},
            ],
            "navigation": ["Items"],
        }
    ]
    assert content(tools["erp_list_entity_sets"].invoke({})) == ["Orders"]
    tools["erp_update"].invoke({"entity_set": "Orders", "key": "42", "values": {"Customer": "B"}})
    update = transport.requests[-1]
    assert update.header("X-CSRF-Token") == "tok-csrf-1" and update.json() == {"Customer": "B"}
    with pytest.raises(Exception, match="no entity set"):
        describe_odata(METADATA, "Nope")


# ---------------------------------------------------------------------------- GraphQL


def test_graphql_separates_queries_from_mutations() -> None:
    transport = MockTransport(
        {"POST /graphql": mock_response(json={"data": {"viewer": {"id": "u1"}}})}
    )
    env = {"SHOP_URL": "https://shop.example.test/graphql", "SHOP_TOKEN": "gql-0123456"}
    tools = by_name(
        connector_tools("shop", env=env, config={"use": "graphql"}, transport=transport)
    )
    assert content(tools["shop_query"].invoke({"query": "{ viewer { id } }"})) == {
        "viewer": {"id": "u1"}
    }
    assert transport.requests[0].json() == {"query": "{ viewer { id } }"}
    refused = tools["shop_query"].invoke({"query": "mutation { deleteAll { ok } }"})
    assert "only runs queries" in refused and len(transport.requests) == 1
    assert tools["shop_mutate"].metadata[APPROVAL_METADATA_KEY] is True
    assert _kinds("query A { a } mutation B { b }") == {"query", "mutation"}
    assert _kinds('{ a(text: "mutation { x }") }') == {"query"}
    assert _kinds("# note\rmutation { drop }") == {"mutation"}
    assert _kinds("# note\r\nmutation { drop }") == {"mutation"}


def ref(kind: str, name: str | None = None, of: Any = None) -> dict[str, Any]:
    return {"kind": kind, "name": name, "ofType": of}


ID = ref("NON_NULL", of=ref("SCALAR", "ID"))
ORDERS = ref("NON_NULL", of=ref("LIST", of=ref("NON_NULL", of=ref("OBJECT", "Order"))))
SCHEMA = {
    "data": {
        "__schema": {
            "queryType": {
                "name": "Query",
                "fields": [
                    {
                        "name": "order",
                        "args": [{"name": "id", "type": ID}],
                        "type": ref("OBJECT", "Order"),
                    },
                    {
                        "name": "orders",
                        "args": [
                            {"name": "first", "type": ref("SCALAR", "Int"), "defaultValue": "10"},
                            {"name": "status", "type": ref("LIST", of=ref("ENUM", "Status"))},
                        ],
                        "type": ORDERS,
                    },
                ],
            },
            "mutationType": None,
            "types": [
                {"name": "Query", "kind": "OBJECT"},
                {"name": "Order", "kind": "OBJECT"},
                {"name": "Status", "kind": "ENUM"},
                {"name": "__Type", "kind": "OBJECT"},
                {"name": "String", "kind": "SCALAR"},
            ],
        }
    }
}
ORDER_TYPE = {
    "data": {
        "__type": {
            "name": "Order",
            "kind": "OBJECT",
            "description": "A customer order.",
            "fields": [
                {"name": "id", "args": [], "type": ID, "description": "Order number."},
                {
                    "name": "lines",
                    "args": [{"name": "first", "type": ref("SCALAR", "Int")}],
                    "type": ref("LIST", of=ref("OBJECT", "Line")),
                },
            ],
            "inputFields": None,
            "enumValues": None,
            "possibleTypes": None,
        }
    }
}


def test_graphql_errors_and_schema_summary() -> None:
    transport = MockTransport(
        {
            "POST /graphql": [
                mock_response(json=SCHEMA),
                mock_response(json=ORDER_TYPE),
                mock_response(json={"data": {"__type": None}}),
                mock_response(json={"data": None, "errors": [{"message": "denied"}]}),
            ]
        }
    )
    env = {"SHOP_URL": "https://shop.example.test/graphql"}
    tools = by_name(
        connector_tools("shop", env=env, config={"use": "graphql"}, transport=transport)
    )
    schema = content(tools["shop_describe_schema"].invoke({}))
    assert schema["queries"] == [
        "order(id: ID!): Order",
        "orders(first: Int = 10, status: [Status]): [Order!]!",
    ]
    assert schema["mutations"] == [] and schema["types"] == ["Order", "Query", "Status"]
    assert content(tools["shop_describe_type"].invoke({"name": "Order"})) == {
        "name": "Order",
        "kind": "OBJECT",
        "description": "A customer order.",
        "fields": [
            {"field": "id: ID!", "description": "Order number."},
            {"field": "lines(first: Int): [Line]"},
        ],
    }
    assert transport.requests[1].json()["variables"] == {"name": "Order"}
    assert "has no type 'Nope'" in tools["shop_describe_type"].invoke({"name": "Nope"})
    assert "answered with errors: denied" in tools["shop_query"].invoke({"query": "{ a }"})
    assert "should match pattern" in tools["shop_describe_type"].invoke({"name": "a b"})


def test_graphql_without_introspection_answers_plainly() -> None:
    apollo = (
        "GraphQL introspection is not allowed by Apollo Server, but the query contained "
        "__schema or __type."
    )
    transport = MockTransport(
        {
            "POST /graphql": [
                mock_response(400, json={"errors": [{"message": apollo}]}),
                mock_response(200, json={"data": None, "errors": [{"message": apollo}]}),
                mock_response(401, json={"errors": [{"message": "no __schema for you"}]}),
            ]
        }
    )
    env = {"SHOP_URL": "https://shop.example.test/graphql", "SHOP_TOKEN": "gql-0123456"}
    tools = by_name(
        connector_tools("shop", env=env, config={"use": "graphql"}, transport=transport)
    )
    off = content(tools["shop_describe_schema"].invoke({}))
    assert off["introspection"] == "not allowed (or limited) by this server"
    assert "not allowed by Apollo Server" in off["server_said"]
    assert content(tools["shop_describe_type"].invoke({"name": "Order"}))["introspection"]
    assert "rejected the credentials (HTTP 401)" in tools["shop_describe_schema"].invoke({})
    transport.add("POST /graphql", mock_response(400, json={"errors": [{"message": apollo}]}))
    assert check_connector("shop", env=env, config={"use": "graphql"}, transport=transport).ok
    depth = {"message": "Query depth limit exceeded.", "extensions": {"code": "DEPTH"}}
    transport.add("POST /graphql", mock_response(413, json={"errors": [depth]}))
    assert "depth limit" in content(tools["shop_describe_schema"].invoke({}))["server_said"]


def test_graphql_introspection_stays_within_common_depth_limits() -> None:
    from awa.connectors._generic import _INTROSPECTION, _TYPE_QUERY

    def nesting(query: str) -> int:
        level = deepest = 0
        for char in query:
            level += {"{": 1, "}": -1}.get(char, 0)
            deepest = max(deepest, level)
        return deepest

    # countries.trevorblades.com (a public API) refuses 9 levels with HTTP 413
    assert nesting(_INTROSPECTION) <= 8 and nesting(_TYPE_QUERY) <= 8


# ---------------------------------------------------------------------------- OpenAPI

DOCUMENT: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Billing API", "version": "1"},
    "servers": [
        {
            "url": "https://billing.example.test/{version}",
            "variables": {"version": {"default": "v2"}},
        }
    ],
    "components": {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}},
        "schemas": {
            "Invoice": {
                "type": "object",
                "required": ["customer"],
                "properties": {
                    "customer": {"type": "string", "description": "Customer id."},
                    "lines": {"type": "array", "items": {"type": "object"}},
                    "id": {"type": "string", "readOnly": True},
                },
            }
        },
        "parameters": {
            "Id": {
                "name": "invoiceId",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        },
    },
    "paths": {
        "/invoices": {
            "get": {
                "operationId": "listInvoices",
                "summary": "List invoices.",
                "tags": ["read"],
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["open", "paid"]},
                    },
                    {
                        "name": "tag",
                        "in": "query",
                        "schema": {"type": "array", "items": {"type": "string"}},
                    },
                ],
            },
            "post": {
                "operationId": "createInvoice",
                "summary": "Create an invoice.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Invoice"}}
                    },
                },
            },
        },
        "/invoices/{invoiceId}": {
            "parameters": [{"$ref": "#/components/parameters/Id"}],
            "get": {"operationId": "getInvoice", "summary": "Read one invoice."},
            "delete": {"operationId": "deleteInvoice", "deprecated": True},
        },
        "/uploads": {
            "post": {
                "operationId": "upload",
                "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
            }
        },
    },
}


def test_openapi_documents_become_operations() -> None:
    operations = {op.name: op for op in openapi_operations(DOCUMENT)}
    assert set(operations) == {"list_invoices", "create_invoice", "get_invoice"}
    create = operations["create_invoice"]
    assert create.write and [(p.name, p.required, p.location) for p in create.params] == [
        ("customer", True, "body"),
        ("lines", False, "body"),
    ]
    get = operations["get_invoice"]
    assert get.path == "/invoices/{invoice_id}" and not get.write
    listing = operations["list_invoices"]
    assert [p.enum for p in listing.params][0] == ("open", "paid")
    assert listing.params[1].explode
    assert {op.name for op in openapi_operations(DOCUMENT, tags=["read"])} == {"list_invoices"}
    with pytest.raises(ConfigurationError, match="more than 1"):
        openapi_operations(DOCUMENT, max_operations=1)
    with pytest.raises(ConfigurationError, match="no operation"):
        openapi_operations(DOCUMENT, include=["nothing"])


def test_openapi_connector_calls_the_api_with_the_documented_auth() -> None:
    spec = openapi_connector(DOCUMENT, name="billing")
    assert spec.title == "Billing API"
    transport = MockTransport(
        {
            "GET /v2/invoices/inv%201": mock_response(json={"id": "inv 1"}),
            "GET /v2/invoices": mock_response(json=[]),
            "POST /v2/invoices": mock_response(201, json={"id": "new"}),
        }
    )
    env = {"BILLING_API_KEY": "api-key-0123456"}
    tools = by_name(
        connector_tools(spec, env=env, config={"requires_approval": False}, transport=transport)
    )
    assert content(tools["billing_get_invoice"].invoke({"invoice_id": "inv 1"})) == {"id": "inv 1"}
    tools["billing_list_invoices"].invoke({"status": "open", "tag": ["a", "b"]})
    tools["billing_create_invoice"].invoke({"customer": "c1"})
    get, listing, create = transport.requests
    assert get.header("X-Api-Key") == "api-key-0123456"
    assert listing.url == "https://billing.example.test/v2/invoices?status=open&tag=a&tag=b"
    assert create.json() == {"customer": "c1"}


def test_openapi_reports_what_it_leaves_out_and_sends_text_bodies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    document = {
        **DOCUMENT,
        "components": {
            **DOCUMENT["components"],
            "securitySchemes": {"key": {"type": "apiKey", "in": "query", "name": "apikey"}},
        },
        "paths": {
            **DOCUMENT["paths"],
            "/invoices/{invoiceId}/pdf": {
                "get": {
                    "operationId": "downloadInvoice",
                    "parameters": [{"$ref": "#/components/parameters/Id"}],
                    "responses": {"200": {"content": {"application/pdf": {}}}},
                }
            },
            "/imports": {
                "post": {
                    "operationId": "importLines",
                    "requestBody": {"required": True, "content": {"text/csv": {}}},
                    "responses": {"202": {"content": {"application/json": {}}}},
                }
            },
        },
    }
    with caplog.at_level("WARNING", logger="awa.connectors"):
        spec = openapi_connector(document, name="billing")
    assert [op.name for op in spec.operations] == [
        "list_invoices",
        "create_invoice",
        "get_invoice",
        "import_lines",
    ]
    assert spec.notes == (
        "left out delete_invoice (DELETE /invoices/{invoiceId}): deprecated",
        "left out upload (POST /uploads): multipart/form-data request bodies are not supported",
        "left out download_invoice (GET /invoices/{invoiceId}/pdf): binary answers "
        "(application/pdf) are not supported",
    )
    assert "left out: upload (POST /uploads)" in caplog.text
    assert spec.to_dict()["notes"] == list(spec.notes)
    with pytest.raises(ConfigurationError, match="cannot use upload"):
        openapi_operations(document, include=["upload"])
    transport = MockTransport(
        {
            "POST /v2/imports": mock_response(202, json={"queued": 2}),
            "GET /v2/invoices": mock_response(json=[]),
        }
    )
    env = {"BILLING_API_KEY": "key-0123456789"}
    config = {"requires_approval": False}
    tools = by_name(connector_tools(spec, env=env, config=config, transport=transport))
    tools["billing_import_lines"].invoke({"body": "sku;qty\nA-1;2\n"})
    sent = transport.requests[0]
    assert sent.header("Content-Type") == "text/csv" and sent.body == b"sku;qty\nA-1;2\n"
    assert sent.url == "https://billing.example.test/v2/imports?apikey=key-0123456789"
    check = check_connector(spec, env=env, config={"health": "list_invoices"}, transport=transport)
    assert "left out upload" in check.message


def test_openapi_from_a_local_file_in_blueprint_style_configuration(tmp_path: Path) -> None:
    path = tmp_path / "billing.yaml"
    path.write_text(yaml.safe_dump(DOCUMENT), encoding="utf-8")
    config = {
        "use": "openapi",
        "spec": str(path),
        "base_url_env": "BILLING_URL",
        "auth": {"type": "bearer", "token_env": "BILLING_TOKEN"},
        "read_only": True,
    }
    ctx = connector_context("billing", env={}, config=config)
    assert ctx.missing() == ("BILLING_URL", "BILLING_TOKEN")
    assert [op.name for op in ctx.operations()] == ["list_invoices", "get_invoice"]
    transport = MockTransport({"GET /invoices": mock_response(json=[])})
    env = {"BILLING_URL": "https://staging.example.test", "BILLING_TOKEN": "tok-0123456"}
    tools = by_name(connector_tools("billing", env=env, config=config, transport=transport))
    tools["billing_list_invoices"].invoke({})
    assert transport.requests[0].url == "https://staging.example.test/invoices"
    assert transport.requests[0].header("Authorization") == "Bearer tok-0123456"


def test_openapi_from_a_url_is_loaded_when_the_tools_are_built() -> None:
    swagger = {
        "swagger": "2.0",
        "info": {"title": "Legacy"},
        "host": "legacy.example.test",
        "basePath": "/api",
        "schemes": ["https"],
        "securityDefinitions": {"basic": {"type": "basic"}},
        "paths": {
            "/orders/{id}": {
                "get": {
                    "operationId": "getOrder",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "type": "integer"}
                    ],
                },
                "put": {
                    "operationId": "updateOrder",
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "type": "integer"},
                        {
                            "name": "order",
                            "in": "body",
                            "schema": {
                                "type": "object",
                                "properties": {"state": {"type": "string"}},
                            },
                        },
                    ],
                },
            }
        },
    }
    transport = MockTransport(
        {
            "GET https://docs.example.test/legacy.json": mock_response(json=swagger),
            "GET /api/orders/5": mock_response(json={"id": 5}),
        }
    )
    env = {
        "LEGACY_SPEC": "https://docs.example.test/legacy.json",
        "LEGACY_USERNAME": "u",
        "LEGACY_PASSWORD": "pw-0123456",
    }
    ctx = connector_context("legacy", env={}, config={"use": "openapi"})
    assert ctx.spec.dynamic and ctx.missing() == ("LEGACY_SPEC",)
    tools = by_name(
        connector_tools("legacy", env=env, config={"use": "openapi"}, transport=transport)
    )
    assert set(tools) == {"legacy_get_order", "legacy_update_order"}
    assert content(tools["legacy_get_order"].invoke({"id": 5})) == {"id": 5}
    assert transport.requests[-1].header("Authorization").startswith("Basic ")  # type: ignore[union-attr]


def test_openapi_refuses_documents_that_are_not_openapi(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("just: text", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not an OpenAPI"):
        openapi_connector(str(path), name="x")


def test_graphql_malformed_schema_answers_are_errors_and_control_characters_are_dropped() -> None:
    odd = {
        "data": {
            "__type": {
                "name": "Order",
                "kind": "OBJECT",
                "description": "Pay\x1b[31m now\x00\nplease",
                "fields": [{"name": "id\x07"}],
            }
        }
    }
    broken = {"data": {"__schema": {"queryType": {"fields": [{"args": 1}]}, "types": [{}]}}}
    transport = MockTransport(
        {"POST /graphql": [mock_response(json=odd), mock_response(json=broken)]}
    )
    env = {"SHOP_URL": "https://shop.example.test/graphql"}
    tools = by_name(
        connector_tools("shop", env=env, config={"use": "graphql"}, transport=transport)
    )
    found = content(tools["shop_describe_type"].invoke({"name": "Order"}))
    assert found["description"] == "Pay [31m now please" and found["fields"] == [{"field": "id"}]
    assert "unexpected shape" in tools["shop_describe_schema"].invoke({})


def test_openapi_connector_with_base_url_is_configured_without_the_url_variable() -> None:
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Bill"},
        "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {}}}}},
    }
    spec = openapi_connector(document, name="bill", base_url="https://api.example.com")
    assert not any(variable.required for variable in spec.env)
    transport = MockTransport({"GET /ping": mock_response(json={"ok": True})})
    check = check_connector(spec, env={}, transport=transport)
    assert check.ok, check.message


def test_a_relative_oauth_token_url_resolves_against_the_document_address() -> None:
    from awa.connectors._openapi import openapi_auth
    from awa.connectors._spec import OAuth2ClientCredentials

    document = {
        "components": {
            "securitySchemes": {
                "oauth": {
                    "type": "oauth2",
                    "flows": {"clientCredentials": {"tokenUrl": "/oauth/token", "scopes": {}}},
                }
            }
        }
    }

    [flow, *_] = openapi_auth(document, "ERP", base="https://erp.example.com/api/openapi.json")

    assert isinstance(flow, OAuth2ClientCredentials)
    assert flow.token_url == "https://erp.example.com/oauth/token"
    with pytest.raises(ConfigurationError, match="tokenUrl '/oauth/token' of the document is"):
        openapi_auth(document, "ERP")
