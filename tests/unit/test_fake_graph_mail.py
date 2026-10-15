"""The mail and people plug-ins of FakeGraph: filters, search, drafts, attachments, move, permissions, and what Graph refuses."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import pytest

from _helpers.fake_graph_proof import COVERED_RULES, proves
from _helpers.fakegraph import people as fake_graph_people
from _helpers.fakegraph.server import V1, FakeGraph, HttpReply

ADA = "ada@contoso.test"
BASE = f"{V1}/users/{ADA}"
T1, T2, T3 = "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z", "2026-10-03T08:00:00Z"


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        yield server


class Box:
    """Ada's mailbox with three received messages and apps with each mail permission."""

    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.mail = graph.store("mail")
        self.ada = graph.add_user(ADA, name="Ada")
        self.bob = graph.add_user("bob@contoso.test", name="Bob")
        self.tokens = {
            name: graph.app_token(graph.add_app(f"app-{name}", "s", roles=roles))
            for name, roles in {
                "read": ("Mail.Read",),
                "rw": ("Mail.ReadWrite",),
                "send": ("Mail.Send",),
                "full": ("Mail.ReadWrite", "Mail.Send"),
            }.items()
        }
        self.old = self.add("Old invoice", T1, sender="billing@fabrikam.test", is_read=True)
        self.mid = self.add("Meeting notes", T2, sender="boss@fabrikam.test", body="Agenda inside")
        self.new = self.add("New invoice", T3, sender="billing@fabrikam.test")

    def add(self, subject: str, received: str, **kwargs: Any) -> dict[str, Any]:
        return self.mail.add_message(self.ada, subject=subject, received=received, **kwargs)

    def call(self, method: str, path: str, who: str = "full", **kwargs: Any) -> Any:
        return self.graph.call(method, f"{BASE}{path}", token=self.tokens[who], **kwargs)

    def subjects(self, query: str, path: str = "/mailFolders/inbox/messages") -> list[str]:
        reply = self.call("GET", f"{path}?{query}")
        assert reply.status == 200, reply.text
        return [m["subject"] for m in reply.json()["value"]]


@pytest.fixture
def box(graph: FakeGraph) -> Box:
    return Box(graph)


# --------------------------------------------------------------------------- list: $filter, $orderby


@proves("outlook.filter_orderby_order_rule")
def test_filter_and_orderby_must_name_the_same_properties_in_the_same_order(box: Box) -> None:
    ok = [
        "$orderby=receivedDateTime asc",
        f"$filter=receivedDateTime ge {T2}",
        f"$filter=receivedDateTime ge {T2}&$orderby=receivedDateTime asc",
        f"$filter=receivedDateTime ge {T2} and isRead eq false&$orderby=receivedDateTime desc",
        "$filter=isRead eq false and subject eq 'x'&$orderby=isRead,subject",
    ]
    for query in ok:
        assert box.call("GET", f"/mailFolders/inbox/messages?{query}").status == 200, query
    bad = [
        "$filter=isRead eq false&$orderby=receivedDateTime desc",
        f"$filter=isRead eq false and receivedDateTime ge {T1}&$orderby=receivedDateTime",
        "$filter=isRead eq false&$orderby=isRead,subject",
        f"$filter=receivedDateTime ge {T1}&$orderby=subject",
    ]
    for query in bad:
        reply = box.call("GET", f"/mailFolders/inbox/messages?{query}")
        assert reply.status == 400, query
        assert reply.error() == (
            "InefficientFilter",
            "The restriction or sort order is too complex for this operation.",
        )


