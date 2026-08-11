"""Handler that renders the GLP-1 care-gap card when a chart or note is opened.

Both events target the Patient, so one handler serves both surfaces. Neither
event is on Canvas's disallowed list for `ADD_OR_UPDATE_PROTOCOL_CARD`
(only `PATIENT_CHART__CONDITIONS` and `PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION`
are), so upserting a card here cannot loop.
"""

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from logger import log

from glp1_care_gap_copilot.card import build_card
from glp1_care_gap_copilot.cohort import evaluate_cohort
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import gaps_with_open_tasks
from glp1_care_gap_copilot.gaps import detect_gaps, weeks_since_last_visit
from glp1_care_gap_copilot.rationale import generate_rationale


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
            # Out of scope produces no card at all, not an empty one.
            return []

        now = datetime.now(timezone.utc)
        gaps = detect_gaps(patient_id, config, now)
        suppressed = gaps_with_open_tasks(patient_id, config, [gap.key for gap in gaps])
        narrative = generate_rationale(
            gaps,
            cohort.on_glp1_medication,
            weeks_since_last_visit(patient_id, now),
            config,
        )

        card = build_card(patient_id, gaps, suppressed, narrative, config)
        return [card.apply()]
