"""Chart summary section wiring: event acceptance, layout, and rendered content."""

import json
from typing import Any, cast
from unittest.mock import Mock, patch

from canvas_sdk.effects import Effect, EffectType
from canvas_sdk.effects.patient_chart_summary_configuration import (
    PatientChartSummaryConfiguration,
)
from canvas_sdk.events import EventType
from canvas_sdk.test_utils.factories import PatientFactory

from canvas_sdk.handlers.action_button import ActionButton

from glp1_care_gap_copilot.handlers.weight_trend_handler import (
    BUILT_IN_SECTIONS,
    MODAL_TITLE,
    SECTION_KEY,
    GLP1ChartSummaryConfiguration,
    GLP1WeightTrendButton,
    GLP1WeightTrendSection,
)
from glp1_care_gap_copilot.render import NO_WEIGHTS_MESSAGE, OUT_OF_SCOPE_MESSAGE
from tests.factories import add_medication, add_weight, days_ago

SECRETS: dict[str, str] = {}


def build_section(
    patient_id: str, secrets: dict[str, str] | None = None
) -> GLP1WeightTrendSection:
    """A section handler wired to a request for this plugin's section."""
    event = Mock()
    event.type = EventType.PATIENT_CHART_SUMMARY__GET_CUSTOM_SECTION
    event.target = Mock(id=patient_id)
    event.context = {"section": SECTION_KEY}
    return GLP1WeightTrendSection(event=event, secrets=secrets or SECRETS)


def content_of(effect: Effect) -> str:
    """The HTML a custom section effect carries."""
    payload = cast(dict[str, Any], json.loads(effect.payload))
    data = payload.get("data", payload)
    return cast(str, data["content"])


def render_for(patient_id: str, secrets: dict[str, str] | None = None) -> str:
    """The HTML rendered into the chart summary for this patient."""
    effects = build_section(patient_id, secrets).compute()
    assert len(effects) == 1
    return content_of(effects[0])


def glp1_patient() -> Any:
    """A patient in scope for the copilot."""
    patient = PatientFactory.create()
    add_medication(patient, "Ozempic 1 mg/dose subcutaneous pen injector")
    return patient


def test_section_only_answers_requests_for_its_own_key() -> None:
    handler = build_section("patient-1")
    assert handler.accept_event() is True

    handler.event.context = {"section": "some_other_plugins_section"}
    assert handler.accept_event() is False


def test_layout_handler_listens_for_the_configuration_event() -> None:
    assert GLP1ChartSummaryConfiguration.RESPONDS_TO == [
        EventType.Name(EventType.PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION)
    ]


def test_layout_puts_our_section_first_without_dropping_any_built_in() -> None:
    # The configuration effect replaces the whole layout, so a missing built-in
    # here means that section silently disappears from every patient's chart.
    effects = GLP1ChartSummaryConfiguration(event=Mock(), secrets=SECRETS).compute()

    assert len(effects) == 1
    sections = json.loads(effects[0].payload)["data"]["sections"]
    keys = [section["key"] for section in sections]

    assert keys[0] == SECTION_KEY
    assert sections[0]["custom"] is True
    # Every section Canvas ships must still be listed, or it vanishes.
    assert len(BUILT_IN_SECTIONS) == len(PatientChartSummaryConfiguration.Section)
    for section in PatientChartSummaryConfiguration.Section:
        assert section.value in keys


def test_layout_failure_leaves_canvas_defaults_alone() -> None:
    handler = GLP1ChartSummaryConfiguration(event=Mock(), secrets=SECRETS)
    with patch(
        "glp1_care_gap_copilot.handlers.weight_trend_handler."
        "PatientChartSummaryConfiguration",
        side_effect=RuntimeError("boom"),
    ):
        # An empty list means "no opinion", which keeps the default layout —
        # far better than emitting a summary with no sections in it.
        assert handler.compute() == []