def test_filter_grammar_compares_dates_flags_text_and_categories(box: Box) -> None:
    box.mid["categories"] = ["awa", "Blue"]
    assert box.subjects(f"$filter=receivedDateTime ge {T2}") == ["New invoice", "Meeting notes"]
    assert box.subjects(f"$filter=receivedDateTime gt {T2}") == ["New invoice"]
    assert box.subjects(f"$filter=receivedDateTime lt {T2}") == ["Old invoice"]
    assert box.subjects("$filter=isRead eq true") == ["Old invoice"]
    assert box.subjects("$filter=contains(subject,'INVOICE')") == ["New invoice", "Old invoice"]
    assert box.subjects("$filter=startswith(subject,'meet')") == ["Meeting notes"]
    assert box.subjects("$filter=from/emailAddress/address eq 'Boss@fabrikam.test'") == [
        "Meeting notes"
    ]
    assert box.subjects("$filter=categories/any(c:c eq 'awa')") == ["Meeting notes"]
    assert box.subjects("$filter=subject eq 'it''s'") == []
    both = f"$filter=receivedDateTime ge {T1} and isRead eq false&$orderby=receivedDateTime asc"
    assert box.subjects(both) == ["Meeting notes", "New invoice"]
    assert box.subjects("$orderby=subject desc") == ["Old invoice", "New invoice", "Meeting notes"]
    assert box.subjects("") == ["New invoice", "Meeting notes", "Old invoice"]  # newest first


@proves("outlook.filter_unknown_property")
def test_an_unknown_or_miscased_property_is_400(box: Box) -> None:
    for query in (
        "$filter=nonsense eq 1",
        "$filter=ReceivedDateTime ge 2026-10-01T00:00:00Z",
        "$orderby=nonsense",
        "$filter=contains(nonsense,'x')",
        "$filter=isRead eq maybe",
        "$filter=receivedDateTime ge yesterday",
        "$filter=subject eq unquoted",
        "$filter=isRead gt true",
    ):
        reply = box.call("GET", f"/mailFolders/inbox/messages?{query}")
        assert reply.status == 400, query
    unknown = box.call("GET", "/mailFolders/inbox/messages?$filter=nonsense eq 1")
    assert unknown.error()[0] == "BadRequest"
    assert "Could not find a property named 'nonsense'" in unknown.error()[1]


def test_filters_the_fake_does_not_know_are_501_not_a_guess(box: Box) -> None:
    for query in (
        "$filter=isRead eq true or isRead eq false",
        "$filter=not isRead",
        "$filter=(isRead eq true)",
        '$search="a OR b"',
    ):
        reply = box.call("GET", f"/mailFolders/inbox/messages?{query}")
        assert reply.status == 501 and reply.error()[0] == "FakeGraphNotImplemented", query


@proves("outlook.messages_paging")
def test_top_is_capped_at_1000_and_a_next_link_carries_the_query(box: Box) -> None:
    assert len(box.call("GET", "/mailFolders/inbox/messages").json()["value"]) == 3
    for number in range(12):
        box.add(f"Bulk {number}", T1)
    first = box.call("GET", "/mailFolders/inbox/messages?$top=5&$select=subject")
    page = first.json()
    assert len(page["value"]) == 5 and "@odata.nextLink" in page
    assert (
        "%24select=subject" in page["@odata.nextLink"]
        or "$select=subject" in page["@odata.nextLink"]
    )
    second = box.graph.call("GET", page["@odata.nextLink"], token=box.tokens["read"]).json()
    assert len(second["value"]) == 5
    assert len(box.call("GET", "/mailFolders/inbox/messages").json()["value"]) == 10  # default
    assert len(box.call("GET", "/mailFolders/inbox/messages?$top=5000").json()["value"]) == 15
    assert box.call("GET", "/mailFolders/inbox/messages?$top=0").status == 400
    assert box.call("GET", "/mailFolders/inbox/messages?$select=nonsense").status == 400


# --------------------------------------------------------------------------- list: $search


@proves("outlook.search_results_by_sent_date_capped")
def test_search_sorts_by_sent_date_caps_at_1000_and_knows_the_default_properties(box: Box) -> None:
    box.old["sentDateTime"] = "2026-10-09T00:00:00Z"  # received first, sent last
    assert box.subjects('$search="invoice"', "/messages") == ["Old invoice", "New invoice"]
    assert box.subjects('$search="agenda"', "/messages") == ["Meeting notes"]  # in the body
    assert box.subjects('$search="boss@fabrikam"', "/messages") == ["Meeting notes"]  # in from
    assert box.subjects("$search=meeting", "/messages") == ["Meeting notes"]  # quotes optional
    assert box.subjects('$search="from:billing subject:new"', "/messages") == ["New invoice"]
    assert box.subjects('$search="hasAttachments:true"', "/messages") == []
    for number in range(1005):
        box.add(f"Flood {number}", T1)
    flood = box.call("GET", '/messages?$search="flood"&$top=1000').json()
    assert len(flood["value"]) == 1000 and "@odata.nextLink" not in flood


