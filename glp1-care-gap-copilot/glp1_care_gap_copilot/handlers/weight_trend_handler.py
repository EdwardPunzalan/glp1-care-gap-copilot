"""Chart summary section showing the patient's recent weigh-ins as a graph.

Three handlers, all registered in the manifest:

- `GLP1WeightTrendSection` supplies the section's content.
- `GLP1ChartSummaryConfiguration` declares the chart summary layout the section
  appears in.
- `GLP1WeightTrendButton` opens the same graph full size in a modal.

The layout handler restates **every** built-in section rather than only the ones
this plugin cares about. `PatientChartSummaryConfiguration` replaces the whole
layout, so listing a subset would silently delete the rest of the chart summary
for every patient on the instance.
"""

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.effects.patient_chart_summary_configuration import (
    PatientChartSummaryConfiguration,
)
from canvas_sdk.effects.patient_chart_summary_custom_section import (
    PatientChartSummaryCustomSection,
)
from canvas_sdk.events import EventType
from canvas_sdk.handlers import BaseHandler
from canvas_sdk.handlers.action_button import ActionButton
from canvas_sdk.handlers.patient_chart_summary_custom_section_handler import (
    PatientChartSummaryCustomSectionHandler,
)
from logger import log

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.graph import Graph
from glp1_care_gap_copilot.render import (
    MODAL_TEMPLATE,
    NO_WEIGHTS_MESSAGE,
    SECTION_TEMPLATE,
    build_patient_graph,
    render_chart,
)

SECTION_KEY = "glp1_weight_trend"
SECTION_ICON = "⚖"
MODAL_TITLE = "GLP-1 weight trend"

# Canvas's built-in chart summary sections, in Canvas's own default order. Kept
# whole so registering our section adds to the summary instead of replacing it.
BUILT_IN_SECTIONS = [
    PatientChartSummaryConfiguration.Section.SOCIAL_DETERMINANTS,
    PatientChartSummaryConfiguration.Section.GOALS,
    PatientChartSummaryConfiguration.Section.CONDITIONS,
    PatientChartSummaryConfiguration.Section.MEDICATIONS,
    PatientChartSummaryConfiguration.Section.ALLERGIES,
    PatientChartSummaryConfiguration.Section.CARE_TEAMS,
    PatientChartSummaryConfiguration.Section.VITALS,
    PatientChartSummaryConfiguration.Section.IMMUNIZATIONS,
    PatientChartSummaryConfiguration.Section.SURGICAL_HISTORY,
    PatientChartSummaryConfiguration.Section.FAMILY_HISTORY,
    PatientChartSummaryConfiguration.Section.CODING_GAPS,
]


class GLP1WeightTrendSection(PatientChartSummaryCustomSectionHandler):
    """Serve the weight-trend graph into the patient chart summary."""

    SECTION_KEY = SECTION_KEY

    def handle(self) -> list[Effect]:
        """Render the graph, degrading to a sentence rather than an error."""
        try:
            return self._handle()
        except Exception as error:  # noqa: BLE001 - never break chart rendering
            log.error(f"[glp1-care-gap-copilot] weight trend render failed: {error}")
            # Defaults, not the parsed config: whatever failed may well have
            # been the config parse itself.
            return [self._section(Graph(), NO_WEIGHTS_MESSAGE, Config.from_secrets({}))]

    def _handle(self) -> list[Effect]:
        # `self.target` is deprecated in the SDK (removed in 1.0.0) even though
        # the docs still use it; read the event directly instead.
        target = getattr(self.event, "target", None)
        patient_id = getattr(target, "id", None)
        config = Config.from_secrets(self.secrets)

        if not patient_id:
            return [self._section(Graph(), NO_WEIGHTS_MESSAGE, config)]

        graph, empty_message = build_patient_graph(str(patient_id), config)
        # Only a graph that exists is worth a line. This section renders on
        # every chart summary on the instance, so logging the empty case would
        # cost one line per chart open to say nothing happened.
        if graph.points:
            log.info(
                "[glp1-care-gap-copilot] weight trend rendered: "
                f"points={len(graph.points)} flagged={graph.flagged_count}"
            )
        return [self._section(graph, empty_message, config)]

    def _section(self, graph: Graph, empty_message: str, config: Config) -> Effect:
        return PatientChartSummaryCustomSection(
            content=render_chart(graph, empty_message, config, template=SECTION_TEMPLATE),
            icon=SECTION_ICON,
        ).apply()


