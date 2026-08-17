"""The monitoring hub: data loading, the page, and its task buttons."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, cast
from unittest.mock import Mock, patch

from canvas_sdk.effects import Effect, EffectType
from canvas_sdk.test_utils.factories import PatientFactory
from canvas_sdk.v1.data.medication import Medication, MedicationCoding
from canvas_sdk.v1.data.patient import Patient
from django.db import connection
from django.test.utils import CaptureQueriesContext

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.handlers.hub_handler import (
    PAGE_PATH,
    TASK_PATH,
    GLP1Hub,
    GLP1HubAPI,
    render_hub,
)
from glp1_care_gap_copilot.hub import (
    SPARK_POINTS,
    Prescription,
    WatchedPatient,
    _missing,
    _with_display,
    load_hub,
    sparkline,
)
from tests.factories import (
    add_appointment,
    add_lab_result,
    add_medication,
    add_observation,
    add_weight,
    complete_safety_check,
    days_ago,
    days_ahead,
    now,
)

CONFIG = Config.from_secrets({})
NOW = datetime.now(timezone.utc)


def glp1_patient(
    first: str = "Ada", last: str = "Lovelace", started_days: int = 200
) -> Patient:
    patient = PatientFactory.create(first_name=first, last_name=last)
    add_medication(
        patient, "Semaglutide 0.5 MG", start_date=days_ago(started_days)
    )
    return patient


# --- who appears --------------------------------------------------------------


def test_only_patients_on_a_glp1_are_watched() -> None:
    on_drug = glp1_patient()
    other = PatientFactory.create(first_name="Not", last_name="Onit")
    add_medication(other, "Lisinopril 10 MG")

    watched = load_hub(CONFIG, {}, now=NOW)

    assert [p.patient_id for p in watched] == [str(on_drug.id)]


def test_a_patient_appears_once_however_many_codings() -> None:
    patient = glp1_patient()
    add_medication(patient, "Ozempic 1 MG", start_date=days_ago(90))

    watched = load_hub(CONFIG, {}, now=NOW)

    assert len(watched) == 1
    # Two distinct drugs, listed once each rather than once per coding row.
    assert len(watched[0].prescriptions) == 2


def test_one_drug_coded_twice_is_listed_once() -> None:
    patient = glp1_patient()
    medication = Medication.objects.filter(patient=patient).first()
    # Canvas routinely carries several codings for one prescription; listing the
    # drug once per coding would read as though it had been prescribed twice.
    MedicationCoding.objects.create(
        medication=medication,
        system="http://snomed.info/sct",
        code="111111",
        display="Semaglutide 0.5 MG",
        user_selected=False,
    )

    watched = load_hub(CONFIG, {}, now=NOW)

    assert [rx.name for rx in watched[0].prescriptions] == ["Semaglutide 0.5 MG"]


def test_high_risk_patients_sort_to_the_front() -> None:
    glp1_patient("Aaron", "Aardvark")
    risky = glp1_patient("Zoe", "Zimmer")

    watched = load_hub(CONFIG, {str(risky.id): "Rapid loss with dehydration"}, now=NOW)

    # Alphabetically last, but first on the page because risk outranks name.
    assert watched[0].patient_id == str(risky.id)
    assert watched[0].is_high_risk is True
    assert watched[1].is_high_risk is False


def test_nobody_is_watched_when_no_drug_names_are_configured() -> None:
    """An empty `icontains` fragment matches every medication ever written.

    `Config` will not hand back an empty list today, but the failure mode if it
    ever did is the whole panel appearing on the hub, so the guard is pinned.
    """
    glp1_patient()
    blank = replace(CONFIG, glp1_med_name_fragments=())

    assert load_hub(blank, {}, now=NOW) == []


def test_a_blank_fragment_is_skipped_rather_than_matching_everything() -> None:
    patient = glp1_patient()
    add_medication(patient, "Lisinopril 10 MG", start_date=days_ago(30))
    sloppy = replace(CONFIG, glp1_med_name_fragments=("semaglutide", " "))

    watched = load_hub(sloppy, {}, now=NOW)

    assert len(watched) == 1
    assert [rx.name for rx in watched[0].prescriptions] == ["Semaglutide 0.5 MG"]


def test_an_unreadable_weight_is_skipped_not_plotted() -> None:
    patient = glp1_patient()
    add_observation(patient, "weight", days_ago(3), value="not a number", units="oz")
    add_weight(patient, 240.0, days_ago(1))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert card.weights == ((card.weights[0][0], 240.0),)


def test_the_cohort_is_capped() -> None:
    for index in range(4):
        glp1_patient(f"P{index}", "Test")
    capped = Config.from_secrets({"HUB_MAX_PATIENTS": "2"})

    assert len(load_hub(capped, {}, now=NOW)) == 2


# --- what a card shows --------------------------------------------------------


def test_a_card_carries_weights_labs_and_followup() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(30))
    add_weight(patient, 244.0, days_ago(2))
    add_appointment(patient, days_ahead(14))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert card.latest_weight == 244.0
    assert card.weight_change == 6.0
    assert card.next_followup is not None
    assert card.spark  # a polyline was built
    assert card.chips  # lab recency chips exist


def test_only_the_newest_weigh_ins_are_plotted() -> None:
    patient = glp1_patient()
    for day in range(SPARK_POINTS + 6):
        add_weight(patient, 250.0 - day, days_ago(day + 1))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert len(card.weights) == SPARK_POINTS
    # Oldest first, so the newest reading is the last point on the line.
    assert card.weights[0][0] < card.weights[-1][0]


def test_weights_outside_the_window_are_not_plotted() -> None:
    patient = glp1_patient()
    add_weight(patient, 300.0, days_ago(800))
    add_weight(patient, 240.0, days_ago(3))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert len(card.weights) == 1


def test_a_missing_weight_is_an_actionable_item() -> None:
    glp1_patient()

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert any(item.gap_key == "stale_weight" for item in card.missing)
    assert any(item.gap_key == "no_followup" for item in card.missing)


def test_labs_are_not_chased_before_the_therapy_gate() -> None:
    patient = glp1_patient(started_days=20)
    add_weight(patient, 240.0, days_ago(1))
    add_appointment(patient, days_ahead(10))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    # The chart card would not ask for labs yet, so neither does the hub.
    assert [item.gap_key for item in card.missing] == []


def test_labs_are_chased_once_past_the_gate() -> None:
    patient = glp1_patient(started_days=200)
    add_weight(patient, 240.0, days_ago(1))
    add_appointment(patient, days_ahead(10))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert [item.gap_key for item in card.missing] == ["labs_overdue"]


def test_tsh_is_only_shown_once_resulted() -> None:
    glp1_patient()

    chips = load_hub(CONFIG, {}, now=NOW)[0].chips

    # Showing "TSH missing" for everyone would invent a gap the chart card
    # would not agree with, since the hub cannot see a thyroid diagnosis.
    assert all("TSH" not in chip.label for chip in chips)
    assert len(chips) == 3


def test_a_resulted_tsh_earns_its_own_chip() -> None:
    patient = glp1_patient()
    add_lab_result(patient, "TSH", days_ago(20))

    chips = load_hub(CONFIG, {}, now=now())[0].chips

    tsh = next(chip for chip in chips if "TSH" in chip.label)
    assert tsh.text == "20d"
    assert tsh.state == "ok"


def test_a_lab_chip_turns_warn_once_past_the_interval() -> None:
    patient = glp1_patient()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(200))

    chips = load_hub(CONFIG, {}, now=now())[0].chips
    a1c = next(chip for chip in chips if "a1c" in chip.label.lower())

    assert a1c.text == "200d"
    assert a1c.state == "warn"


def test_the_newest_result_is_the_one_shown() -> None:
    patient = glp1_patient()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(200))
    add_lab_result(patient, "Hemoglobin A1c", days_ago(12))

    chips = load_hub(CONFIG, {}, now=now())[0].chips
    a1c = next(chip for chip in chips if "a1c" in chip.label.lower())

    assert a1c.text == "12d"


def test_an_older_result_does_not_overwrite_a_newer_one() -> None:
    """Row order is the database's choice, so the comparison has to hold both ways."""
    patient = glp1_patient()
    add_lab_result(patient, "Hemoglobin A1c", days_ago(12))
    add_lab_result(patient, "Hemoglobin A1c", days_ago(200))

    chips = load_hub(CONFIG, {}, now=now())[0].chips
    a1c = next(chip for chip in chips if "a1c" in chip.label.lower())

    assert a1c.text == "12d"


