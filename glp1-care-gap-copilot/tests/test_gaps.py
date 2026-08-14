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

from glp1_care_gap_copilot.cohort import evaluate_cohort
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import (
    GAP_LABS_OVERDUE,
    GAP_NO_FOLLOWUP,
    GAP_STALE_WEIGHT,
    Gap,
    detect_gaps,
    weeks_since_last_visit,
)
from tests.factories import (
    add_appointment,
    add_condition,
    add_medication,
    add_observation,
    days_ago,
    days_ahead,
    now,
)

CONFIG = Config.from_secrets({})

# Long enough on therapy to be past the lab rule's time-on-therapy gate.
DAYS_ON_THERAPY = 120


def detected(patient_id: str, config: Config = CONFIG) -> list[Gap]:
    """Every gap detected for a patient right now.

    The cohort is evaluated here rather than stubbed, because the lab rule reads
    the GLP-1 start date out of it.
    """
    moment = now()
    cohort = evaluate_cohort(patient_id, config)
    return detect_gaps(patient_id, config, moment, cohort)


def gap_keys(patient_id: str, config: Config = CONFIG) -> set[str]:
    """Keys of every gap detected for a patient right now."""
    return {gap.key for gap in detected(patient_id, config)}


def find_gap(patient_id: str, key: str, config: Config = CONFIG) -> Gap | None:
    """The detected gap with the given key, or None."""
    return next((gap for gap in detected(patient_id, config) if gap.key == key), None)


def on_therapy(days: int = DAYS_ON_THERAPY) -> Patient:
    """A patient who has been on a GLP-1 for `days` days."""
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG", start_date=days_ago(days))
    return patient


def add_lab_result(patient: Patient, lab_name: str, performed_at: datetime) -> None:
    """Record a resulted lab of the given name for the patient."""
    report = LabReportFactory.create(
        patient=patient, date_performed=performed_at, deleted=False
    )
    test = LabTestFactory.create(report=report, ontology_test_name=lab_name)
    LabValueFactory.create(report=report, test=test, value="5.4", units="%")


def comorbid_patient(days: int = DAYS_ON_THERAPY) -> Patient:
    """On therapy long enough, and carrying a qualifying comorbidity."""
    patient = on_therapy(days)
    add_condition(patient, "E11.9", display="Type 2 diabetes mellitus")
    return patient


def all_labs_current(patient: Patient, days: int = 10) -> None:
    """Result every base lab recently, so the lab rule is satisfied."""
    for name in ("Comprehensive metabolic panel", "Lipid panel", "Hemoglobin A1c"):
        add_lab_result(patient, name, days_ago(days))


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
    config = Config.from_secrets({"WEIGHT_CHECK_INTERVAL_DAYS": "14"})

    assert GAP_STALE_WEIGHT in gap_keys(str(patient.id), config)


def test_another_patients_weight_does_not_satisfy_the_rule() -> None:
    patient = PatientFactory.create()
    other = PatientFactory.create()
    add_observation(other, "weight", days_ago(1))

    assert GAP_STALE_WEIGHT in gap_keys(str(patient.id))


# --- Labs overdue -----------------------------------------------------------


def test_no_labs_expected_before_the_time_on_therapy_gate() -> None:
    # Three weeks in, a metabolic panel measures the diet the patient was on
    # before the drug, so nothing is asked for yet.
    patient = comorbid_patient(days=21)

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_labs_are_expected_once_past_the_gate() -> None:
    patient = comorbid_patient(days=91)

    assert GAP_LABS_OVERDUE in gap_keys(str(patient.id))


def test_the_gate_boundary_itself_qualifies() -> None:
    assert GAP_LABS_OVERDUE in gap_keys(str(comorbid_patient(days=90).id))


def test_a_patient_not_on_a_glp1_is_asked_for_no_labs() -> None:
    # In scope via an obesity diagnosis, but never started on therapy — there
    # is no treatment to monitor.
    patient = PatientFactory.create()
    add_condition(patient, "E66.9", display="Obesity")
    add_condition(patient, "E11.9", display="Type 2 diabetes mellitus")

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_lab_resulted_inside_the_interval_is_not_a_gap() -> None:
    patient = comorbid_patient()
    all_labs_current(patient, days=89)

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_lab_exactly_at_the_threshold_is_not_yet_a_gap() -> None:
    patient = comorbid_patient()
    all_labs_current(patient, days=90)

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_lab_one_day_past_the_threshold_is_a_gap() -> None:
    patient = comorbid_patient()
    all_labs_current(patient, days=91)

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["days_since_last"] == 91
    assert gap.detail["interval_days"] == 90