def test_effect_is_a_chart_summary_custom_section() -> None:
    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(1))

    effects = build_section(str(patient.id)).compute()

    assert effects[0].type == EffectType.PATIENT_CHART_SUMMARY__CUSTOM_SECTION


def test_out_of_scope_patient_sees_why_the_section_is_empty() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Lisinopril 10 MG")
    add_weight(patient, 240.0, days_ago(1))

    assert OUT_OF_SCOPE_MESSAGE in render_for(str(patient.id))


def test_in_scope_patient_with_no_weights_gets_a_plain_sentence() -> None:
    assert NO_WEIGHTS_MESSAGE in render_for(str(glp1_patient().id))


def test_graph_renders_a_point_and_a_date_for_each_weigh_in() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(60))
    add_weight(patient, 246.0, days_ago(30))
    add_weight(patient, 244.0, days_ago(1))

    html = render_for(str(patient.id))

    assert html.count("<circle") == 3
    assert "<polyline" in html
    assert ">250<" in html and ">244<" in html


def test_a_rapid_drop_renders_the_red_highlight_and_a_warning() -> None:
    patient = glp1_patient()
    add_weight(patient, 252.0, days_ago(8))
    add_weight(patient, 238.0, days_ago(1))

    html = render_for(str(patient.id))

    # The class names always appear in the stylesheet, so assert on the
    # elements actually emitted into the plot.
    assert '<rect class="wt-drop-band"' in html
    assert '<line class="wt-drop-line"' in html
    assert "-14 lb in 7d" in html
    assert "Rapid weight loss" in html
    assert "more than 10 lb lost within 7 days" in html


def test_a_gradual_loss_renders_no_highlight_at_all() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(30))
    add_weight(patient, 244.0, days_ago(1))

    html = render_for(str(patient.id))

    assert '<rect class="wt-drop-band"' not in html
    assert '<line class="wt-drop-line"' not in html
    assert "Rapid weight loss" not in html


def test_multiple_drops_are_counted_in_the_warning() -> None:
    patient = glp1_patient()
    add_weight(patient, 270.0, days_ago(22))
    add_weight(patient, 254.0, days_ago(15))
    add_weight(patient, 252.0, days_ago(8))
    add_weight(patient, 236.0, days_ago(1))

    html = render_for(str(patient.id))

    assert "2 intervals" in html


def test_thresholds_and_point_count_are_configurable() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(60))
    add_weight(patient, 244.0, days_ago(30))
    add_weight(patient, 240.0, days_ago(1))

    tightened = render_for(
        str(patient.id),
        {
            "WEIGHT_DROP_ALERT_LB": "3",
            "WEIGHT_TREND_POINTS": "2",
            "WEIGHT_DROP_MAX_INTERVAL_DAYS": "30",
        },
    )

    # Only the newest two readings are plotted, and their 4 lb gap now trips
    # the lowered threshold.
    assert tightened.count("<circle") == 2
    assert "more than 3 lb lost within 30 days" in tightened


def test_a_missing_target_degrades_instead_of_raising() -> None:
    event = Mock()
    event.target = None
    event.context = {"section": SECTION_KEY}
    handler = GLP1WeightTrendSection(event=event, secrets=SECRETS)

    assert NO_WEIGHTS_MESSAGE in content_of(handler.compute()[0])


def test_an_unexpected_failure_still_returns_a_section() -> None:
    patient = glp1_patient()
    handler = build_section(str(patient.id))
    with patch(
        "glp1_care_gap_copilot.handlers.weight_trend_handler.build_patient_graph",
        side_effect=RuntimeError("boom"),
    ):
        effects = handler.compute()

    # A broken section must still render something rather than blank the
    # chart summary or bubble an exception into the page.
    assert len(effects) == 1
    assert NO_WEIGHTS_MESSAGE in content_of(effects[0])


def test_fractional_thresholds_render_without_a_trailing_zero() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(8))
    add_weight(patient, 243.0, days_ago(1))

    html = render_for(str(patient.id), {"WEIGHT_DROP_ALERT_LB": "6.5"})

    # "6.5 lb", not "6.5000000 lb" — and a whole number stays "10", not "10.0".
    assert "more than 6.5 lb lost within 7 days" in html