@proves("outlook.search_excludes_filter_orderby")
def test_search_cannot_be_combined_with_filter_or_orderby(box: Box) -> None:
    for extra in ("$filter=isRead eq false", "$orderby=receivedDateTime"):
        reply = box.call("GET", f'/messages?$search="invoice"&{extra}')
        assert reply.status == 400 and reply.error()[0] == "BadRequest", extra


def test_messages_lists_every_folder_and_a_folder_only_its_own(box: Box) -> None:
    box.mail.add_message(box.ada, "archive", subject="Filed")
    assert "Filed" in box.subjects("", "/messages") and len(box.subjects("", "/messages")) == 4
    assert "Filed" not in box.subjects("")
    assert box.subjects("", "/mailFolders/archive/messages") == ["Filed"]


# --------------------------------------------------------------------------- drafts and reply


@proves("outlook.create_reply_draft")
def test_create_reply_makes_a_draft_for_the_sender_with_a_web_link_and_needs_only_readwrite(
    box: Box,
) -> None:
    reply = box.call("POST", f"/messages/{box.mid['id']}/createReply", who="rw")
    assert reply.status == 201
    draft = reply.json()
    assert draft["isDraft"] is True and draft["subject"] == "RE: Meeting notes"
    assert [r["emailAddress"]["address"] for r in draft["toRecipients"]] == ["boss@fabrikam.test"]
    assert draft["webLink"].startswith("https://outlook.office365.com/owa/?ItemID=")
    assert quote(draft["id"]) in draft["webLink"]
    assert draft["conversationId"] == box.mid["conversationId"]
    assert "Agenda inside" in draft["body"]["content"]  # the quoted original
    assert draft["body"]["contentType"] == "html"
    assert box.mail.box(box.ada.id)["drafts"][0]["id"] == draft["id"]
    assert box.mail.box(box.ada.id)["sentitems"] == []  # nothing was sent
    # a reply-to address wins over the sender
    routed = box.add("Routed", T3, reply_to=("helpdesk@fabrikam.test",))
    other = box.call("POST", f"/messages/{routed['id']}/createReply", who="rw").json()
    assert [r["emailAddress"]["address"] for r in other["toRecipients"]] == [
        "helpdesk@fabrikam.test"
    ]
    # reply-all adds the others but not the mailbox itself
    group = box.add("Group", T3, recipients=(ADA, "carol@fabrikam.test"))
    everyone = box.call("POST", f"/messages/{group['id']}/createReplyAll", who="rw").json()
    assert [r["emailAddress"]["address"] for r in everyone["toRecipients"]] == [
        "ext@fabrikam.test",
        "carol@fabrikam.test",
    ]
    assert box.call("POST", f"/messages/{box.mid['id']}/createReply", who="read").status == 403
    assert box.call("POST", f"/messages/{box.mid['id']}/createReply", who="send").status == 403
    assert box.call("POST", "/messages/missing/createReply", who="rw").status == 404