class GLP1ChartSummaryConfiguration(BaseHandler):
    """Place the weight-trend section at the top of the chart summary."""

    RESPONDS_TO = [
        EventType.Name(EventType.PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION)
    ]

    def compute(self) -> list[Effect]:
        """Return the full summary layout with our section added to the front."""
        try:
            sections = [
                PatientChartSummaryConfiguration.CustomSection(name=SECTION_KEY),
                *BUILT_IN_SECTIONS,
            ]
            return [PatientChartSummaryConfiguration(sections=sections).apply()]
        except Exception as error:  # noqa: BLE001 - never break chart rendering
            # Returning nothing leaves Canvas's default layout intact, which is
            # a far better failure than a chart summary with no sections in it.
            log.error(f"[glp1-care-gap-copilot] summary layout failed: {error}")
            return []


class GLP1WeightTrendButton(ActionButton):
    """Open the weight trend full size from the patient header.

    The graph lives in the chart summary, but that section is sandboxed page
    markup and cannot return an effect when something in it is clicked, and
    Canvas has no `ButtonLocation` for custom summary sections. An action button
    is the supported way to launch a modal, so the full-size view is reached
    from the patient header rather than from the section's own corner.

    The modal carries its content inline rather than pointing at a URL, which
    keeps the plugin free of HTTP endpoints and keeps the patient id out of a
    query string.
    """

    # Canvas caps patient-header buttons at max-width:90px with 10px padding
    # each side, leaving ~68px for text at the header's 12.6px Lato. Measured in
    # the live DOM: "Weight log" is 59.6px and fits; "Weight trend" is 73.3px
    # and rendered as "Weight tr...". This is platform CSS the plugin cannot
    # override — Canvas's own "Outside Records" button truncates the same way —
    # so the title has to fit instead.
    BUTTON_TITLE = "Weight log"
    BUTTON_KEY = "glp1_weight_trend_expand"
    BUTTON_LOCATION = ActionButton.ButtonLocation.CHART_PATIENT_HEADER
    PRIORITY = 1

    def visible(self) -> bool:
        """Offer the button only where there is a graph worth enlarging."""
        try:
            patient_id = self._patient_id()
            if not patient_id:
                return False
            config = Config.from_secrets(self.secrets)
            graph, _ = build_patient_graph(patient_id, config)
            return graph.has_data
        except Exception as error:  # noqa: BLE001 - a broken button must not break the header
            log.error(f"[glp1-care-gap-copilot] weight trend button check failed: {error}")
            return False

    def handle(self) -> list[Effect]:
        """Open the full-size chart in a modal."""
        patient_id = self._patient_id()
        config = Config.from_secrets(self.secrets)
        if patient_id:
            graph, empty_message = build_patient_graph(patient_id, config)
        else:
            graph, empty_message = Graph(), NO_WEIGHTS_MESSAGE
        log.info(
            "[glp1-care-gap-copilot] weight trend expanded: "
            f"points={len(graph.points)} flagged={graph.flagged_count}"
        )
        return [
            LaunchModalEffect(
                content=render_chart(
                    graph, empty_message, config, template=MODAL_TEMPLATE, modal=True
                ),
                target=LaunchModalEffect.TargetType.DEFAULT_MODAL,
                title=MODAL_TITLE,
            ).apply()
        ]

    def _patient_id(self) -> str:
        # `self.target` is deprecated in the SDK; read the event directly.
        target = getattr(self.event, "target", None)
        return str(getattr(target, "id", "") or "")
