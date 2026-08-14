"""Cohort eligibility — is this patient one the copilot should say anything about?

Runs first and short-circuits: a patient who is neither on a GLP-1 nor carrying
an obesity-related diagnosis costs two cheap `.exists()` queries and no card.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from django.db.models import Q

from canvas_sdk.v1.data.condition import ClinicalStatus, Condition
from canvas_sdk.v1.data.medication import Medication, Status

from glp1_care_gap_copilot.config import Config


@dataclass(frozen=True)
class CohortMatch:
    """Why (or whether) a patient is in scope for the care-gap card."""

    on_glp1_medication: bool
    has_obesity_condition: bool
    #: When the patient's longest-running active GLP-1 was started. The lab
    #: rule gates on time-on-therapy, and carrying the date here means the
    #: cohort query answers both questions instead of two queries answering one
    #: each.
    glp1_start: datetime | None = None

    @property
    def in_scope(self) -> bool:
        """True when either cohort criterion matched."""
        return self.on_glp1_medication or self.has_obesity_condition

    def days_on_glp1(self, now: datetime) -> int | None:
        """Whole days since the GLP-1 was started, or None if not on one."""
        if self.glp1_start is None:
            return None
        return max((now - self.glp1_start).days, 0)


def _icd10_prefix_query(prefixes: tuple[str, ...]) -> Q | None:
    """Match condition codings against ICD-10 prefixes, dotted or undotted.

    Canvas instances store ICD-10 codes both ways ("Z68.41" and "Z6841"), so
    each prefix is matched in both forms rather than assuming one convention.
    """
    query = Q()
    matched = False
    for prefix in prefixes:
        cleaned = prefix.strip()
        if not cleaned:
            continue
        query |= Q(codings__code__istartswith=cleaned)
        undotted = cleaned.replace(".", "")
        if undotted and undotted != cleaned:
            query |= Q(codings__code__istartswith=undotted)
        matched = True
    return query if matched else None


def has_active_condition_with_prefixes(patient_id: str, prefixes: tuple[str, ...]) -> bool:
    """Whether the patient has an active condition coded under any given prefix."""
    query = _icd10_prefix_query(prefixes)
    if query is None:
        return False
    return cast(
        bool,
        Condition.objects.for_patient(patient_id)
        .filter(
            clinical_status=ClinicalStatus.ACTIVE,
            deleted=False,
            entered_in_error__isnull=True,
        )
        .filter(query)
        .exists(),
    )


def earliest_glp1_start(patient_id: str, fragments: tuple[str, ...]) -> datetime | None:
    """When the patient's longest-running active GLP-1 was started.

    Returns the *earliest* start among matching active medications: a patient
    switched from semaglutide to tirzepatide has been on GLP-1 therapy
    continuously, and restarting their monitoring clock at the switch would
    excuse them from labs they are already overdue for.
    """
    query = Q()
    matched = False
    for fragment in fragments:
        cleaned = fragment.strip()
        if not cleaned:
            continue
        query |= Q(codings__display__icontains=cleaned)
        matched = True
    if not matched:
        return None
    return cast(
        "datetime | None",
        Medication.objects.for_patient(patient_id)
        .filter(status=Status.ACTIVE, deleted=False, entered_in_error__isnull=True)
        .filter(query)
        .order_by("start_date")
        .values_list("start_date", flat=True)
        .first(),
    )


def has_active_glp1_medication(patient_id: str, fragments: tuple[str, ...]) -> bool:
    """Whether the patient has an active medication whose name matches a fragment."""
    return earliest_glp1_start(patient_id, fragments) is not None


def evaluate_cohort(patient_id: str, config: Config) -> CohortMatch:
    """Decide whether the patient is in scope, and on which criterion."""
    glp1_start = earliest_glp1_start(patient_id, config.glp1_med_name_fragments)
    if glp1_start is not None:
        # Short-circuit: the medication match alone puts the patient in scope,
        # so skip the condition query entirely.
        return CohortMatch(
            on_glp1_medication=True,
            has_obesity_condition=False,
            glp1_start=glp1_start,
        )
    has_obesity = has_active_condition_with_prefixes(patient_id, config.obesity_icd10_prefixes)
    return CohortMatch(on_glp1_medication=False, has_obesity_condition=has_obesity)
