"""LLM narrative: PHI minimization, sanitization, and graceful degradation.

The PHI assertions here are the plugin's central safety claim. They check the
payload by allow-list — anything a future change adds to `build_payload` fails
these tests unless it is explicitly declared safe.
"""

import json
from http import HTTPStatus
from typing import cast
from unittest.mock import Mock, patch

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import (
    GAP_LABS_OVERDUE,
    GAP_NO_FOLLOWUP,
    GAP_STALE_WEIGHT,
    Gap,
)
from glp1_care_gap_copilot.rationale import (
    GLP1_MED_CLASS,
    MAX_RATIONALE_CHARS,
    build_payload,
    fallback_sentence,
    generate_rationale,
    sanitize,
)

LLM_CONFIG = Config.from_secrets({"ANTHROPIC_API_KEY": "sk-test"})

GAPS = [
    Gap(
        key=GAP_STALE_WEIGHT,
        label="No weight recorded in 58 days",
        detail={"days_since_last": 58},
    ),
    Gap(
        key=GAP_LABS_OVERDUE,
        label="Labs overdue: hemoglobin a1c",
        detail={"missing": ["hemoglobin a1c"], "days_since_last": 214},
    ),
]

# Every key the payload is permitted to contain, at either level.
ALLOWED_TOP_LEVEL_KEYS = {"gaps", "med_class", "weeks_since_last_visit"}
ALLOWED_GAP_KEYS = {"type", "days_since_last", "missing", "horizon_days"}


def test_payload_contains_only_declared_keys() -> None:
    payload = build_payload(GAPS, on_glp1_medication=True, weeks_since_last_visit=12)

    assert set(payload) <= ALLOWED_TOP_LEVEL_KEYS
    for gap in cast(list[dict[str, object]], payload["gaps"]):
        assert set(gap) <= ALLOWED_GAP_KEYS


def test_payload_carries_the_derived_scalars() -> None:
    payload = build_payload(GAPS, on_glp1_medication=True, weeks_since_last_visit=12)

    assert payload["med_class"] == GLP1_MED_CLASS
    assert payload["weeks_since_last_visit"] == 12
    assert payload["gaps"] == [
        {"type": GAP_STALE_WEIGHT, "days_since_last": 58},
        {"type": GAP_LABS_OVERDUE, "missing": ["hemoglobin a1c"], "days_since_last": 214},
    ]


def test_payload_omits_med_class_for_a_condition_only_patient() -> None:
    payload = build_payload(GAPS, on_glp1_medication=False, weeks_since_last_visit=None)

    assert "med_class" not in payload
    assert "weeks_since_last_visit" not in payload


def test_payload_contains_no_identifiers() -> None:
    payload = build_payload(GAPS, on_glp1_medication=True, weeks_since_last_visit=12)
    serialized = json.dumps(payload).lower()

    for identifier in (
        "patient",
        "mrn",
        "dob",
        "birth",
        "name",
        "address",
        "phone",
        "email",
        "provider",
        "staff",
        "note",
        "semaglutide",
        "id",
    ):
        assert identifier not in serialized


def test_payload_never_carries_the_raw_gap_label() -> None:
    # Labels are rendered to the clinician but are free text; only `detail`
    # scalars are safe to transmit.
    payload = build_payload(GAPS, on_glp1_medication=True, weeks_since_last_visit=None)
    serialized = json.dumps(payload)

    for gap in GAPS:
        assert gap.label not in serialized


def test_prompt_sent_to_the_model_is_exactly_the_payload() -> None:
    client = Mock()
    client.request.return_value = Mock(code=HTTPStatus.OK, response="A sentence.")

    with patch("canvas_sdk.clients.llms.LlmAnthropic", return_value=client):
        generate_rationale(GAPS, True, 12, LLM_CONFIG)

    (user_prompt,), _ = client.set_user_prompt.call_args
    assert json.loads(user_prompt[0]) == build_payload(GAPS, True, 12)


def test_model_response_is_used_when_the_call_succeeds() -> None:
    client = Mock()
    client.request.return_value = Mock(
        code=HTTPStatus.OK, response="Weight check is 58 days overdue."
    )

    with patch("canvas_sdk.clients.llms.LlmAnthropic", return_value=client):
        narrative = generate_rationale(GAPS, True, 12, LLM_CONFIG)

    assert narrative == "Weight check is 58 days overdue."


