"""The monitoring hub: a full page, plus the route its buttons call.

`GLP1Hub` is the app-drawer icon. It opens the hub as a **page**, not a modal —
this is somewhere a clinician starts their day, not a dialog they dismiss — and
carries a badge with the number of high-risk patients.

`GLP1HubAPI` serves the page's HTML and the two routes its buttons post to —
one that files a single task, one that runs the nightly scan on demand. All are
gated on `StaffSessionAuthMixin`, so an unauthenticated request cannot read the
panel or write a task.

The session authenticates the *request*; it does not author the record. Tasks
filed here are created by Canvas Bot, same as the ones the nightly cron files —
verified on a live instance. Anything that needs to know who asked has to carry
it in the task itself.

The manual scan lives here rather than behind its own app-drawer icon. It used
to need one, because a modal has nowhere to post to; a page with its own
authenticated route does not.
"""

from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, cast

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.effects.task import AddTask, TaskStatus
from canvas_sdk.handlers.application import Application
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string
from logger import log

from glp1_care_gap_copilot.alerts import scan_for_alerts
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.dedupe import task_title
from glp1_care_gap_copilot.handlers.alert_scan_handler import run_scan
from glp1_care_gap_copilot.hub import load_hub

PLUGIN_NAME = "glp1_care_gap_copilot"
API_PREFIX = "/hub"
PAGE_PATH = f"/plugin-io/api/{PLUGIN_NAME}{API_PREFIX}/page"
TASK_PATH = f"/plugin-io/api/{PLUGIN_NAME}{API_PREFIX}/task"
SCAN_PATH = f"/plugin-io/api/{PLUGIN_NAME}{API_PREFIX}/scan"
HUB_TITLE = "GLP-1 Monitoring Hub"

#: Cache buster for the page URL, fixed at import so it changes once per
#: deploy. The page is a plain 200 at a stable URL, so the browser is entitled
#: to reuse it — and did: after a fix was deployed the hub kept serving the
#: previous response until a hard reload. A clinician would have read that as
#: the plugin being broken.
_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))

#: Gap keys the page is allowed to file. An allow-list rather than trusting the
#: posted value: the body comes from a browser and must not be able to write an
#: arbitrary task title onto a patient.
FILEABLE_GAPS = {"stale_weight", "labs_overdue", "no_followup", "safety_check_due"}


def _risk_by_patient(config: Config) -> dict[str, str]:
    """Patient id to the reason the safety rule flagged them.

    Reuses the scan the nightly job and the chart banner use, so the hub cannot
    show a different set of high-risk patients than they act on.
    """
    return {
        alert.patient_id: alert.headline
        for alert in scan_for_alerts(config)
        if alert.is_triggered
    }


def render_hub(config: Config, now: datetime | None = None) -> str:
    """Build the hub page."""
    moment = now or datetime.now(timezone.utc)
    risk = _risk_by_patient(config)
    patients = load_hub(config, risk, now=moment)
    return cast(
        str,
        render_to_string(
            "templates/hub.html",
            {
                "patients": patients,
                "high_risk": [p for p in patients if p.is_high_risk],
                "open_items": sum(len(p.missing) for p in patients),
                "scanned_at": moment.strftime("%b %-d at %H:%M UTC"),
                "task_url": TASK_PATH,
                "scan_url": SCAN_PATH,
            },
        ),
    )


class GLP1Hub(Application):
    """App-drawer entry that opens the monitoring hub as a full page."""

    def on_open(self) -> Effect | list[Effect]:
        """Open the hub. The page itself is served by `GLP1HubAPI`."""
        return LaunchModalEffect(
            url=f"{PAGE_PATH}?v={_CACHE_BUST}",
            target=LaunchModalEffect.TargetType.PAGE,
            title=HUB_TITLE,
        ).apply()

    def compute_notification_badge(self) -> int | None:
        """How many patients the safety rule currently flags.

        Counts every high-risk patient, so the badge tracks the real number
        rather than merely showing that there is at least one. Zero clears a
        stale badge; a failure returns None so a broken count is never shown as
        a wrong one.
        """
        try:
            return len(_risk_by_patient(Config.from_secrets(self.secrets)))
        except Exception as error:  # noqa: BLE001 - a bad count is worse than none
            log.error(f"[glp1-care-gap-copilot] hub badge failed: {error}")
            return None


class GLP1HubAPI(StaffSessionAuthMixin, SimpleAPI):
    """Serves the hub page and files the tasks its buttons ask for."""

    PREFIX = API_PREFIX

    @api.get("/page")
    def page(self) -> list[Response | Effect]:
        """Render the hub."""
        try:
            html = render_hub(Config.from_secrets(self.secrets))
        except Exception as error:  # noqa: BLE001 - show a page, not a stack trace
            log.error(f"[glp1-care-gap-copilot] hub page failed: {error}")
            html = (
                "<p style='font-family:Lato,sans-serif;padding:32px'>"
                "The monitoring hub could not be built. Patient charts are "
                "unaffected.</p>"
            )
        return [HTMLResponse(html, status_code=HTTPStatus.OK)]

    @api.post("/task")
    def create_task(self) -> list[Response | Effect]:
        """File one task for a named gap on a named patient.

        Uses the same marked title the chart card and the nightly scan use, so
        none of the three will raise a duplicate of a task another created.
        """
        body: dict[str, Any] = self.request.json() or {}
        patient_id = str(body.get("patient_id") or "").strip()
        gap_key = str(body.get("gap_key") or "").strip()
        summary = str(body.get("summary") or "").strip()

        if not patient_id or gap_key not in FILEABLE_GAPS:
            # Refuse rather than guess: an unrecognised key here would write an
            # unmarked task the dedupe could never match again.
            return [
                JSONResponse(
                    {"error": "unknown patient or gap"},
                    status_code=HTTPStatus.BAD_REQUEST,
                )
            ]

        config = Config.from_secrets(self.secrets)
        log.info(
            f"[glp1-care-gap-copilot] hub filing task: gap={gap_key} "
            f"patient={patient_id}"
        )
        return [
            AddTask(
                patient_id=patient_id,
                title=task_title(config, gap_key, summary or gap_key),
                status=TaskStatus.OPEN,
                team_id=str(config.outreach_team_dbid)
                if config.outreach_team_dbid
                else None,
            ).apply(),
            # Accepted, not created: the effect is applied after this returns,
            # so claiming the task exists would be a lie the UI could not check.
            JSONResponse({"status": "accepted"}, status_code=HTTPStatus.ACCEPTED),
        ]

    @api.post("/scan")
    def scan_now(self) -> list[Response | Effect]:
        """Run the nightly safety scan immediately.

        The same `run_scan` the cron calls, so what this files is exactly what
        tonight would have filed — which is the point of being able to press it.
        Already-open tasks are skipped by the scan's own dedupe, so pressing it
        twice does not double the queue.
        """
        try:
            config = Config.from_secrets(self.secrets)
            effects = run_scan(config)
        except Exception as error:  # noqa: BLE001 - report it, file nothing
            log.error(f"[glp1-care-gap-copilot] manual scan failed: {error}")
            return [
                JSONResponse(
                    {"error": "scan failed"},
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            ]
        return [
            *effects,
            JSONResponse(
                {"status": "accepted", "filed": len(effects)},
                status_code=HTTPStatus.ACCEPTED,
            ),
        ]
