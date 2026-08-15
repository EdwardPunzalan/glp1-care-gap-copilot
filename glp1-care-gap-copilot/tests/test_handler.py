"""End-to-end handler behavior: event wiring, scoping, and effect shape."""

import json
from datetime import date
from typing import Any, cast
from unittest.mock import Mock, patch

from canvas_sdk.effects import Effect, EffectType
from canvas_sdk.events import EventType
from canvas_sdk.test_utils.factories import PatientFactory, TaskFactory
from canvas_sdk.v1.data.task import TaskStatus

from glp1_care_gap_copilot.card import (
    ALREADY_OPEN_SUFFIX,
    CARD_KEY,
    CONTACT_BUTTON,
    OUTREACH_BUTTON,
    SAFETY_CHECK_BUTTON,
)
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import task_title
from glp1_care_gap_copilot.gaps import (
    GAP_SAFETY_CHECK_DUE,
    GAP_SAFETY_REVIEW,
    GAP_STALE_WEIGHT,
)
from glp1_care_gap_copilot.handlers.care_gap_handler import GLP1CareGapHandler
from glp1_care_gap_copilot.safety_signals import BANNER_KEY
from tests.factories import (
    add_appointment,
    add_condition,
    add_medication,
    add_observation,
    add_weight,
    complete_safety_check,
    days_ago,
    days_ahead,
)

SECRETS: dict[str, str] = {}


def build_handler(patient_id: str, secrets: dict[str, str] | None = None) -> GLP1CareGapHandler:
    """A handler wired to a NOTE_OPENED event targeting the given patient."""
    event = Mock()
    event.type = EventType.NOTE_OPENED
    event.target = Mock(id=patient_id)
    event.context = {"note": {"id": "note-1"}}
    return GLP1CareGapHandler(event=event, secrets=secrets or SECRETS)


def payload_of(effect: Effect) -> dict[str, Any]:
    """The decoded payload of a protocol card effect."""
    return cast(dict[str, Any], json.loads(effect.payload))


def card_of(effects: list[Effect]) -> Effect:
    """The single protocol-card effect among a render's effects.

    Every render also carries a banner effect — raising the safety alert or
    clearing a stale one — so the card is selected by type rather than by index.
    """
    cards = [
        effect
        for effect in effects
        if effect.type == EffectType.ADD_OR_UPDATE_PROTOCOL_CARD
    ]
    assert len(cards) == 1, f"expected exactly one card, got {len(cards)}"
    return cards[0]


def test_handler_responds_to_both_chart_surfaces() -> None:
    assert GLP1CareGapHandler.RESPONDS_TO == [
        EventType.Name(EventType.NOTE_OPENED),
        EventType.Name(EventType.PATIENT_CHART__MEDICATIONS),
    ]


def test_out_of_scope_patient_gets_no_card_at_all() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Lisinopril 10 MG")

    assert build_handler(str(patient.id)).compute() == []


def test_glp1_patient_with_gaps_gets_a_due_card() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")

    effects = build_handler(str(patient.id)).compute()

    payload = payload_of(card_of(effects))
    assert payload["key"] == CARD_KEY
    assert payload["patient"] == str(patient.id)
    assert payload["data"]["status"] == "due"


def test_card_lists_every_open_gap_with_an_action() -> None:
    patient = PatientFactory.create()
    # Past the time-on-therapy gate, so the lab rule actually applies.
    add_medication(patient, "Tirzepatide 5 MG", start_date=days_ago(120))

    effects = build_handler(str(patient.id)).compute()
    recommendations = payload_of(effects[0])["data"]["recommendations"]

    titles = [rec["title"] for rec in recommendations]
    assert any("weight" in title.lower() for title in titles)
    assert any("lipid panel" in title.lower() for title in titles)
    assert any("follow-up" in title.lower() for title in titles)