@proves("outlook.draft_writes_need_readwrite")
def test_drafts_are_written_with_readwrite_and_only_drafts_have_a_writable_body(box: Box) -> None:
    draft = {
        "subject": "Hello",
        "body": {"contentType": "Text", "content": "Hi there"},
        "toRecipients": [{"emailAddress": {"address": "bob@contoso.test"}}],
    }
    assert box.call("POST", "/messages", who="send", json_body=draft).status == 403
    assert box.call("POST", "/messages", who="read", json_body=draft).status == 403
    made = box.call("POST", "/messages", who="rw", json_body=draft)
    assert made.status == 201 and made.json()["isDraft"] is True
    draft_id = made.json()["id"]
    patched = box.call(
        "PATCH",
        f"/messages/{draft_id}",
        who="rw",
        json_body={"body": {"contentType": "text", "content": "Changed"}, "subject": "New subject"},
    )
    assert patched.status == 200 and patched.json()["bodyPreview"] == "Changed"
    assert box.mail.find(box.ada.id, draft_id)[1]["subject"] == "New subject"
    received = box.call(
        "PATCH",
        f"/messages/{box.mid['id']}",
        who="rw",
        json_body={"isRead": True, "subject": "Hacked"},
    )
    assert received.status == 400 and received.error()[0] == "ErrorInvalidProperty"
    assert box.mail.find(box.ada.id, box.mid["id"])[1]["isRead"] is False  # nothing changed
    assert box.call("PATCH", f"/messages/{draft_id}", who="send", json_body={}).status == 403


@proves("outlook.send_draft")
def test_sending_a_draft_needs_mail_send_answers_202_and_lands_in_sent_items(box: Box) -> None:
    draft = box.call("POST", f"/messages/{box.mid['id']}/createReply", who="rw").json()
    path = f"/messages/{draft['id']}/send"
    assert box.call("POST", path, who="rw").status == 403  # Mail.ReadWrite alone cannot send
    sent = box.call("POST", path, who="send")
    assert sent.status == 202 and sent.body == b""
    assert [m["subject"] for m in box.mail.box(box.ada.id)["sentitems"]] == ["RE: Meeting notes"]


@proves("outlook.sent_draft_leaves_drafts")
def test_a_sent_draft_id_is_gone_and_a_draft_without_recipients_is_refused(box: Box) -> None:
    draft = box.call("POST", f"/messages/{box.mid['id']}/createReply", who="rw").json()
    box.call("POST", f"/messages/{draft['id']}/send", who="send")
    gone = box.call("GET", f"/messages/{draft['id']}")
    assert gone.status == 404 and gone.error()[0] == "ErrorItemNotFound"
    assert box.call("POST", f"/messages/{draft['id']}/send", who="send").status == 404
    empty = box.call(
        "POST", "/messages", who="rw", json_body={"subject": "x", "toRecipients": []}
    ).json()
    refused = box.call("POST", f"/messages/{empty['id']}/send", who="send")
    assert refused.status == 400 and refused.error()[0] == "ErrorInvalidRecipients"
    bad = box.call(
        "POST",
        "/messages",
        who="rw",
        json_body={"toRecipients": [{"emailAddress": {"address": "no-at-sign"}}]},
    ).json()
    assert box.call("POST", f"/messages/{bad['id']}/send", who="send").status == 400
    received = box.call("POST", f"/messages/{box.mid['id']}/send", who="send")
    assert received.status == 400  # not a draft


def test_a_sent_draft_is_delivered_to_a_known_mailbox_and_a_lost_answer_can_be_scripted(
    box: Box,
) -> None:
    box.call("POST", f"/messages/{box.mid['id']}/createReply", who="rw")
    made = box.call(
        "POST",
        "/messages",
        who="rw",
        json_body={
            "subject": "For Bob",
            "toRecipients": [{"emailAddress": {"address": "bob@contoso.test"}}],
        },
    ).json()
    box.mail.fail_after_send(504)
    lost = box.call("POST", f"/messages/{made['id']}/send", who="send")
    assert lost.status == 504  # the answer is lost ...
    # ... the mail left
    assert [m["subject"] for m in box.mail.box(box.bob.id)["inbox"]] == ["For Bob"]
    assert box.call("GET", f"/messages/{made['id']}").status == 404  # and the draft is gone


# --------------------------------------------------------------------------- move, categories, headers