def test_current_labs_are_not_asked_for_again() -> None:
    patient = glp1_patient(started_days=200)
    add_weight(patient, 240.0, days_ago(1))
    add_appointment(patient, days_ahead(10))
    for name in ("Comprehensive metabolic panel", "Lipid panel", "Hemoglobin A1c"):
        add_lab_result(patient, name, days_ago(10))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert card.missing == ()


def test_a_stale_weight_is_chased_by_age() -> None:
    patient = glp1_patient(started_days=20)
    add_weight(patient, 240.0, days_ago(120))
    add_appointment(patient, days_ahead(10))

    card = load_hub(CONFIG, {}, now=now())[0]

    assert [item.gap_key for item in card.missing] == ["stale_weight"]
    assert "120 days" in card.missing[0].label


# --- the sparkline ------------------------------------------------------------


def test_a_single_reading_draws_no_line() -> None:
    assert sparkline(((NOW, 240.0),)) == ""


def test_a_card_with_one_reading_has_no_line_to_close_or_mark() -> None:
    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(2))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert card.spark_area == ""
    assert card.spark_last is None
    assert card.weight_change is None
    assert card.latest_weight == 240.0


def test_two_readings_at_the_same_moment_fall_back_to_even_spacing() -> None:
    moment = days_ago(5)
    points = sparkline(((moment, 250.0), (moment, 240.0)))

    xs = [float(point.split(",")[0]) for point in points.split(" ")]
    # No elapsed time to scale by, so index spacing is the only sane fallback.
    assert xs == [4.0, 216.0]


