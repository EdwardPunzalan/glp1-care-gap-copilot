"""One place that turns a patient into rendered weight-trend HTML.

Both surfaces go through here — the chart summary section and the full-screen
modal — so the graph, the flagging rule, and the empty-state wording cannot
drift apart between them. The only difference the callers pass in is how big it
is and whether it offers a full-screen button.
"""

from typing import cast

from canvas_sdk.templates import render_to_string

from glp1_care_gap_copilot.cohort import evaluate_cohort
from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.graph import Graph, build_graph
from glp1_care_gap_copilot.weight_trend import recent_weigh_ins

SECTION_TEMPLATE = "templates/weight_trend.html"
MODAL_TEMPLATE = "templates/weight_trend_modal.html"

OUT_OF_SCOPE_MESSAGE = (
    "Weight trend is shown for patients on a GLP-1 or carrying an "
    "obesity-related diagnosis."
)
NO_WEIGHTS_MESSAGE = "No weight has been recorded for this patient yet."


def _number_label(value: float) -> str:
    """Render a configured number without a trailing `.0` on whole values."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def build_patient_graph(patient_id: str, config: Config) -> tuple[Graph, str]:
    """The patient's graph plus the message to show if it has nothing to draw."""
    if not evaluate_cohort(patient_id, config).in_scope:
        return Graph(), OUT_OF_SCOPE_MESSAGE
    weigh_ins = recent_weigh_ins(patient_id, config.weight_trend_points)
    graph = build_graph(
        weigh_ins,
        config.weight_drop_alert_lb,
        config.weight_drop_max_interval_days,
    )
    return graph, NO_WEIGHTS_MESSAGE


def render_chart(
    graph: Graph,
    empty_message: str,
    config: Config,
    *,
    template: str,
    modal: bool = False,
) -> str:
    """Render one of the two weight-trend surfaces."""
    return cast(
        str,
        render_to_string(
            template,
            {
                "graph": graph,
                "empty_message": empty_message,
                "threshold_label": _number_label(config.weight_drop_alert_lb),
                "interval_label": _number_label(config.weight_drop_max_interval_days),
                "modal": modal,
            },
        ),
    )
