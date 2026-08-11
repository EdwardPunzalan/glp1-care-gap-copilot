"""Open-task suppression — a gap already being worked must not get a second task."""

from canvas_sdk.test_utils.factories import PatientFactory, TaskFactory
from canvas_sdk.v1.data.task import TaskStatus

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import gaps_with_open_tasks, task_title
from glp1_care_gap_copilot.gaps import GAP_NO_FOLLOWUP, GAP_STALE_WEIGHT

CONFIG = Config.from_secrets({})
BOTH_GAPS = [GAP_STALE_WEIGHT, GAP_NO_FOLLOWUP]


def test_task_title_carries_the_prefix_and_gap_key() -> None:
    title = task_title(CONFIG, GAP_STALE_WEIGHT, "No weight recorded in 58 days")

    assert title == "[GLP-1 Copilot: stale_weight] No weight recorded in 58 days"


def test_open_task_for_a_gap_is_detected() -> None:
    patient = PatientFactory.create()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(CONFIG, GAP_STALE_WEIGHT, "No weight recorded in 58 days"),
    )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, BOTH_GAPS) == {GAP_STALE_WEIGHT}


def test_a_closed_task_no_longer_suppresses_the_gap() -> None:
    patient = PatientFactory.create()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.CLOSED,
        title=task_title(CONFIG, GAP_STALE_WEIGHT, "No weight recorded"),
    )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, BOTH_GAPS) == set()


def test_a_completed_task_no_longer_suppresses_the_gap() -> None:
    patient = PatientFactory.create()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.COMPLETED,
        title=task_title(CONFIG, GAP_STALE_WEIGHT, "No weight recorded"),
    )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, BOTH_GAPS) == set()


def test_a_task_for_a_different_gap_does_not_suppress() -> None:
    patient = PatientFactory.create()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(CONFIG, GAP_NO_FOLLOWUP, "No follow-up scheduled"),
    )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, [GAP_STALE_WEIGHT]) == set()


def test_unrelated_tasks_are_ignored() -> None:
    patient = PatientFactory.create()
    TaskFactory.create(
        patient=patient, status=TaskStatus.OPEN, title="Call patient about billing"
    )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, BOTH_GAPS) == set()


def test_another_patients_open_task_does_not_suppress() -> None:
    patient = PatientFactory.create()
    other = PatientFactory.create()
    TaskFactory.create(
        patient=other,
        status=TaskStatus.OPEN,
        title=task_title(CONFIG, GAP_STALE_WEIGHT, "No weight recorded"),
    )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, BOTH_GAPS) == set()


def test_multiple_gaps_can_be_suppressed_at_once() -> None:
    patient = PatientFactory.create()
    for gap_key in BOTH_GAPS:
        TaskFactory.create(
            patient=patient, status=TaskStatus.OPEN, title=task_title(CONFIG, gap_key, "x")
        )

    assert gaps_with_open_tasks(str(patient.id), CONFIG, BOTH_GAPS) == set(BOTH_GAPS)


def test_no_gaps_means_no_query_result() -> None:
    patient = PatientFactory.create()

    assert gaps_with_open_tasks(str(patient.id), CONFIG, []) == set()


def test_a_renamed_prefix_no_longer_matches_older_tasks() -> None:
    patient = PatientFactory.create()
    TaskFactory.create(
        patient=patient,
        status=TaskStatus.OPEN,
        title=task_title(CONFIG, GAP_STALE_WEIGHT, "No weight recorded"),
    )
    renamed = Config.from_secrets({"TASK_TITLE_PREFIX": "Weight Program"})

    assert gaps_with_open_tasks(str(patient.id), renamed, BOTH_GAPS) == set()
