"""Comorbidity detection and lab requirement selection."""

from canvas_sdk.test_utils.factories import PatientFactory

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.labs import (
    LAB_TSH,
    Comorbidities,
    detect_comorbidities,
    lab_interval_days,
    required_labs,
)
from tests.factories import add_condition

CONFIG = Config.from_secrets({})


def test_no_qualifying_diagnosis_is_no_comorbidity() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "J45.909", display="Asthma")

    found = detect_comorbidities(str(patient.id), CONFIG)

    assert found.any is False
    assert found.names == []


def test_every_group_is_detected_independently() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E78.5", display="Hyperlipidemia")
    add_condition(patient, "E11.9", display="Type 2 diabetes")
    add_condition(patient, "R73.03", display="Prediabetes")
    add_condition(patient, "E03.9", display="Hypothyroidism")

    found = detect_comorbidities(str(patient.id), CONFIG)

    assert found.names == [
        "high cholesterol",
        "diabetes",
        "pre-diabetes",
        "hypothyroidism",
    ]


def test_type_1_diabetes_counts_too() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E10.9", display="Type 1 diabetes mellitus")

    assert detect_comorbidities(str(patient.id), CONFIG).diabetes is True


def test_undotted_codes_match() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E039", display="Hypothyroidism, unspecified")

    assert detect_comorbidities(str(patient.id), CONFIG).hypothyroidism is True


def test_a_resolved_condition_does_not_count() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E11.9", clinical_status="resolved", display="T2DM")

    assert detect_comorbidities(str(patient.id), CONFIG).any is False


def test_clearing_every_group_skips_the_lookup_entirely() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E11.9", display="Type 2 diabetes")
    cleared = Config.from_secrets(
        {
            "DIABETES_ICD10_PREFIXES": "none",
            "HIGH_CHOLESTEROL_ICD10_PREFIXES": "none",
            "PREDIABETES_ICD10_PREFIXES": "none",
            "HYPOTHYROID_ICD10_PREFIXES": "none",
        }
    )

    assert detect_comorbidities(str(patient.id), cleared).any is False


def test_clearing_the_thyroid_group_switches_tsh_off() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E03.9", display="Hypothyroidism")
    no_thyroid = Config.from_secrets({"HYPOTHYROID_ICD10_PREFIXES": "none"})

    found = detect_comorbidities(str(patient.id), no_thyroid)

    assert found.hypothyroidism is False
    assert all(lab.key != LAB_TSH for lab in required_labs(found))


# --- requirement selection ----------------------------------------------------


def test_the_base_three_labs_apply_to_everyone() -> None:
    labs = required_labs(Comorbidities())

    assert [lab.key for lab in labs] == [
        "metabolic panel",
        "lipid panel",
        "hemoglobin a1c",
    ]


def test_tsh_is_added_only_for_thyroid_patients() -> None:
    assert required_labs(Comorbidities(diabetes=True))[-1].key != LAB_TSH
    assert required_labs(Comorbidities(hypothyroidism=True))[-1].key == LAB_TSH


def test_a_metabolic_panel_is_satisfied_by_either_panel() -> None:
    metabolic = required_labs(Comorbidities())[0]

    assert metabolic.satisfied_by == (
        "comprehensive metabolic panel",
        "basic metabolic panel",
    )


# --- interval tiers -----------------------------------------------------------


def test_a_comorbidity_selects_the_short_interval() -> None:
    assert lab_interval_days(Comorbidities(prediabetes=True), CONFIG) == 90


def test_obesity_alone_selects_the_long_interval() -> None:
    assert lab_interval_days(Comorbidities(), CONFIG) == 365


def test_both_intervals_are_configurable() -> None:
    config = Config.from_secrets(
        {"LAB_INTERVAL_DAYS": "60", "LAB_INTERVAL_OBESITY_ONLY_DAYS": "180"}
    )

    assert lab_interval_days(Comorbidities(diabetes=True), config) == 60
    assert lab_interval_days(Comorbidities(), config) == 180