@proves("outlook.move_returns_new_id")
def test_move_answers_201_with_a_new_id_and_the_old_id_is_gone(box: Box) -> None:
    moved = box.call(
        "POST", f"/messages/{box.mid['id']}/move", who="rw", json_body={"destinationId": "archive"}
    )
    assert moved.status == 201
    body = moved.json()
    assert body["id"] != box.mid["id"] and body["parentFolderId"] == box.mail.folder_id(
        box.ada.id, "archive"
    )
    assert box.call("GET", f"/messages/{box.mid['id']}").status == 404
    assert box.call("GET", f"/messages/{body['id']}").status == 200
    assert box.subjects("", "/mailFolders/archive/messages") == ["Meeting notes"]
    unknown = box.call(
        "POST", f"/messages/{body['id']}/move", who="rw", json_body={"destinationId": "nowhere"}
    )
    assert unknown.status == 404 and unknown.error()[0] == "ErrorFolderNotFound"
    assert (
        box.call(
            "POST", f"/messages/{body['id']}/move", who="read", json_body={"destinationId": "inbox"}
        ).status
        == 403
    )


@proves("outlook.categories_replaced_whole")
def test_patching_categories_replaces_the_list(box: Box) -> None:
    path = f"/messages/{box.mid['id']}"
    box.call("PATCH", path, who="rw", json_body={"categories": ["a", "b"]})
    box.call("PATCH", path, who="rw", json_body={"categories": ["c"]})
    assert box.call("GET", f"{path}?$select=categories").json()["categories"] == ["c"]


@proves("outlook.internet_headers_only_on_select")
def test_internet_message_headers_come_only_with_select(box: Box) -> None:
    message = box.add("Auto", T3, headers={"Auto-Submitted": "auto-replied"})
    plain = box.call("GET", f"/messages/{message['id']}").json()
    assert "internetMessageHeaders" not in plain
    chosen = box.call("GET", f"/messages/{message['id']}?$select=internetMessageHeaders").json()
    assert chosen["internetMessageHeaders"] == [{"name": "Auto-Submitted", "value": "auto-replied"}]
    listed = box.call("GET", "/mailFolders/inbox/messages?$select=subject,internetMessageHeaders")
    assert any("internetMessageHeaders" in m for m in listed.json()["value"])


# --------------------------------------------------------------------------- attachments


@proves("outlook.attachment_listing_and_value")
def test_attachments_are_listed_and_read_as_raw_bytes_and_a_reference_has_no_value(
    box: Box,
) -> None:
    big = b"B" * (4 * 1024 * 1024)  # above the inline limit: the fake keeps any size
    message = box.add(
        "With files",
        T3,
        attachments=(
            {"name": "a.txt", "content": b"hello", "content_type": "text/plain"},
            {"name": "big.bin", "content": big},
            {"name": "logo.png", "content": b"\x89PNG", "inline": True},
            {"name": "Cloud link", "kind": "reference"},
        ),
    )
    assert message["hasAttachments"] is True
    listed = box.call("GET", f"/messages/{message['id']}/attachments?$select=id,name,size,isInline")
    items = listed.json()["value"]
    assert [(a["name"], a["size"], a["isInline"]) for a in items] == [
        ("a.txt", 5, False),
        ("big.bin", len(big), False),
        ("logo.png", 4, True),
        ("Cloud link", 0, False),
    ]
    assert "contentBytes" not in items[0]
    full = box.call("GET", f"/messages/{message['id']}/attachments/{items[0]['id']}").json()
    assert full["contentBytes"] == "aGVsbG8=" and full["contentType"] == "text/plain"
    raw = box.call("GET", f"/messages/{message['id']}/attachments/{items[0]['id']}/$value")
    assert raw.status == 200 and raw.body == b"hello"
    assert {k.lower(): v for k, v in raw.headers.items()}["content-type"].startswith("text/plain")
    assert (
        box.call("GET", f"/messages/{message['id']}/attachments/{items[1]['id']}/$value").body
        == big
    )
    ref = box.call("GET", f"/messages/{message['id']}/attachments/{items[3]['id']}/$value")
    assert ref.status == 405
    assert box.call("GET", f"/messages/{message['id']}/attachments/missing").status == 404
    assert box.call("GET", f"/messages/{message['id']}/attachments?$select=nonsense").status == 400
    only_inline = box.add("Inline only", T3, attachments=({"name": "i.png", "inline": True},))
    assert only_inline["hasAttachments"] is False
    twins = box.add("Twins", T3, attachments=({"name": "x.txt"}, {"name": "x.txt"}))
    listed = box.call("GET", f"/messages/{twins['id']}/attachments?$select=id").json()["value"]
    assert len({attachment["id"] for attachment in listed}) == 2


