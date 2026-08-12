"""End-to-end handler behavior: event wiring, scoping, and effect shape."""

import json
from typing import Any, cast
from unittest.mock import Mock, patch

from canvas_sdk.effects import Effect, EffectType
from canvas_sdk.events import EventType
from canvas_sdk.test_utils.factories import PatientFactory, TaskFactory
from canvas_sdk.v1.data.task import TaskStatus

from glp1_care_gap_copilot.card import CARD_KEY, OUTREACH_BUTTON
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import task_title
from glp1_care_gap_copilot.gaps import GAP_STALE_WEIGHT
from glp1_care_gap_copilot.handlers.care_gap_handler import GLP1CareGapHandler
from tests.factories import (
    add_appointment,
    add_condition,
    add_medication,
    add_observation,
    days_ago,
    days_ahead,
)

SECRETS = {"REQUIRED_LAB_NAMES": "lipid panel"}


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

    assert len(effects) == 1
    assert effects[0].type == EffectType.ADD_OR_UPDATE_PROTOCOL_CARD
    payload = payload_of(effects[0])
    assert payload["key"] == CARD_KEY
    assert payload["patient"] == str(patient.id)
    assert payload["data"]["status"] == "due"


def test_card_lists_every_open_gap_with_an_action() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Tirzepatide 5 MG")

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
    secrets = {**SECRETS, "REQUIRED_LAB_NAMES": "hemoglobin a1c"}

    effects = build_handler(str(patient.id), secrets).compute()
    payload = payload_of(effects[0])

    # A1c is diabetes-conditional and this patient has no diabetes diagnosis,
    # so no lab gap applies and every rule is satisfied.
    assert payload["data"]["status"] == "satisfied"
    assert payload["data"]["recommendations"] == []


def test_obesity_condition_alone_is_enough_to_render_a_card() -> None:
    patient = PatientFactory.create()
    add_condition(patient, "E66.01", display="Morbid obesity")

    effects = build_handler(str(patient.id)).compute()

    assert len(effects) == 1


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

    assert len(effects) == 1
