"""Deterministic care-gap detection.

Every rule here is a threshold comparison against chart data. Nothing in this
module writes to the chart — it answers "what is overdue, and by how long", and
the answer is reproducible from the same inputs.

Query budget per render is fixed: one weight lookup, one lookup per configured
required lab, one future-appointment existence check, one last-visit lookup,
plus at most one diabetes-condition check. It does not grow with the size of the
patient's chart.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

from django.db.models import Q

from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.lab import LabValue
from canvas_sdk.v1.data.observation import Observation

from glp1_care_gap_copilot.cohort import has_active_condition_with_prefixes
from glp1_care_gap_copilot.config import Config

GAP_STALE_WEIGHT = "stale_weight"
GAP_LABS_OVERDUE = "labs_overdue"
GAP_NO_FOLLOWUP = "no_followup"
#: Not an overdue-monitoring gap like the others — a safety signal. It is
#: carried as a Gap so it inherits the card row, the outreach button, and the
#: open-task dedupe for free, but it is detected in `safety_signals` and always
#: sorts to the front of the card.
GAP_SAFETY_REVIEW = "safety_review"

WEIGHT_OBSERVATION_NAMES = ("weight", "bmi")


@dataclass(frozen=True)
class Gap:
    """One open care gap.

    `detail` holds only derived scalars — day counts and lab names — which are
    what the narrative sentence is assembled from.
    """

    key: str
    label: str
    detail: dict[str, Any] = field(default_factory=dict)


def _days_since(moment: datetime | None, now: datetime) -> int | None:
    if moment is None:
        return None
    return max((now - moment).days, 0)


def latest_weight_datetime(patient_id: str) -> datetime | None:
    """Most recent weight or BMI observation datetime, or None if never recorded."""
    name_query = Q()
    for name in WEIGHT_OBSERVATION_NAMES:
        name_query |= Q(name__iexact=name)
    return cast(
        datetime | None,
        Observation.objects.for_patient(patient_id)
        .filter(
            name_query,
            deleted=False,
            entered_in_error__isnull=True,
            effective_datetime__isnull=False,
        )
        .order_by("-effective_datetime")
        .values_list("effective_datetime", flat=True)
        .first(),
    )


def latest_lab_datetime(patient_id: str, lab_name: str) -> datetime | None:
    """Most recent result date for a lab, matched by result coding or test name."""
    return cast(
        datetime | None,
        LabValue.objects.filter(
            Q(codings__name__icontains=lab_name)
            | Q(test__ontology_test_name__icontains=lab_name),
            report__patient__id=patient_id,
            report__deleted=False,
            report__entered_in_error__isnull=True,
            report__date_performed__isnull=False,
        )
        .order_by("-report__date_performed")
        .values_list("report__date_performed", flat=True)
        .first(),
    )


def has_upcoming_appointment(patient_id: str, now: datetime, horizon_days: int) -> bool:
    """Whether a non-cancelled appointment is booked within the horizon."""
    horizon = now + timedelta(days=horizon_days)
    return cast(
        bool,
        Appointment.objects.filter(
            patient__id=patient_id,
            entered_in_error__isnull=True,
            start_time__gte=now,
            start_time__lte=horizon,
        )
        .exclude(status=AppointmentProgressStatus.CANCELLED)
        .exists(),
    )


def last_visit_datetime(patient_id: str, now: datetime) -> datetime | None:
    """Start time of the most recent past appointment the patient attended."""
    return cast(
        datetime | None,
        Appointment.objects.filter(
            patient__id=patient_id,
            entered_in_error__isnull=True,
            start_time__lt=now,
        )
        .exclude(
            status__in=(
                AppointmentProgressStatus.CANCELLED,
                AppointmentProgressStatus.NOSHOWED,
            )
        )
        .order_by("-start_time")
        .values_list("start_time", flat=True)
        .first(),
    )


def expected_lab_names(patient_id: str, config: Config) -> tuple[str, ...]:
    """The labs this patient is expected to have, by explicit rule.

    Labs listed in `diabetes_only_lab_names` are expected only when the patient
    carries a matching diagnosis; every other configured lab always applies.
    """
    conditional = {name.lower() for name in config.diabetes_only_lab_names}
    unconditional = tuple(
        name for name in config.required_lab_names if name.lower() not in conditional
    )
    requested_conditional = tuple(
        name for name in config.required_lab_names if name.lower() in conditional
    )
    if not requested_conditional:
        return unconditional
    if has_active_condition_with_prefixes(patient_id, config.diabetes_icd10_prefixes):
        return config.required_lab_names
    return unconditional


def _stale_weight_gap(patient_id: str, config: Config, now: datetime) -> Gap | None:
    last_weight = latest_weight_datetime(patient_id)
    days = _days_since(last_weight, now)
    if days is None:
        return Gap(
            key=GAP_STALE_WEIGHT,
            label="No weight ever recorded",
            detail={"days_since_last": None},
        )
    if days <= config.weight_check_interval_days:
        return None
    return Gap(
        key=GAP_STALE_WEIGHT,
        label=f"No weight recorded in {days} days",
        detail={"days_since_last": days},
    )


def _labs_overdue_gap(patient_id: str, config: Config, now: datetime) -> Gap | None:
    expected = expected_lab_names(patient_id, config)
    missing: list[str] = []
    staleness: list[int] = []
    for lab_name in expected:
        last_result = latest_lab_datetime(patient_id, lab_name)
        days = _days_since(last_result, now)
        if days is None:
            missing.append(lab_name)
        elif days > config.lab_interval_days:
            missing.append(lab_name)
            staleness.append(days)
    if not missing:
        return None
    readable = ", ".join(missing)
    return Gap(
        key=GAP_LABS_OVERDUE,
        label=f"Labs overdue before dose increase: {readable}",
        detail={
            "missing": missing,
            "days_since_last": max(staleness) if staleness else None,
        },
    )


def _no_followup_gap(patient_id: str, config: Config, now: datetime) -> Gap | None:
    if has_upcoming_appointment(patient_id, now, config.followup_horizon_days):
        return None
    return Gap(
        key=GAP_NO_FOLLOWUP,
        label=(
            "No follow-up appointment scheduled in the next "
            f"{config.followup_horizon_days} days"
        ),
        detail={"horizon_days": config.followup_horizon_days},
    )


def detect_gaps(patient_id: str, config: Config, now: datetime) -> list[Gap]:
    """Evaluate every gap rule for the patient, newest thresholds applied."""
    candidates = (
        _stale_weight_gap(patient_id, config, now),
        _labs_overdue_gap(patient_id, config, now),
        _no_followup_gap(patient_id, config, now),
    )
    return [gap for gap in candidates if gap is not None]


def weeks_since_last_visit(patient_id: str, now: datetime) -> int | None:
    """Whole weeks since the patient's last attended visit, or None if never."""
    days = _days_since(last_visit_datetime(patient_id, now), now)
    if days is None:
        return None
    return days // 7
