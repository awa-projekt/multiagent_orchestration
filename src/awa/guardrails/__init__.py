"""Guardrails (Runtime layer): deterministic checks that accept or reject inputs and outputs.

Agents, teams and blueprints take them as `input_guardrails=` and `output_guardrails=`; a
blocking result raises `GuardrailTripwireError` when asked to, and an `on_fail="retry"` check
first sends its reason back to the model.
"""

from awa.guardrails._guardrails import (
    BlocklistGuardrail,
    Guardrail,
    GuardrailResult,
    GuardrailStage,
    GuardrailTripwireError,
    LengthGuardrail,
    MustIncludeGuardrail,
    NonEmptyFieldsGuardrail,
    OnFail,
    PIIGuardrail,
    PIIMode,
    SchemaGuardrail,
    Severity,
    retries_on_fail,
    run_guardrails,
    run_input_guardrails,
    run_output_guardrails,
)

__all__ = [
    "BlocklistGuardrail",
    "Guardrail",
    "GuardrailResult",
    "GuardrailStage",
    "GuardrailTripwireError",
    "LengthGuardrail",
    "MustIncludeGuardrail",
    "NonEmptyFieldsGuardrail",
    "OnFail",
    "PIIGuardrail",
    "PIIMode",
    "SchemaGuardrail",
    "Severity",
    "retries_on_fail",
    "run_guardrails",
    "run_input_guardrails",
    "run_output_guardrails",
]
