"""Panel-wide safety scanning, for surfaces that are not a patient's chart.

The chart card only fires when someone opens the chart, which means the patient
nobody opened — the one most likely to be missed — is exactly the one it cannot
help. This module inverts that: it finds the patients who need attention without
anyone having gone looking.

**Scope is driven by weigh-ins, not by panel size.** A rapid drop can only appear
when a new weight lands, so scanning patients weighed in the last
`alert_scan_lookback_days` is *complete* for this rule while costing a query
budget set by how many people the clinic weighed, not by how many are enrolled.
Scanning every GLP-1 patient nightly would cost the same answer far more slowly,
and would grow without bound as the panel grows.

Only the safety rules surface here — rapid loss with a warning sign, and rapid
loss nobody has screened. Overdue labs and stale weights are real gaps but they
are not time-critical, and mixing them in would bury the urgent ones.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from canvas_sdk.v1.data.observation import Observation

from glp1_care_gap_copilot.cohort import evaluate_cohort
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import GAP_SAFETY_CHECK_DUE, GAP_SAFETY_REVIEW, Gap
from glp1_care_gap_copilot.safety_signals import (
    evaluate_safety,
    safety_check_gap,
    safety_check_is_due,
    safety_gap,
)
from glp1_care_gap_copilot.weight_trend import WEIGHT_OBSERVATION_NAME


@dataclass(frozen=True)
class PatientAlert:
    """One patient the scan thinks somebody should look at."""

    patient_id: str
    #: The gaps worth acting on, safety only, most urgent first.
    gaps: tuple[Gap, ...]

    @property
    def is_triggered(self) -> bool:
        """True when a warning sign accompanied the drop, not merely a gap in screening."""
        return any(gap.key == GAP_SAFETY_REVIEW for gap in self.gaps)

    @property
    def headline(self) -> str:
        """The single line to show for this patient."""
        return self.gaps[0].label if self.gaps else ""


def patients_weighed_since(moment: datetime, limit: int) -> list[str]:
    """Distinct patients with a usable weight recorded since `moment`.

    One query, capped. The cap is a safety valve rather than a paging cursor: it
    is sized to exceed any realistic day's weigh-ins, and being hit is logged as
    a signal that the lookback or the cap needs revisiting.
    """
    rows = (
        Observation.objects.filter(
            name__iexact=WEIGHT_OBSERVATION_NAME,
            deleted=False,
            entered_in_error__isnull=True,
            effective_datetime__gte=moment,
        )
        .exclude(patient__isnull=True)
        .order_by()
        .values_list("patient__id", flat=True)
        .distinct()[:limit]
    )
    return [str(patient_id) for patient_id in rows]


def scan_for_alerts(config: Config, now: datetime | None = None) -> list[PatientAlert]:
    """Every patient whose recent weigh-in raises a safety signal.

    Each patient is put through the *same* `evaluate_safety` the chart card uses,
    so the dashboard, the nightly task, and the chart can never disagree about
    whether someone is flagged.
    """
    moment = now or datetime.now(timezone.utc)
    since = moment - timedelta(days=config.alert_scan_lookback_days)
    alerts: list[PatientAlert] = []

    for patient_id in patients_weighed_since(since, config.alert_scan_max_patients):
        # Cohort first: it is two cheap existence checks and rules out anyone the
        # plugin has no business commenting on.
        if not evaluate_cohort(patient_id, config).in_scope:
            continue

        signal = evaluate_safety(patient_id, config)
        gaps: list[Gap] = []
        if signal.triggered:
            gaps.append(safety_gap(signal))
        if safety_check_is_due(signal):
            # No follow-up lookup here: the scan is about who needs attention,
            # and the scheduling detail belongs on the chart where it can be
            # acted on. Passing True keeps the label free of a claim the scan
            # did not verify.
            gaps.append(safety_check_gap(signal, followup_booked=True))
        if gaps:
            alerts.append(PatientAlert(patient_id=patient_id, gaps=tuple(gaps)))

    # Warning signs before unscreened drops, so the urgent ones lead any list.
    alerts.sort(key=lambda alert: (not alert.is_triggered, alert.patient_id))
    return alerts


ALERT_GAP_KEYS = (GAP_SAFETY_REVIEW, GAP_SAFETY_CHECK_DUE)
