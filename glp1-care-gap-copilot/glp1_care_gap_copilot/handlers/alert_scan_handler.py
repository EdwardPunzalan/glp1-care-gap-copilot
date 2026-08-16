"""Nightly scan that files safety alerts as tasks.

The chart card waits to be visited. This does not: it runs on a schedule, finds
the patients whose recent weigh-ins raise a safety signal, and puts the work in
the queue clinicians already use — so the patient nobody opened still gets seen.

Only safety signals are filed. Overdue labs and stale weights are real gaps, but
they are not time-critical, and a queue that mixes them buries the ones that are.
"""

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.effects.task import AddTask, TaskStatus
from canvas_sdk.handlers.cron_task import CronTask
from logger import log

from glp1_care_gap_copilot.alerts import PatientAlert, scan_for_alerts
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import patients_with_open_tasks, task_title
from glp1_care_gap_copilot.gaps import Gap

#: Cap on tasks emitted by one run. A handler returns its effects in a single
#: batch with a hard size ceiling, so an unbounded run could silently lose the
#: lot. Hitting this is logged loudly rather than passed over: it means the
#: scan found more than a normal day's work and wants looking at.
MAX_TASKS_PER_RUN = 100


def build_tasks(alerts: list[PatientAlert], config: Config) -> list[Effect]:
    """Turn scan results into task effects, skipping work already queued.

    Dedupe is the difference between a useful queue and an abandoned one. A
    patient overdue for three months would otherwise collect ninety identical
    tasks, and the team would stop reading any of them.
    """
    if not alerts:
        return []

    already_open = patients_with_open_tasks(
        [alert.patient_id for alert in alerts], config
    )

    effects: list[Effect] = []
    skipped = 0
    for alert in alerts:
        open_keys = already_open.get(alert.patient_id, set())
        for gap in alert.gaps:
            if gap.key in open_keys:
                skipped += 1
                continue
            if len(effects) >= MAX_TASKS_PER_RUN:
                log.error(
                    "[glp1-care-gap-copilot] alert scan hit the "
                    f"{MAX_TASKS_PER_RUN}-task ceiling; remaining alerts were not "
                    "filed this run"
                )
                return effects
            effects.append(_task(alert, gap, config))
    log.info(
        f"[glp1-care-gap-copilot] alert scan: {len(effects)} task(s) filed, "
        f"{skipped} already open"
    )
    return effects


def _task(alert: PatientAlert, gap: Gap, config: Config) -> Effect:
    return AddTask(
        patient_id=alert.patient_id,
        title=task_title(config, gap.key, gap.label),
        status=TaskStatus.OPEN,
        team_id=str(config.outreach_team_dbid) if config.outreach_team_dbid else None,
    ).apply()


def run_scan(config: Config, now: datetime | None = None) -> list[Effect]:
    """Scan and build task effects. Shared by the cron and the manual trigger."""
    alerts = scan_for_alerts(config, now=now)
    log.info(f"[glp1-care-gap-copilot] alert scan found {len(alerts)} patient(s)")
    return build_tasks(alerts, config)


class GLP1AlertScan(CronTask):
    """File safety alerts as tasks once a night."""

    # 07:00 UTC — early morning US, so the queue is populated before clinic and
    # the scan itself runs off business hours.
    SCHEDULE = "0 7 * * *"

    def execute(self) -> list[Effect]:
        """Run the scan, never letting a failure kill the schedule."""
        try:
            config = Config.from_secrets(self.secrets)
            return run_scan(config, now=datetime.now(timezone.utc))
        except Exception as error:  # noqa: BLE001 - a failed run must not break cron
            log.error(f"[glp1-care-gap-copilot] alert scan failed: {error}")
            return []
