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
from logger import log

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import task_title
from glp1_care_gap_copilot.gaps import (
    GAP_LABS_OVERDUE,
    GAP_SAFETY_CHECK_DUE,
    GAP_SAFETY_REVIEW,
    Gap,
)
from glp1_care_gap_copilot.labs import order_code_keys

CARD_KEY = "glp1-care-gaps"
CARD_TITLE = "GLP-1 Care Gaps"
ALREADY_OPEN_SUFFIX = " — outreach task already open"
OUTREACH_BUTTON = "Outreach task"
LAB_ORDER_BUTTON = "Order labs"
#: The safety row asks for a phone call, not routine outreach, so it says so.
CONTACT_BUTTON = "Contact patient"
#: The screening row asks an MA to fill in the form, not to call anyone.
SAFETY_CHECK_BUTTON = "Task MA to screen"


def _assignee(config: Config) -> TaskAssigner:
    """Assign to the configured outreach team, or leave unassigned if unset."""
    if config.outreach_team_dbid is None:
        return TaskAssigner(to=AssigneeType.UNASSIGNED)
    return TaskAssigner(to=AssigneeType.TEAM, id=config.outreach_team_dbid)


#: The screening task spells out what to do, because whoever picks it up is
#: acting on it days later without the chart in front of them.
SAFETY_CHECK_INSTRUCTION = (
    "Complete the GLP-1 Safety Check questionnaire with the patient at their "
    "next visit."
)


def build_outreach_task(gap: Gap, config: Config) -> TaskCommand:
    """A staged outreach task naming the gap it closes."""
    comment = f"Identified by {config.task_title_prefix}: {gap.label}."
    if gap.key == GAP_SAFETY_CHECK_DUE:
        comment = f"{comment} {SAFETY_CHECK_INSTRUCTION}"
    return TaskCommand(
        title=task_title(config, gap.key, gap.label),
        assign_to=_assignee(config),
        comment=comment,
    )


def _requested_codes(gap: Gap, config: Config) -> list[str]:
    """The order codes for this gap's missing labs, in requirement order."""
    missing = gap.detail.get("missing_keys")
    if not isinstance(missing, list) or not missing:
        return []
    requested = []
    for requirement_key in missing:
        for candidate in order_code_keys(str(requirement_key).lower()):
            code = config.lab_test_order_codes.get(candidate.lower())
            if code:
                requested.append(code)
                break
    return requested


def resolve_lab_orders(gap: Gap, config: Config) -> list[tuple[str, LabOrderCommand]]:
    """One (partner name, staged order) per configured partner that stocks the tests.

    Two queries regardless of how many partners are configured: the partners are
    fetched in one lookup and their catalogs in a second, then grouped in Python.
    A per-partner query would put the card's cost at the mercy of configuration.

    A partner that stocks none of the codes is skipped rather than rendered with
    a button Canvas would reject — the same rule the single-partner path always
    followed.
    """
    if not config.lab_partner_names or not config.lab_test_order_codes:
        return []
    requested = _requested_codes(gap, config)
    if not requested:
        return []

    name_query = Q()
    for name in config.lab_partner_names:
        name_query |= Q(name__iexact=name)
    partners = {
        str(row["id"]): row["name"]
        for row in LabPartner.objects.filter(name_query, active=True).values("id", "name")
    }
    if not partners:
        return []

    # Joined on the partner's UUID, not `lab_partner__in`: the FK resolves
    # against the integer `dbid`, so passing UUIDs there raises rather than
    # returning nothing.
    stocked: dict[str, set[str]] = {}
    for partner_id, order_code in LabPartnerTest.objects.filter(
        lab_partner__id__in=list(partners), order_code__in=requested
    ).values_list("lab_partner__id", "order_code"):
        stocked.setdefault(str(partner_id), set()).add(order_code)

    # Configured order decides button order, so the practice's first choice
    # stays first on the card no matter how the database sorts.
    by_name = {name.lower(): (pid, name) for pid, name in partners.items()}
    orders = []
    for configured in config.lab_partner_names:
        found = by_name.get(configured.strip().lower())
        if found is None:
            continue
        partner_id, partner_name = found
        codes = [code for code in requested if code in stocked.get(partner_id, set())]
        if not codes:
            log.warning(
                f"[glp1-care-gap-copilot] lab partner {partner_name!r} stocks none of "
                f"the configured order codes {requested}; no button for it"
            )
            continue
        orders.append(
            (
                partner_name,
                LabOrderCommand(
                    lab_partner=partner_id,
                    tests_order_codes=codes,
                    comment=f"Monitoring labs flagged by {config.task_title_prefix}.",
                ),
            )
        )
    return orders


def _lab_recommendations(gap: Gap, config: Config) -> list[Recommendation]:
    """One row per lab the order can be sent to.

    With a single configured partner this is exactly the old single row, so a
    practice using one lab sees no change. With several, each gets its own
    button carrying a *complete* order — which is the point: changing the lab on
    an already-staged order makes Canvas clear the tests, because its test
    picker is scoped to one partner. Choosing up front avoids that entirely.
    """
    orders = resolve_lab_orders(gap, config)
    if not orders:
        return [Recommendation(title=gap.label)]
    if len(orders) == 1:
        return [
            Recommendation(
                title=gap.label, button=LAB_ORDER_BUTTON, commands=[orders[0][1]]
            )
        ]
    rows = []
    for index, (partner_name, order) in enumerate(orders):
        # The first row carries the full gap text; the rest would only repeat
        # the same lab list back, so they say what differs instead.
        title = gap.label if index == 0 else f"…the same order, sent to {partner_name}"
        rows.append(
            Recommendation(
                title=title,
                button=f"Order at {partner_name}",
                commands=[order],
            )
        )
    return rows


def _recommendations(gap: Gap, config: Config, suppressed: bool) -> list[Recommendation]:
    if suppressed:
        return [Recommendation(title=gap.label + ALREADY_OPEN_SUFFIX)]

    if gap.key == GAP_LABS_OVERDUE:
        return _lab_recommendations(gap, config)

    button = OUTREACH_BUTTON
    if gap.key == GAP_SAFETY_REVIEW:
        button = CONTACT_BUTTON
    elif gap.key == GAP_SAFETY_CHECK_DUE:
        button = SAFETY_CHECK_BUTTON
    return [
        Recommendation(
            title=gap.label,
            button=button,
            commands=[build_outreach_task(gap, config)],
        )
    ]


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
            recommendation
            for gap in gaps
            for recommendation in _recommendations(
                gap, config, gap.key in suppressed_gap_keys
            )
        ],
    )
