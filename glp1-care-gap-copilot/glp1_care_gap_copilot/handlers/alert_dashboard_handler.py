"""App-drawer surfaces for the panel-wide safety alerts.

Two applications, deliberately separate:

- `GLP1AlertDashboard` **reads**. Opening it lists who needs attention and
  carries a badge count on its icon, so the number is visible without anyone
  opening anything.
- `GLP1AlertScanNow` **writes**. Opening it runs the scan and files tasks.

They are split because an application runs `on_open` every time it is opened. A
single app that both showed the list and filed tasks would file them again every
time somebody glanced at the list. Keeping the action behind its own icon also
means it cannot happen by accident.

A button inside the dashboard would have been tidier than a second icon, but it
would need an HTTP endpoint for the page to call — and this plugin deliberately
has none, which is one less authenticated surface to get wrong.
"""

from datetime import datetime, timezone
from typing import cast

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.handlers.application import Application
from canvas_sdk.templates import render_to_string
from logger import log

from glp1_care_gap_copilot.alerts import scan_for_alerts
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.handlers.alert_scan_handler import run_scan

DASHBOARD_TEMPLATE = "templates/alert_dashboard.html"
RESULT_TEMPLATE = "templates/alert_scan_result.html"
DASHBOARD_TITLE = "GLP-1 safety alerts"
RESULT_TITLE = "GLP-1 alert scan"


class GLP1AlertDashboard(Application):
    """List the patients a clinician should look at, without opening a chart."""

    def on_open(self) -> Effect | list[Effect]:
        """Render the current alerts. Reads only — never files anything."""
        try:
            config = Config.from_secrets(self.secrets)
            alerts = scan_for_alerts(config)
            content = cast(
                str,
                render_to_string(
                    DASHBOARD_TEMPLATE,
                    {
                        "alerts": alerts,
                        "lookback_days": config.alert_scan_lookback_days,
                        "scanned_at": datetime.now(timezone.utc).strftime(
                            "%b %-d at %H:%M UTC"
                        ),
                    },
                ),
            )
        except Exception as error:  # noqa: BLE001 - a broken panel must still open
            log.error(f"[glp1-care-gap-copilot] alert dashboard failed: {error}")
            content = (
                "<p style='font-family:sans-serif;padding:24px'>"
                "The alert list could not be built. The chart cards are "
                "unaffected.</p>"
            )
        return LaunchModalEffect(
            content=content,
            target=LaunchModalEffect.TargetType.DEFAULT_MODAL,
            title=DASHBOARD_TITLE,
        ).apply()

    def compute_notification_badge(self) -> int | None:
        """Count of flagged patients, shown on the app icon.

        This is the whole point of the feature: a number the clinician sees
        without opening anything. Zero clears the badge rather than leaving a
        stale one behind, and a failure returns None so a broken count never
        shows a wrong one.
        """
        try:
            return len(scan_for_alerts(Config.from_secrets(self.secrets)))
        except Exception as error:  # noqa: BLE001 - a bad count is worse than none
            log.error(f"[glp1-care-gap-copilot] alert badge failed: {error}")
            return None


class GLP1AlertScanNow(Application):
    """Run the nightly scan on demand, so it can be tested without waiting."""

    def on_open(self) -> Effect | list[Effect]:
        """File tasks for anything currently flagged, and report what happened."""
        try:
            config = Config.from_secrets(self.secrets)
            effects = run_scan(config)
            content = cast(
                str,
                render_to_string(
                    RESULT_TEMPLATE,
                    {
                        "filed": len(effects),
                        "ran_at": datetime.now(timezone.utc).strftime(
                            "%b %-d at %H:%M UTC"
                        ),
                    },
                ),
            )
            # The modal is one effect among the tasks; order does not matter to
            # Canvas, but the tasks are the point and are returned regardless.
            return [
                *effects,
                LaunchModalEffect(
                    content=content,
                    target=LaunchModalEffect.TargetType.DEFAULT_MODAL,
                    title=RESULT_TITLE,
                ).apply(),
            ]
        except Exception as error:  # noqa: BLE001 - report the failure, file nothing
            log.error(f"[glp1-care-gap-copilot] manual alert scan failed: {error}")
            return LaunchModalEffect(
                content=(
                    "<p style='font-family:sans-serif;padding:24px'>"
                    "The scan failed and nothing was filed. Check the plugin "
                    "logs.</p>"
                ),
                target=LaunchModalEffect.TargetType.DEFAULT_MODAL,
                title=RESULT_TITLE,
            ).apply()
