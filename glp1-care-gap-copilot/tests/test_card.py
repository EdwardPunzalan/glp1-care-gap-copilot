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
    resolve_lab_orders,
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
    label="Monitoring labs due: hemoglobin A1c",
    detail={
        "missing": ["hemoglobin A1c"],
        # Order codes are mapped by requirement key, not by the display label.
        "missing_keys": ["hemoglobin a1c"],
        "days_since_last": 214,
        "interval_days": 90,
    },
)


def configured_partner(name: str = "Quest", order_code: str = "496") -> LabPartner:
    """A lab partner offering a test under a specific order code."""
    partner = LabPartnerFactory.create(name=name, active=True)
    LabPartnerTestFactory.create(
        lab_partner=partner, order_name="HEMOGLOBIN A1c", order_code=order_code
    )
    return partner


# A configured partner plus a mapping from the missing lab name to its code.
LAB_SECRETS = {
    "LAB_PARTNER_NAME": "Quest",
    "LAB_TEST_ORDER_CODES": "hemoglobin a1c:496",
}


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


def test_lab_order_button_appears_when_the_partner_and_code_resolve() -> None:
    PatientFactory.create()
    configured_partner()
    config = Config.from_secrets(LAB_SECRETS)

    card = build_card("patient-1", [LABS_GAP], set(), "n", config)
    recommendation = card.recommendations[0]

    assert recommendation.button == LAB_ORDER_BUTTON
    order = recommendation.commands[0]
    assert isinstance(order, LabOrderCommand)
    assert order.tests_order_codes == ["496"]


def test_only_the_mapped_code_is_ordered_not_every_name_match() -> None:
    """A catalog carries many variants of one panel; order exactly the mapped one."""
    partner = LabPartnerFactory.create(name="Quest", active=True)
    for order_name, code in [
        ("HEMOGLOBIN A1c", "496"),
        ("HEMOGLOBIN A1c WITH eAG", "16802"),
        ("HEMOGLOBIN A1c (REFL)", "40073"),
        ("CARDIO IQ(R) HEMOGLOBIN A1c", "91732"),
    ]:
        LabPartnerTestFactory.create(
            lab_partner=partner, order_name=order_name, order_code=code
        )
    config = Config.from_secrets(LAB_SECRETS)

    order = resolve_lab_orders(LABS_GAP, config)[0][1]

    assert order is not None
    assert order.tests_order_codes == ["496"]


def test_no_code_map_means_no_button() -> None:
    configured_partner()
    config = Config.from_secrets({"LAB_PARTNER_NAME": "Quest"})

    assert resolve_lab_orders(LABS_GAP, config) == []


def test_unmapped_lab_name_yields_no_button() -> None:
    configured_partner()
    config = Config.from_secrets(
        {"LAB_PARTNER_NAME": "Quest", "LAB_TEST_ORDER_CODES": "lipid panel:7600"}
    )

    # The gap is missing hemoglobin a1c, which this map does not cover.
    assert resolve_lab_orders(LABS_GAP, config) == []


def test_a_code_the_partner_does_not_offer_yields_no_button() -> None:
    configured_partner(order_code="496")
    config = Config.from_secrets(
        {"LAB_PARTNER_NAME": "Quest", "LAB_TEST_ORDER_CODES": "hemoglobin a1c:99999"}
    )

    assert resolve_lab_orders(LABS_GAP, config) == []


def test_no_lab_partner_configured_renders_the_gap_without_a_button() -> None:
    card = build_card("patient-1", [LABS_GAP], set(), "n", CONFIG)
    recommendation = card.recommendations[0]

    assert recommendation.title == LABS_GAP.label
    assert not recommendation.button
    assert not recommendation.commands


def test_unknown_lab_partner_renders_the_gap_without_a_button() -> None:
    configured_partner(name="Quest")
    config = Config.from_secrets({**LAB_SECRETS, "LAB_PARTNER_NAME": "LabCorp"})

    assert resolve_lab_orders(LABS_GAP, config) == []


def test_inactive_partner_is_not_used() -> None:
    partner = LabPartnerFactory.create(name="Quest", active=False)
    LabPartnerTestFactory.create(
        lab_partner=partner, order_name="HEMOGLOBIN A1c", order_code="496"
    )
    config = Config.from_secrets(LAB_SECRETS)

    assert resolve_lab_orders(LABS_GAP, config) == []


def test_a_gap_with_no_missing_labs_produces_no_order() -> None:
    configured_partner()
    config = Config.from_secrets(LAB_SECRETS)
    empty = Gap(key=GAP_LABS_OVERDUE, label="Labs overdue", detail={"missing": []})

    assert resolve_lab_orders(empty, config) == []


def test_partner_lookup_is_case_insensitive() -> None:
    configured_partner(name="Quest Diagnostics")
    config = Config.from_secrets(
        {**LAB_SECRETS, "LAB_PARTNER_NAME": "quest diagnostics"}
    )

    assert resolve_lab_orders(LABS_GAP, config) != []


