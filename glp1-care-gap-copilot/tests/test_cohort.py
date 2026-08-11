"""Cohort eligibility — who the copilot speaks up about, and who it ignores."""

from canvas_sdk.test_utils.factories import PatientFactory
from canvas_sdk.v1.data.condition import ClinicalStatus
from canvas_sdk.v1.data.medication import Status

from glp1_care_gap_copilot.cohort import (
    evaluate_cohort,
    has_active_condition_with_prefixes,
    has_active_glp1_medication,
)
from glp1_care_gap_copilot.config import Config
from tests.factories import add_condition, add_medication

CONFIG = Config.from_secrets({})


def test_patient_on_a_glp1_is_in_scope() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG/0.5ML Pen Injector")

    match = evaluate_cohort(str(patient.id), CONFIG)

    assert match.in_scope is True
    assert match.on_glp1_medication is True


def test_medication_matching_is_case_insensitive() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "TIRZEPATIDE 2.5 MG")

    assert evaluate_cohort(str(patient.id), CONFIG).on_glp1_medication is True


def test_brand_name_medications_are_in_scope() -> None:
    # Canvas stores the brand in the coding display ("Ozempic 4 mg tablet"), so
    # a generics-only fragment list misses nearly every real GLP-1 patient.
    # Found on a live instance: an Ozempic patient scored out of scope.
    for brand in ("Ozempic 4 mg tablet", "Wegovy 2.4 MG/0.75ML Pen", "Mounjaro 5 MG/0.5ML"):
        patient = PatientFactory.create()
        add_medication(patient, brand)

        assert evaluate_cohort(str(patient.id), CONFIG).on_glp1_medication is True, brand


def test_inactive_glp1_does_not_put_patient_in_scope() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG", status=Status.INACTIVE)

    assert evaluate_cohort(str(patient.id), CONFIG).in_scope is False


def test_obesity_condition_alone_puts_patient_in_scope() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E66.01", display="Morbid obesity")

    match = evaluate_cohort(str(patient.id), CONFIG)

    assert match.in_scope is True
    assert match.on_glp1_medication is False
    assert match.has_obesity_condition is True


def test_undotted_icd10_codes_still_match_a_dotted_prefix() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "Z6841", display="BMI 41.0-41.9")

    assert evaluate_cohort(str(patient.id), CONFIG).has_obesity_condition is True


def test_resolved_obesity_condition_does_not_put_patient_in_scope() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E66.01", clinical_status=ClinicalStatus.RESOLVED)

    assert evaluate_cohort(str(patient.id), CONFIG).in_scope is False


def test_unrelated_patient_is_out_of_scope() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Lisinopril 10 MG")
    add_condition(patient, "I10", display="Hypertension")

    match = evaluate_cohort(str(patient.id), CONFIG)

    assert match.in_scope is False


def test_another_patients_records_do_not_leak_into_scope() -> None:
    patient = PatientFactory.create()
    other = PatientFactory.create()
    add_medication(other, "Semaglutide 0.5 MG")

    assert evaluate_cohort(str(patient.id), CONFIG).in_scope is False


def test_blank_configured_fragments_match_nothing_rather_than_everything() -> None:
    # Defensive: a list of only blanks must not collapse into an empty Q(),
    # which would match every medication and put the whole panel in scope.
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")

    assert has_active_glp1_medication(str(patient.id), ("", "  ")) is False


def test_blank_configured_prefixes_match_nothing_rather_than_everything() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E66.01")

    assert has_active_condition_with_prefixes(str(patient.id), ("", "  ")) is False
    assert has_active_condition_with_prefixes(str(patient.id), ()) is False


def test_configured_fragments_replace_the_defaults() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    config = Config.from_secrets({"GLP1_MED_NAME_FRAGMENTS": "retatrutide"})

    assert evaluate_cohort(str(patient.id), config).on_glp1_medication is False
