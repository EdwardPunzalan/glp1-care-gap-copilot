"""Gap detection rules, exercised on both sides of every threshold."""

from datetime import datetime

from canvas_sdk.test_utils.factories import (
    LabReportFactory,
    LabTestFactory,
    LabValueCodingFactory,
    LabValueFactory,
    PatientFactory,
)
from canvas_sdk.v1.data.appointment import AppointmentProgressStatus
from canvas_sdk.v1.data.patient import Patient

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import (
    GAP_LABS_OVERDUE,
    GAP_NO_FOLLOWUP,
    GAP_STALE_WEIGHT,
    Gap,
    detect_gaps,
    expected_lab_names,
    weeks_since_last_visit,
)
from tests.factories import (
    add_appointment,
    add_condition,
    add_observation,
    days_ago,
    days_ahead,
    now,
)

# Only the weight rule is under test in most cases; disable the others by
# satisfying them, so each assertion isolates one rule.
CONFIG = Config.from_secrets({"REQUIRED_LAB_NAMES": "hemoglobin a1c"})


def gap_keys(patient_id: str, config: Config = CONFIG) -> set[str]:
    """Keys of every gap detected for a patient right now."""
    return {gap.key for gap in detect_gaps(patient_id, config, now())}


def find_gap(patient_id: str, key: str, config: Config = CONFIG) -> Gap | None:
    """The detected gap with the given key, or None."""
    return next(
        (gap for gap in detect_gaps(patient_id, config, now()) if gap.key == key), None
    )


def add_lab_result(patient: Patient, lab_name: str, performed_at: datetime) -> None:
    """Record a resulted lab of the given name for the patient."""
    report = LabReportFactory.create(
        patient=patient, date_performed=performed_at, deleted=False
    )
    test = LabTestFactory.create(report=report, ontology_test_name=lab_name)
    LabValueFactory.create(report=report, test=test, value="5.4", units="%")


def diabetic_patient() -> Patient:
    """A patient for whom A1c is an expected lab under the default rules."""
    patient = PatientFactory.create()
    add_condition(patient, "E11.9", display="Type 2 diabetes mellitus")
    return patient


# --- Stale weight -----------------------------------------------------------


def test_weight_recorded_inside_the_interval_is_not_a_gap() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(29))

    assert GAP_STALE_WEIGHT not in gap_keys(str(patient.id))


def test_weight_exactly_at_the_threshold_is_not_yet_a_gap() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(30))

    assert GAP_STALE_WEIGHT not in gap_keys(str(patient.id))


def test_weight_one_day_past_the_threshold_is_a_gap() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(31))

    gap = find_gap(str(patient.id), GAP_STALE_WEIGHT)

    assert gap is not None
    assert gap.detail["days_since_last"] == 31
    assert "31 days" in gap.label


def test_only_the_most_recent_weight_counts() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(200))
    add_observation(patient, "weight", days_ago(5))

    assert GAP_STALE_WEIGHT not in gap_keys(str(patient.id))


def test_a_recent_bmi_satisfies_the_weight_rule() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "bmi", days_ago(3), value="34")

    assert GAP_STALE_WEIGHT not in gap_keys(str(patient.id))


def test_no_weight_ever_recorded_is_a_gap() -> None:
    patient = PatientFactory.create()

    gap = find_gap(str(patient.id), GAP_STALE_WEIGHT)

    assert gap is not None
    assert gap.detail["days_since_last"] is None
    assert gap.label == "No weight ever recorded"


def test_a_shorter_configured_interval_flags_sooner() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(20))
    config = Config.from_secrets(
        {"WEIGHT_CHECK_INTERVAL_DAYS": "14", "REQUIRED_LAB_NAMES": "hemoglobin a1c"}
    )

    assert GAP_STALE_WEIGHT in gap_keys(str(patient.id), config)


def test_another_patients_weight_does_not_satisfy_the_rule() -> None:
    patient = PatientFactory.create()
    other = PatientFactory.create()
    add_observation(other, "weight", days_ago(1))

    assert GAP_STALE_WEIGHT in gap_keys(str(patient.id))


# --- Labs overdue -----------------------------------------------------------


def test_lab_resulted_inside_the_interval_is_not_a_gap() -> None:
    patient = diabetic_patient()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(89))

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_lab_exactly_at_the_threshold_is_not_yet_a_gap() -> None:
    patient = diabetic_patient()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(90))

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_lab_one_day_past_the_threshold_is_a_gap() -> None:
    patient = diabetic_patient()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(91))

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["missing"] == ["hemoglobin a1c"]
    assert gap.detail["days_since_last"] == 91