ALL = "User.Read.All"


@pytest.fixture
def graph_people() -> Iterator[FakeGraph]:
    with FakeGraph() as server:
        server.use(fake_graph_people)
        store = server.store("people")
        store.add("Carla Meier", "carla.meier@contoso.example")
        store.add("Carl Meyer", "carl.meyer@contoso.example")
        store.add("David Li", "david.li@contoso.example")
        store.add("Hiking&Recreation Team", "hiking@contoso.example")
        store.add("Gail Gast", "gail.gast@fabrikam.example", guest=True)
        yield server


def token(graph_people: FakeGraph, *roles: str) -> str:
    return graph_people.app_token(graph_people.add_app("app", "secret", roles=roles or (ALL,)))


def users(
    graph_people: FakeGraph, query: str, *, consistency: bool = True, **kwargs: str
) -> HttpReply:
    headers = {"ConsistencyLevel": "eventual"} if consistency else {}
    return graph_people.call(
        "GET",
        f"{V1}/users?{query}",
        token=kwargs.get("tok") or token(graph_people),
        headers=headers,
    )


def search(text: str) -> str:
    return "$search=" + quote(text, safe="") + "&$count=true"


def names(reply: HttpReply) -> list[str]:
    return sorted(u["displayName"] for u in reply.json()["value"])


@proves("people.search_needs_consistency_level")
def test_search_without_consistency_level_or_count_is_400(graph_people: FakeGraph) -> None:
    reply = users(graph_people, search('"displayName:carl"'), consistency=False)
    assert reply.status == 400
    assert reply.error()[0] == "Request_UnsupportedQuery"
    no_count = users(graph_people, "$search=" + quote('"displayName:carl"'))
    assert no_count.status == 400
    assert users(graph_people, search('"displayName:carl"')).status == 200


@proves("people.search_clause_syntax")
def test_a_clause_needs_quotes_a_property_and_uppercase_operators(graph_people: FakeGraph) -> None:
    for bad in (
        "carl",
        '"carl"',
        '"nope:carl"',
        '"displayName:carl" or "mail:x"',
        '("displayName:a"',
    ):
        assert users(graph_people, search(bad)).status == 400, bad
    escaped = users(graph_people, search('"displayName:carla\\" OR \\"mail:"'))
    assert escaped.status == 200 and names(escaped) == []
    both = users(
        graph_people, search('"displayName:carla" OR ("displayName:david" AND "mail:david")')
    )
    assert names(both) == ["Carla Meier", "David Li"]


@proves("people.search_matches_word_beginnings")
def test_words_match_at_their_beginning_in_any_order_and_mail_from_the_start(
    graph_people: FakeGraph,
) -> None:
    assert names(users(graph_people, search('"displayName:carl"'))) == ["Carl Meyer", "Carla Meier"]
    assert names(users(graph_people, search('"displayName:meier carla"'))) == ["Carla Meier"]
    assert names(users(graph_people, search('"displayName:li"'))) == ["David Li"]
    assert names(users(graph_people, search('"displayName:arla"'))) == []
    assert names(users(graph_people, search('"mail:carla.mei"'))) == ["Carla Meier"]
    assert names(users(graph_people, search('"mail:meier"'))) == []


@proves("people.search_ampersand_refused")
def test_an_ampersand_in_a_search_value_is_400(graph_people: FakeGraph) -> None:
    reply = users(graph_people, search('"displayName:Hiking&Recreation"'))
    assert reply.status == 400
    assert "Unrecognized query argument" in reply.error()[1]


