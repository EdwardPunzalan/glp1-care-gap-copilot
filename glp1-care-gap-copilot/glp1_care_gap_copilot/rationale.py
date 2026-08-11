"""The one-sentence narrative shown at the top of the card.

PHI minimization is the whole design constraint here. `build_payload` is the
only thing that ever reaches the model, and it is built by construction from
derived scalars — day counts, lab names, a medication *class* — rather than by
filtering a patient record down. Nothing that identifies a patient can reach the
payload because nothing that identifies a patient is ever put into it.

The card must always render, so every failure path — no key, disabled, HTTP
error, exception, empty response — falls back to a deterministic sentence built
from the same facts.
"""

import re
from http import HTTPStatus

from logger import log

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import (
    GAP_LABS_OVERDUE,
    GAP_NO_FOLLOWUP,
    GAP_STALE_WEIGHT,
    Gap,
)

GLP1_MED_CLASS = "GLP-1 receptor agonist"
MAX_RATIONALE_CHARS = 320
MAX_OUTPUT_TOKENS = 200

SYSTEM_PROMPT = (
    "You summarize care-gap data for a clinician's chart card. "
    "You are given only anonymous derived numbers, never patient data. "
    "Write exactly one factual sentence, at most 30 words, restating which "
    "monitoring items are overdue and by how long. "
    "Do not give clinical advice, do not recommend treatment, do not suggest "
    "medication or dosing changes, and do not speculate about the patient. "
    "Return the sentence only, with no preamble, list, or markdown."
)


def build_payload(
    gaps: list[Gap],
    on_glp1_medication: bool,
    weeks_since_last_visit: int | None,
) -> dict[str, object]:
    """Assemble the PHI-free payload sent to the model.

    Only derived scalars go in. No name, DOB, MRN, patient id, contact info,
    medication name or dose, raw lab value, note text, or provider identity.
    """
    payload: dict[str, object] = {
        "gaps": [{"type": gap.key, **gap.detail} for gap in gaps],
    }
    if on_glp1_medication:
        payload["med_class"] = GLP1_MED_CLASS
    if weeks_since_last_visit is not None:
        payload["weeks_since_last_visit"] = weeks_since_last_visit
    return payload


def _describe(gap: Gap) -> str:
    days = gap.detail.get("days_since_last")
    if gap.key == GAP_STALE_WEIGHT:
        if isinstance(days, int):
            return f"no weight recorded in {days} days"
        return "no weight ever recorded"
    if gap.key == GAP_LABS_OVERDUE:
        missing = gap.detail.get("missing")
        names = ", ".join(missing) if isinstance(missing, list) else "expected labs"
        return f"{names} not resulted within the monitoring interval"
    if gap.key == GAP_NO_FOLLOWUP:
        horizon = gap.detail.get("horizon_days")
        if isinstance(horizon, int):
            return f"no follow-up booked in the next {horizon} days"
        return "no follow-up booked"
    return gap.label.lower()


def fallback_sentence(gaps: list[Gap], weeks_since_last_visit: int | None) -> str:
    """Deterministic narrative used whenever the LLM path is unavailable."""
    if not gaps:
        return "GLP-1 monitoring is up to date; no open care gaps."
    described = "; ".join(_describe(gap) for gap in gaps)
    sentence = f"Open GLP-1 monitoring gaps: {described}."
    if weeks_since_last_visit is not None:
        sentence = f"{sentence[:-1]} (last visit {weeks_since_last_visit} weeks ago)."
    return sentence


def sanitize(text: str) -> str:
    """Reduce a model response to one plain, length-capped sentence."""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    collapsed = re.sub(r"^[\s>*\-#`]+", "", collapsed)
    collapsed = collapsed.replace("`", "").replace("*", "").replace("_", "")
    if not collapsed:
        return ""
    if len(collapsed) > MAX_RATIONALE_CHARS:
        collapsed = collapsed[:MAX_RATIONALE_CHARS].rstrip() + "…"
    return collapsed


def generate_rationale(
    gaps: list[Gap],
    on_glp1_medication: bool,
    weeks_since_last_visit: int | None,
    config: Config,
) -> str:
    """Narrative for the card — model-written when possible, templated otherwise."""
    fallback = fallback_sentence(gaps, weeks_since_last_visit)
    if not gaps or not config.llm_available():
        return fallback

    payload = build_payload(gaps, on_glp1_medication, weeks_since_last_visit)
    try:
        import json

        from canvas_sdk.clients.llms import LlmAnthropic
        from canvas_sdk.clients.llms.structures.settings import LlmSettingsAnthropic

        client = LlmAnthropic(
            LlmSettingsAnthropic(
                api_key=config.anthropic_api_key,
                model=config.llm_model,
                temperature=0.0,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
        )
        client.set_system_prompt([SYSTEM_PROMPT])
        client.set_user_prompt([json.dumps(payload)])
        response = client.request()
    except Exception as error:  # noqa: BLE001 - the card must render regardless
        log.warning(f"[glp1-care-gap-copilot] LLM rationale failed: {error}")
        return fallback

    if response.code != HTTPStatus.OK:
        log.warning(f"[glp1-care-gap-copilot] LLM rationale returned {response.code}")
        return fallback

    sentence = sanitize(str(response.response))
    return sentence or fallback
