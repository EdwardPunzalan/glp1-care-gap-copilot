"""Panel-wide alert scanning and the tasks it files."""

from unittest.mock import Mock, patch

from canvas_sdk.effects import EffectType
from canvas_sdk.test_utils.factories import PatientFactory, TaskFactory
from canvas_sdk.v1.data.task import TaskStatus

from glp1_care_gap_copilot.alerts import patients_weighed_since, scan_for_alerts
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import patients_with_open_tasks, task_title
from glp1_care_gap_copilot.gaps import GAP_SAFETY_CHECK_DUE, GAP_SAFETY_REVIEW
from glp1_care_gap_copilot.handlers.alert_scan_handler import (
    MAX_TASKS_PER_RUN,
    GLP1AlertScan,
    build_tasks,
    run_scan,
)
from tests.factories import (
    add_condition,
    add_medication,
    add_weight,
    complete_safety_check,
    days_ago,
    now,
)

CONFIG = Config.from_secrets({})


def glp1_patient() -> PatientFactory:
    patient = PatientFactory.create()
    add_medication(patient, "Semaglutide 0.5 MG")
    return patient


def rapid_loss_patient() -> PatientFactory:
    """A 9 lb drop over 7 days, with the newest weigh-in yesterday."""
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(15))
    add_weight(patient, 248.0, days_ago(8))
    add_weight(patient, 239.0, days_ago(1))
    return patient


# --- who gets scanned ---------------------------------------------------------


def test_only_patients_weighed_in_the_window_are_scanned() -> None:
    fresh = glp1_patient()
    add_weight(fresh, 240.0, days_ago(1))
    stale = glp1_patient()
    add_weight(stale, 240.0, days_ago(60))

    found = patients_weighed_since(days_ago(2), 500)

    assert str(fresh.id) in found
    assert str(stale.id) not in found


def test_a_patient_is_listed_once_however_often_they_weigh_in() -> None:
    patient = glp1_patient()
    for day in range(0, 2):
        add_weight(patient, 240.0 - day, days_ago(day))

    assert patients_weighed_since(days_ago(2), 500).count(str(patient.id)) == 1


def test_the_scan_cap_bounds_one_run() -> None:
    for _ in range(4):
        patient = glp1_patient()
        add_weight(patient, 240.0, days_ago(1))

    assert len(patients_weighed_since(days_ago(2), 2)) == 2


def test_an_out_of_scope_patient_is_skipped() -> None:
    # Weighed yesterday and losing fast, but on no GLP-1 and with no obesity
    # diagnosis — not this plugin's business.
    patient = PatientFactory.create()
    add_weight(patient, 250.0, days_ago(8))
    add_weight(patient, 239.0, days_ago(1))

    assert scan_for_alerts(CONFIG) == []


# --- what the scan flags ------------------------------------------------------


def test_a_gradual_loser_raises_nothing() -> None:
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(40))
    add_weight(patient, 244.0, days_ago(1))

    assert scan_for_alerts(CONFIG) == []


def test_an_unscreened_rapid_drop_is_flagged() -> None:
    patient = rapid_loss_patient()

    alerts = scan_for_alerts(CONFIG)

    assert len(alerts) == 1
    assert alerts[0].patient_id == str(patient.id)
    assert [gap.key for gap in alerts[0].gaps] == [GAP_SAFETY_CHECK_DUE]
    assert alerts[0].is_triggered is False


def test_a_rapid_drop_with_a_warning_sign_is_flagged_as_contact() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, ("GLP1SC_GI",), created=days_ago(1))

    alerts = scan_for_alerts(CONFIG)

    assert alerts[0].is_triggered is True
    assert alerts[0].gaps[0].key == GAP_SAFETY_REVIEW
    assert "nausea" in alerts[0].headline


def test_a_screened_and_clear_patient_drops_off_entirely() -> None:
    patient = rapid_loss_patient()
    complete_safety_check(patient, (), created=days_ago(1))

    # Someone looked and found nothing: no contact needed, no screening owed.
    assert scan_for_alerts(CONFIG) == []


def test_contact_sorts_ahead_of_screen() -> None:
    rapid_loss_patient()
    urgent = rapid_loss_patient()
    complete_safety_check(urgent, ("GLP1SC_GI",), created=days_ago(1))

    alerts = scan_for_alerts(CONFIG)

    assert len(alerts) == 2
    assert alerts[0].patient_id == str(urgent.id)
    assert alerts[0].is_triggered is True


