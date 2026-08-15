"""Handler that renders the GLP-1 care-gap card when a chart or note is opened.

Both events target the Patient, so one handler serves both surfaces. Neither
event is on Canvas's disallowed list for `ADD_OR_UPDATE_PROTOCOL_CARD`
(only `PATIENT_CHART__CONDITIONS` and `PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION`
are), so upserting a card here cannot loop.
"""

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.effects.banner_alert import AddBannerAlert, RemoveBannerAlert
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from logger import log

from glp1_care_gap_copilot.card import build_card
from glp1_care_gap_copilot.cohort import evaluate_cohort
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import gaps_with_open_tasks
from glp1_care_gap_copilot.gaps import (
    GAP_NO_FOLLOWUP,
    detect_gaps,
    weeks_since_last_visit,
)
from glp1_care_gap_copilot.rationale import build_narrative
from glp1_care_gap_copilot.safety_signals import (
    BANNER_KEY,
    SafetySignal,
    banner_narrative,
    evaluate_safety,
    safety_check_gap,
    safety_check_is_due,
    safety_gap,
)


class GLP1CareGapHandler(BaseHandler):
    """Compute GLP-1 monitoring gaps and surface them as a protocol card."""

    RESPONDS_TO = [
        EventType.Name(EventType.NOTE_OPENED),
        EventType.Name(EventType.PATIENT_CHART__MEDICATIONS),
    ]

    def compute(self) -> list[Effect]:
        """Render the card, or nothing at all for out-of-scope patients."""
        try:
            return self._compute()
        except Exception as error:  # noqa: BLE001 - never break chart rendering
            log.error(f"[glp1-care-gap-copilot] card render failed: {error}")
            return []

    def _compute(self) -> list[Effect]:
        target = getattr(self.event, "target", None)
        patient_id = getattr(target, "id", None)
        if not patient_id:
            return []

        config = Config.from_secrets(self.secrets)

        cohort = evaluate_cohort(patient_id, config)
        if not cohort.in_scope:
            # Out of scope produces no card at all, not an empty one. Logged
            # because "no card" and "plugin broken" look identical in the UI.
            log.info("[glp1-care-gap-copilot] patient out of scope; no card")
            return []

        now = datetime.now(timezone.utc)
        signal = evaluate_safety(patient_id, config)
        gaps = detect_gaps(patient_id, config, now, cohort)
        leading = []
        if signal.triggered:
            # Front of the card: a safety signal outranks every monitoring gap.
            leading.append(safety_gap(signal))
        if safety_check_is_due(signal):
            # The screening ask states whether a visit exists to screen at, so
            # the clinician can send the scheduling outreach alongside it.
            followup_booked = not any(gap.key == GAP_NO_FOLLOWUP for gap in gaps)
            leading.append(safety_check_gap(signal, followup_booked))
        gaps = [*leading, *gaps]
        suppressed = gaps_with_open_tasks(patient_id, config, [gap.key for gap in gaps])
        narrative = build_narrative(gaps, weeks_since_last_visit(patient_id, now))

        card = build_card(patient_id, gaps, suppressed, narrative, config)
        log.info(
            "[glp1-care-gap-copilot] card rendered: "
            f"on_glp1={cohort.on_glp1_medication} "
            f"gaps={[gap.key for gap in gaps]} suppressed={sorted(suppressed)} "
            f"safety={[finding.key for finding in signal.findings]}"
        )
        return [card.apply(), self._banner(patient_id, signal)]

    def _banner(self, patient_id: str, signal: SafetySignal) -> Effect:
        """Raise or clear the chart banner to match the current signal.

        A banner Canvas has already drawn stays on the chart until something
        removes it, so the resolved case has to emit `RemoveBannerAlert` rather
        than simply emitting nothing — otherwise the alert outlives the problem
        and the clinician learns to ignore it.
        """
        if not signal.triggered:
            return RemoveBannerAlert(key=BANNER_KEY, patient_id=patient_id).apply()
        return AddBannerAlert(
            patient_id=patient_id,
            key=BANNER_KEY,
            narrative=banner_narrative(signal),
            placement=[AddBannerAlert.Placement.CHART],
            intent=AddBannerAlert.Intent.ALERT,
        ).apply()
