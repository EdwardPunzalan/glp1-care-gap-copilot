"""Query budget for the card render.

The handler runs on every note open and every chart load, for every patient, so
the cost of one render is the cost that matters. These tests pin the query count
so a future change that introduces a per-row query fails here rather than in
production.
"""

from datetime import date
from unittest.mock import Mock

from canvas_sdk.events import EventType
from canvas_sdk.test_utils.factories import (
    LabReportFactory,
    LabTestFactory,
    LabValueFactory,
    PatientFactory,
)
from canvas_sdk.v1.data.patient import Patient
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glp1_care_gap_copilot.handlers.care_gap_handler import GLP1CareGapHandler
from glp1_care_gap_copilot.handlers.weight_trend_handler import (
    SECTION_KEY,
    GLP1WeightTrendSection,
)
from tests.factories import (
    add_appointment,
    add_condition,
    add_medication,
    add_observation,
    add_weight,
    complete_safety_check,
    days_ago,
    days_ahead,
)

SECRETS: dict[str, str] = {}

# Worst realistic case: in scope, past the therapy gate, every gap open. Costs
# one comorbidity lookup plus one per required lab.
MAX_QUERIES_IN_SCOPE = 12
# A thyroid diagnosis adds TSH, so four lab lookups instead of three.
MAX_QUERIES_WITH_TSH = 13
# Out of scope must stay cheap — this is the common case across a whole panel.
MAX_QUERIES_OUT_OF_SCOPE = 2
# The chart summary section: a cohort check plus one windowed weigh-in read.
MAX_QUERIES_TREND_SECTION = 3


def render(patient_id: str) -> int:
    """Run one card render and return the number of queries it issued."""
    event = Mock()
    event.type = EventType.NOTE_OPENED
    event.target = Mock(id=patient_id)
    handler = GLP1CareGapHandler(event=event, secrets=SECRETS)
    with CaptureQueriesContext(connection) as captured:
        handler.compute()
    return len(captured)


def loaded_patient() -> Patient:
    """A patient with enough history that a naive implementation would fan out.

    On therapy for over a year, so the lab rule is past its time-on-therapy
    gate and the budget actually covers the lab lookups rather than
    short-circuiting before them.
    """
    patient = PatientFactory.create()
    add_medication(patient, "Ozempic 4 mg tablet", start_date=days_ago(400))
    add_condition(patient, "E11.9", display="Type 2 diabetes mellitus")
    # History deep enough that a per-row query pattern would show up clearly.
    for days in range(1, 40):
        add_observation(patient, "weight", days_ago(200 + days))
        add_appointment(patient, days_ago(200 + days))
    for days in range(1, 20):
        report = LabReportFactory.create(
            patient=patient, date_performed=days_ago(300 + days), deleted=False
        )
        test = LabTestFactory.create(report=report, ontology_test_name="Hemoglobin A1c")
        LabValueFactory.create(report=report, test=test, value="7.1", units="%")
    return patient


def test_out_of_scope_patient_is_cheap() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Lisinopril 10 MG")

    assert render(str(patient.id)) <= MAX_QUERIES_OUT_OF_SCOPE


def test_full_render_stays_within_budget() -> None:
    patient = loaded_patient()

    assert render(str(patient.id)) <= MAX_QUERIES_IN_SCOPE


def test_query_count_does_not_grow_with_chart_size() -> None:
    """The budget is fixed: a chart with 20x the history costs the same."""
    small = PatientFactory.create()
    # Same time on therapy as the loaded patient — this test isolates chart
    # size, so both sides must be on the same side of the lab rule's gate.
    add_medication(small, "Ozempic 4 mg tablet", start_date=days_ago(400))
    add_condition(small, "E11.9")
    add_observation(small, "weight", days_ago(400))
    add_appointment(small, days_ahead(200))

    large = loaded_patient()

    assert render(str(large.id)) == render(str(small.id))


def render_section(patient_id: str) -> int:
    """Run one weight-trend section render and return the queries it issued."""
    event = Mock()
    event.type = EventType.PATIENT_CHART_SUMMARY__GET_CUSTOM_SECTION
    event.target = Mock(id=patient_id)
    event.context = {"section": SECTION_KEY}
    handler = GLP1WeightTrendSection(event=event, secrets=SECRETS)
    with CaptureQueriesContext(connection) as captured:
        handler.compute()
    return len(captured)


def test_weight_trend_section_stays_within_budget() -> None:
    # Cohort check, then a single windowed read of the weigh-ins.
    assert render_section(str(loaded_patient().id)) <= MAX_QUERIES_TREND_SECTION


def test_weight_trend_section_is_cheap_when_out_of_scope() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Lisinopril 10 MG")
    for days in range(1, 40):
        add_observation(patient, "weight", days_ago(days))

    # Out of scope short-circuits before any weight is read at all.
    assert render_section(str(patient.id)) <= MAX_QUERIES_OUT_OF_SCOPE


def test_weight_trend_query_count_does_not_grow_with_weigh_in_count() -> None:
    """The window is bounded by configuration, not by how often they weigh in."""
    few = PatientFactory.create()
    add_medication(few, "Ozempic 4 mg tablet")
    add_observation(few, "weight", days_ago(10))

    many = loaded_patient()

    assert render_section(str(many.id)) == render_section(str(few.id))


# The safety rule's expensive path. Without a rapid drop it short-circuits at 10
# queries; a flagged drop adds exactly three — committed interviews, their
# responses, and one OR-ed condition lookup covering all seven findings.
MAX_QUERIES_SAFETY_TRIGGERED = 14


def rapid_loss_patient() -> Patient:
    """A loaded chart that also has a flagged drop and a completed safety check."""
    patient = loaded_patient()
    add_weight(patient, 250.0, days_ago(21))
    add_weight(patient, 248.0, days_ago(14))
    add_weight(patient, 239.0, days_ago(7))
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(5))
    return patient


def test_triggered_safety_render_stays_within_budget() -> None:
    assert render(str(rapid_loss_patient().id)) <= MAX_QUERIES_SAFETY_TRIGGERED


def test_safety_query_count_does_not_grow_with_form_count() -> None:
    """Ten completed safety checks cost the same as one."""
    few = rapid_loss_patient()

    many = rapid_loss_patient()
    for days in range(2, 12):
        complete_safety_check(many, ("GLP1SC_GI",), created=days_ago(days))

    assert render(str(many.id)) == render(str(few.id))


def test_safety_query_count_does_not_grow_with_condition_count() -> None:
    """A chart full of coded findings is still one condition query."""
    few = rapid_loss_patient()

    many = rapid_loss_patient()
    for code in ("E86.0", "R11.2", "R53.83", "R10.11", "E44", "M62.84", "R63.0"):
        add_condition(many, code, onset_date=date.today())

    assert render(str(many.id)) == render(str(few.id))


def test_a_thyroid_patient_adds_only_one_more_query() -> None:
    """TSH is a fourth lab lookup, not a second pass over the other three."""
    patient = loaded_patient()
    add_condition(patient, "E03.9", display="Hypothyroidism, unspecified")

    assert render(str(patient.id)) <= MAX_QUERIES_WITH_TSH


def test_a_patient_below_the_therapy_gate_skips_the_lab_queries() -> None:
    """Nothing is expected yet, so no comorbidity or lab lookup is issued."""
    early = PatientFactory.create()
    add_medication(early, "Ozempic 4 mg tablet", start_date=days_ago(10))

    assert render(str(early.id)) < render(str(loaded_patient().id))