# --- the full-size modal -----------------------------------------------------


def build_button(
    patient_id: str, secrets: dict[str, str] | None = None
) -> GLP1WeightTrendButton:
    """A patient-header button rendered for the given patient."""
    event = Mock()
    event.target = Mock(id=patient_id)
    event.context = {}
    return GLP1WeightTrendButton(event=event, secrets=secrets or SECRETS)


def modal_content(effect: Effect) -> str:
    """The HTML a launch-modal effect carries."""
    payload = cast(dict[str, Any], json.loads(effect.payload))
    return cast(str, payload.get("data", payload)["content"])


def test_the_button_sits_in_the_patient_header() -> None:
    # Canvas has no ButtonLocation for a custom summary section, so the
    # full-size view is reached from the header instead.
    assert (
        GLP1WeightTrendButton.BUTTON_LOCATION
        == ActionButton.ButtonLocation.CHART_PATIENT_HEADER
    )
    # Must stay within the ~68px of text Canvas allows a header button, or it
    # renders ellipsised. Character count is not the constraint — pixel width
    # is — so this pins the measured-good label rather than a length limit.
    assert GLP1WeightTrendButton.BUTTON_TITLE == "Weight log"


def test_the_button_is_offered_when_there_is_a_graph() -> None:
    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(1))

    assert build_button(str(patient.id)).visible() is True


def test_the_button_is_hidden_when_there_is_nothing_to_enlarge() -> None:
    # In scope, but never weighed.
    assert build_button(str(glp1_patient().id)).visible() is False


def test_the_button_is_hidden_for_out_of_scope_patients() -> None:
    patient = PatientFactory.create()
    add_medication(patient, "Lisinopril 10 MG")
    add_weight(patient, 240.0, days_ago(1))

    assert build_button(str(patient.id)).visible() is False


def test_a_failed_visibility_check_hides_the_button() -> None:
    handler = build_button("patient-1")
    with patch(
        "glp1_care_gap_copilot.handlers.weight_trend_handler.build_patient_graph",
        side_effect=RuntimeError("boom"),
    ):
        # A broken check must not take the patient header down with it.
        assert handler.visible() is False


def test_clicking_opens_the_same_graph_at_modal_size() -> None:
    patient = glp1_patient()
    add_weight(patient, 252.0, days_ago(8))
    add_weight(patient, 238.0, days_ago(1))

    effects = build_button(str(patient.id)).handle()

    assert len(effects) == 1
    assert effects[0].type == EffectType.LAUNCH_MODAL
    content = modal_content(effects[0])
    assert "wt-modal" in content
    # Same rule, same flag, same numbers as the section.
    assert '<rect class="wt-drop-band"' in content
    assert "-14 lb in 7d" in content
    assert "Rapid weight loss" in content


def test_the_modal_is_titled() -> None:
    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(1))

    payload = cast(
        dict[str, Any], json.loads(build_button(str(patient.id)).handle()[0].payload)
    )
    assert payload.get("data", payload)["title"] == MODAL_TITLE


def test_clicking_without_a_target_still_returns_a_modal() -> None:
    event = Mock()
    event.target = None
    event.context = {}
    handler = GLP1WeightTrendButton(event=event, secrets=SECRETS)

    effects = handler.handle()

    assert len(effects) == 1
    assert NO_WEIGHTS_MESSAGE in modal_content(effects[0])


def test_the_section_no_longer_ships_a_dead_expand_button() -> None:
    # An in-section button cannot launch a modal, so shipping one would be a
    # control that does nothing when clicked.
    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(1))

    html = render_for(str(patient.id))

    assert "wt-expand" not in html
    assert "fetch(" not in html


def test_the_button_is_hidden_when_there_is_no_patient_in_context() -> None:
    event = Mock()
    event.target = None
    event.context = {}

    assert GLP1WeightTrendButton(event=event, secrets=SECRETS).visible() is False