def test_a_flat_series_is_centred_rather_than_dividing_by_zero() -> None:
    points = sparkline(((days_ago(10), 200.0), (days_ago(1), 200.0)))

    ys = {point.split(",")[1] for point in points.split(" ")}
    assert len(ys) == 1


def test_the_line_is_scaled_by_time_not_by_index() -> None:
    points = sparkline(
        ((days_ago(200), 250.0), (days_ago(190), 248.0), (days_ago(1), 240.0))
    )
    xs = [float(point.split(",")[0]) for point in points.split(" ")]

    # A ten-day step then a six-month gap must not be drawn evenly spaced.
    assert xs[1] - xs[0] < (xs[2] - xs[1]) / 5


def test_the_area_closes_to_the_baseline() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(20))
    add_weight(patient, 240.0, days_ago(1))

    card = load_hub(CONFIG, {}, now=NOW)[0]

    assert card.spark_area.startswith("4.0,44.0")
    assert card.spark_area.endswith("44.0")
    assert card.spark_last is not None


# --- query budget -------------------------------------------------------------


def test_the_page_cost_does_not_grow_with_patient_count() -> None:
    """The whole point of bulk loading: a bigger panel is not a slower page."""

    def build(count: int) -> int:
        for index in range(count):
            patient = glp1_patient(f"Pt{index}", "Bulk")
            add_weight(patient, 240.0, days_ago(3))
            add_appointment(patient, days_ahead(20))
        with CaptureQueriesContext(connection) as captured:
            load_hub(CONFIG, {}, now=NOW)
        return len(captured)

    one = build(1)
    many = build(6)

    assert many == one


# --- the page -----------------------------------------------------------------


def render(secrets: dict[str, str] | None = None) -> str:
    with patch(
        "glp1_care_gap_copilot.handlers.hub_handler.scan_for_alerts", return_value=[]
    ):
        return render_hub(Config.from_secrets(secrets or {}), now=NOW)


def test_the_page_lists_a_watched_patient() -> None:
    patient = glp1_patient("Ada", "Lovelace")
    add_weight(patient, 240.0, days_ago(2))

    html = render()

    assert "Ada Lovelace" in html
    assert str(patient.id) in html
    assert "Watched patients" in html


def test_the_page_says_so_when_nobody_is_on_a_glp1() -> None:
    assert "No patients are on an active GLP-1" in render()


def test_the_page_bands_high_risk_patients_at_the_top() -> None:
    patient = glp1_patient("Zoe", "Zimmer")
    add_weight(patient, 250.0, days_ago(15))
    add_weight(patient, 248.0, days_ago(8))
    add_weight(patient, 239.0, days_ago(1))
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(2))

    html = render_hub(CONFIG, now=NOW)

    assert "Needs contact today" in html
    assert html.index("Needs contact today") < html.index("Watched patients")