def test_fully_monitored_patient_gets_a_satisfied_card() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    add_observation(patient, "weight", days_ago(3))
    add_appointment(patient, days_ahead(20))

    effects = build_handler(str(patient.id), SECRETS).compute()
    payload = payload_of(effects[0])

    # Started on therapy today, so the lab rule has not engaged yet and every
    # other rule is satisfied.
    assert payload["data"]["status"] == "satisfied"
    assert payload["data"]["recommendations"] == []


def test_obesity_condition_alone_is_enough_to_render_a_card() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E66.01", display="Morbid obesity")

    effects = build_handler(str(patient.id)).compute()

    assert card_of(effects) is not None


def test_an_open_task_removes_that_gaps_button() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(Config.from_secrets(SECRETS), GAP_STALE_WEIGHT, "No weight"),
    )

    effects = build_handler(str(patient.id)).compute()
    recommendations = payload_of(effects[0])["data"]["recommendations"]

    weight_rec = next(rec for rec in recommendations if "weight" in rec["title"].lower())
    assert "already open" in weight_rec["title"]
    assert not weight_rec.get("button")
    # Other gaps keep their buttons.
    followup = next(rec for rec in recommendations if "follow-up" in rec["title"].lower())
    assert followup["button"] == OUTREACH_BUTTON


def test_outreach_button_carries_a_staged_task_command() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")

    effects = build_handler(str(patient.id)).compute()
    recommendations = payload_of(effects[0])["data"]["recommendations"]

    weight_rec = next(rec for rec in recommendations if "weight" in rec["title"].lower())
    command = weight_rec["commands"][0]
    assert command["command"]["type"] == "task"
    # Staged, not committed — the clinician still reviews and signs it.
    assert command["context"]["effect_type"] == "ORIGINATE_TASK_COMMAND"
    assert command["context"]["title"].startswith("[GLP-1 Copilot: stale_weight]")


def test_narrative_is_present_on_the_card() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")

    effects = build_handler(str(patient.id)).compute()

    assert payload_of(effects[0])["data"]["narrative"]


def test_event_without_a_target_produces_nothing() -> None:
    event = Mock()
    event.type = EventType.NOTE_OPENED
    event.target = Mock(id=None)

    assert GLP1CareGapHandler(event=event, secrets=SECRETS).compute() == []


def test_an_unexpected_error_does_not_break_chart_rendering() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    handler = build_handler(str(patient.id))

    with patch(
        "glp1_care_gap_copilot.handlers.care_gap_handler.detect_gaps",
        side_effect=RuntimeError("database is on fire"),
    ):
        assert handler.compute() == []


def test_malformed_secrets_do_not_prevent_a_card() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    secrets = {
        "WEIGHT_CHECK_INTERVAL_DAYS": "not-a-number",
        "OUTREACH_TEAM_DBID": "the-weight-team",
    }

    effects = build_handler(str(patient.id), secrets).compute()

    assert card_of(effects) is not None


# --- the safety banner --------------------------------------------------------


def banner_of(effects: list[Effect]) -> Effect:
    """The single banner effect among a render's effects."""
    banners = [
        effect
        for effect in effects
        if effect.type
        in (EffectType.ADD_BANNER_ALERT, EffectType.REMOVE_BANNER_ALERT)
    ]
    assert len(banners) == 1, f"expected exactly one banner, got {len(banners)}"
    return banners[0]


def rapid_loss_with_symptom(patient: Any) -> None:
    """A 9 lb weekly drop plus a positive safety check."""
    add_weight(patient, 250.0, days_ago(21))
    add_weight(patient, 248.0, days_ago(14))
    add_weight(patient, 239.0, days_ago(7))
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(5))


def test_a_triggered_safety_signal_raises_an_alert_banner() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_with_symptom(patient)

    effects = build_handler(str(patient.id)).compute()
    banner = banner_of(effects)

    assert banner.type == EffectType.ADD_BANNER_ALERT
    payload = payload_of(banner)
    # `key` sits at the top level of a banner payload; the styling and text
    # live under `data`.
    assert payload["key"] == BANNER_KEY
    assert payload["data"]["intent"] == "alert"
    assert payload["data"]["placement"] == ["chart"]
    assert "nausea" in payload["data"]["narrative"]