@proves("people.list_users_permissions")
def test_listing_needs_user_read_all_or_readbasic(graph_people: FakeGraph) -> None:
    mail_only = token(graph_people, "Mail.Read")
    reply = users(graph_people, search('"displayName:carl"'), tok=mail_only)
    assert reply.status == 403
    assert reply.error()[0] == "Authorization_RequestDenied"
    app = graph_people.add_app("public", None, scopes=("User.ReadBasic.All",), public_client=True)
    user = graph_people.add_user("ada@contoso.test")
    basic = graph_people.issue_token(app, kind="user", scopes=("User.ReadBasic.All",), user=user)
    assert users(graph_people, search('"displayName:carl"'), tok=basic).status == 200


@proves("people.get_missing_user_404")
def test_reading_a_deleted_user_is_404(graph_people: FakeGraph) -> None:
    store = graph_people.store("people")
    carla = next(p for p in store.people.values() if p["displayName"] == "Carla Meier")
    tok = token(graph_people)
    assert graph_people.call("GET", f"{V1}/users/{carla['id']}", token=tok).status == 200
    store.delete(carla["id"])
    gone = graph_people.call("GET", f"{V1}/users/{carla['id']}", token=tok)
    assert gone.status == 404 and gone.error()[0] == "Request_ResourceNotFound"


@proves("people.readbasic_hides_other_properties")
def test_readbasic_answers_null_for_account_enabled(graph_people: FakeGraph) -> None:
    store = graph_people.store("people")
    carla = next(p for p in store.people.values() if p["displayName"] == "Carla Meier")
    url = f"{V1}/users/{carla['id']}?$select=id,displayName,accountEnabled,userType"
    full = graph_people.call("GET", url, token=token(graph_people)).json()
    assert full["accountEnabled"] is True and full["userType"] == "Member"
    app = graph_people.add_app("public", None, scopes=("User.ReadBasic.All",), public_client=True)
    basic = graph_people.issue_token(
        app,
        kind="user",
        scopes=("User.ReadBasic.All",),
        user=graph_people.add_user("ada@contoso.test"),
    )
    seen = graph_people.call("GET", url, token=basic).json()
    assert seen["accountEnabled"] is None and seen["userType"] is None
    assert seen["displayName"] == "Carla Meier"


@proves("people.guest_upn_has_ext")
def test_a_guest_has_ext_in_the_user_principal_name(graph_people: FakeGraph) -> None:
    reply = users(
        graph_people, search('"displayName:gail"') + "&$select=displayName,userPrincipalName"
    )
    (gail,) = reply.json()["value"]
    assert "#EXT#" in gail["userPrincipalName"]


@proves("people.page_size")
def test_the_page_holds_100_by_default_and_999_at_most(graph_people: FakeGraph) -> None:
    store = graph_people.store("people")
    for n in range(105):
        store.add(f"Person {n:03d}", f"p{n}@contoso.example")
    first = users(graph_people, "$select=id").json()  # type: ignore[attr-defined]
    assert len(first["value"]) == 100
    rest = graph_people.call(
        "GET",
        first["@odata.nextLink"].removeprefix(graph_people.url),
        token=token(graph_people),
        headers={"ConsistencyLevel": "eventual"},
    ).json()
    assert len(rest["value"]) == 10 and "@odata.nextLink" not in rest
    assert len(users(graph_people, "$top=2000").json()["value"]) == 110  # type: ignore[attr-defined]


@proves("people.filter_startswith")
def test_startswith_matches_the_beginning_of_the_name(graph_people: FakeGraph) -> None:
    query = "$filter=" + quote("startswith(displayName,'carl')", safe="")
    assert names(users(graph_people, query, consistency=False)) == ["Carl Meyer", "Carla Meier"]
    quoted = "$filter=" + quote("startswith(displayName,'o''brien')", safe="")
    assert users(graph_people, quoted, consistency=False).status == 200
    other = "$filter=" + quote("endswith(mail,'x')", safe="")
    assert users(graph_people, other, consistency=False).status == 400


def test_every_people_rule_has_a_test() -> None:
    ids = {rule.id for rule in fake_graph_people.plugin.rules}
    assert ids <= COVERED_RULES, sorted(ids - COVERED_RULES)
    assert all(rule.verified_live is False for rule in fake_graph_people.plugin.rules)
