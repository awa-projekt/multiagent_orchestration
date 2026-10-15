"""Quality, validity and key rules of every builtin skill shipped in `awa/skills/builtin`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import awa.skills
from awa.skills import Skill, SkillRegistry

BUILTIN_DIR = Path(awa.skills.__file__).resolve().parent / "builtin"
SPEC_KEYS = {"name", "description", "license", "compatibility", "allowed-tools", "metadata"}
# Generic business-process skills (extraction, validation, communication, governance, ...).
BUSINESS = {
    "approval-requests",
    "customer-communication",
    "data-analysis",
    "data-validation",
    "document-extraction",
    "spreadsheet-enrichment",
    "escalation-triage",
    "meeting-and-decision-notes",
    "policy-compliance-check",
    "process-mapping",
    "report-generation",
}
# Department-level skills (sales, marketing, support, finance, procurement, people, legal,
# risk, operations): the same bar as BUSINESS plus checks on authority and neutrality.
DEPARTMENT = {
    "audit-evidence-collection",
    "budget-variance-analysis",
    "change-management",
    "competitive-analysis",
    "complaint-handling",
    "contract-clause-review",
    "contract-obligation-tracking",
    "customer-feedback-analysis",
    "customer-invoice-preparation",
    "data-subject-requests",
    "email-triage",
    "employee-onboarding-planning",
    "expense-review",
    "incident-postmortem",
    "inventory-and-order-management",
    "invoice-reconciliation",
    "lead-qualification",
    "marketing-copy",
    "customer-order-intake",
    "payment-follow-up",
    "procurement-sourcing",
    "project-status-reporting",
    "quotation-drafting",
    "recruiting-screening",
    "refund-and-credit-requests",
    "risk-assessment",
    "sales-outreach",
    "sop-writing",
    "support-ticket-resolution",
    "vendor-onboarding",
}
DEPARTMENT_CATEGORIES = {
    "finance",
    "legal",
    "marketing",
    "operations",
    "people",
    "planning",
    "procurement",
    "risk",
    "sales",
    "support",
}
PRODUCT_NAMES = re.compile(
    r"\b(Salesforce|HubSpot|SAP|Oracle|NetSuite|QuickBooks|Xero|Workday|Jira|"
    r"ServiceNow|Slack|Excel|Google|Microsoft|LinkedIn|Coupa|Ariba|Intercom|Asana|Notion)\b"
)
# General-purpose skills for how agents work (coordination, quality, safety, engineering).
GENERAL = {
    "agent-collaboration",
    "code-review",
    "context-management",
    "delegation-briefs",
    "handoff-protocol",
    "idempotent-retries",
    "orchestration-planning",
    "prompt-injection-awareness",
    "quality-review",
    "research-synthesis",
    "retrieval-grounding",
    "root-cause-debugging",
    "safe-tool-execution",
    "security-review",
    "source-verification",
    "structured-output",
    "task-planning",
    "writing",
}
EXPECTED = GENERAL | BUSINESS | DEPARTMENT
BUILTIN = SkillRegistry.builtin()


def test_builtin_set() -> None:
    assert set(BUILTIN.names()) == EXPECTED
    assert {path.name for path in BUILTIN_DIR.iterdir() if path.is_dir()} >= EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_builtin_skill_is_valid(name: str) -> None:
    skill = BUILTIN.get(name)

    assert Skill.from_path(BUILTIN_DIR / name) == skill
    assert skill.source_path == BUILTIN_DIR / name / "SKILL.md"
    assert re.search(r"\bUse (when|whenever|for|at|before)\b", skill.description), (
        "say when to use it"
    )
    assert 100 <= len(skill.description) <= 400
    assert re.fullmatch(r"\d+\.\d+\.\d+", skill.version)
    assert skill.category
    assert len(skill.tags) >= 3
    assert skill.instructions.startswith("# ")
    assert len(skill.instructions.splitlines()) <= 80


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_builtin_frontmatter_uses_only_spec_fields(name: str) -> None:
    text = (BUILTIN_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---")[1])

    assert set(frontmatter) <= SPEC_KEYS
    assert all(isinstance(value, str) for value in frontmatter["metadata"].values())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_builtin_resources_are_referenced_and_readable(name: str) -> None:
    skill = BUILTIN.get(name)
    mentioned = set(re.findall(r"`((?:references|scripts|assets)/[^`]+)`", skill.instructions))

    assert mentioned == set(skill.resources)
    for resource in skill.resources:
        assert skill.read_resource(resource).strip()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_builtin_skills_are_generic(name: str) -> None:
    skill = BUILTIN.get(name)
    texts = [skill.instructions, *(skill.read_resource(path) for path in skill.resources)]

    for text in texts:
        assert not re.search(r"\bawa\b|ToolPolicy|verification_report", text)


def test_multi_agent_skills_are_in_the_coordination_category() -> None:
    coordination = {skill.name for skill in BUILTIN.all() if skill.category == "coordination"}

    assert {
        "agent-collaboration",
        "delegation-briefs",
        "handoff-protocol",
        "orchestration-planning",
    } <= coordination


@pytest.mark.parametrize("name", sorted(BUSINESS | DEPARTMENT))
def test_business_skills_are_actionable(name: str) -> None:
    skill = BUILTIN.get(name)
    sections = re.findall(r"^## ", skill.instructions, flags=re.MULTILINE)

    assert 40 <= len(skill.instructions.splitlines()) <= 80
    assert len(sections) >= 3
    assert "```" in skill.instructions, "give a template or an output format"
    assert not skill.metadata, "only version, category, tags, risk"


@pytest.mark.parametrize("name", sorted(DEPARTMENT))
def test_department_skills_are_neutral(name: str) -> None:
    skill = BUILTIN.get(name)
    texts = [skill.instructions, *(skill.read_resource(path) for path in skill.resources)]

    assert skill.category in DEPARTMENT_CATEGORIES
    for text in texts:
        assert not PRODUCT_NAMES.search(text), "no product or vendor names"
        for line in text.splitlines():
            if re.search(r"\b(legal|medical|financial|tax|investment) advice\b", line, re.I):
                assert re.search(r"\b(not|never|no)\b", line), f"advice claim: {line!r}"


def test_business_skills_bundle_their_references() -> None:
    assert BUILTIN.get("document-extraction").resources == ("references/confidence-rubric.md",)
    assert BUILTIN.get("escalation-triage").resources == ("references/severity-matrix.md",)
    assert BUILTIN.get("report-generation").resources == ("references/skeleton.md",)
    assert BUILTIN.get("lead-qualification").resources == ("references/icp-scorecard.md",)
    assert BUILTIN.get("invoice-reconciliation").resources == ("references/match-tolerances.md",)
    assert BUILTIN.get("procurement-sourcing").resources == ("references/comparison-matrix.md",)
    assert BUILTIN.get("contract-clause-review").resources == ("references/clause-checklist.md",)
    assert BUILTIN.get("risk-assessment").resources == ("references/risk-matrix.md",)
    assert BUILTIN.get("incident-postmortem").resources == ("references/postmortem-template.md",)


def test_business_skills_stay_within_their_authority() -> None:
    communication = BUILTIN.get("customer-communication")
    compliance = BUILTIN.get("policy-compliance-check")

    assert communication.risk == "medium"
    assert "legal, medical, financial, or tax advice" in communication.instructions
    assert "not a legal opinion" in compliance.instructions
    assert "never approve your own request" in BUILTIN.get("approval-requests").instructions


def test_approval_requests_call_the_gated_tool_instead_of_asking_in_text() -> None:
    # Agents must call the approval tool instead of writing "Approval needed: ...".
    text = " ".join(BUILTIN.get("approval-requests").instructions.split())

    assert "calling it with the exact values *is* the approval request" in text
    assert "Call the tool; do not write the request as text or ask in the chat instead" in text
    assert text.index("When the tool itself waits for approval") < text.index("## Template")


def test_department_skills_stay_within_their_authority() -> None:
    def text(name: str) -> str:
        return " ".join(BUILTIN.get(name).instructions.split())

    assert "not legal advice" in text("contract-clause-review")
    assert "protected characteristics" in text("recruiting-screening")
    assert "human decision required" in text("recruiting-screening")
    assert "record the opt-out" in text("sales-outreach")
    assert "Every claim must be true and substantiated" in text("sales-outreach")
    assert "needs evidence on file" in text("marketing-copy")
    assert "never the one on the invoice" in text("invoice-reconciliation")
    assert "the implementer does not approve their own" in text("change-management")
    assert "accepting them is not an agent decision" in text("risk-assessment")
    assert "do not create, backdate, or alter records" in text("audit-evidence-collection")
    assert "Blameless means the system is examined" in text("incident-postmortem")
    medium = {name for name in DEPARTMENT if BUILTIN.get(name).risk == "medium"}
    assert {"change-management", "invoice-reconciliation", "recruiting-screening"} <= medium


NAMES = sorted(BUILTIN.names())

MAX_SKILL_FILE_BYTES = 6_500
MAX_RESOURCE_BYTES = 4_000
OVERCLAIMS = re.compile(
    r"error-free|production-ready|guaranteed?\b|foolproof|100 ?%|always correct|never fails|"
    r"nothing happens without|fully automatic|everything works",
    re.IGNORECASE,
)
VAGUE = re.compile(
    r"\betc\.|\band so on\b|\bas appropriate\b|\bappropriately\b|\bproperly\b|"
    r"\bbest practices?\b|\bbe careful\b|\bas needed\b|\bwhen necessary\b",
    re.IGNORECASE,
)
GERMAN_WORDS = re.compile(
    r"\b(und|nicht|oder|der|die|das|ein|eine|mit|f\u00fcr|wird|sind|Rechnung|Kunde)\b"
)


def _all_text(skill: Skill) -> str:
    files = [(skill.source_path or Path()).read_text(encoding="utf-8")]
    files += [skill.read_resource(path) for path in skill.resources]
    return "\n".join(files)


def test_there_are_builtin_skills() -> None:
    assert len(NAMES) >= 58


@pytest.mark.parametrize("name", NAMES)
def test_frontmatter_has_the_required_fields(name: str) -> None:
    text = (BUILTIN_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---")[1])

    assert frontmatter["name"] == name, "the folder name and the skill name must be equal"
    assert set(frontmatter) <= SPEC_KEYS
    assert frontmatter["description"].strip()
    assert frontmatter["metadata"]["risk"] in {"low", "medium", "high"}
    assert frontmatter["metadata"]["category"]
    assert len(frontmatter["metadata"]["tags"].split()) >= 3


@pytest.mark.parametrize("name", NAMES)
def test_size_budget(name: str) -> None:
    skill = BUILTIN.get(name)

    assert skill.source_path is not None
    assert skill.source_path.stat().st_size <= MAX_SKILL_FILE_BYTES, "split or shorten the skill"
    for resource in skill.resources:
        assert len(skill.read_resource(resource).encode()) <= MAX_RESOURCE_BYTES


@pytest.mark.parametrize("name", NAMES)
def test_description_says_what_it_does_and_when_to_use_it(name: str) -> None:
    description = " ".join(BUILTIN.get(name).description.split())

    assert re.search(r"\bUse (when|whenever|for|at|before)\b", description)
    assert description.index("Use ") > 40, "state what the skill does before when to use it"


@pytest.mark.parametrize("name", NAMES)
def test_text_makes_no_overclaim_and_stays_concrete(name: str) -> None:
    text = _all_text(BUILTIN.get(name))

    assert not OVERCLAIMS.search(text), OVERCLAIMS.search(text)
    assert not VAGUE.search(text), VAGUE.search(text)


@pytest.mark.parametrize("name", NAMES)
def test_text_is_english_and_ascii(name: str) -> None:
    text = _all_text(BUILTIN.get(name))

    assert text.isascii(), [c for c in text if not c.isascii()][:5]
    assert not GERMAN_WORDS.search(text)


@pytest.mark.parametrize("name", NAMES)
def test_skill_gives_a_concrete_output_format(name: str) -> None:
    skill = BUILTIN.get(name)
    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", skill.instructions, re.DOTALL | re.MULTILINE)

    assert blocks, "every skill shows its output format or template in a fenced block"
    assert all(block.strip().count("\n") >= 2 for block in blocks[:1]), "at least three lines"


@pytest.mark.parametrize("name", NAMES)
def test_skill_stays_clear_of_instructions_to_hide_or_bypass(name: str) -> None:
    text = _all_text(BUILTIN.get(name)).lower()

    for phrase in ("ignore previous instructions", "do not tell the user", "hide this from"):
        if phrase in text:
            # The injection-awareness skill may quote the phrase as an example of an attack.
            assert name == "prompt-injection-awareness", phrase


def core(name: str) -> str:
    return SkillRegistry.builtin().get(name).instructions


def test_data_answers_name_the_period_and_check_with_an_unfiltered_count() -> None:
    text = core("data-analysis")

    assert 'Never report "no data" or a zero without the period the data covers.' in text
    assert "one unfiltered count first" in text


def test_grounded_answers_promise_nothing_without_support() -> None:
    assert "promise nothing" in core("retrieval-grounding")


def test_a_value_within_the_allowed_range_is_compliant() -> None:
    text = core("policy-compliance-check")

    assert "within the range the policy allows is compliant" in text
    assert "at most an informational note" in text