def test_http_error_falls_back_to_the_template() -> None:
    client = Mock()
    client.request.return_value = Mock(
        code=HTTPStatus.INTERNAL_SERVER_ERROR, response="boom"
    )

    with patch("canvas_sdk.clients.llms.LlmAnthropic", return_value=client):
        narrative = generate_rationale(GAPS, True, 12, LLM_CONFIG)

    assert narrative == fallback_sentence(GAPS, 12)


def test_an_exception_falls_back_to_the_template() -> None:
    with patch("canvas_sdk.clients.llms.LlmAnthropic", side_effect=RuntimeError("timeout")):
        narrative = generate_rationale(GAPS, True, 12, LLM_CONFIG)

    assert narrative == fallback_sentence(GAPS, 12)


def test_an_empty_model_response_falls_back_to_the_template() -> None:
    client = Mock()
    client.request.return_value = Mock(code=HTTPStatus.OK, response="   ")

    with patch("canvas_sdk.clients.llms.LlmAnthropic", return_value=client):
        narrative = generate_rationale(GAPS, True, 12, LLM_CONFIG)

    assert narrative == fallback_sentence(GAPS, 12)


def test_a_missing_api_key_skips_the_model_entirely() -> None:
    with patch("canvas_sdk.clients.llms.LlmAnthropic") as client:
        narrative = generate_rationale(GAPS, True, 12, Config.from_secrets({}))

    client.assert_not_called()
    assert narrative == fallback_sentence(GAPS, 12)


def test_the_kill_switch_skips_the_model_entirely() -> None:
    config = Config.from_secrets(
        {"ANTHROPIC_API_KEY": "sk-test", "ENABLE_LLM_RATIONALE": "false"}
    )

    with patch("canvas_sdk.clients.llms.LlmAnthropic") as client:
        narrative = generate_rationale(GAPS, True, 12, config)

    client.assert_not_called()
    assert narrative == fallback_sentence(GAPS, 12)


def test_no_gaps_skips_the_model_entirely() -> None:
    with patch("canvas_sdk.clients.llms.LlmAnthropic") as client:
        narrative = generate_rationale([], True, 12, LLM_CONFIG)

    client.assert_not_called()
    assert narrative == "GLP-1 monitoring is up to date; no open care gaps."


def test_fallback_describes_every_gap() -> None:
    sentence = fallback_sentence(GAPS, 12)

    assert "58 days" in sentence
    assert "hemoglobin a1c" in sentence
    assert "12 weeks ago" in sentence


def test_fallback_handles_a_never_recorded_weight() -> None:
    gap = Gap(key=GAP_STALE_WEIGHT, label="No weight ever recorded", detail={"days_since_last": None})

    assert "no weight ever recorded" in fallback_sentence([gap], None)


def test_fallback_handles_a_missing_followup() -> None:
    gap = Gap(key=GAP_NO_FOLLOWUP, label="No follow-up", detail={"horizon_days": 90})

    assert "no follow-up booked in the next 90 days" in fallback_sentence([gap], None)


def test_fallback_degrades_when_gap_detail_is_incomplete() -> None:
    # Defensive: a gap whose detail lost its scalars still yields a sentence.
    followup = Gap(key=GAP_NO_FOLLOWUP, label="No follow-up", detail={})
    labs = Gap(key=GAP_LABS_OVERDUE, label="Labs overdue", detail={})
    unknown = Gap(key="future_rule", label="Something Else Is Due", detail={})

    sentence = fallback_sentence([followup, labs, unknown], None)

    assert "no follow-up booked" in sentence
    assert "expected labs not resulted" in sentence
    assert "something else is due" in sentence


def test_sanitize_flattens_whitespace_and_markdown() -> None:
    assert sanitize("  - **Weight**\n  is  overdue.  ") == "Weight is overdue."


def test_sanitize_caps_length() -> None:
    result = sanitize("word " * 500)

    assert len(result) <= MAX_RATIONALE_CHARS + 1
    assert result.endswith("…")


def test_sanitize_handles_empty_input() -> None:
    assert sanitize("") == ""
