"""Which labs a GLP-1 patient owes, and how overdue they are.

The rule has three gates, all of which must pass before any lab is expected:

1. **Time on therapy.** Nothing is expected until the patient has been on a
   GLP-1 for `glp1_min_days_for_labs`. Ordering a metabolic panel on someone
   three weeks into treatment measures the diet they were on before it.
2. **Which labs.** A metabolic panel, a lipid panel, and an A1c for everyone
   past that gate. TSH with reflex to T4 is added only for patients carrying a
   thyroid diagnosis.
3. **How often.** A patient carrying a metabolic comorbidity — high
   cholesterol, diabetes, pre-diabetes, or hypothyroidism — is on the short
   interval. Obesity with none of those is on the long one.

The two-tier interval is the point of the design: obesity alone is a slower
clinical picture than obesity plus dysglycemia, and putting both on a 90-day
cycle would bury the patients who actually need watching under the ones who
don't.

`satisfied_by` is what makes "CMP or BMP" expressible: a requirement is met by
*any* of its named labs, so a patient with a recent BMP is not chased for a CMP.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from canvas_sdk.v1.data.condition import ClinicalStatus, Condition
from canvas_sdk.v1.data.lab import LabValue

from glp1_care_gap_copilot.cohort import _icd10_prefix_query
from glp1_care_gap_copilot.config import Config

#: Requirement keys. Also the keys operators use in `LAB_TEST_ORDER_CODES`.
LAB_METABOLIC_PANEL = "metabolic panel"
LAB_LIPID_PANEL = "lipid panel"
LAB_A1C = "hemoglobin a1c"
LAB_TSH = "tsh"


@dataclass(frozen=True)
class LabRequirement:
    """One lab the patient owes, and what counts as having done it."""

    key: str
    label: str
    #: Any one of these result names satisfies the requirement. Matched as a
    #: case-insensitive substring against both the result coding and the
    #: partner's test name.
    satisfied_by: tuple[str, ...]


#: Order matters — it is the order the labs are listed to the clinician.
BASE_REQUIREMENTS: tuple[LabRequirement, ...] = (
    LabRequirement(
        key=LAB_METABOLIC_PANEL,
        label="comprehensive or basic metabolic panel",
        satisfied_by=("comprehensive metabolic panel", "basic metabolic panel"),
    ),
    LabRequirement(
        key=LAB_LIPID_PANEL,
        label="lipid panel",
        satisfied_by=("lipid panel",),
    ),
    LabRequirement(
        key=LAB_A1C,
        label="hemoglobin A1c",
        satisfied_by=("hemoglobin a1c", "hba1c"),
    ),
)

#: Added only for patients carrying a thyroid diagnosis.
TSH_REQUIREMENT = LabRequirement(
    key=LAB_TSH,
    label="TSH with reflex to T4",
    satisfied_by=("tsh", "thyroid stimulating hormone", "thyrotropin"),
)


@dataclass(frozen=True)
class Comorbidities:
    """Which qualifying diagnoses the patient carries, beyond obesity itself."""

    high_cholesterol: bool = False
    diabetes: bool = False
    prediabetes: bool = False
    hypothyroidism: bool = False

    @property
    def any(self) -> bool:
        """True when at least one qualifying comorbidity is present."""
        return (
            self.high_cholesterol
            or self.diabetes
            or self.prediabetes
            or self.hypothyroidism
        )

    @property
    def names(self) -> list[str]:
        """Human-readable list, for the card and the narrative."""
        found = []
        if self.high_cholesterol:
            found.append("high cholesterol")
        if self.diabetes:
            found.append("diabetes")
        if self.prediabetes:
            found.append("pre-diabetes")
        if self.hypothyroidism:
            found.append("hypothyroidism")
        return found


def detect_comorbidities(patient_id: str, config: Config) -> Comorbidities:
    """Find every qualifying diagnosis in one query.

    All four prefix groups are OR-ed into a single lookup and the matched codes
    are sorted back into groups in Python, rather than issuing four queries that
    each scan the same conditions.
    """
    groups = (
        config.high_cholesterol_icd10_prefixes,
        config.diabetes_icd10_prefixes,
        config.prediabetes_icd10_prefixes,
        config.hypothyroid_icd10_prefixes,
    )
    combined = Q()
    matched = False
    for prefixes in groups:
        clause = _icd10_prefix_query(prefixes)
        if clause is not None:
            combined |= clause
            matched = True
    if not matched:
        # Every group cleared with the `none` sentinel: nothing to look for, so
        # skip the query entirely rather than issuing an unfiltered one.
        return Comorbidities()

    codes = {
        str(code or "").upper().replace(".", "")
        for code in Condition.objects.for_patient(patient_id)
        .filter(
            clinical_status=ClinicalStatus.ACTIVE,
            deleted=False,
            entered_in_error__isnull=True,
        )
        .filter(combined)
        .values_list("codings__code", flat=True)
        if code
    }

    def present(prefixes: tuple[str, ...]) -> bool:
        cleaned = tuple(
            prefix.strip().upper().replace(".", "") for prefix in prefixes if prefix.strip()
        )
        return bool(cleaned) and any(code.startswith(cleaned) for code in codes)

    return Comorbidities(
        high_cholesterol=present(config.high_cholesterol_icd10_prefixes),
        diabetes=present(config.diabetes_icd10_prefixes),
        prediabetes=present(config.prediabetes_icd10_prefixes),
        hypothyroidism=present(config.hypothyroid_icd10_prefixes),
    )


#: Every requirement, keyed for lookup by key.
_BY_KEY = {
    requirement.key: requirement
    for requirement in (*BASE_REQUIREMENTS, TSH_REQUIREMENT)
}


def order_code_keys(requirement_key: str) -> tuple[str, ...]:
    """Config keys that may carry this requirement's lab order code.

    The requirement key first, then the result names it is satisfied by. The
    aliases exist because the requirement keys changed when "CMP or BMP" was
    introduced — an instance configured against the older, per-lab-name scheme
    (`comprehensive metabolic panel:10231`) keeps resolving instead of silently
    losing its order button on upgrade.
    """
    requirement = _BY_KEY.get(requirement_key)
    if requirement is None:
        return (requirement_key,)
    return (requirement.key, *requirement.satisfied_by)


def required_labs(comorbidities: Comorbidities) -> tuple[LabRequirement, ...]:
    """The labs this patient owes.

    TSH is thyroid-only by explicit clinical decision: adding it for everyone
    would order a test most of these patients have no indication for.
    """
    if comorbidities.hypothyroidism:
        return (*BASE_REQUIREMENTS, TSH_REQUIREMENT)
    return BASE_REQUIREMENTS


def lab_interval_days(comorbidities: Comorbidities, config: Config) -> int:
    """How long a result stays current for this patient."""
    if comorbidities.any:
        return config.lab_interval_days
    return config.lab_interval_obesity_only_days


def latest_result_datetime(
    patient_id: str, requirement: LabRequirement
) -> datetime | None:
    """Most recent result date for any lab that satisfies the requirement."""
    query = Q()
    for name in requirement.satisfied_by:
        query |= Q(codings__name__icontains=name) | Q(
            test__ontology_test_name__icontains=name
        )
    result: datetime | None = (
        LabValue.objects.filter(
            query,
            report__patient__id=patient_id,
            report__deleted=False,
            report__entered_in_error__isnull=True,
            report__date_performed__isnull=False,
        )
        .order_by("-report__date_performed")
        .values_list("report__date_performed", flat=True)
        .first()
    )
    return result
