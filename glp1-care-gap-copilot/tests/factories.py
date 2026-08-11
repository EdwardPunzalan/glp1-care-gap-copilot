"""Record builders for models the SDK does not yet ship a factory for.

Each helper fills the NOT NULL columns the plugin does not care about so that
tests only state the fields relevant to the rule under test.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from canvas_sdk.test_utils.factories import MedicationFactory
from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.condition import ClinicalStatus, Condition, ConditionCoding
from canvas_sdk.v1.data.medication import MedicationCoding, Status
from canvas_sdk.v1.data.observation import Observation
from canvas_sdk.v1.data.patient import Patient


def now() -> datetime:
    """A stable 'current time' for threshold assertions."""
    return datetime.now(timezone.utc)


def days_ago(count: int) -> datetime:
    """A timestamp `count` days before now."""
    return now() - timedelta(days=count)


def days_ahead(count: int) -> datetime:
    """A timestamp `count` days after now."""
    return now() + timedelta(days=count)


def add_medication(
    patient: Patient, display: str, status: str = Status.ACTIVE, **kwargs: Any
) -> None:
    """Give the patient a medication whose coding displays `display`."""
    medication = MedicationFactory.create(
        patient=patient, status=status, deleted=False, **kwargs
    )
    MedicationCoding.objects.create(
        medication=medication,
        system="http://www.nlm.nih.gov/research/umls/rxnorm",
        code="000000",
        display=display,
        user_selected=False,
    )


def add_condition(
    patient: Patient,
    code: str,
    clinical_status: str = ClinicalStatus.ACTIVE,
    display: str = "condition",
) -> None:
    """Give the patient a condition coded with `code`."""
    condition = Condition.objects.create(
        patient=patient,
        clinical_status=clinical_status,
        deleted=False,
        onset_date=date(2024, 1, 1),
        resolution_date=date(2024, 1, 1),
        surgical=False,
        notes="",
    )
    ConditionCoding.objects.create(
        condition=condition,
        system="ICD-10",
        code=code,
        display=display,
        user_selected=False,
    )


def add_observation(
    patient: Patient, name: str, effective_datetime: datetime, value: str = "200"
) -> None:
    """Record an observation such as a weight or BMI."""
    Observation.objects.create(
        patient=patient,
        name=name,
        value=value,
        units="lbs",
        category="vital-signs",
        deleted=False,
        note_id=0,
        effective_datetime=effective_datetime,
    )


def add_appointment(
    patient: Patient,
    start_time: datetime,
    status: str = AppointmentProgressStatus.CONFIRMED,
) -> Appointment:
    """Book an appointment for the patient."""
    return Appointment.objects.create(
        patient=patient,
        start_time=start_time,
        duration_minutes=30,
        status=status,
        comment="",
        description="",
        telehealth_instructions_sent=False,
    )