def test_the_page_offers_a_task_button_for_a_missing_item() -> None:
    glp1_patient()

    html = render()

    assert 'data-gap="stale_weight"' in html
    assert "Create task" in html
    assert TASK_PATH in html


def test_the_page_uses_no_ad_prefixed_classes() -> None:
    """Ad-blocker filter lists hide `ad-*` elements, which once blanked a page."""
    glp1_patient()

    html = render()

    assert 'class="ad' not in html
    assert "gh-card" in html


# --- the app and its route ----------------------------------------------------


def test_the_app_opens_a_page_not_a_modal() -> None:
    effect = GLP1Hub(event=Mock(), secrets={}).on_open()
    payload = json.loads(cast(Effect, effect).payload)

    assert payload["data"]["target"] == "page"
    # Cache-busted: a stale response at this URL once made a deployed fix look
    # like the plugin was broken.
    assert payload["data"]["url"].startswith(f"{PAGE_PATH}?v=")


def test_the_badge_counts_every_high_risk_patient() -> None:
    handler = GLP1Hub(event=Mock(), secrets={})
    alerts = [Mock(patient_id="a", is_triggered=True, headline="x"),
              Mock(patient_id="b", is_triggered=True, headline="y"),
              Mock(patient_id="c", is_triggered=False, headline="z")]

    with patch(
        "glp1_care_gap_copilot.handlers.hub_handler.scan_for_alerts",
        return_value=alerts,
    ):
        # Two triggered, one merely unscreened — the badge is for the urgent.
        assert handler.compute_notification_badge() == 2


def test_a_broken_badge_shows_nothing_rather_than_a_wrong_number() -> None:
    handler = GLP1Hub(event=Mock(), secrets={})

    with patch(
        "glp1_care_gap_copilot.handlers.hub_handler.scan_for_alerts",
        side_effect=RuntimeError("boom"),
    ):
        assert handler.compute_notification_badge() is None


def build_api(body: dict[str, Any] | None = None) -> GLP1HubAPI:
    event = Mock()
    event.context = {"method": "POST", "path": TASK_PATH, "body": "", "headers": {}}
    handler = GLP1HubAPI(event=event, secrets={})
    request = Mock()
    request.json.return_value = body or {}
    handler.__dict__["request"] = request
    return handler


def test_the_route_serves_the_page() -> None:
    glp1_patient()
    event = Mock()
    event.context = {"method": "GET", "path": PAGE_PATH, "body": "", "headers": {}}
    handler = GLP1HubAPI(event=event, secrets={})

    with patch(
        "glp1_care_gap_copilot.handlers.hub_handler.scan_for_alerts", return_value=[]
    ):
        responses = handler.page()

    assert responses[0].status_code == HTTPStatus.OK
    assert b"GLP-1 Monitoring Hub" in responses[0].content


def test_a_broken_page_still_returns_html() -> None:
    event = Mock()
    event.context = {"method": "GET", "path": PAGE_PATH, "body": "", "headers": {}}
    handler = GLP1HubAPI(event=event, secrets={})

    with patch(
        "glp1_care_gap_copilot.handlers.hub_handler.render_hub",
        side_effect=RuntimeError("boom"),
    ):
        responses = handler.page()

    assert responses[0].status_code == HTTPStatus.OK
    assert b"could not be built" in responses[0].content


def test_the_route_files_a_marked_task() -> None:
    handler = build_api(
        {"patient_id": "p1", "gap_key": "stale_weight", "summary": "No weight on file"}
    )

    results = handler.create_task()
    effect = next(r for r in results if getattr(r, "type", None) == EffectType.CREATE_TASK)
    response = next(r for r in results if hasattr(r, "status_code"))

    assert json.loads(effect.payload)["data"]["title"].startswith(
        "[GLP-1 Copilot: stale_weight]"
    )
    # Accepted, not created: the effect is applied after the response returns.
    assert response.status_code == HTTPStatus.ACCEPTED


def test_the_route_refuses_an_unknown_gap() -> None:
    handler = build_api({"patient_id": "p1", "gap_key": "whatever", "summary": "hi"})

    results = handler.create_task()

    # An unmarked title would be invisible to the dedupe forever after.
    assert all(getattr(r, "type", None) != EffectType.CREATE_TASK for r in results)
    assert results[0].status_code == HTTPStatus.BAD_REQUEST


