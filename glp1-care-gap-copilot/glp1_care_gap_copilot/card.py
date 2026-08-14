"""ProtocolCard assembly.

Buttons here only ever *stage* a command into the note — the clinician reviews
and signs it. Where a button cannot be built correctly (no lab partner
configured, tests not offered by that partner), the gap still renders as an
informational bullet: the plugin never emits a command it knows to be invalid.
"""

from canvas_sdk.commands import LabOrderCommand, TaskCommand
from canvas_sdk.commands.commands.task import AssigneeType, TaskAssigner
from canvas_sdk.effects.protocol_card import ProtocolCard, Recommendation
from canvas_sdk.v1.data.lab import LabPartner, LabPartnerTest
from logger import log

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import task_title
from glp1_care_gap_copilot.gaps import GAP_LABS_OVERDUE, GAP_SAFETY_REVIEW, Gap

CARD_KEY = "glp1-care-gaps"
CARD_TITLE = "GLP-1 Care Gaps"
ALREADY_OPEN_SUFFIX = " — outreach task already open"
OUTREACH_BUTTON = "Outreach task"
LAB_ORDER_BUTTON = "Order labs"
#: The safety row asks for a phone call, not routine outreach, so it says so.
CONTACT_BUTTON = "Contact patient"


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

    Which test to order is taken from the operator-configured
    `LAB_TEST_ORDER_CODES` map — one exact order code per lab name — and each
    code is then confirmed to exist in the partner's catalog before it is used.

    Matching by name was tried first and is wrong: a real catalog carries many
    near-identical variants (XPC Lab lists 8 "comprehensive metabolic panel"
    entries and no plain "lipid panel" at all), so a name match either ordered
    every variant at once or found nothing. Picking among them is a clinical and
    contractual decision, so the plugin requires it to be stated rather than
    inferred. Anything unmapped or unrecognized yields None, and the caller
    renders the gap without a button.
    """
    if not config.lab_partner_name or not config.lab_test_order_codes:
        return None
    missing = gap.detail.get("missing")
    if not isinstance(missing, list) or not missing:
        return None

    requested = [
        code
        for code in (config.lab_test_order_codes.get(name.lower()) for name in missing)
        if code
    ]
    if not requested:
        return None

    partner = LabPartner.objects.filter(
        name__iexact=config.lab_partner_name, active=True
    ).first()
    if partner is None:
        return None

    # Confirm each configured code is actually offered by this partner, so a
    # stale mapping degrades to "no button" instead of a command Canvas rejects.
    known = set(
        LabPartnerTest.objects.filter(
            lab_partner=partner, order_code__in=requested
        ).values_list("order_code", flat=True)
    )
    order_codes = [code for code in requested if code in known]
    if not order_codes:
        log.warning(
            f"[glp1-care-gap-copilot] none of the configured order codes {requested} "
            f"are offered by lab partner {config.lab_partner_name!r}; no order button"
        )
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

    button = CONTACT_BUTTON if gap.key == GAP_SAFETY_REVIEW else OUTREACH_BUTTON
    return Recommendation(
        title=gap.label,
        button=button,
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
