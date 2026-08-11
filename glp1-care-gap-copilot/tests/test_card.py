"""Card assembly — button presence, suppression, and failing closed on lab orders."""

from canvas_sdk.commands import LabOrderCommand, TaskCommand
from canvas_sdk.commands.commands.task import AssigneeType
from canvas_sdk.effects.protocol_card import ProtocolCard
from canvas_sdk.test_utils.factories import (
    LabPartnerFactory,
    LabPartnerTestFactory,
    PatientFactory,
)
from canvas_sdk.v1.data.lab import LabPartner

from glp1_care_gap_copilot.card import (
    ALREADY_OPEN_SUFFIX,
    CARD_KEY,
    CARD_TITLE,
    LAB_ORDER_BUTTON,
    OUTREACH_BUTTON,
    build_card,
    build_outreach_task,
    resolve_lab_order,
)
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import GAP_LABS_OVERDUE, GAP_STALE_WEIGHT, Gap

CONFIG = Config.from_secrets({})

WEIGHT_GAP = Gap(
    key=GAP_STALE_WEIGHT,
    label="No weight recorded in 58 days",
    detail={"days_since_last": 58},
)
LABS_GAP = Gap(
    key=GAP_LABS_OVERDUE,
    label="Labs overdue: hemoglobin a1c",
    detail={"missing": ["hemoglobin a1c"], "days_since_last": 214},
)


def configured_partner(
    name: str = "Quest", order_name: str = "Hemoglobin A1c"
) -> LabPartner:
    """A lab partner offering a test whose name matches the missing lab."""
    partner = LabPartnerFactory.create(name=name, active=True)
    LabPartnerTestFactory.create(
        lab_partner=partner, order_name=order_name, order_code="A1C-1"
    )
    return partner


# --- Card shape -------------------------------------------------------------


def test_card_is_keyed_per_patient_so_it_upserts() -> None:
    card = build_card("patient-1", [WEIGHT_GAP], set(), "narrative", CONFIG)

    assert card.key == CARD_KEY
    assert card.patient_id == "patient-1"
    assert card.title == CARD_TITLE
    assert card.narrative == "narrative"


def test_card_is_due_when_a_gap_is_open() -> None:
    card = build_card("patient-1", [WEIGHT_GAP], set(), "narrative", CONFIG)

    assert card.status == ProtocolCard.Status.DUE


def test_card_is_satisfied_when_all_gaps_are_closed() -> None:
    card = build_card("patient-1", [], set(), "narrative", CONFIG)

    assert card.status == ProtocolCard.Status.SATISFIED
    assert card.recommendations == []


def test_each_gap_becomes_one_recommendation() -> None:
    card = build_card("patient-1", [WEIGHT_GAP, LABS_GAP], set(), "n", CONFIG)

    assert [rec.title for rec in card.recommendations] == [
        WEIGHT_GAP.label,
        LABS_GAP.label,
    ]


# --- Outreach task button ---------------------------------------------------


def test_weight_gap_offers_an_outreach_task() -> None:
    card = build_card("patient-1", [WEIGHT_GAP], set(), "n", CONFIG)
    recommendation = card.recommendations[0]

    assert recommendation.button == OUTREACH_BUTTON
    assert isinstance(recommendation.commands[0], TaskCommand)


def test_outreach_task_title_carries_the_dedupe_marker() -> None:
    task = build_outreach_task(WEIGHT_GAP, CONFIG)

    assert task.title.startswith("[GLP-1 Copilot: stale_weight]")
    assert WEIGHT_GAP.label in str(task.comment)


def test_outreach_task_is_unassigned_when_no_team_is_configured() -> None:
    task = build_outreach_task(WEIGHT_GAP, CONFIG)

    assert task.assign_to["to"] == AssigneeType.UNASSIGNED


def test_outreach_task_goes_to_the_configured_team() -> None:
    config = Config.from_secrets({"OUTREACH_TEAM_DBID": "42"})

    task = build_outreach_task(WEIGHT_GAP, config)

    assert task.assign_to["to"] == AssigneeType.TEAM
    assert task.assign_to["id"] == 42


# --- Suppression ------------------------------------------------------------


def test_a_suppressed_gap_still_renders_but_loses_its_button() -> None:
    card = build_card("patient-1", [WEIGHT_GAP], {GAP_STALE_WEIGHT}, "n", CONFIG)
    recommendation = card.recommendations[0]

    assert recommendation.title == WEIGHT_GAP.label + ALREADY_OPEN_SUFFIX
    assert not recommendation.button
    assert not recommendation.commands


def test_suppressing_one_gap_leaves_the_others_actionable() -> None:
    card = build_card("patient-1", [WEIGHT_GAP, LABS_GAP], {GAP_STALE_WEIGHT}, "n", CONFIG)

    assert not card.recommendations[0].button
    assert card.recommendations[1].title == LABS_GAP.label


# --- Lab order button -------------------------------------------------------


def test_lab_order_button_appears_when_the_partner_and_test_resolve() -> None:
    PatientFactory.create()
    configured_partner()
    config = Config.from_secrets({"LAB_PARTNER_NAME": "Quest"})

    card = build_card("patient-1", [LABS_GAP], set(), "n", config)
    recommendation = card.recommendations[0]

    assert recommendation.button == LAB_ORDER_BUTTON
    order = recommendation.commands[0]
    assert isinstance(order, LabOrderCommand)
    assert order.tests_order_codes == ["A1C-1"]


def test_no_lab_partner_configured_renders_the_gap_without_a_button() -> None:
    card = build_card("patient-1", [LABS_GAP], set(), "n", CONFIG)
    recommendation = card.recommendations[0]

    assert recommendation.title == LABS_GAP.label
    assert not recommendation.button
    assert not recommendation.commands


def test_unknown_lab_partner_renders_the_gap_without_a_button() -> None:
    configured_partner(name="Quest")
    config = Config.from_secrets({"LAB_PARTNER_NAME": "LabCorp"})

    assert resolve_lab_order(LABS_GAP, config) is None


def test_partner_that_does_not_offer_the_test_renders_no_button() -> None:
    configured_partner(name="Quest", order_name="Basic Metabolic Panel")
    config = Config.from_secrets({"LAB_PARTNER_NAME": "Quest"})

    assert resolve_lab_order(LABS_GAP, config) is None


def test_inactive_partner_is_not_used() -> None:
    partner = LabPartnerFactory.create(name="Quest", active=False)
    LabPartnerTestFactory.create(
        lab_partner=partner, order_name="Hemoglobin A1c", order_code="A1C-1"
    )
    config = Config.from_secrets({"LAB_PARTNER_NAME": "Quest"})

    assert resolve_lab_order(LABS_GAP, config) is None


def test_a_gap_with_no_missing_labs_produces_no_order() -> None:
    configured_partner()
    config = Config.from_secrets({"LAB_PARTNER_NAME": "Quest"})
    empty = Gap(key=GAP_LABS_OVERDUE, label="Labs overdue", detail={"missing": []})

    assert resolve_lab_order(empty, config) is None


def test_partner_lookup_is_case_insensitive() -> None:
    configured_partner(name="Quest Diagnostics")
    config = Config.from_secrets({"LAB_PARTNER_NAME": "quest diagnostics"})

    assert resolve_lab_order(LABS_GAP, config) is not None
