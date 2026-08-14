"""The card's narrative sentence — deterministic, offline, and non-actionable."""

from glp1_care_gap_copilot.gaps import (
    GAP_LABS_OVERDUE,
    GAP_NO_FOLLOWUP,
    GAP_SAFETY_REVIEW,
    GAP_STALE_WEIGHT,
    Gap,
)
from glp1_care_gap_copilot.rationale import build_narrative

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


def test_narrative_describes_every_gap() -> None:
    sentence = build_narrative(GAPS, 12)

    assert "58 days" in sentence
    assert "hemoglobin a1c" in sentence
    assert "12 weeks ago" in sentence


def test_narrative_is_a_single_sentence() -> None:
    sentence = build_narrative(GAPS, 12)

    assert sentence.endswith(".")
    assert sentence.count(".") == 1


def test_narrative_is_deterministic() -> None:
    assert build_narrative(GAPS, 12) == build_narrative(GAPS, 12)


def test_narrative_states_facts_without_recommending() -> None:
    """The sentence restates findings; it must not tell the clinician what to do."""
    sentence = build_narrative(GAPS, 12).lower()

    for directive in ("should", "recommend", "consider", "start", "increase", "order "):
        assert directive not in sentence


def test_no_gaps_reads_as_up_to_date() -> None:
    assert build_narrative([], 4) == "GLP-1 monitoring is up to date; no open care gaps."


def test_last_visit_is_omitted_when_unknown() -> None:
    sentence = build_narrative(GAPS, None)

    assert "last visit" not in sentence
    assert sentence.endswith(".")


def test_narrative_handles_a_never_recorded_weight() -> None:
    gap = Gap(
        key=GAP_STALE_WEIGHT,
        label="No weight ever recorded",
        detail={"days_since_last": None},
    )

    assert "no weight ever recorded" in build_narrative([gap], None)


def test_narrative_handles_a_missing_followup() -> None:
    gap = Gap(key=GAP_NO_FOLLOWUP, label="No follow-up", detail={"horizon_days": 90})

    assert "no follow-up booked in the next 90 days" in build_narrative([gap], None)


def test_narrative_degrades_when_gap_detail_is_incomplete() -> None:
    # Defensive: a gap whose detail lost its scalars still yields a sentence.
    followup = Gap(key=GAP_NO_FOLLOWUP, label="No follow-up", detail={})
    labs = Gap(key=GAP_LABS_OVERDUE, label="Labs overdue", detail={})
    unknown = Gap(key="future_rule", label="Something Else Is Due", detail={})

    sentence = build_narrative([followup, labs, unknown], None)

    assert "no follow-up booked" in sentence
    assert "expected labs not resulted" in sentence
    assert "something else is due" in sentence


def test_narrative_makes_no_network_call() -> None:
    """The module must stay offline — no client, no key, no egress."""
    import glp1_care_gap_copilot.rationale as rationale

    source = rationale.__doc__ or ""
    assert "Deterministic and offline" in source
    assert not hasattr(rationale, "generate_rationale")
    assert not hasattr(rationale, "build_payload")


# --- the safety sentence ------------------------------------------------------

SAFETY_GAP = Gap(
    key=GAP_SAFETY_REVIEW,
    label="Rapid weight loss (9 lb in 7d) with nausea — contact patient",
    detail={
        "drop_lb": 9.0,
        "interval_days": 7,
        "findings": ["gi_symptoms"],
        "labels": ["Persistent nausea, vomiting, or diarrhea"],
    },
)


def test_a_safety_signal_leads_with_its_own_sentence() -> None:
    sentence = build_narrative([SAFETY_GAP, *GAPS], None)

    # "Contact this patient" and "labs are overdue" do not belong in the same
    # breath, so the safety finding gets its own clause up front.
    assert sentence.startswith("SAFETY: 9 lb lost in 7 days alongside ")
    assert "Open GLP-1 monitoring gaps:" in sentence


def test_a_lone_safety_signal_needs_no_monitoring_clause() -> None:
    sentence = build_narrative([SAFETY_GAP], None)

    assert sentence == (
        "SAFETY: 9 lb lost in 7 days alongside persistent nausea, vomiting, "
        "or diarrhea."
    )
    assert "monitoring gaps" not in sentence


def test_a_lone_safety_signal_still_reports_the_last_visit() -> None:
    sentence = build_narrative([SAFETY_GAP], 6)

    assert sentence.endswith("(last visit 6 weeks ago).")


def test_a_safety_gap_stripped_of_its_scalars_still_reads() -> None:
    # Defensive: the sentence degrades rather than raising if detail is lost.
    bare = Gap(key=GAP_SAFETY_REVIEW, label="Rapid weight loss", detail={})

    sentence = build_narrative([bare], None)

    assert sentence == "SAFETY: rapid weight loss alongside warning signs."
