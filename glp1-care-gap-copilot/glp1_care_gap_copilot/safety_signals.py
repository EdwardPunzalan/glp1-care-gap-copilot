"""Rapid weight loss paired with a clinical warning sign.

A clinician asked to be told when a GLP-1 patient loses weight fast *and* shows
one of seven findings that turn "the drug is working" into "this patient may be
in trouble". Weight loss alone is the point of the therapy; weight loss plus
vomiting, dehydration, or an empty plate is a reason to pick up the phone.

Two independent sources feed the same rule:

- **The GLP-1 Safety Check questionnaire** this plugin ships. All seven findings
  are on it, so one completed form can answer the rule by itself.
- **Coded conditions.** Every finding has an ICD-10 proxy, but in practice only
  the GI and constitutional ones get coded during a routine visit — nobody
  reaches for `M62.84` to say a patient looks sarcopenic. The condition path is
  therefore a safety net for charts nobody screened, not a replacement for the
  form.

Both are matched against a window around the drop so an old, unrelated record
cannot pair with a recent one.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.db.models import Q

from canvas_sdk.v1.data.condition import ClinicalStatus, Condition
from canvas_sdk.v1.data.questionnaire import Interview, InterviewQuestionResponse

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import GAP_SAFETY_REVIEW, Gap
from glp1_care_gap_copilot.graph import Segment, build_graph
from glp1_care_gap_copilot.weight_trend import recent_weigh_ins

QUESTIONNAIRE_CODE = "GLP1_SAFETY_CHECK"
#: Canvas truncates banner narratives past 90 characters.
BANNER_NARRATIVE_LIMIT = 90
BANNER_KEY = "glp1-safety-review"
#: The response option code that counts as a positive finding. Defined by this
#: plugin's own questionnaire YAML, so it cannot drift underneath us.
AFFIRMATIVE_CODE = "Y"
#: Cap on how many committed safety-check interviews are read for one patient.
#: The newest few are the only ones inside any sane window.
MAX_INTERVIEWS = 10


@dataclass(frozen=True)
class Finding:
    """One of the seven warning signs, and how it can be detected."""

    key: str
    label: str
    #: Question code on the shipped questionnaire.
    question_code: str
    #: ICD-10 prefixes that stand in for this finding when it is coded instead.
    #: Deliberately a proxy: a coded match is suggestive, not equivalent.
    icd10_prefixes: tuple[str, ...]


#: Order matters — it is the order findings are listed back to the clinician.
FINDINGS: tuple[Finding, ...] = (
    Finding(
        key="poor_oral_intake",
        label="Very poor oral intake",
        question_code="GLP1SC_INTAKE",
        icd10_prefixes=("R63.0", "R63.3"),
    ),
    Finding(
        key="gi_symptoms",
        label="Persistent nausea, vomiting, or diarrhea",
        question_code="GLP1SC_GI",
        icd10_prefixes=("R11", "R19.7", "K52.9"),
    ),
    Finding(
        key="dehydration",
        label="Dehydration or orthostasis",
        question_code="GLP1SC_DEHYDRATION",
        icd10_prefixes=("E86", "I95.1"),
    ),
    Finding(
        key="fatigue",
        label="Weakness or significant fatigue",
        question_code="GLP1SC_FATIGUE",
        icd10_prefixes=("R53",),
    ),
    Finding(
        key="muscle_loss",
        label="Evidence of muscle loss",
        question_code="GLP1SC_MUSCLE",
        icd10_prefixes=("M62.84", "M62.5"),
    ),
    Finding(
        key="inadequate_protein",
        label="Inadequate protein intake",
        question_code="GLP1SC_PROTEIN",
        icd10_prefixes=("E43", "E44", "E46"),
    ),
    Finding(
        key="ruq_pain",
        label="Abdominal or RUQ pain suggesting gallbladder disease",
        question_code="GLP1SC_RUQ",
        icd10_prefixes=("R10.1", "K80", "K81"),
    ),
)

_BY_QUESTION_CODE = {finding.question_code: finding for finding in FINDINGS}


@dataclass(frozen=True)
class SafetySignal:
    """The verdict for one patient."""

    #: The rapid drop the findings were matched against, if there was one.
    drop: Segment | None = None
    #: Findings confirmed on the questionnaire.
    from_questionnaire: tuple[Finding, ...] = ()
    #: Findings inferred from coded conditions.
    from_conditions: tuple[Finding, ...] = ()
    #: True when the questionnaire has never been completed inside the window.
    #: Distinguishes "screened and clear" from "nobody looked".
    screened: bool = False
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def triggered(self) -> bool:
        """True when a rapid drop and enough findings coincide."""
        return self.drop is not None and bool(self.findings)

    @property
    def labels(self) -> list[str]:
        """Finding labels, in the canonical order."""
        return [finding.label for finding in self.findings]


def latest_rapid_drop(patient_id: str, config: Config) -> Segment | None:
    """The most recent interval the trend graph would paint red, if any.

    Reuses `build_graph` rather than re-deriving the rule so the banner can
    never disagree with the chart the clinician is looking at.
    """
    weigh_ins = recent_weigh_ins(patient_id, config.weight_trend_points)
    graph = build_graph(
        weigh_ins, config.weight_drop_alert_lb, config.weight_drop_max_interval_days
    )
    flagged = [segment for segment in graph.segments if segment.flagged]
    return flagged[-1] if flagged else None


def _window(drop_end: datetime, window_days: int) -> tuple[date, date]:
    span = timedelta(days=window_days)
    return (drop_end - span).date(), (drop_end + span).date()


def _questionnaire_findings(
    patient_id: str, earliest: date, latest: date
) -> tuple[tuple[Finding, ...], bool]:
    """Positive findings from committed safety checks, and whether any exist.

    The second element separates "the form says no" from "there is no form",
    which the narrative needs in order to stay honest about its own blind spot.
    """
    interview_dbids = list(
        Interview.objects.for_patient(patient_id)
        .committed()
        .filter(
            deleted=False,
            questionnaires__code=QUESTIONNAIRE_CODE,
            created__date__gte=earliest,
            created__date__lte=latest,
        )
        .order_by("-created")
        .values_list("dbid", flat=True)[:MAX_INTERVIEWS]
    )
    if not interview_dbids:
        return (), False

    answered = set(
        InterviewQuestionResponse.objects.filter(
            interview__dbid__in=interview_dbids,
            response_option__code=AFFIRMATIVE_CODE,
        ).values_list("question__code", flat=True)
    )
    return (
        tuple(finding for finding in FINDINGS if finding.question_code in answered),
        True,
    )


def _condition_findings(
    patient_id: str, earliest: date, latest: date
) -> tuple[Finding, ...]:
    """Positive findings inferred from active conditions coded in the window.

    One query for all seven findings: the prefixes are OR-ed together and the
    matched codes are mapped back to findings in Python, rather than issuing a
    query per finding.
    """
    # No empty-prefix guard: FINDINGS is a module constant and every entry
    # carries at least one prefix, so an empty query is not reachable.
    query = Q()
    for finding in FINDINGS:
        for prefix in finding.icd10_prefixes:
            query |= Q(codings__code__istartswith=prefix)
            undotted = prefix.replace(".", "")
            if undotted != prefix:
                query |= Q(codings__code__istartswith=undotted)

    codes = set(
        Condition.objects.for_patient(patient_id)
        .filter(
            clinical_status=ClinicalStatus.ACTIVE,
            deleted=False,
            entered_in_error__isnull=True,
            onset_date__gte=earliest,
            onset_date__lte=latest,
        )
        .filter(query)
        .values_list("codings__code", flat=True)
    )
    if not codes:
        return ()

    normalized = {str(code or "").upper().replace(".", "") for code in codes}
    hits = []
    for finding in FINDINGS:
        prefixes = tuple(
            prefix.upper().replace(".", "") for prefix in finding.icd10_prefixes
        )
        if any(code.startswith(prefixes) for code in normalized if code):
            hits.append(finding)
    return tuple(hits)


def evaluate_safety(patient_id: str, config: Config) -> SafetySignal:
    """Decide whether this patient's rapid loss is accompanied by a warning sign.

    Short-circuits on the drop: with no rapid loss there is nothing to pair a
    finding with, and the expensive questionnaire and condition reads never run.
    """
    drop = latest_rapid_drop(patient_id, config)
    if drop is None:
        return SafetySignal()

    earliest, latest = _window(drop.ended, config.safety_window_days)
    from_questionnaire, screened = _questionnaire_findings(patient_id, earliest, latest)
    from_conditions = _condition_findings(patient_id, earliest, latest)

    # A finding confirmed on the form and inferred from a code is still one
    # finding — union by key, then restore the canonical order.
    positive_keys = {finding.key for finding in from_questionnaire} | {
        finding.key for finding in from_conditions
    }
    findings = tuple(finding for finding in FINDINGS if finding.key in positive_keys)
    if len(findings) < config.safety_min_findings:
        findings = ()

    return SafetySignal(
        drop=drop,
        from_questionnaire=from_questionnaire,
        from_conditions=from_conditions,
        screened=screened,
        findings=findings,
    )


def safety_gap(signal: SafetySignal) -> Gap:
    """Render the signal as a card row, with the drop and findings named.

    The label states both halves of the rule so the row explains itself without
    the clinician having to open the graph to find out what "rapid" meant.
    """
    drop = signal.drop
    assert drop is not None, "safety_gap requires a triggered signal"
    return Gap(
        key=GAP_SAFETY_REVIEW,
        label=(
            f"Rapid weight loss ({drop.drop:.0f} lb in {drop.interval_days}d) with "
            f"{', '.join(signal.labels).lower()} — contact patient"
        ),
        detail={
            "drop_lb": drop.drop,
            "interval_days": drop.interval_days,
            "findings": [finding.key for finding in signal.findings],
            "labels": signal.labels,
        },
    )


def banner_narrative(signal: SafetySignal) -> str:
    """The <=90 character banner line.

    Names one finding when there is exactly one, and counts them otherwise —
    a comma-separated list of all seven would be truncated into nonsense.
    """
    count = len(signal.findings)
    if count == 1:
        detail = signal.findings[0].label.lower()
    else:
        detail = f"{count} warning signs"
    narrative = f"Rapid weight loss with {detail} — review and contact patient"
    if len(narrative) > BANNER_NARRATIVE_LIMIT:
        narrative = narrative[: BANNER_NARRATIVE_LIMIT - 1].rstrip() + "…"
    return narrative