def test_the_safety_row_leads_the_card() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_with_symptom(patient)

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    recommendations = payload["data"]["recommendations"]

    # A safety signal outranks every routine monitoring gap on the card.
    assert "Rapid weight loss" in recommendations[0]["title"]
    assert recommendations[0]["button"] == CONTACT_BUTTON
    assert payload["data"]["narrative"].startswith("SAFETY:")


def test_a_patient_without_the_signal_gets_the_banner_cleared() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")

    banner = banner_of(build_handler(str(patient.id)).compute())

    # Emitting nothing would leave a previously-raised banner on the chart
    # forever, so the resolved case actively removes it.
    assert banner.type == EffectType.REMOVE_BANNER_ALERT
    # A remove payload carries only the identity — no `data` block.
    assert payload_of(banner)["key"] == BANNER_KEY


def test_an_open_safety_task_removes_the_contact_button() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_with_symptom(patient)
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(Config.from_secrets({}), GAP_SAFETY_REVIEW, "already called"),
    )

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    safety_row = payload["data"]["recommendations"][0]

    assert safety_row.get("button") in (None, "")
    assert ALREADY_OPEN_SUFFIX.strip(" —") in safety_row["title"]


# --- the screening ask ---------------------------------------------------------


def rapid_loss_unscreened(patient: Any) -> None:
    """A 9 lb weekly drop with nobody having completed a safety check."""
    add_weight(patient, 250.0, days_ago(21))
    add_weight(patient, 248.0, days_ago(14))
    add_weight(patient, 239.0, days_ago(7))


def test_an_unscreened_rapid_drop_asks_an_ma_to_screen() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_unscreened(patient)
    add_appointment(patient, days_ahead(14))

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    row = payload["data"]["recommendations"][0]

    # The row names what is missing, not merely that a form is missing.
    assert "oral intake" in row["title"]
    assert "protein intake" in row["title"]
    assert "muscle loss" in row["title"]
    assert "not assessed" in row["title"]
    assert "screen at next visit" in row["title"]
    assert row["button"] == SAFETY_CHECK_BUTTON
    assert row["commands"][0]["context"]["title"].startswith(
        "[GLP-1 Copilot: safety_check_due]"
    )


def test_with_no_visit_booked_both_tasks_are_offered() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_unscreened(patient)

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    rows = payload["data"]["recommendations"]
    titles = [row["title"] for row in rows]
    buttons = [row.get("button") for row in rows]

    # The screening row says there is nowhere to screen, and the scheduling
    # row sits alongside it so the clinician can send both.
    assert any("no visit booked" in title for title in titles)
    assert any("follow-up appointment" in title for title in titles)
    assert SAFETY_CHECK_BUTTON in buttons
    assert OUTREACH_BUTTON in buttons


def test_a_completed_screen_removes_the_ask() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_unscreened(patient)
    complete_safety_check(patient, (), created=days_ago(3))

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    titles = [row["title"] for row in payload["data"]["recommendations"]]

    assert not any("not assessed" in title for title in titles)


def test_a_triggered_alert_still_asks_for_the_missing_screen() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_unscreened(patient)
    add_condition(patient, "E86.0", display="Dehydration", onset_date=date.today())

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    titles = [row["title"] for row in payload["data"]["recommendations"]]

    # Contact-the-patient leads; the screening ask follows it — and says
    # something different, naming what the dehydration code cannot tell us.
    assert "dehydration" in titles[0]
    assert "not assessed" in titles[1]
    assert "dehydration" not in titles[1]


def test_an_open_screening_task_removes_that_button() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    rapid_loss_unscreened(patient)
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(
            Config.from_secrets({}), GAP_SAFETY_CHECK_DUE, "already asked"
        ),
    )

    payload = payload_of(card_of(build_handler(str(patient.id)).compute()))
    row = next(
        r for r in payload["data"]["recommendations"] if "not assessed" in r["title"]
    )

    assert not row.get("button")
