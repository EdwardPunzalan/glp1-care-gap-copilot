"""ProtocolCard assembly.

Buttons here only ever *stage* a command into the note — the clinician reviews
and signs it. Where a button cannot be built correctly (no lab partner
configured, tests not offered by that partner), the gap still renders as an
informational bullet: the plugin never emits a command it knows to be invalid.
"""

from django.db.models import Q

from canvas_sdk.commands import LabOrderCommand, TaskCommand
from canvas_sdk.commands.commands.task import AssigneeType, TaskAssigner
from canvas_sdk.effects.protocol_card import ProtocolCard, Recommendation
from canvas_sdk.v1.data.lab import LabPartner, LabPartnerTest

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import task_title
from glp1_care_gap_copilot.gaps import GAP_LABS_OVERDUE, Gap

CARD_KEY = "glp1-care-gaps"
CARD_TITLE = "GLP-1 Care Gaps"
ALREADY_OPEN_SUFFIX = " — outreach task already open"
OUTREACH_BUTTON = "Outreach task"
LAB_ORDER_BUTTON = "Order labs"


def _assignee(config: Config) -> TaskAssigner:
    """Assign to the configured outreach team, or leave unassigned if unset."""
    if config.outreach_team_dbid is None:
        return TaskAssigner(to=AssigneeType.UNASSIGNED)
    return TaskAssigner(to=AssigneeType.TEAM, id=config.outreach_team_dbid)


def build_outreach_task(gap: Gap, config: Config) -> TaskCommand:
    """A staged outreach task naming the gap it closes."""
    return TaskCommand(
        title=task_title(config, gap.key, gap.label),
        assign_to=_assignee(config),
        comment=f"Identified by {config.task_title_prefix}: {gap.label}.",
    )


def resolve_lab_order(gap: Gap, config: Config) -> LabOrderCommand | None:
    """Build a lab order for the missing tests, or None if it cannot be validated.

    `LabOrderCommand` validates `lab_partner` and `tests_order_codes` against the
    instance's records, so both are resolved here first. Anything unresolvable
    yields None and the caller renders the gap without a button.
    """
    if not config.lab_partner_name:
        return None
    missing = gap.detail.get("missing")
    if not isinstance(missing, list) or not missing:
        return None

    partner = LabPartner.objects.filter(
        name__iexact=config.lab_partner_name, active=True
    ).first()
    if partner is None:
        return None

    name_query = Q()
    for lab_name in missing:
        name_query |= Q(order_name__icontains=lab_name)
    order_codes = [
        code
        for code in LabPartnerTest.objects.filter(name_query, lab_partner=partner)
        .values_list("order_code", flat=True)
        .distinct()
        if code
    ]
    if not order_codes:
        return None

    return LabOrderCommand(
        lab_partner=str(partner.id),
        tests_order_codes=order_codes,
        comment=f"Monitoring labs flagged by {config.task_title_prefix}.",
    )


def _recommendation(gap: Gap, config: Config, suppressed: bool) -> Recommendation:
    if suppressed:
        return Recommendation(title=gap.label + ALREADY_OPEN_SUFFIX)

    if gap.key == GAP_LABS_OVERDUE:
        lab_order = resolve_lab_order(gap, config)
        if lab_order is None:
            return Recommendation(title=gap.label)
        return Recommendation(
            title=gap.label, button=LAB_ORDER_BUTTON, commands=[lab_order]
        )

    return Recommendation(
        title=gap.label,
        button=OUTREACH_BUTTON,
        commands=[build_outreach_task(gap, config)],
    )


def build_card(
    patient_id: str,
    gaps: list[Gap],
    suppressed_gap_keys: set[str],
    narrative: str,
    config: Config,
) -> ProtocolCard:
    """Assemble the upsertable card for this patient."""
    return ProtocolCard(
        patient_id=patient_id,
        key=CARD_KEY,
        title=CARD_TITLE,
        narrative=narrative,
        status=ProtocolCard.Status.DUE if gaps else ProtocolCard.Status.SATISFIED,
        recommendations=[
            _recommendation(gap, config, gap.key in suppressed_gap_keys) for gap in gaps
        ],
    )
