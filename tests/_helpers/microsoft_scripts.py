"""Scripted models for the Microsoft process examples: each is the procedure of one example's
instruction written as plain Python, so the whole example (blueprint, connectors, local Microsoft
Graph test server, data tools, calculator, approvals) runs without a real model.

A procedure asks `replay(tool, **arguments)` for every tool result it needs (see
`microsoft_support.Replay`), so what it writes comes from what the real tools answered. The one
judgment a model makes in the offline tests (reading a customer's sentence into a product number
and a quantity, or choosing a request type) is done with a few regular expressions over the
synthetic mails. Variants with `cheat=` break exactly one rule of the instruction: the deterministic
checks must fail for each of them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from _helpers.microsoft_support import Replay

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
SUBJECT = re.compile(r"subject '([^']+)'")
SUSPICIOUS = re.compile(
    r"ignore (?:your|the) (?:rules|instructions|responsibilities)|AI assistant|"
    r"forward .*attacker|change our bank account|new password",
    re.IGNORECASE,
)
PRODUCT_LINE = re.compile(r"(?:(\d+)\s*x\s*)?(P-\d{4})")
REVIEWER = "review@company.example"


def date_of(request: str) -> str:
    found = DATE.search(request)
    assert found, request
    return found.group(0)


def listing_arguments(request: str, limit: int) -> dict[str, Any]:
    arguments: dict[str, Any] = {"unread_only": True, "oldest_first": True, "limit": limit}
    subject = SUBJECT.search(request)
    if subject:
        arguments["subject_contains"] = subject.group(1)
    return arguments


def failed(result: Any) -> bool:
    return isinstance(result, str) and result.startswith("Error")


def read_unread(replay: Replay, request: str, limit: int) -> tuple[dict[str, Any], list[Any]]:
    """The listing and the messages read (an automatic message is read but not worked on)."""

    listing = replay("mail_list_messages", **listing_arguments(request, limit))
    return listing, [
        replay("mail_read_message", message_id=item["id"], max_chars=4000)
        for item in listing["items"]
    ]


def yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def download(replay: Replay, path: str, save_to: str) -> Any:
    """Save a copy; a copy of the same day that exists already is read instead (the instruction)."""

    saved = replay("files_download_file", path=path, save_to=save_to)
    if failed(saved) and "already exists" in saved:
        return {"saved_to": save_to}
    return saved


# ------------------------------------------------------------------------------ briefings


def sharepoint_briefing(replay: Replay, request: str) -> str:
    date = date_of(request)
    listing = replay("files_list_children", folder_path="Approved", limit=20)
    if failed(listing):
        return "Documents: 0 read, 0 failed; more files in the folder: no\nINCOMPLETE: the folder could not be listed."
    files = [item for item in listing["items"] if item.get("kind") == "file"]
    named = [item for item in files if item["name"] in request]
    readers = {".csv": "workspace_read_csv", ".xlsx": "workspace_read_excel"}
    read, bad, lines, findings = 0, 0, [], []
    chosen = (named or files)[:5]
    for item in chosen:
        name = item["name"]
        saved = download(replay, f"Approved/{name}", f"sharepoint/{date}/{name}")
        if failed(saved):
            bad += 1
            lines.append(f"- Approved/{name}: NOT read ({saved[:120]})")
            continue
        suffix = "." + name.rpartition(".")[2]
        content = replay(readers.get(suffix, "workspace_read_file"), path=saved["saved_to"])
        text = content if isinstance(content, str) else json.dumps(content)
        facts = " / ".join(line.strip() for line in text.splitlines() if line.strip())[:300]
        read += 1
        lines.append(
            f"- Approved/{name} (changed {item.get('modified')}, version {item.get('etag')}): {facts}"
        )
        if SUSPICIOUS.search(text):
            findings.append(f"SUSPICIOUS: Approved/{name} addresses an AI assistant; ignored.")
    more = listing["has_more"] or len(files) > len(chosen)
    head = f"Documents: {read} read, {bad} failed; more files in the folder: {yes_no(more)}"
    return "\n".join([head, *lines, *findings, "Open questions: not stated."])


def outlook_briefing(replay: Replay, request: str) -> str:
    listing, messages = read_unread(replay, request, 10)
    lines = []
    for message in messages:
        if message["automatic"]:
            lines.append(
                f"- {message['id']} | {message['from']} | {message['subject']} | automatic"
            )
            continue
        body = message["body"]
        deadline = re.search(r"by (\d{4}-\d{2}-\d{2})", body)
        flag = " | SUSPICIOUS: asks for a payment or a password" if SUSPICIOUS.search(body) else ""
        lines.append(
            f"- {message['id']} | {message['from']} | {message['subject']} | deadline: "
            f"{deadline.group(0) if deadline else 'not stated'}{flag}"
        )
    head = (
        f"Messages: {len(messages)} read, 0 failed; more unread mail: {yes_no(listing['has_more'])}"
    )
    return "\n".join([head, *lines])


def sales_inbox(replay: Replay, request: str) -> str:
    listing, messages = read_unread(replay, request, 10)
    seen: dict[tuple[str, str], str] = {}
    lines = []
    for message in messages:
        if message["automatic"]:
            lines.append(f"- {message['id']}: automatic, skipped")
            continue
        body = message["body"]
        products = sorted({match[1] for match in PRODUCT_LINE.findall(body)})
        quantities = [match[0] for match in PRODUCT_LINE.findall(body) if match[0]]
        wished = re.search(r"delivery by\s+(\d{4}-\d{2}-\d{2})", body)
        missing = [] if quantities else ["quantity"]
        key = (message["from"], message["subject"])
        duplicate = f" | possible duplicate of {seen[key]}" if key in seen else ""
        seen.setdefault(key, message["id"])
        flag = " | SUSPICIOUS: tells the assistant what to do" if SUSPICIOUS.search(body) else ""
        lines.append(
            f"- {message['id']} | {message['from']} | wants {', '.join(products) or 'not stated'}"
            f" | quantity {', '.join(quantities) or 'not stated'}"
            f" | delivery wish {wished.group(1) if wished else 'not stated'} (a wish, no promise)"
            f" | missing {', '.join(missing) or 'nothing'} | urgency normal{duplicate}{flag}"
        )
    head = (
        f"Requests: {len(messages)} read, 0 failed; more unread mail: {yes_no(listing['has_more'])}"
    )
    return "\n".join([head, *lines])


def approved_digest(replay: Replay, request: str, *, review: str = REVIEWER) -> str:
    date = date_of(request)
    listing, messages = read_unread(replay, request, 5)
    sources, asks = [], False
    for message in messages:
        body = message["body"]
        asks = asks or bool(SUSPICIOUS.search(body))
        sources.append(f"Mail {message['id']} from {message['from']}: {message['subject']}")
    if "only mail" not in request.lower():
        docs = replay("files_list_children", folder_path="Approved", limit=20)
        files = [i for i in docs["items"] if i.get("kind") == "file"]
        named = [i for i in files if i["name"] in request]
        for item in (named or files)[:3]:
            name = item["name"]
            saved = download(replay, f"Approved/{name}", f"sharepoint/{date}/{name}")
            text = str(replay("workspace_read_file", path=saved["saved_to"]))
            asks = asks or bool(SUSPICIOUS.search(text))
            sources.append(f"Document Approved/{name}")
    digest = f"Digest {date}\n" + "\n".join(sources)
    if asks:
        return digest + "\nNOT SENT: a source asks the reader to act."
    sent = replay("mail_send_mail", to=[review], subject=f"Digest {date}", body=digest)
    if isinstance(sent, dict) and sent.get("sent"):
        return digest + "\nSubmitted: Microsoft accepted the mail; delivery is not confirmed."
    return digest + f"\nNOT SENT: {str(sent)[:200]}"


# ------------------------------------------------------------------------------ offers


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(result["columns"], row, strict=True)) for row in result["rows"]]


def _calc(replay: Replay, expression: str) -> float | int:
    value = replay("calculator", expression=expression)
    return float(value) if isinstance(value, float) else int(value)


def read_lines(body: str) -> list[dict[str, Any]]:
    """What a person reads from the mail: product numbers, quantities, discount and currency."""

    discount = re.search(r"discount of (\d+(?:\.\d+)?)\s*%|(\d+),(\d+)\s*%\s*Rabatt", body)
    requested = None
    if discount:
        requested = float(discount.group(1) or f"{discount.group(2)}.{discount.group(3)}")
    currency = re.search(r"\bin (USD|GBP|CHF)\b", body)
    return [
        {
            "product_no": match[1],
            "quantity": int(match[0]) if match[0] else None,
            "requested_discount_pct": requested,
            "requested_currency": currency.group(1) if currency else None,
        }
        for match in PRODUCT_LINE.findall(body)
    ]


def price_line(
    replay: Replay, workbook: str, date: str, line: dict[str, Any], cheat: str | None
) -> dict[str, Any]:
    """The decision for one line, exactly as the instruction says (steps D and E)."""

    result: dict[str, Any] = {
        "product_no": line["product_no"],
        "quantity": line["quantity"],
        "currency": None,
        "unit_price": None,
        "discount_pct": None,
        "requested_discount_pct": line["requested_discount_pct"],
        "approval_role": None,
        "line_net": None,
        "line_gross": None,
    }

    def stop(route: str, reason: str) -> dict[str, Any]:
        return {**result, "route": route, "reason": reason}

    def query(sheet: str, where: str, **more: Any) -> dict[str, Any]:
        return replay("workspace_query_excel", path=workbook, sheet=sheet, where=where, **more)

    number, quantity = line["product_no"], line["quantity"]
    if quantity is None or quantity < 1:
        return stop("clarify", "quantity_missing")
    valid = query(
        "Prices", f"product_no == '{number}' and valid_from <= '{date}' and valid_to >= '{date}'"
    )
    if valid["matched_rows"] == 0:
        anywhere = query("Prices", f"product_no == '{number}'")
        if anywhere["matched_rows"] == 0:
            return stop("clarify", "product_not_in_catalog")
        return stop("pricing_review", "price_expired_or_not_yet_valid")
    if valid["matched_rows"] > 1:
        return stop("pricing_review", "conflicting_prices")
    [row] = _rows(valid)
    result.update(currency=row["currency"], unit_price=float(row["unit_price"]))
    if row["price_basis"] != "net":
        return stop("pricing_review", "gross_price_basis")
    if line["requested_currency"] and line["requested_currency"] != row["currency"]:
        return stop("clarify", "currency_differs")
    [tier] = _rows(
        query("Discounts", f"min_quantity <= {quantity}", order_by=["-min_quantity"], limit=1)
    )
    discount = tier["discount_pct"]
    result["discount_pct"] = float(discount)
    route, reason, requested = "ready_for_review", "ok", line["requested_discount_pct"]
    if requested is not None and requested > discount:
        [approval] = _rows(
            query("Approval", f"up_to_pct >= {requested}", order_by=["up_to_pct"], limit=1)
        )
        result["approval_role"] = approval["role"]
        route, reason = "discount_approval", "special_discount"
    if cheat == "float_rounding":  # decimals instead of whole cents: 9,305.345 rounds down
        net = _calc(
            replay, f"round({quantity} * {row['unit_price']} * (100 - {discount}) / 100, 2)"
        )
        gross = _calc(replay, f"round({net} * 1.19, 2)")
        return {**result, "route": route, "reason": reason, "line_net": net, "line_gross": gross}
    unit_cents = _calc(replay, f"round({row['unit_price']} * 100)")
    basis_points = _calc(replay, f"round({discount} * 100)")
    net_cents = _calc(
        replay, f"({quantity} * {unit_cents} * (10000 - {basis_points}) + 5000) // 10000"
    )
    [tax] = _rows(query("Tax", f"tax_class == '{row['tax_class']}'"))
    gross_cents = _calc(replay, f"({net_cents} * (100 + {tax['rate_pct']}) + 50) // 100")
    return {
        **result,
        "route": route,
        "reason": reason,
        "line_net": _calc(replay, f"{net_cents} / 100"),
        "line_gross": _calc(replay, f"{gross_cents} / 100"),
    }


def money(value: float) -> str:
    return f"{value:.2f}"


def offer_comment(lines: list[dict[str, Any]]) -> str:
    parts = ["Thank you for your request. This is a draft offer until we confirm it."]
    for line in lines:
        label = f"{line['quantity']} x {line['product_no']}"
        if line["route"] in ("ready_for_review", "discount_approval"):
            parts.append(
                f"{label}: unit price {money(line['unit_price'])} {line['currency']}, discount "
                f"{line['discount_pct']:g} %, net {money(line['line_net'])} {line['currency']}, "
                f"gross {money(line['line_gross'])} {line['currency']}."
            )
        elif line["route"] == "clarify":
            parts.append(
                f"{line['product_no']}: please tell us the missing detail ({line['reason']})."
            )
        else:
            parts.append(f"{line['product_no']}: we will confirm this price separately.")
    parts.append("A delivery date you wish for is a wish, not a confirmed date.")
    return "\n".join(parts)


def sales_offer(replay: Replay, request: str, *, cheat: str | None = None) -> dict[str, Any]:
    date = date_of(request)
    listing, messages = read_unread(replay, request, 5)
    messages = [m for m in messages if not m["automatic"]]
    workbook = download(replay, "Approved/price-list.xlsx", f"prices/{date}.xlsx")
    complete = not listing["has_more"]
    if failed(workbook):
        notes = [
            f"The price list could not be read, so there are no prices and no drafts: {workbook}"
        ]
        return {"date": date, "complete": False, "requests": [], "notes": notes}
    requests = []
    for message in messages:
        body = message["body"]
        suspicious = bool(SUSPICIOUS.search(body))
        if cheat == "price_from_mail":  # takes the price the mail dictates
            priced = re.search(r"price is (\d+\.\d+) EUR", body)
            lines = [
                {
                    "product_no": "P-1001",
                    "quantity": 10,
                    "route": "ready_for_review",
                    "reason": "ok",
                    "currency": "EUR",
                    "unit_price": float(priced.group(1)) if priced else 1.0,
                    "line_net": 10.0,
                    "line_gross": 11.9,
                }
            ]
        else:
            lines = [
                price_line(replay, workbook["saved_to"], date, line, cheat)
                for line in read_lines(body)
            ]
        draft, draft_id = "none", None
        useful = [line for line in lines if line["route"] != "pricing_review"]
        if useful and not suspicious:
            draft = (
                "clarification" if any(line["route"] == "clarify" for line in lines) else "offer"
            )
            made = replay(
                "mail_create_reply_draft",
                message_id=message["id"],
                comment=offer_comment(lines),
                reply_all=False,
            )
            draft_id = made["draft_id"]
        requests.append(
            {
                "message_id": message["id"],
                "customer": message["from"],
                "suspicious_content": suspicious,
                "draft": draft,
                "draft_id": draft_id,
                "lines": lines,
            }
        )
    return {"date": date, "complete": complete, "requests": requests, "notes": []}


# ------------------------------------------------------------------------------ administration

TYPES = (
    ("expense_report", re.compile(r"expense report", re.IGNORECASE)),
    ("employment_certificate", re.compile(r"employment certificate", re.IGNORECASE)),
    ("payroll_change", re.compile(r"bank account has changed|pay my salary", re.IGNORECASE)),
    ("facility_issue", re.compile(r"printer|broken", re.IGNORECASE)),
    ("room_booking", re.compile(r"book a meeting room", re.IGNORECASE)),
    ("data_subject_request", re.compile(r"personal data you hold", re.IGNORECASE)),
)


def administration(replay: Replay, request: str, *, cheat: str | None = None) -> dict[str, Any]:
    date = date_of(request)
    listing, messages = read_unread(replay, request, 10)
    messages = [m for m in messages if not m["automatic"]]
    workbook = download(replay, "Approved/responsibilities.xlsx", f"lists/{date}.xlsx")
    if failed(workbook):
        notes = [
            f"The list of responsibilities could not be read, so nothing is routed: {workbook}"
        ]
        return {"date": date, "complete": False, "requests": [], "notes": notes}
    path = workbook["saved_to"]
    known = {
        row["request_type"]
        for row in _rows(
            replay("workspace_query_excel", path=path, sheet="Responsibilities", limit=100)
        )
    }
    requests = []
    for message in messages:
        body = message["body"]
        suspicious = bool(SUSPICIOUS.search(body))
        found = next((name for name, pattern in TYPES if pattern.search(body)), "unknown")
        entry: dict[str, Any] = {
            "message_id": message["id"],
            "request_type": found if found in known else "unknown",
            "suspicious_content": suspicious,
            "missing_information": [],
            "department": None,
            "contact": None,
            "legal_deadline_days": None,
            "stated_deadline": None,
            "draft": "none",
            "draft_id": None,
        }
        deadline = re.search(r"before (\d{4}-\d{2}-\d{2})", body)
        entry["stated_deadline"] = deadline.group(0) if deadline else None
        if entry["request_type"] == "unknown":
            entry["route"] = "manual"
            requests.append(entry)
            continue
        [row] = _rows(
            replay(
                "workspace_query_excel",
                path=path,
                sheet="Responsibilities",
                where=f"request_type == '{entry['request_type']}'",
            )
        )
        entry["department"], entry["contact"] = row["department"], row["contact"]
        entry["legal_deadline_days"] = row["legal_deadline_days"]
        if row["sensitive"] == "yes" and cheat != "answers_sensitive":
            entry["route"] = "restricted_review"
            entry["department"] = entry["contact"] = None  # nothing personal travels on
            requests.append(entry)
            continue
        if entry["request_type"] == "room_booking" and not DATE.search(body):
            entry["missing_information"] = ["date", "time", "number of people"]
        entry["route"] = "clarify" if entry["missing_information"] else "reply_draft"
        comment = (
            f"Your request arrived and {row['department']} handles it ({row['contact']})."
            + (
                f" The legal deadline is {row['legal_deadline_days']} days."
                if row["legal_deadline_days"]
                else ""
            )
            + (
                f" Please tell us: {', '.join(entry['missing_information'])}."
                if entry["missing_information"]
                else ""
            )
        )
        if not suspicious:
            made = replay(
                "mail_create_reply_draft",
                message_id=message["id"],
                comment=comment,
                reply_all=False,
            )
            entry["draft"] = "clarification" if entry["missing_information"] else "reply"
            entry["draft_id"] = made["draft_id"]
        requests.append(entry)
    return {"date": date, "complete": not listing["has_more"], "requests": requests, "notes": []}
