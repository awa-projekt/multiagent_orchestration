"""Connectors (extension): configurable links to external systems, tested offline with mocks.

Entry points: `list_connectors`, `connector_tools(name, env=..., config=...)`, `check_connector`
(probes a live system), `discover()` (what is configured) and `openapi_connector`; MCP servers
are in `awa.mcp`. Writes wait for approval by default (switched off by
`requires_approval: false` or `ungated`); configured credential values are removed from results.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa._internal.http import HttpRequest
    from awa._internal.http_fake import MockTransport, mock_response
    from awa.connectors._catalog import (
        ConnectorCheck,
        check_connector,
        connector_tools,
        get_connector,
        list_connectors,
        register_connector,
    )
    from awa.connectors._discover import (
        ConnectorStatus,
        EnvironmentReport,
        ProviderStatus,
        discover,
    )
    from awa.connectors._engine import CONNECTOR_METADATA_KEY, Transport
    from awa.connectors._generic import auth_from_config, operation_from_config
    from awa.connectors._openapi import openapi_connector, openapi_operations
    from awa.connectors._spec import (
        ApiKeyAuth,
        BasicAuth,
        BearerAuth,
        ConnectorSpec,
        EnvVar,
        OAuth2ClientCredentials,
        OAuth2RefreshToken,
        Operation,
        Pagination,
        Param,
    )

_HOMES = homes(
    {
        "awa._internal.http_fake": "MockTransport mock_response",
        "awa.connectors._catalog": (
            "ConnectorCheck check_connector connector_tools get_connector list_connectors"
            " register_connector"
        ),
        "awa.connectors._discover": "ConnectorStatus EnvironmentReport ProviderStatus discover",
        "awa.connectors._engine": "CONNECTOR_METADATA_KEY Transport",
        "awa._internal.http": "HttpRequest",
        "awa.connectors._generic": "auth_from_config operation_from_config",
        "awa.connectors._openapi": "openapi_connector openapi_operations",
        "awa.connectors._spec": (
            "ApiKeyAuth BasicAuth BearerAuth ConnectorSpec EnvVar OAuth2ClientCredentials"
            " OAuth2RefreshToken Operation Pagination Param"
        ),
    }
)

__all__ = [
    "CONNECTOR_METADATA_KEY",
    "ApiKeyAuth",
    "BasicAuth",
    "BearerAuth",
    "ConnectorCheck",
    "ConnectorSpec",
    "ConnectorStatus",
    "EnvVar",
    "EnvironmentReport",
    "HttpRequest",
    "MockTransport",
    "OAuth2ClientCredentials",
    "OAuth2RefreshToken",
    "Operation",
    "Pagination",
    "Param",
    "ProviderStatus",
    "Transport",
    "auth_from_config",
    "check_connector",
    "connector_tools",
    "discover",
    "get_connector",
    "list_connectors",
    "mock_response",
    "openapi_connector",
    "openapi_operations",
    "operation_from_config",
    "register_connector",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