def test_a_legacy_order_code_key_still_resolves() -> None:
    """An instance configured before the requirement keys changed keeps working."""
    PatientFactory.create()
    configured_partner(order_code="10231")
    legacy = Config.from_secrets(
        {
            "LAB_PARTNER_NAME": "Quest",
            # Keyed by the old full lab name, not the new requirement key.
            "LAB_TEST_ORDER_CODES": "comprehensive metabolic panel:10231",
        }
    )
    gap = Gap(
        key=GAP_LABS_OVERDUE,
        label="Monitoring labs due: comprehensive or basic metabolic panel",
        detail={"missing_keys": ["metabolic panel"], "interval_days": 90},
    )

    card = build_card("patient-1", [gap], set(), "n", legacy)

    assert card.recommendations[0].button == LAB_ORDER_BUTTON
    assert card.recommendations[0].commands[0].tests_order_codes == ["10231"]


# --- ordering to more than one lab --------------------------------------------

TWO_LABS = {
    "LAB_PARTNER_NAME": "Quest, LabCorp",
    "LAB_TEST_ORDER_CODES": "hemoglobin a1c:496",
}


def test_a_single_configured_partner_keeps_the_original_button() -> None:
    """A practice using one lab sees exactly the card it saw before."""
    PatientFactory.create()
    configured_partner()

    card = build_card("patient-1", [LABS_GAP], set(), "n", Config.from_secrets(LAB_SECRETS))

    assert len(card.recommendations) == 1
    assert card.recommendations[0].button == LAB_ORDER_BUTTON


def test_each_configured_partner_gets_its_own_button() -> None:
    PatientFactory.create()
    configured_partner("Quest")
    configured_partner("LabCorp")

    card = build_card("patient-1", [LABS_GAP], set(), "n", Config.from_secrets(TWO_LABS))

    assert [rec.button for rec in card.recommendations] == [
        "Order at Quest",
        "Order at LabCorp",
    ]


def test_every_button_carries_the_complete_order() -> None:
    """The point of the feature: no row stages a partial order."""
    PatientFactory.create()
    configured_partner("Quest")
    configured_partner("LabCorp")

    card = build_card("patient-1", [LABS_GAP], set(), "n", Config.from_secrets(TWO_LABS))

    for rec in card.recommendations:
        assert rec.commands[0].tests_order_codes == ["496"]


def test_configured_order_decides_button_order() -> None:
    PatientFactory.create()
    configured_partner("Quest")
    configured_partner("LabCorp")
    reversed_config = Config.from_secrets(
        {**TWO_LABS, "LAB_PARTNER_NAME": "LabCorp, Quest"}
    )

    card = build_card("patient-1", [LABS_GAP], set(), "n", reversed_config)

    # The practice's first choice stays first, whatever order the DB returns.
    assert card.recommendations[0].button == "Order at LabCorp"


def test_only_the_first_row_repeats_the_lab_list() -> None:
    PatientFactory.create()
    configured_partner("Quest")
    configured_partner("LabCorp")

    card = build_card("patient-1", [LABS_GAP], set(), "n", Config.from_secrets(TWO_LABS))
    titles = [rec.title for rec in card.recommendations]

    assert titles[0] == LABS_GAP.label
    assert titles[1] == "…the same order, sent to LabCorp"


def test_a_partner_that_stocks_nothing_gets_no_button() -> None:
    PatientFactory.create()
    configured_partner("Quest")
    LabPartnerFactory.create(name="LabCorp", active=True)  # carries no tests

    card = build_card("patient-1", [LABS_GAP], set(), "n", Config.from_secrets(TWO_LABS))

    # Rendering a button Canvas would reject is worse than rendering none.
    assert [rec.button for rec in card.recommendations] == [LAB_ORDER_BUTTON]


def test_an_inactive_partner_gets_no_button() -> None:
    PatientFactory.create()
    configured_partner("Quest")
    retired = LabPartnerFactory.create(name="LabCorp", active=False)
    LabPartnerTestFactory.create(
        lab_partner=retired, order_name="HEMOGLOBIN A1c", order_code="496"
    )

    card = build_card("patient-1", [LABS_GAP], set(), "n", Config.from_secrets(TWO_LABS))

    assert [rec.button for rec in card.recommendations] == [LAB_ORDER_BUTTON]


def test_a_partner_stocking_only_some_codes_orders_only_those() -> None:
    PatientFactory.create()
    quest = configured_partner("Quest")
    LabPartnerTestFactory.create(
        lab_partner=quest, order_name="LIPID PANEL", order_code="7600"
    )
    configured_partner("LabCorp")  # stocks 496 only
    gap = Gap(
        key=GAP_LABS_OVERDUE,
        label="Monitoring labs due: hemoglobin A1c, lipid panel",
        detail={"missing_keys": ["hemoglobin a1c", "lipid panel"], "interval_days": 90},
    )
    config = Config.from_secrets(
        {
            "LAB_PARTNER_NAME": "Quest, LabCorp",
            "LAB_TEST_ORDER_CODES": "hemoglobin a1c:496, lipid panel:7600",
        }
    )

    card = build_card("patient-1", [gap], set(), "n", config)

    assert card.recommendations[0].commands[0].tests_order_codes == ["496", "7600"]
    assert card.recommendations[1].commands[0].tests_order_codes == ["496"]
