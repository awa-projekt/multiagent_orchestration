"""Register model prices in Langfuse, so it prices model names it does not know.

Langfuse computes the cost of a generation from a model definition whose `matchPattern` matches
the generation's model name. Its built-in definitions cover public model names (the providers' own
model ids), not your own names such as Azure OpenAI deployment names
(`chat-prod-eu`). This script creates one custom model definition per entry of an awa price
file, with prices per token for the usage types awa sends (`input`, `output`,
`input_cached_tokens` / `input_cache_read`, `input_cache_creation`).

awa already sends the cost of every call when it knows the prices (`connect_langfuse(prices=...)`,
a blueprint's `prices:`); registered definitions also price runs traced without them, and keep
the cost visible when the price file is not passed.

Usage:
    python scripts/langfuse_prices.py prices.yaml              # create the definitions
    python scripts/langfuse_prices.py prices.yaml --dry-run    # print what would be sent
    python scripts/langfuse_prices.py prices.yaml --list       # show the custom definitions

The price file is an awa price table:

    chat-prod-eu: {input_per_mtok: 2.5, output_per_mtok: 10.0, cache_read_per_mtok: 1.25}

Reads `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` (or `LANGFUSE_HOST`)
from the environment or the project's `.env`; the keys are never printed. A model already defined
with the same name is replaced (deleted, then created), so running the script twice is safe.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from awa.contracts.usage import ModelPrice, PriceTable
from awa.core.config import env_value

MODELS_PATH = "/api/public/models"


def model_definition(name: str, price: ModelPrice) -> dict[str, Any]:
    """The Langfuse model definition of `name`: prices per token for awa's usage types."""

    per_token = 1_000_000
    read = price.input_per_mtok if price.cache_read_per_mtok is None else price.cache_read_per_mtok
    write = (
        price.input_per_mtok if price.cache_write_per_mtok is None else price.cache_write_per_mtok
    )
    return {
        "modelName": name,
        # Exact name, any case; awa reports Azure deployments by their deployment name.
        "matchPattern": f"(?i)^{re.escape(name)}$",
        "unit": "TOKENS",
        # Prices per usage type go in a pricing tier: Langfuse 4 accepts either flat
        # inputPrice/outputPrice or pricingTiers, and only tiers carry cache types.
        "pricingTiers": [
            {
                "name": "Standard",
                "isDefault": True,
                "priority": 0,
                "conditions": [],
                "prices": {
                    "input": price.input_per_mtok / per_token,
                    "output": price.output_per_mtok / per_token,
                    "input_cached_tokens": read / per_token,
                    "input_cache_read": read / per_token,
                    "input_cache_creation": write / per_token,
                },
            }
        ],
    }


class Langfuse:
    """The few calls of the Langfuse public API this script needs."""

    def __init__(self) -> None:
        public = env_value("LANGFUSE_PUBLIC_KEY") or ""
        secret = env_value("LANGFUSE_SECRET_KEY") or ""
        if not public or not secret:
            raise SystemExit("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        base = env_value("LANGFUSE_BASE_URL") or env_value("LANGFUSE_HOST")
        if not base:
            raise SystemExit("set LANGFUSE_BASE_URL (for example http://localhost:3000)")
        self.base = base.rstrip("/")
        token = base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(  # noqa: S310 - the base URL is the user's Langfuse
            self.base + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=self._headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            text = response.read().decode("utf-8")
        return json.loads(text) if text else None

    def custom_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.call("GET", f"{MODELS_PATH}?page={page}&limit=100")
            batch = data.get("data", [])
            models += [m for m in batch if not m.get("isLangfuseManaged")]
            if page >= int(data.get("meta", {}).get("totalPages", 1)):
                return models
            page += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("prices", type=Path, help="awa price file (YAML)")
    parser.add_argument("--dry-run", action="store_true", help="print, do not send")
    parser.add_argument("--list", action="store_true", help="list the custom model definitions")
    args = parser.parse_args(argv)
    table = PriceTable.from_yaml(args.prices)
    definitions = [model_definition(name, price) for name, price in table.prices.items()]
    if args.dry_run:
        print(json.dumps(definitions, indent=2))
        return 0
    langfuse = Langfuse()
    existing = {m["modelName"]: m for m in langfuse.custom_models()}
    if args.list:
        for model in existing.values():
            print(f"{model['modelName']}: {model.get('matchPattern')} {model.get('prices')}")
        return 0
    for definition in definitions:
        name = definition["modelName"]
        if name in existing:
            langfuse.call("DELETE", f"{MODELS_PATH}/{urllib.parse.quote(existing[name]['id'])}")
        langfuse.call("POST", MODELS_PATH, definition)
        print(f"registered {name} at {langfuse.base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
