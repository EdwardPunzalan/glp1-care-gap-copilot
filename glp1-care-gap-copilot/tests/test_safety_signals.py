"""The rapid-loss-plus-warning-sign safety rule."""

from datetime import date

import pytest
from canvas_sdk.test_utils.factories import PatientFactory

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.safety_signals import (
    BANNER_NARRATIVE_LIMIT,
    FINDINGS,
    banner_narrative,
    evaluate_safety,
    latest_rapid_drop,
    safety_check_gap,
    safety_check_is_due,
    safety_gap,
)
from tests.factories import (
    add_condition,
    add_medication,
    add_weight,
    complete_safety_check,
    days_ago,
)

CONFIG = Config.from_secrets({})


def glp1_patient() -> PatientFactory:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    return patient


def rapid_loss_patient() -> PatientFactory:
    """A patient whose newest interval is a 9 lb drop over 7 days."""
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(21))
    add_weight(patient, 248.0, days_ago(14))
    add_weight(patient, 239.0, days_ago(7))
    return patient


def gradual_loss_patient() -> PatientFactory:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(60))
    add_weight(patient, 244.0, days_ago(30))
    add_weight(patient, 240.0, days_ago(7))
    return patient


# --- the drop half of the rule -----------------------------------------------


def test_no_weights_means_no_drop_to_pair_with() -> None:
    assert latest_rapid_drop(str(glp1_patient().id), CONFIG) is None


def test_gradual_loss_is_not_a_rapid_drop() -> None:
    assert latest_rapid_drop(str(gradual_loss_patient().id), CONFIG) is None


def test_the_drop_carries_the_dates_it_happened_between() -> None:
    drop = latest_rapid_drop(str(rapid_loss_patient().id), CONFIG)

    assert drop is not None
    assert drop.drop == pytest.approx(9.0)
    assert drop.interval_days == 7
    # The segment has to know *when*, not just where on the x axis, or the
    # window cannot be anchored.
    assert drop.ended > drop.started


def test_the_newest_drop_wins_when_there_are_several() -> None:
    patient = glp1_patient()
    add_weight(patient, 280.0, days_ago(28))
    add_weight(patient, 264.0, days_ago(21))  # older rapid drop
    add_weight(patient, 262.0, days_ago(14))
    add_weight(patient, 250.0, days_ago(7))  # newest rapid drop

    drop = latest_rapid_drop(str(patient.id), CONFIG)

    assert drop is not None
    assert drop.drop == pytest.approx(12.0)


# --- the findings half, from the questionnaire -------------------------------


def test_a_rapid_drop_alone_does_not_trigger() -> None:
    signal = evaluate_safety(str(rapid_loss_patient().id), CONFIG)

    assert signal.drop is not None
    assert signal.triggered is False
    assert signal.findings == ()


def test_one_positive_answer_triggers_the_rule() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(5))

    signal = evaluate_safety(str(patient.id), CONFIG)

    assert signal.triggered is True
    assert signal.labels == ["Persistent nausea, vomiting, or diarrhea"]
    assert signal.screened is True


def test_an_all_negative_form_is_screened_but_not_triggered() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, (), created=days_ago(5))

    signal = evaluate_safety(str(patient.id), CONFIG)

    # "We looked and they are fine" must be distinguishable from "nobody
    # looked" — otherwise the narrative cannot be honest about its blind spot.
    assert signal.screened is True
    assert signal.triggered is False


def test_an_uncommitted_form_is_ignored() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(
        patient, ("GLP1SC_GI",), created=days_ago(5), committed=False
    )

    signal = evaluate_safety(str(patient.id), CONFIG)

    # A half-filled draft is not a clinical assertion.
    assert signal.screened is False
    assert signal.triggered is False


def test_a_form_outside_the_window_does_not_pair_with_the_drop() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(200))

    signal = evaluate_safety(str(patient.id), CONFIG)

    assert signal.triggered is False


def test_findings_are_reported_in_canonical_order() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(
        patient, ("GLP1SC_RUQ", "GLP1SC_INTAKE", "GLP1SC_GI"), created=days_ago(2)
    )

    signal = evaluate_safety(str(patient.id), CONFIG)

    assert signal.labels == [
        "Very poor oral intake",
        "Persistent nausea, vomiting, or diarrhea",
        "Abdominal or RUQ pain suggesting gallbladder disease",
    ]


def test_no_drop_short_circuits_before_reading_the_form() -> None:
    patient = gradual_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(2))

    signal = evaluate_safety(str(patient.id), CONFIG)

    # Warning signs without rapid loss are not this rule's business.
    assert signal.triggered is False
    assert signal.drop is None


# --- the findings half, from coded conditions --------------------------------


def test_a_coded_condition_triggers_without_any_form() -> None:
    patient = rapid_loss_patient()
    add_condition(patient, "E86.0", display="Dehydration", onset_date=date.today())

    signal = evaluate_safety(str(patient.id), CONFIG)

    assert signal.triggered is True
    assert signal.screened is False
    assert signal.labels == ["Dehydration"]


def test_undotted_icd10_codes_match_too() -> None:
    patient = rapid_loss_patient()
    add_condition(patient, "R112", display="Nausea with vomiting", onset_date=date.today())

    assert evaluate_safety(str(patient.id), CONFIG).triggered is True


