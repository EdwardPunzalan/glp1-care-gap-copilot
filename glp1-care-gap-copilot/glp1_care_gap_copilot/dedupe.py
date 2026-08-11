"""Suppression of outreach buttons for gaps that already have an open task.

The plugin marks every task it originates with a stable title prefix carrying
the gap key, so a later render can recognize its own outreach without a custom
data model. A gap with an open task still renders — hiding it would mislead the
clinician — but loses its button, which makes a duplicate impossible.
"""

from canvas_sdk.v1.data.task import Task, TaskStatus

from glp1_care_gap_copilot.config import Config


def task_title(config: Config, gap_key: str, summary: str) -> str:
    """Build the marked task title the dedupe check later looks for."""
    return f"[{config.task_title_prefix}: {gap_key}] {summary}"


def _marker(config: Config, gap_key: str) -> str:
    return f"[{config.task_title_prefix}: {gap_key}]"


def gaps_with_open_tasks(patient_id: str, config: Config, gap_keys: list[str]) -> set[str]:
    """Which of the given gap keys already have an open outreach task."""
    if not gap_keys:
        return set()
    titles = Task.objects.filter(
        patient__id=patient_id,
        status=TaskStatus.OPEN,
        title__istartswith=f"[{config.task_title_prefix}:",
    ).values_list("title", flat=True)
    open_titles = list(titles)
    return {
        gap_key
        for gap_key in gap_keys
        if any(title.startswith(_marker(config, gap_key)) for title in open_titles)
    }