def test_never_resulted_lab_is_a_gap_with_no_staleness() -> None:
    patient = diabetic_patient()

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["missing"] == ["hemoglobin a1c"]
    assert gap.detail["days_since_last"] is None


def test_lab_matched_by_result_coding_rather_than_test_name() -> None:
    patient = diabetic_patient()
    report = LabReportFactory.create(
        patient=patient, date_performed=days_ago(5), deleted=False
    )
    test = LabTestFactory.create(report=report, ontology_test_name="Panel 1234")
    value = LabValueFactory.create(report=report, test=test, value="5.4", units="%")
    LabValueCodingFactory.create(value=value, name="Hemoglobin A1c", code="4548-4")

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_each_expected_lab_is_evaluated_independently() -> None:
    patient = PatientFactory.create()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(5))
    config = Config.from_secrets(
        {"REQUIRED_LAB_NAMES": "hemoglobin a1c,lipid panel", "DIABETES_ONLY_LAB_NAMES": "none"}
    )

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE, config)

    assert gap is not None
    # With the conditional rule cleared by the sentinel, A1c applies to every
    # patient — and it is satisfied here, so only the lipid panel is missing.
    assert gap.detail["missing"] == ["lipid panel"]


def test_a1c_is_only_expected_for_a_diabetic_patient() -> None:
    patient = PatientFactory.create()
    config = Config.from_secrets({"REQUIRED_LAB_NAMES": "hemoglobin a1c"})

    assert expected_lab_names(str(patient.id), config) == ()

    add_condition(patient, "E11.9", display="Type 2 diabetes mellitus")

    assert expected_lab_names(str(patient.id), config) == ("hemoglobin a1c",)


def test_unconditional_labs_apply_regardless_of_diagnosis() -> None:
    patient = PatientFactory.create()
    config = Config.from_secrets({"REQUIRED_LAB_NAMES": "lipid panel"})

    assert expected_lab_names(str(patient.id), config) == ("lipid panel",)


# --- Follow-up --------------------------------------------------------------


def test_appointment_inside_the_horizon_satisfies_the_rule() -> None:
    patient = PatientFactory.create()
    add_appointment(patient, days_ahead(30))

    assert GAP_NO_FOLLOWUP not in gap_keys(str(patient.id))


def test_appointment_beyond_the_horizon_is_still_a_gap() -> None:
    patient = PatientFactory.create()
    add_appointment(patient, days_ahead(120))

    assert GAP_NO_FOLLOWUP in gap_keys(str(patient.id))


def test_cancelled_appointment_does_not_satisfy_the_rule() -> None:
    patient = PatientFactory.create()
    add_appointment(patient, days_ahead(10), status=AppointmentProgressStatus.CANCELLED)

    assert GAP_NO_FOLLOWUP in gap_keys(str(patient.id))


def test_past_appointment_does_not_satisfy_the_rule() -> None:
    patient = PatientFactory.create()
    add_appointment(patient, days_ago(10))

    assert GAP_NO_FOLLOWUP in gap_keys(str(patient.id))


def test_no_followup_gap_reports_the_configured_horizon() -> None:
    patient = PatientFactory.create()

    gap = find_gap(str(patient.id), GAP_NO_FOLLOWUP)

    assert gap is not None
    assert gap.detail["horizon_days"] == 90


# --- Last visit -------------------------------------------------------------


def test_weeks_since_last_visit_counts_whole_weeks() -> None:
    patient = PatientFactory.create()
    add_appointment(patient, days_ago(20))

    assert weeks_since_last_visit(str(patient.id), now()) == 2


def test_weeks_since_last_visit_ignores_no_shows_and_future_visits() -> None:
    patient = PatientFactory.create()
    add_appointment(patient, days_ago(3), status=AppointmentProgressStatus.NOSHOWED)
    add_appointment(patient, days_ahead(3))

    assert weeks_since_last_visit(str(patient.id), now()) is None


def test_weeks_since_last_visit_is_none_for_a_patient_who_never_visited() -> None:
    patient = PatientFactory.create()

    assert weeks_since_last_visit(str(patient.id), now()) is None


# --- All clear --------------------------------------------------------------


def test_a_fully_monitored_patient_has_no_gaps() -> None:
    patient = diabetic_patient()
    add_observation(patient, "weight", days_ago(2))
    add_lab_result(patient, "Hemoglobin A1c", days_ago(10))
    add_appointment(patient, days_ahead(14))

    assert detect_gaps(str(patient.id), CONFIG, now()) == []