def test_an_old_condition_does_not_pair_with_a_recent_drop() -> None:
    patient = rapid_loss_patient()
    add_condition(
        patient, "E86.0", display="Dehydration", onset_date=date(2020, 1, 1)
    )

    assert evaluate_safety(str(patient.id), CONFIG).triggered is False


def test_an_unrelated_condition_is_not_a_finding() -> None:
    patient = rapid_loss_patient()
    add_condition(patient, "J45.909", display="Asthma", onset_date=date.today())

    assert evaluate_safety(str(patient.id), CONFIG).triggered is False


def test_the_same_finding_from_both_sources_counts_once() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_DEHYDRATION",), created=days_ago(2))
    add_condition(patient, "E86.0", display="Dehydration", onset_date=date.today())

    signal = evaluate_safety(str(patient.id), CONFIG)

    assert signal.labels == ["Dehydration"]
    assert len(signal.findings) == 1


# --- configuration ------------------------------------------------------------


def test_the_minimum_finding_count_is_configurable() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(2))
    strict = Config.from_secrets({"SAFETY_MIN_FINDINGS": "2"})

    assert evaluate_safety(str(patient.id), CONFIG).triggered is True
    assert evaluate_safety(str(patient.id), strict).triggered is False


def test_the_window_is_configurable() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(45))

    assert evaluate_safety(str(patient.id), CONFIG).triggered is False
    wide = Config.from_secrets({"SAFETY_WINDOW_DAYS": "90"})
    assert evaluate_safety(str(patient.id), wide).triggered is True


# --- presentation -------------------------------------------------------------


def test_the_card_row_names_the_drop_and_the_findings() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(2))

    gap = safety_gap(evaluate_safety(str(patient.id), CONFIG))

    assert "9 lb in 7d" in gap.label
    assert "nausea" in gap.label
    assert gap.detail["findings"] == ["gi_symptoms"]


def test_a_single_finding_is_named_in_the_banner() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(2))

    narrative = banner_narrative(evaluate_safety(str(patient.id), CONFIG))

    assert "nausea" in narrative
    assert len(narrative) <= BANNER_NARRATIVE_LIMIT


def test_many_findings_are_counted_rather_than_listed() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(
        patient,
        tuple(finding.question_code for finding in FINDINGS),
        created=days_ago(2),
    )

    signal = evaluate_safety(str(patient.id), CONFIG)
    narrative = banner_narrative(signal)

    assert len(signal.findings) == 7
    assert "7 warning signs" in narrative
    # Listing all seven would blow the 90-character cap and get truncated into
    # nonsense by Canvas.
    assert len(narrative) <= BANNER_NARRATIVE_LIMIT


def test_a_long_single_finding_label_is_truncated_not_dropped() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_RUQ",), created=days_ago(2))

    narrative = banner_narrative(evaluate_safety(str(patient.id), CONFIG))

    assert len(narrative) <= BANNER_NARRATIVE_LIMIT
    assert narrative.endswith("…")


def test_safety_gap_refuses_an_untriggered_signal() -> None:
    with pytest.raises(AssertionError):
        safety_gap(evaluate_safety(str(glp1_patient().id), CONFIG))


# --- the screening ask ---------------------------------------------------------


def test_no_drop_means_no_screening_ask() -> None:
    # Screening is prompted by rapid loss, not by the absence of a form.
    signal = evaluate_safety(str(gradual_loss_patient().id), CONFIG)

    assert safety_check_is_due(signal) is False


def test_a_drop_with_no_form_asks_for_screening() -> None:
    signal = evaluate_safety(str(rapid_loss_patient().id), CONFIG)

    assert safety_check_is_due(signal) is True


def test_a_completed_form_settles_the_screening_ask() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, (), created=days_ago(3))

    signal = evaluate_safety(str(patient.id), CONFIG)

    # All-negative still counts as screened — someone looked.
    assert safety_check_is_due(signal) is False


def test_a_form_outside_the_window_does_not_settle_it() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, (), created=days_ago(200))

    assert safety_check_is_due(evaluate_safety(str(patient.id), CONFIG)) is True


def test_a_coded_finding_does_not_replace_the_form() -> None:
    patient = rapid_loss_patient()
    add_condition(patient, "E86.0", display="Dehydration", onset_date=date.today())

    signal = evaluate_safety(str(patient.id), CONFIG)

    # The condition path sees four of seven findings at best, so a diagnosis is
    # never a substitute for the form that covers eating, protein, and muscle.
    assert signal.triggered is True
    assert safety_check_is_due(signal) is True


def test_the_screening_row_names_the_next_visit_when_one_is_booked() -> None:
    signal = evaluate_safety(str(rapid_loss_patient().id), CONFIG)

    gap = safety_check_gap(signal, followup_booked=True)

    assert "screen at next visit" in gap.label
    assert gap.detail["followup_booked"] is True


def test_the_screening_row_says_so_when_no_visit_is_booked() -> None:
    signal = evaluate_safety(str(rapid_loss_patient().id), CONFIG)

    gap = safety_check_gap(signal, followup_booked=False)

    # "At the next visit" is an instruction with nowhere to land, so the row
    # tells the clinician to send the scheduling outreach too.
    assert "no visit booked" in gap.label
    assert gap.detail["followup_booked"] is False


def test_the_screening_row_refuses_a_signal_without_a_drop() -> None:
    signal = evaluate_safety(str(glp1_patient().id), CONFIG)

    with pytest.raises(AssertionError):
        safety_check_gap(signal, followup_booked=True)
