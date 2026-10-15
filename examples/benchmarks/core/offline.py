"""Offline doubles of the core benchmark: a fake network and scripted models.

`network()` answers the requests of the keyless endpoints (web search, pages, the holidays REST
API, the GraphQL API) from canned data, so the scenarios run without internet. `script_for`
returns the scripted model of a scenario: one `Script` plays every agent of a blueprint, found by
its instructions in the system prompt, and decides from the conversation what the agent does
next, like a careful model would. Scripts compute their answers from what the tools returned.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from awa.blueprints import AcceptanceDesign, Blueprint, BlueprintDesign
from awa.blueprints.json_schema import model_from_json_schema
from awa.connectors import mock_response
from awa.evals import JudgeVerdict
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

# ------------------------------------------------------------------------ fake network

_PAGES: dict[str, tuple[str, str]] = {
    # name: (url, text)
    "Debian": (
        "https://en.wikipedia.org/wiki/Debian",
        "Debian is a free operating system made by a volunteer project. The first version was "
        "released on 16 August 1993. It is the base of many distributions.",
    ),
    "Ubuntu": (
        "https://en.wikipedia.org/wiki/Ubuntu",
        "Ubuntu is a Linux distribution based on Debian and published by Canonical. Its first "
        "release was on 20 October 2004.",
    ),
    "Fedora": (
        "https://en.wikipedia.org/wiki/Fedora_Linux",
        "Fedora Linux is a distribution sponsored by Red Hat. The first release, Fedora Core 1, "
        "appeared on 6 November 2003.",
    ),
    "Python": (
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "Python is a high-level programming language. It first appeared on 20 February 1991.",
    ),
    "Java": (
        "https://en.wikipedia.org/wiki/Java_(programming_language)",
        "Java is a class-based, object-oriented programming language. It first appeared on "
        "23 May 1995.",
    ),
    "Go": (
        "https://en.wikipedia.org/wiki/Go_(programming_language)",
        "Go is a statically typed, compiled programming language designed at Google. It first "
        "appeared on 10 November 2009.",
    ),
}

_HOLIDAYS: dict[str, list[tuple[str, str, bool]]] = {
    "DE": [
        ("2024-10-03", "Tag der Deutschen Einheit", True),
        ("2024-10-31", "Reformationstag", False),
        ("2024-12-25", "Erster Weihnachtstag", True),
    ],
    "PL": [
        ("2024-11-01", "Wszystkich Swietych", True),
        ("2024-11-11", "Dzien Niepodleglosci", True),
    ],
    "FR": [("2024-11-01", "Toussaint", True), ("2024-11-11", "Armistice 1918", True)],
}

_COUNTRIES: dict[str, dict[str, Any]] = {
    code: {
        "code": code,
        "name": name,
        "capital": capital,
        "currency": currency,
        "continent": {
            "code": continent,
            "name": {"EU": "Europe", "AS": "Asia", "NA": "North America"}[continent],
        },
    }
    for code, name, capital, currency, continent in [
        ("PL", "Poland", "Warsaw", "PLN", "EU"),
        ("DE", "Germany", "Berlin", "EUR", "EU"),
        ("FR", "France", "Paris", "EUR", "EU"),
        ("JP", "Japan", "Tokyo", "JPY", "AS"),
        ("CH", "Switzerland", "Bern", "CHE,CHF,CHW", "EU"),
        ("CA", "Canada", "Ottawa", "CAD", "NA"),
        ("US", "United States", "Washington D.C.", "USD,USN,USS", "NA"),
    ]
}


def _search_page(query: str) -> str:
    hits = [
        f'<div class="result"><a class="result__a" href="{url}">{name} - Wikipedia</a>'
        f'<a class="result__snippet">{text[:80]}</a></div>'
        for name, (url, text) in _PAGES.items()
        if name.lower() in query.lower().split() or f"{name.lower()}," in query.lower()
    ]
    return "<html><body>" + "".join(hits) + "</body></html>"


def _answer(url: str, method: str, data: bytes | None) -> Any:
    parts = urllib.parse.urlsplit(url)
    if parts.hostname and parts.hostname.endswith("duckduckgo.com"):
        query = urllib.parse.parse_qs((data or b"").decode()).get("q", [""])[0]
        return mock_response(text=_search_page(query), content_type="text/html", url=url)
    for url_known, text in _PAGES.values():
        if url == url_known:
            body = f"<html><head><title>{url.rsplit('/', 1)[1]}</title></head><body><p>{text}</p></body></html>"
            return mock_response(text=body, content_type="text/html", url=url)
    holidays = re.fullmatch(r"/api/v3/PublicHolidays/(\d{4})/([A-Z]{2})", parts.path)
    if parts.hostname == "date.nager.at" and holidays:
        year, country = holidays.groups()
        rows = [
            {
                "date": day,
                "localName": name,
                "name": name,
                "countryCode": country,
                "global": flag,
                "counties": None if flag else ["DE-BY"],
                "types": ["Public"],
            }
            for day, name, flag in _HOLIDAYS.get(country, [])
            if day.startswith(year)
        ]
        return mock_response(json=rows, url=url)
    if parts.hostname == "countries.trevorblades.com" and method == "POST":
        query = json.loads((data or b"{}").decode()).get("query", "")
        codes = re.findall(r'"([A-Z]{2})"', query)
        return mock_response(
            json={"data": {"countries": [_COUNTRIES[c] for c in codes if c in _COUNTRIES]}}, url=url
        )
    return mock_response(404, text="not found", url=url)


@contextlib.contextmanager
def network() -> Iterator[None]:
    """Answer every web, REST and GraphQL request of the tools from canned data."""

    def fake_fetch(
        policy: Any,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        secret_headers: Any = (),
        data: bytes | None = None,
        follow_redirects: bool = True,
    ) -> Any:
        return _answer(url, method, data)

    # Every awa module that imported the hardened client's `fetch` (web tools, search, connectors).
    patched = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("awa.")
        and getattr(getattr(module, "fetch", None), "__module__", "").endswith("_internal.http")
    ]
    assert patched, "no awa module uses the HTTP client any more: update offline.network()"
    saved = [(module, module.fetch) for module in patched]
    for module in patched:
        module.fetch = fake_fetch
    try:
        yield
    finally:
        for module, original in saved:
            module.fetch = original
        # A module imported inside the block took the fake from the patched client: reset it too.
        real = sys.modules["awa._internal.http"].fetch
        for module in list(sys.modules.values()):
            if getattr(module, "fetch", None) is fake_fetch:
                module.fetch = real


# ------------------------------------------------------------------------ conversation helpers


def system_text(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def request(call: ModelCall) -> str:
    """The first user message: the task the agent works on."""

    return next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))


def conversation(call: ModelCall) -> str:
    return "\n".join(str(m.content) for m in call.messages if not isinstance(m, SystemMessage))


def results(call: ModelCall, tool: str) -> list[str]:
    return [str(m.content) for m in call.messages if isinstance(m, ToolMessage) and m.name == tool]


def called(call: ModelCall, tool: str) -> list[dict[str, Any]]:
    return [
        dict(item["args"])
        for m in call.messages
        if isinstance(m, AIMessage)
        for item in m.tool_calls
        if item["name"] == tool
    ]


def tool_data(text: str) -> Any:
    """The data a tool returned: the JSON inside the tool-output frame, unwrapped."""

    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        return text
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if isinstance(value, dict) and isinstance(value.get("content"), str):
        with contextlib.suppress(ValueError):
            return json.loads(value["content"])
        return value["content"]
    return value


def find(data: Any, key: str) -> Any:
    """The first value stored under `key` anywhere in nested `data`."""

    if isinstance(data, dict):
        if key in data:
            return data[key]
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            found = find(item, key)
            if found is not None:
                return found
    return None


def delegated_results(call: ModelCall) -> dict[str, str]:
    found: dict[str, str] = {}
    for content in results(call, "delegate_task"):
        match = re.search(r'<result agent="([\w-]+)"[^>]*>\s*(.*?)\s*</result>', content, re.S)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def german_number(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def iso_date(text: str) -> str:
    day, month, year = text.split(".")
    return f"{year}-{month}-{day}"


class Script:
    """Plays every agent of a blueprint; one method per agent name."""

    def __init__(self, blueprint: Blueprint) -> None:
        self.blueprint = blueprint
        self.markers = {s.name: s.instructions.strip().splitlines()[0] for s in blueprint.agents}
        assert len(set(self.markers.values())) == len(self.markers), "instructions must differ"

    def registry(self) -> ModelRegistry:
        registry = ModelRegistry()
        registry.register("default", self.model())
        return registry

    def model(self) -> FunctionChatModel:
        return FunctionChatModel(self)

    def agent_of(self, call: ModelCall) -> str:
        prompt = system_text(call)
        found = [name for name, marker in self.markers.items() if marker in prompt]
        assert found, f"cannot tell the agent from the prompt: {prompt[:200]!r}"
        return max(found, key=lambda name: len(self.markers[name]))

    def __call__(self, call: ModelCall) -> Any:
        if "JudgeVerdict" in call.tool_names:
            return structured_output(
                JudgeVerdict, {"score": 0.9, "reasoning": "scripted judge: met"}
            )
        return getattr(self, self.agent_of(call))(call)

    def finish(self, agent: str, data: dict[str, Any]) -> Any:
        schema = self.blueprint.agent(agent).output_schema
        assert schema is not None
        return structured_output(
            model_from_json_schema(schema, name=f"{agent}_output").__name__, data
        )


# ------------------------------------------------------------------------ 1 market brief

_YEAR = re.compile(r"[^.]*\b((?:19|20)\d\d)\b[^.]*\.")


class BriefScript(Script):
    @staticmethod
    def items(text: str) -> list[str]:
        return sorted(
            (n for n in _PAGES if re.search(rf"\b{n}\b", text)), key=lambda n: text.index(n)
        )

    def brief_lead(self, call: ModelCall) -> Any:
        found, names = delegated_results(call), self.items(request(call))
        if "researcher" not in found:
            return tool_call(
                "delegate_task", {"agent": "researcher", "task": f"Research: {', '.join(names)}."}
            )
        if "analyst" not in found:
            return tool_call(
                "delegate_task", {"agent": "analyst", "task": f"Check:\n{found['researcher']}"}
            )
        if "writer" not in found:
            task = f"Findings:\n{found['researcher']}\nCheck:\n{found['analyst']}"
            return tool_call("delegate_task", {"agent": "writer", "task": task})
        rows = [
            line.split(" | ") for line in found["researcher"].splitlines() if line.startswith("- ")
        ]
        items = [
            {
                "name": n[2:],
                "key_fact": f,
                "year": int(y),
                "evidence": q.strip('"'),
                "source_url": u,
            }
            for n, f, y, u, q in rows
        ]
        return self.finish(
            "brief_lead",
            {
                "topic": ", ".join(names),
                "executive_summary": found["writer"],
                "items": items,
                "sources": [item["source_url"] for item in items],
                "gaps": [],
            },
        )

    def researcher(self, call: ModelCall) -> Any:
        if "Confirm the year" in request(call):
            return "Confirmed: the year is stated on the page I read."
        names = self.items(request(call))
        if not results(call, "web_search"):
            return tool_call(
                "web_search", {"query": " ".join(f"{n}," for n in names) + " first release"}
            )
        urls = re.findall(
            r"https://en\.wikipedia\.org/\S+", str(tool_data(results(call, "web_search")[0]))
        )
        pages = results(call, "fetch_url")
        if not pages:
            return [tool_call("fetch_url", {"url": url}) for url in urls]
        bullets = []
        for text in pages:
            body = str(tool_data(text))
            url = re.search(r"URL: (\S+)", body)
            sentence = _YEAR.search(body.split("\n\n", 1)[-1])
            assert url and sentence, body
            name = next(n for n, (u, _) in _PAGES.items() if u == url.group(1))
            quote = " ".join(sentence.group(0).split())
            bullets.append(
                f'- {name} | first release | {sentence.group(1)} | {url.group(1)} | "{quote}"'
            )
        return "\n".join(bullets)

    def analyst(self, call: ModelCall) -> Any:
        rows = [line for line in request(call).splitlines() if line.startswith("- ")]
        if not results(call, "consult_peer"):
            last = rows[-1].split(" | ")[0][2:]
            return tool_call(
                "consult_peer", {"peer": "researcher", "question": f"Confirm the year for {last}."}
            )
        return "\n".join(
            f"{row.split(' | ')[0][2:]}: confirmed, the quote states the year." for row in rows
        )

    def writer(self, call: ModelCall) -> Any:
        rows = [line.split(" | ") for line in request(call).splitlines() if line.startswith("- ")]
        return " ".join(f"{n[2:]} first appeared in {y}." for n, _, y, _, _ in rows)


# ------------------------------------------------------------------------ 2 supplier orders


class OrdersScript(Script):
    @staticmethod
    def picked(ask: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """The open orders the request asks for: named orders, one supplier, or all."""

        open_rows = [r for r in rows if r["Status"] != "storniert"]
        ids = re.findall(r"B-\d{4}", ask)
        if ids:
            return [r for r in open_rows if r["Bestellnr"] in ids]
        if "Kowalski" in ask:
            return [r for r in open_rows if "Kowalski" in r["Lieferant"]]
        return open_rows

    @staticmethod
    def national(call: ModelCall) -> dict[str, list[str]]:
        """The nationwide holiday dates by country, from the holidays calls so far."""

        pairs = zip(
            called(call, "holidays_public_holidays"),
            results(call, "holidays_public_holidays"),
            strict=True,
        )
        return {
            args["country"]: [h["date"] for h in tool_data(text) if h["global"]]
            for args, text in pairs
        }

    def order_checker(self, call: ModelCall) -> Any:
        if not results(call, "read_csv"):
            return tool_call("read_csv", {"path": "orders.csv", "limit": 50})
        table = tool_data(results(call, "read_csv")[0])
        rows = [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
        picked = self.picked(request(call), rows)
        countries = sorted({(iso_date(r["Wunschliefertermin"])[:4], r["Land"]) for r in picked})
        asked = {(a["year"], a["country"]) for a in called(call, "holidays_public_holidays")}
        if asked != set(countries):
            return [
                tool_call("holidays_public_holidays", {"year": y, "country": c})
                for y, c in countries
            ]
        national = self.national(call)
        if not called(call, "check_delivery_date"):
            return [
                tool_call(
                    "check_delivery_date",
                    {
                        "delivery_date": iso_date(r["Wunschliefertermin"]),
                        "holidays": national[r["Land"]],
                    },
                )
                for r in picked
            ]
        checks = [tool_data(text) for text in results(call, "check_delivery_date")]
        flagged = [(r, c) for r, c in zip(picked, checks, strict=True) if not c["ok"]]
        asks = called(call, "request_reschedule")
        if len(asks) < len(flagged):
            return [
                tool_call(
                    "request_reschedule",
                    {
                        "order_no": r["Bestellnr"],
                        "new_date": c["next_business_day"],
                        "reason": f"{c['delivery_date']} is a {'weekend' if c['is_weekend'] else 'public holiday'}",
                    },
                )
                for r, c in flagged
            ]
        sent = {
            a["order_no"]
            for a, text in zip(asks, results(call, "request_reschedule"), strict=True)
            if "Reschedule request" in text
        }
        return self.finish(
            "order_checker",
            {
                "checked_orders": len(picked),
                "flagged_orders": sorted(r["Bestellnr"] for r, _ in flagged),
                "flagged_total_eur": round(
                    sum(german_number(r["Betrag_EUR"]) for r, _ in flagged), 2
                ),
                "rescheduled_orders": sorted(sent),
                "rejected": sorted(
                    r["Bestellnr"] for r, _ in flagged if r["Bestellnr"] not in sent
                ),
                "flags": [
                    {
                        "order_no": r["Bestellnr"],
                        "reason": "weekend" if c["is_weekend"] else "public holiday",
                        "proposed_date": c["next_business_day"],
                    }
                    for r, c in flagged
                ],
            },
        )


# ------------------------------------------------------------------------ 3 GraphQL lookup


class LookupScript(Script):
    def trade_analyst(self, call: ModelCall) -> Any:
        if not results(call, "json_query"):
            return tool_call("json_query", {"path": "suppliers.json", "query": "suppliers"})
        suppliers = find(tool_data(results(call, "json_query")[0]), "suppliers") or tool_data(
            results(call, "json_query")[0]
        )
        if not isinstance(suppliers, list):
            suppliers = find(suppliers, "result")
        codes = sorted({s["country"] for s in suppliers})
        if not results(call, "countries_query"):
            query = "{ countries(filter: {code: {in: [%s]}}) { code name capital currency continent { code name } } }"
            return tool_call(
                "countries_query", {"query": query % ", ".join(f'"{c}"' for c in codes)}
            )
        countries = {
            c["code"]: c for c in find(tool_data(results(call, "countries_query")[0]), "countries")
        }
        question = request(call).lower()
        if "capital" in question:
            country = countries[next(s["country"] for s in suppliers if s["id"] == "S-05")]
            return self.finish(
                "trade_analyst",
                {
                    "supplier_ids": ["S-05"],
                    "count": 1,
                    "capital": country["capital"],
                    "uses_euro": country["currency"] == "EUR",
                    "summary": f"The capital is {country['capital']}.",
                },
            )
        outside = "outside europe" in question
        chosen = [
            s
            for s in suppliers
            if (
                countries[s["country"]]["continent"]["code"] != "EU"
                if outside
                else countries[s["country"]]["currency"] == "EUR"
            )
        ]
        return self.finish(
            "trade_analyst",
            {
                "supplier_ids": sorted(s["id"] for s in chosen),
                "count": len(chosen),
                "total_annual_spend_eur": sum(s["annual_spend_eur"] for s in chosen),
                "summary": "Countries: "
                + ", ".join(sorted({countries[s["country"]]["name"] for s in chosen})),
            },
        )


# ------------------------------------------------------------------------ 4 document intake

_NUMBER = r"([\d.]+,\d\d)"
_TOTALS = re.compile(rf"Netto {_NUMBER} EUR \| USt \d+ % {_NUMBER} EUR \| Brutto {_NUMBER} EUR")


class IntakeScript(Script):
    VALIDATE = (
        "vendor_vat_id",
        "iban",
        "po_number",
        "net_amount",
        "tax_amount",
        "gross_amount",
        "invoice_date",
        "due_date",
    )
    BOOK = ("po_number", "net_amount", "tax_amount", "gross_amount", "due_date")

    def intake_clerk(self, call: ModelCall) -> Any:
        path = re.search(r"inbox/\S+?\.(pdf|docx|xlsx)", request(call))
        assert path is not None
        tool = {"pdf": "extract_pdf_text", "docx": "extract_docx_text", "xlsx": "read_excel"}[
            path.group(1)
        ]
        if not results(call, tool):
            if tool != "read_excel":
                return tool_call(tool, {"path": path.group()})
            head = {"path": path.group(), "sheet": "Kopf", "header_row": 0}
            return [
                tool_call(tool, head),
                tool_call(tool, {"path": path.group(), "sheet": "Leistungen"}),
            ]
        texts = results(call, tool)
        if "no text layer" in texts[0]:
            reason = "no text layer, manual entry needed"
            return self.outcome(call, path.group(), {}, None, [reason])
        fields = self.fields(tool, texts)
        if not results(call, "validate_invoice"):
            return tool_call("validate_invoice", {k: fields[k] for k in self.VALIDATE})
        verdict = tool_data(results(call, "validate_invoice")[0])
        reasons = [f"{v['rule']}: {v['detail']}" for v in verdict["violations"]]
        booked = results(call, "book_invoice")
        if verdict["valid"] and not booked:
            args = {k: fields[k] for k in self.BOOK}
            args |= {"vendor_id": verdict["vendor_id"], "invoice_number": fields["invoice_number"]}
            return tool_call("book_invoice", args)
        if booked and not booked[0].startswith("Booked invoice"):
            reasons.append(booked[0])  # the approval was rejected: the reviewer's note
        return self.outcome(call, path.group(), fields, verdict, reasons)

    def outcome(
        self,
        call: ModelCall,
        document: str,
        fields: dict[str, Any],
        verdict: dict[str, Any] | None,
        reasons: list[str],
    ) -> Any:
        """Hold the document (once), or answer when it is booked or held."""

        booked = results(call, "book_invoice")
        done = bool(booked) and booked[0].startswith("Booked invoice")
        if not done and not results(call, "hold_invoice"):
            return tool_call(
                "hold_invoice",
                {
                    "document": document,
                    "invoice_number": fields.get("invoice_number"),
                    "reasons": reasons or ["not booked"],
                },
            )
        return self.finish(
            "intake_clerk",
            {
                "document": document,
                "invoice_number": fields.get("invoice_number"),
                "vendor_id": verdict["vendor_id"] if verdict else None,
                "gross_amount": fields.get("gross_amount"),
                "decision": "booked" if done else "on_hold",
                "reasons": [] if done else reasons,
            },
        )

    @staticmethod
    def fields(tool: str, texts: list[str]) -> dict[str, Any]:
        if tool == "read_excel":
            head = {row[0]: row[1] for row in tool_data(texts[0])["rows"]}
            rows = {
                row[0]: row[4]
                for row in tool_data(texts[1])["rows"]
                if row[0] in ("Netto", "Brutto") or str(row[0]).startswith("USt")
            }
            return {
                "invoice_number": head["Rechnungsnummer"],
                "vendor_vat_id": head["USt-IdNr"],
                "iban": head["IBAN"],
                "po_number": head["Bestellung"],
                "invoice_date": head["Rechnungsdatum"],
                "due_date": head["Fällig am"],
                "net_amount": float(rows["Netto"]),
                "tax_amount": float(next(v for k, v in rows.items() if k.startswith("USt"))),
                "gross_amount": float(rows["Brutto"]),
            }
        text = str(tool_data(texts[0]))
        totals = _TOTALS.search(text)
        dates = re.search(r"Rechnungsdatum: (\S+)\s+Fällig am: (\S+)", text)
        assert totals and dates, text
        net, tax, gross = (german_number(g) for g in totals.groups())
        number = re.search(r"(?:RECHNUNG|Rechnung) ([\w-]+)", text)
        return {
            "invoice_number": number.group(1) if number else "",
            "vendor_vat_id": re.search(r"USt-IdNr: (\w+)", text).group(1),  # type: ignore[union-attr]
            "iban": re.search(r"IBAN (\w+)", text).group(1),
            "po_number": re.search(r"PO-\d+", text).group(),  # type: ignore[union-attr]
            "invoice_date": iso_date(dates.group(1)),
            "due_date": iso_date(dates.group(2)),
            "net_amount": net,
            "tax_amount": tax,
            "gross_amount": gross,
        }


# ------------------------------------------------------------------------ 5 agent network

FINAL_TURN = "Write the final answer for the user."
TASK_LINE = re.compile(r"Task (task_\d+): (.+)")


class NetworkScript(Script):
    def risk_lead(self, call: ModelCall) -> Any:
        if not results(call, "create_tasks"):
            goal = request(call).strip()
            tasks = [
                {
                    "key": "spend",
                    "title": "Spend analysis",
                    "description": goal,
                    "required_capabilities": ["data-analysis"],
                },
                {
                    "key": "delivery",
                    "title": "Delivery analysis",
                    "description": goal,
                    "required_capabilities": ["logistics-analysis"],
                },
                {
                    "key": "memo",
                    "title": "Risk memo",
                    "description": goal,
                    "required_capabilities": ["writing"],
                    "depends_on": ["spend", "delivery"],
                },
            ]
            return tool_call("create_tasks", {"tasks": tasks})
        last = call.last_message
        if isinstance(last, ToolMessage) and last.name == "create_tasks":
            return "Planned; the teammates take it from here."
        if FINAL_TURN not in conversation(call):
            return "Noted."
        memo = re.search(
            r'<handback task_id="task_\d+" agent="risk_writer"[^>]*>\s*(.*?)\s*</handback>',
            conversation(call),
            re.S,
        )
        assert memo is not None, conversation(call)[-500:]
        return memo.group(1)

    def spend_analyst(self, call: ModelCall) -> Any:
        if not results(call, "query_csv"):
            return tool_call(
                "query_csv",
                {
                    "path": "spend.csv",
                    "group_by": ["supplier"],
                    "aggregates": ["sum(amount_eur)"],
                    "order_by": ["-sum(amount_eur)"],
                },
            )
        table = tool_data(results(call, "query_csv")[0])
        ranking = [(row[0], float(row[1])) for row in table["rows"]]
        if not results(call, "write_shared"):
            return tool_call(
                "write_shared",
                {"key": "spend/top_supplier", "value": f"{ranking[0][0]} {ranking[0][1]:.2f}"},
            )
        return "Spend ranking: " + "; ".join(f"{n} {v:.2f} EUR" for n, v in ranking)

    def logistics_analyst(self, call: ModelCall) -> Any:
        if not results(call, "json_query"):
            return tool_call("json_query", {"path": "deliveries.json", "query": "deliveries"})
        rows = find(tool_data(results(call, "json_query")[0]), "deliveries") or tool_data(
            results(call, "json_query")[0]
        )
        by: dict[str, list[int]] = {}
        for row in rows:
            by.setdefault(row["supplier"], []).append(row["days_late"])
        average = sorted(((sum(v) / len(v), n) for n, v in by.items()), reverse=True)
        if not results(call, "write_shared"):
            return tool_call(
                "write_shared",
                {
                    "key": "delivery/slowest_supplier",
                    "value": f"{average[0][1]} {average[0][0]:.2f}",
                },
            )
        return "Average days late: " + "; ".join(f"{n} {a:.2f}" for a, n in average)

    def risk_writer(self, call: ModelCall) -> Any:
        if not results(call, "read_shared"):
            return [
                tool_call("read_shared", {"key": "spend/top_supplier"}),
                tool_call("read_shared", {"key": "delivery/slowest_supplier"}),
            ]
        (spend_name, spend_total), (slow_name, slow_avg) = (
            re.fullmatch(r"(.+) (\d+\.\d\d)", str(tool_data(text)["value"])).groups()  # type: ignore[union-attr]
            for text in results(call, "read_shared")
        )
        return (
            f"Highest spend: {spend_name} with {spend_total} EUR. Slowest delivery: {slow_name} with "
            f"{slow_avg} days late on average. Risk: a key supplier and a late one need different "
            "levers. Next step: review delivery terms of the slowest supplier."
        )


# ------------------------------------------------------------------------ 6 automation


class ClerkScript(Script):
    def clerk(self, call: ModelCall) -> Any:
        text, booked = request(call), results(call, "book_invoice")
        if "Summarize" in text:
            if not results(call, "ledger_summary"):
                return tool_call("ledger_summary")
            return str(tool_data(results(call, "ledger_summary")[0]))
        if not booked:
            invoice = re.search(
                r"invoice (?:no\. )?(RE-\d+) from ([\w ]+?)(?: over|, total) ([\d.]+)",
                text.replace("Invoice", "invoice"),
            )
            assert invoice is not None, text
            return tool_call(
                "book_invoice",
                {
                    "invoice_no": invoice.group(1),
                    "supplier": invoice.group(2),
                    "amount": float(invoice.group(3)),
                },
            )
        return str(tool_data(booked[0]))


# ------------------------------------------------------------------------ 7 build

ACCEPTANCE: dict[str, Any] = {
    "criteria": [
        "A matching invoice up to 10,000 EUR is booked.",
        "An invoice above 10,000 EUR waits for a manager's approval.",
    ],
    "systems": [
        {
            "name": "ERP",
            "key": "erp",
            "description": "Purchase orders and supplier invoices.",
            "kind": "odata",
            "writes": True,
            "tables": [
                {
                    "name": "purchase_orders",
                    "description": "One purchase order.",
                    "columns": [
                        {"name": "po_number"},
                        {"name": "vendor"},
                        {"name": "amount", "type": "real"},
                    ],
                    "rows": ["PO-7, Nordwind GmbH, 1000", "PO-8, Nordwind GmbH, 12000"],
                },
                {
                    "name": "invoices",
                    "description": "One supplier invoice.",
                    "columns": [
                        {"name": "invoice_id"},
                        {"name": "po_number"},
                        {"name": "amount", "type": "real"},
                        {"name": "status"},
                    ],
                    "rows": ["INV-1, PO-7, 1005, new", "INV-2, PO-8, 12000, new"],
                },
            ],
        }
    ],
    "rules": [
        {
            "name": "within_tolerance",
            "description": "Whether an invoice amount is at most 2 % above the purchase order amount.",
            "arguments": [{"name": "invoice_amount"}, {"name": "po_amount"}],
            "expression": "invoice_amount <= po_amount * 1.02",
            "examples": [
                {"arguments": '{"invoice_amount": 1005, "po_amount": 1000}', "result": "true"},
                {"arguments": '{"invoice_amount": 1100, "po_amount": 1000}', "result": "false"},
            ],
        }
    ],
    "cases": [
        {
            "id": "matching-invoice",
            "input": "Check invoice INV-1.",
            "expectations": [
                {"kind": "tool_called", "value": "within_tolerance"},
                {"kind": "contains", "value": "booked"},
            ],
        },
        {
            "id": "large-invoice",
            "input": "Check invoice INV-2.",
            "expectations": [{"kind": "approval", "value": "erp_execute"}],
        },
    ],
}


def build_responses(call: ModelCall) -> Any:
    """The test designer, the architect (one repair round) and the designed clerk."""

    system = system_text(call)
    if system.startswith("You write the acceptance tests"):
        return structured_output(AcceptanceDesign, ACCEPTANCE)
    if system.startswith("You design multi-agent systems"):
        repair = "## Failing eval cases" in str(call.last_message.content)
        tools = [
            "connector:erp.query",
            "connector:erp.execute",
            *(["within_tolerance"] if repair else []),
        ]
        blueprint = {
            "name": "invoice_desk",
            "description": "Checks supplier invoices against purchase orders.",
            "entrypoint": "agent",
            "agents": [
                {
                    "name": "clerk",
                    "description": "Checks one invoice and books or holds it.",
                    "instructions": "Read the invoice and its purchase order in the ERP tables.",
                    "tools": tools,
                }
            ],
        }
        return structured_output(BlueprintDesign, blueprint)
    if "JudgeVerdict" in call.tool_names:
        return structured_output(JudgeVerdict, {"score": 0.9, "reasoning": "scripted judge: met"})
    ask = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    done = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
    invoice = "INV-2" if "INV-2" in ask else "INV-1"
    if not done:
        sql = "SELECT * FROM invoices JOIN purchase_orders USING (po_number) WHERE invoice_id = ?"
        return tool_call("erp_query", {"query": sql, "parameters": [invoice]})
    if invoice == "INV-2":
        return tool_call(
            "erp_execute",
            {"statement": "UPDATE invoices SET status = 'booked' WHERE invoice_id = 'INV-2'"},
        )
    if "within_tolerance" in call.tool_names and len(done) == 1:
        return tool_call("within_tolerance", {"invoice_amount": 1005, "po_amount": 1000})
    return "INV-1 matches PO-7 within 2 % and is booked."


def registry_of(model: FunctionChatModel) -> ModelRegistry:
    """A registry whose `default`, `fast` and `strong` profiles all use `model`."""

    registry = ModelRegistry()
    for profile in ("default", "fast", "strong"):
        registry.register(profile, model)
    return registry


SCRIPTS: dict[str, Callable[[Blueprint], Script]] = {
    "01_market_brief": BriefScript,
    "02_supplier_orders": OrdersScript,
    "03_graphql_lookup": LookupScript,
    "04_document_intake": IntakeScript,
    "05_agent_network": NetworkScript,
    "06_automation": ClerkScript,
}


def script_for(slug: str, blueprint: Blueprint) -> Script:
    """The scripted model of scenario folder `slug` (not for the build scenario)."""

    return SCRIPTS[slug](blueprint)


__all__ = ["SCRIPTS", "Script", "build_responses", "network", "registry_of", "script_for"]