def test_a_drop_older_than_the_lookback_is_not_rescanned() -> None:
    # The drop is real but the last weigh-in was weeks ago, so nothing new has
    # happened and the nightly scan should not keep raising it.
    patient = glp1_patient()
    add_weight(patient, 250.0, days_ago(40))
    add_weight(patient, 239.0, days_ago(33))

    assert scan_for_alerts(CONFIG) == []


# --- the tasks it files -------------------------------------------------------


def test_a_flagged_patient_gets_a_task() -> None:
    rapid_loss_patient()

    effects = run_scan(CONFIG)

    assert len(effects) == 1
    assert effects[0].type == EffectType.CREATE_TASK


def test_an_existing_open_task_is_not_duplicated() -> None:
    patient = rapid_loss_patient()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(CONFIG, GAP_SAFETY_CHECK_DUE, "already raised"),
    )

    # This is what stops a three-month-old gap collecting ninety tasks.
    assert run_scan(CONFIG) == []


def test_a_closed_task_does_not_suppress_a_new_one() -> None:
    patient = rapid_loss_patient()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.COMPLETED,
        title=task_title(CONFIG, GAP_SAFETY_CHECK_DUE, "handled last month"),
    )

    assert len(run_scan(CONFIG)) == 1


def test_open_tasks_for_all_patients_come_from_one_query() -> None:
    first = rapid_loss_patient()
    second = rapid_loss_patient()
    TaskFactory.create(
        patient=first,
        status=TaskStatus.OPEN,
        title=task_title(CONFIG, GAP_SAFETY_CHECK_DUE, "raised"),
    )

    found = patients_with_open_tasks([str(first.id), str(second.id)], CONFIG)

    assert found == {str(first.id): {GAP_SAFETY_CHECK_DUE}}


def test_the_task_ceiling_stops_an_oversized_batch() -> None:
    alert = Mock()
    alert.patient_id = "p1"
    alert.gaps = [Mock(key=f"k{i}", label=f"gap {i}") for i in range(MAX_TASKS_PER_RUN + 20)]

    with patch(
        "glp1_care_gap_copilot.handlers.alert_scan_handler.patients_with_open_tasks",
        return_value={},
    ):
        effects = build_tasks([alert], CONFIG)

    # One batch has a hard size ceiling; overflowing it would lose every task.
    assert len(effects) == MAX_TASKS_PER_RUN


def test_no_alerts_files_nothing() -> None:
    assert build_tasks([], CONFIG) == []


# --- the scheduled handler ----------------------------------------------------


def test_the_cron_runs_nightly() -> None:
    assert GLP1AlertScan.SCHEDULE == "0 7 * * *"


def test_the_cron_files_tasks_for_flagged_patients() -> None:
    rapid_loss_patient()
    handler = GLP1AlertScan(event=Mock(), secrets={})

    effects = handler.execute()

    assert len(effects) == 1


def test_a_failing_scan_does_not_break_the_schedule() -> None:
    handler = GLP1AlertScan(event=Mock(), secrets={})

    with patch(
        "glp1_care_gap_copilot.handlers.alert_scan_handler.run_scan",
        side_effect=RuntimeError("database is on fire"),
    ):
        assert handler.execute() == []


def test_the_scan_reuses_the_chart_rule_rather_than_restating_it() -> None:
    """The dashboard, the task, and the chart must never disagree.

    Pins the scan to the *same* entry point the chart card calls. A second
    implementation of the rule would drift, and the first anyone would know is a
    patient flagged in one place and not the other.
    """
    import glp1_care_gap_copilot.alerts as alerts_module

    patient = glp1_patient()
    add_weight(patient, 240.0, days_ago(1))

    with patch.object(alerts_module, "evaluate_safety") as evaluate:
        evaluate.return_value = Mock(triggered=False, drop=None, screened=True)
        with patch.object(alerts_module, "safety_check_is_due", return_value=False):
            scan_for_alerts(CONFIG, now=now())

    evaluate.assert_called_once_with(str(patient.id), CONFIG)


# --- dedupe ------------------------------------------------------------------


def test_no_patients_means_no_task_query() -> None:
    assert patients_with_open_tasks([], CONFIG) == {}


def test_a_title_matching_the_prefix_loosely_is_ignored() -> None:
    """`istartswith` matches without the space, so the parse must re-check."""
    patient = rapid_loss_patient()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        # No space after the colon — matches the query, not the real format.
        title=f"[{CONFIG.task_title_prefix}:handwritten note]",
    )

    found = patients_with_open_tasks([str(patient.id)], CONFIG)

    # Treating this as a gap key would silently suppress a real alert.
    assert found == {}
