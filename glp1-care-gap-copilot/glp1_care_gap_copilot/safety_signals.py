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
from glp1_care_gap_copilot.gaps import GAP_SAFETY_CHECK_DUE, GAP_SAFETY_REVIEW, Gap
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
    #: True when, in practice, the only way this finding gets recorded is the
    #: questionnaire. The ICD-10 proxies exist but nobody reaches for `M62.84`
    #: during a routine GLP-1 visit, so an absent code says nothing at all.
    #: These are the findings the screening row names as unassessed.
    form_only: bool = False
    #: Short name used when listing what has not been assessed.
    short_label: str = ""

    @property
    def brief(self) -> str:
        """The short name, falling back to the full label."""
        return self.short_label or self.label.lower()


#: Order matters — it is the order findings are listed back to the clinician.
FINDINGS: tuple[Finding, ...] = (
    Finding(
        key="poor_oral_intake",
        label="Very poor oral intake",
        question_code="GLP1SC_INTAKE",
        icd10_prefixes=("R63.0", "R63.3"),
        form_only=True,
        short_label="oral intake",
    ),
    Finding(
        key="gi_symptoms",
        label="Persistent nausea, vomiting, or diarrhea",
        question_code="GLP1SC_GI",
        icd10_prefixes=("R11", "R19.7", "K52.9"),
    ),
    Finding(
        key="dehydration",
        label="Dehydration",
        question_code="GLP1SC_DEHYDRATION",
        # E86 is volume depletion/dehydration. Orthostasis (I95.1) was
        # deliberately dropped: it is a sign that often has causes other than
        # volume loss, so pairing it with rapid weight loss produced alerts the
        # clinician did not want.
        icd10_prefixes=("E86",),
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
        form_only=True,
        short_label="muscle loss",
    ),
    Finding(
        key="inadequate_protein",
        label="Inadequate protein intake",
        question_code="GLP1SC_PROTEIN",
        icd10_prefixes=("E43", "E44", "E46"),
        form_only=True,
        short_label="protein intake",
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
    # Matched on the questionnaire *code*, never on its id. Editing the shipped
    # template makes Canvas retire the old row and create a new one under the
    # same code, so an id match would silently stop counting every check
    # completed before the most recent deploy. `.distinct()` guards the other
    # side of that: the code now matches more than one row, and the join would
    # otherwise return an interview once per matching version.
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
        .values_list("dbid", flat=True)
        .distinct()[:MAX_INTERVIEWS]
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


def _readable_list(items: list[str]) -> str:
    """Join with commas and a final "and", the way a person would write it."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def safety_check_is_due(signal: SafetySignal) -> bool:
    """Whether this patient lost weight fast and nobody screened them.

    Fires whenever there is a rapid drop and no committed safety check inside
    the window — including when coded conditions already tripped the alert. The
    condition path only sees four of the seven findings with any reliability, so
    "the diagnoses already told us something" is not a reason to skip the form
    that covers eating, protein, and muscle loss.
    """
    return signal.drop is not None and not signal.screened


def unassessed_findings(signal: SafetySignal) -> tuple[Finding, ...]:
    """The findings nothing on this chart can speak to.

    Only the form-only findings count. A missing nausea code is weak evidence
    that the patient has no nausea — a clinician would likely have coded it —
    but a missing sarcopenia code is no evidence at all, because nobody codes
    sarcopenia at a weight-management visit. Naming the second group is honest;
    naming the first would cry wolf.

    A finding already confirmed by a coded condition drops off the list: it is
    not unassessed, it is known.
    """
    known = {finding.key for finding in signal.from_conditions} | {
        finding.key for finding in signal.from_questionnaire
    }
    return tuple(
        finding
        for finding in FINDINGS
        if finding.form_only and finding.key not in known
    )


def safety_check_gap(signal: SafetySignal, followup_booked: bool) -> Gap:
    """Ask an MA to complete the safety check at the patient's next visit.

    The row names *what is missing* rather than merely that a form is missing.
    On a chart where a coded condition already raised the alert, "no safety
    check on file" reads as a duplicate of the row above it; "oral intake,
    protein intake and muscle loss not assessed" is a different, actionable
    statement — and it stays true even when the diagnoses look reassuring.

    Whether a follow-up is already booked changes the ask, so it is stated too:
    with no visit scheduled, "at the next visit" is an instruction with nowhere
    to land, and the scheduling outreach has to go with it.
    """
    drop = signal.drop
    assert drop is not None, "safety_check_gap requires a drop to have been found"

    missing = unassessed_findings(signal)
    if missing:
        gap_text = f"{_readable_list([f.brief for f in missing])} not assessed"
    else:
        # Every form-only finding happens to be coded already, which is rare.
        # There is still no completed form, so the ask stands.
        gap_text = "no safety check on file"

    tail = (
        "screen at next visit"
        if followup_booked
        else "no visit booked — schedule and screen"
    )
    return Gap(
        key=GAP_SAFETY_CHECK_DUE,
        label=(
            f"Rapid weight loss ({drop.drop:.0f} lb in {drop.interval_days}d) — "
            f"{gap_text} — {tail}"
        ),
        detail={
            "drop_lb": drop.drop,
            "interval_days": drop.interval_days,
            "followup_booked": followup_booked,
            "unassessed": [finding.key for finding in missing],
            "unassessed_text": gap_text,
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