def test_the_route_refuses_a_missing_patient() -> None:
    handler = build_api({"gap_key": "stale_weight", "summary": "hi"})

    assert handler.create_task()[0].status_code == HTTPStatus.BAD_REQUEST


def test_the_scan_route_files_what_tonight_would_have_filed() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(15))
    add_weight(patient, 248.0, days_ago(8))
    add_weight(patient, 239.0, days_ago(1))

    results = build_api().scan_now()
    tasks = [r for r in results if getattr(r, "type", None) == EffectType.CREATE_TASK]
    response = next(r for r in results if hasattr(r, "status_code"))

    assert tasks
    assert json.loads(response.content)["filed"] == len(tasks)


def test_the_scan_route_reports_an_empty_run() -> None:
    glp1_patient()

    results = build_api().scan_now()

    assert len(results) == 1
    assert json.loads(results[0].content)["filed"] == 0


def test_a_failed_scan_files_nothing_and_says_so() -> None:
    with patch(
        "glp1_care_gap_copilot.handlers.hub_handler.run_scan",
        side_effect=RuntimeError("boom"),
    ):
        results = build_api().scan_now()

    assert all(getattr(r, "type", None) != EffectType.CREATE_TASK for r in results)
    assert results[0].status_code == HTTPStatus.INTERNAL_SERVER_ERROR


def test_the_page_offers_the_manual_scan() -> None:
    """The user asked for a way to run the nightly job on demand for testing."""
    assert 'data-action="scan"' in render()


def test_the_route_requires_a_staff_session() -> None:
    """The panel is PHI and the routes write tasks, so neither is anonymous.

    It does not decide authorship: a filed task is created by Canvas Bot
    regardless, which was only discoverable by filing one on a live instance.
    """
    from canvas_sdk.handlers.simple_api import StaffSessionAuthMixin

    assert issubclass(GLP1HubAPI, StaffSessionAuthMixin)


def test_the_page_costs_a_fixed_number_of_queries() -> None:
    """Documented in the README; asserted here so the number cannot drift."""
    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(3))
    add_appointment(patient, days_ahead(20))

    with CaptureQueriesContext(connection) as captured:
        load_hub(CONFIG, {}, now=NOW)

    assert len(captured) == 4


# --- a prescription with no start date ----------------------------------------
#
# `Medication.start_date` is declared non-null on the SDK model, so the test
# database will not store a NULL — but live rows on `xpc-dev` come back as None
# and crashed the entire page. These exercise the paths directly.


def test_time_on_therapy_is_unknown_without_a_start_date() -> None:
    assert Prescription("Semaglutide", None).days_on(NOW) is None


def test_a_card_says_nothing_rather_than_guessing_time_on_therapy() -> None:
    patient = WatchedPatient(
        patient_id="p1",
        name="Ada Lovelace",
        prescriptions=(Prescription("Semaglutide", None),),
    )

    displayed = _with_display(patient, CONFIG, NOW)

    assert displayed.prescriptions[0].days_on_display == ""


def test_labs_are_not_chased_when_no_start_date_is_known() -> None:
    """The gate is time on therapy; an unknown start cannot clear it."""
    missing = _missing(
        weights=[(days_ago(1), 240.0)],
        labs={},
        followup=days_ahead(10),
        prescriptions=[Prescription("Semaglutide", None)],
        config=CONFIG,
        now=NOW,
    )

    assert missing == ()


def test_a_dated_prescription_still_opens_the_gate_beside_an_undated_one() -> None:
    missing = _missing(
        weights=[(days_ago(1), 240.0)],
        labs={},
        followup=days_ahead(10),
        prescriptions=[
            Prescription("Semaglutide", None),
            Prescription("Tirzepatide", days_ago(200)),
        ],
        config=CONFIG,
        now=NOW,
    )

    assert [item.gap_key for item in missing] == ["labs_overdue"]


def test_time_on_therapy_drops_to_the_unit_a_reader_can_hold() -> None:
    def label(days: int) -> str:
        patient = WatchedPatient(
            patient_id="p1",
            name="Ada",
            prescriptions=(Prescription("Semaglutide", days_ago(days)),),
        )
        return _with_display(patient, CONFIG, now()).prescriptions[0].days_on_display

    assert label(14) == "14 days on therapy"
    assert label(120) == "4 mo on therapy"
    # A live chart read "102 mo on therapy", which nobody converts in their head.
    assert label(3100) == "8 yr on therapy"