def test_never_resulted_lab_is_a_gap_with_no_staleness() -> None:
    patient = comorbid_patient()

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["missing_keys"] == ["metabolic panel", "lipid panel", "hemoglobin a1c"]
    assert gap.detail["days_since_last"] is None


def test_lab_matched_by_result_coding_rather_than_test_name() -> None:
    patient = comorbid_patient()
    add_lab_result(patient, "Comprehensive metabolic panel", days_ago(5))
    add_lab_result(patient, "Lipid panel", days_ago(5))
    report = LabReportFactory.create(
        patient=patient, date_performed=days_ago(5), deleted=False
    )
    test = LabTestFactory.create(report=report, ontology_test_name="Panel 1234")
    value = LabValueFactory.create(report=report, test=test, value="5.4", units="%")
    LabValueCodingFactory.create(value=value, name="Hemoglobin A1c", code="4548-4")

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_each_required_lab_is_evaluated_independently() -> None:
    patient = comorbid_patient()
    add_lab_result(patient, "Comprehensive metabolic panel", days_ago(5))
    add_lab_result(patient, "Hemoglobin A1c", days_ago(5))

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["missing_keys"] == ["lipid panel"]


def test_a_bmp_satisfies_the_metabolic_panel_requirement() -> None:
    # "CMP or BMP" — a patient with a recent BMP is not chased for a CMP.
    patient = comorbid_patient()
    add_lab_result(patient, "Basic metabolic panel", days_ago(5))
    add_lab_result(patient, "Lipid panel", days_ago(5))
    add_lab_result(patient, "Hemoglobin A1c", days_ago(5))

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


# --- The two-tier interval ----------------------------------------------------


def test_obesity_alone_is_on_the_yearly_interval() -> None:
    patient = on_therapy()
    add_condition(patient, "E66.9", display="Obesity")
    all_labs_current(patient, days=200)

    # 200 days would be overdue on the 90-day tier, but obesity alone is a
    # slower picture and gets a year.
    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


def test_obesity_alone_is_still_flagged_past_a_year() -> None:
    patient = on_therapy(days=500)
    add_condition(patient, "E66.9", display="Obesity")
    all_labs_current(patient, days=400)

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["interval_days"] == 365
    assert gap.detail["comorbidities"] == []


def test_a_comorbidity_moves_the_patient_to_the_short_interval() -> None:
    patient = on_therapy()
    add_condition(patient, "E66.9", display="Obesity")
    add_condition(patient, "E78.5", display="Hyperlipidemia, unspecified")
    all_labs_current(patient, days=200)

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["interval_days"] == 90
    assert gap.detail["comorbidities"] == ["high cholesterol"]


def test_prediabetes_counts_as_a_comorbidity() -> None:
    patient = on_therapy()
    add_condition(patient, "R73.03", display="Prediabetes")
    all_labs_current(patient, days=200)

    assert GAP_LABS_OVERDUE in gap_keys(str(patient.id))


# --- TSH is thyroid-only ------------------------------------------------------


def test_tsh_is_not_asked_for_without_a_thyroid_diagnosis() -> None:
    patient = comorbid_patient()

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert "tsh" not in gap.detail["missing_keys"]


def test_tsh_is_added_for_a_hypothyroid_patient() -> None:
    patient = on_therapy()
    add_condition(patient, "E03.9", display="Hypothyroidism, unspecified")

    gap = find_gap(str(patient.id), GAP_LABS_OVERDUE)

    assert gap is not None
    assert gap.detail["missing_keys"] == [
        "metabolic panel",
        "lipid panel",
        "hemoglobin a1c",
        "tsh",
    ]
    assert "TSH with reflex to T4" in gap.label


def test_a_recent_tsh_satisfies_the_thyroid_requirement() -> None:
    patient = on_therapy()
    add_condition(patient, "E03.9", display="Hypothyroidism, unspecified")
    all_labs_current(patient, days=5)
    add_lab_result(patient, "TSH", days_ago(5))

    assert GAP_LABS_OVERDUE not in gap_keys(str(patient.id))


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
    patient = comorbid_patient()
    add_observation(patient, "weight", days_ago(2))
    all_labs_current(patient, days=10)
    add_appointment(patient, days_ahead(14))

    assert detected(str(patient.id)) == []
