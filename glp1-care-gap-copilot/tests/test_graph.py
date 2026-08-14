"""Graph geometry and drop flagging, tested on numbers rather than markup."""

from datetime import datetime, timedelta, timezone

from glp1_care_gap_copilot.graph import (
    PLOT_BOTTOM,
    PLOT_LEFT,
    PLOT_RIGHT,
    PLOT_TOP,
    build_graph,
)
from glp1_care_gap_copilot.weight_trend import WeighIn

THRESHOLD = 5.0
INTERVAL = 7.0
START = datetime(2026, 3, 1, tzinfo=timezone.utc)


def series(*readings: tuple[int, float]) -> list[WeighIn]:
    """Weigh-ins given as (day offset, pounds), oldest first."""
    return [
        WeighIn(recorded=START + timedelta(days=day), pounds=pounds)
        for day, pounds in readings
    ]


def test_no_weigh_ins_produces_an_empty_graph() -> None:
    graph = build_graph([], THRESHOLD, INTERVAL)

    assert graph.has_data is False
    assert graph.has_line is False
    assert graph.points == []
    assert graph.segments == []
    assert graph.flagged_count == 0


def test_single_weigh_in_is_centered_and_draws_no_line() -> None:
    graph = build_graph(series((0, 240.0)), THRESHOLD, INTERVAL)

    assert graph.has_data is True
    assert graph.has_line is False
    assert graph.segments == []
    assert graph.points[0].x == (PLOT_LEFT + PLOT_RIGHT) / 2
    assert graph.net_label == "single reading on record"


# --- the flagging rule: large AND fast ---------------------------------------


def test_a_big_loss_over_one_week_is_flagged() -> None:
    graph = build_graph(series((0, 252.0), (7, 238.0)), THRESHOLD, INTERVAL)

    assert graph.flagged_count == 1
    segment = graph.segments[0]
    assert segment.flagged is True
    assert segment.drop == 14.0
    assert segment.interval_days == 7.0
    assert segment.drop_label == "-14 lb in 7d"


def test_the_same_loss_spread_over_months_is_not_flagged() -> None:
    # 14 lb across ten weeks is the medication working, not a safety signal.
    graph = build_graph(series((0, 252.0), (70, 238.0)), THRESHOLD, INTERVAL)

    assert graph.segments[0].drop == 14.0
    assert graph.segments[0].interval_days == 70.0
    assert graph.flagged_count == 0


def test_a_loss_just_past_the_window_is_not_flagged() -> None:
    # 8 days apart: over the 7-day window, so it stays quiet. Practices that
    # weigh on a looser schedule raise WEIGHT_DROP_MAX_INTERVAL_DAYS.
    graph = build_graph(series((0, 252.0), (8, 238.0)), THRESHOLD, INTERVAL)

    assert graph.flagged_count == 0


def test_the_window_boundary_itself_counts() -> None:
    # Exactly 7 days is "within a week", so the boundary is inclusive — unlike
    # the pounds threshold, which is strictly greater-than.
    assert build_graph(series((0, 252.0), (7, 238.0)), THRESHOLD, INTERVAL).flagged_count == 1


def test_a_small_loss_over_one_week_is_not_flagged() -> None:
    # Fast but not large: fails the other half of the rule.
    graph = build_graph(series((0, 252.0), (7, 249.0)), THRESHOLD, INTERVAL)

    assert graph.flagged_count == 0


def test_drop_exactly_at_the_pound_threshold_is_not_flagged() -> None:
    # The rule is "more than 5 lb", so 5.0 itself stays quiet.
    graph = build_graph(series((0, 250.0), (7, 245.0)), THRESHOLD, INTERVAL)

    assert graph.segments[0].drop == 5.0
    assert graph.flagged_count == 0


def test_a_large_gain_is_never_flagged() -> None:
    # Only losses carry the GLP-1 safety signal this graph exists to surface.
    graph = build_graph(series((0, 236.0), (7, 249.0)), THRESHOLD, INTERVAL)

    assert graph.segments[0].drop == -13.0
    assert graph.flagged_count == 0
    assert graph.segments[0].flagged is False


def test_each_qualifying_interval_is_flagged_independently() -> None:
    # Weekly weights: two rapid drops with a quiet week between them.
    graph = build_graph(
        series((0, 260.0), (7, 244.0), (14, 242.0), (21, 228.0)), THRESHOLD, INTERVAL
    )

    assert [segment.flagged for segment in graph.segments] == [True, False, True]
    assert graph.flagged_count == 2


def test_both_halves_of_the_rule_are_configurable() -> None:
    readings = series((0, 250.0), (14, 247.0))

    assert build_graph(readings, THRESHOLD, INTERVAL).flagged_count == 0
    # Lowering the pounds alone is still not enough — the gap is 14 days.
    assert build_graph(readings, 2.0, INTERVAL).flagged_count == 0
    # Widening the window as well finally trips it.
    assert build_graph(readings, 2.0, 14.0).flagged_count == 1


def test_same_day_readings_are_inside_any_window() -> None:
    graph = build_graph(series((0, 252.0), (0, 236.0)), THRESHOLD, INTERVAL)

    assert graph.segments[0].interval_days == 0.0
    assert graph.flagged_count == 1
    # Same-day readings collapse the interval; a zero-width rect would vanish.
    assert graph.segments[0].band_width >= 1.0


# --- layout ------------------------------------------------------------------


def test_heavier_readings_sit_higher_on_the_plot() -> None:
    graph = build_graph(series((0, 260.0), (30, 210.0)), THRESHOLD, INTERVAL)

    heavier, lighter = graph.points
    # SVG's y axis grows downward, so "higher" means a smaller y.
    assert heavier.y < lighter.y


def test_x_axis_is_scaled_by_elapsed_time_not_by_index() -> None:
    # Two readings a day apart followed by one six months later must not be
    # drawn evenly spaced — the whole point is that the interval is visible.
    graph = build_graph(
        series((0, 250.0), (1, 249.0), (180, 240.0)), THRESHOLD, INTERVAL
    )

    first, second, third = graph.points
    assert second.x - first.x < (third.x - second.x) / 10


def test_every_point_stays_inside_the_plot_area() -> None:
    graph = build_graph(
        series((0, 260.0), (10, 205.0), (20, 233.0), (30, 199.0)), THRESHOLD, INTERVAL
    )

    for point in graph.points:
        assert PLOT_LEFT <= point.x <= PLOT_RIGHT
        assert PLOT_TOP <= point.y <= PLOT_BOTTOM


def test_identical_weights_do_not_divide_by_zero() -> None:
    graph = build_graph(
        series((0, 230.0), (30, 230.0), (60, 230.0)), THRESHOLD, INTERVAL
    )

    assert graph.has_data is True
    assert graph.flagged_count == 0
    assert len({point.y for point in graph.points}) == 1


def test_identical_timestamps_fall_back_to_even_spacing() -> None:
    graph = build_graph(series((0, 250.0), (0, 244.0), (0, 240.0)), THRESHOLD, INTERVAL)

    first, second, third = graph.points
    assert second.x - first.x == third.x - second.x
    assert first.x == PLOT_LEFT
    assert third.x == PLOT_RIGHT


def test_flagged_band_spans_the_interval() -> None:
    graph = build_graph(series((0, 252.0), (7, 238.0)), THRESHOLD, INTERVAL)

    segment = graph.segments[0]
    assert segment.band_x == segment.x1
    assert segment.band_width == segment.x2 - segment.x1
    assert segment.label_x == (segment.x1 + segment.x2) / 2


def test_polyline_lists_every_point_in_order() -> None:
    graph = build_graph(series((0, 250.0), (30, 240.0)), THRESHOLD, INTERVAL)

    coordinates = graph.polyline.split(" ")
    assert len(coordinates) == 2
    assert coordinates[0].startswith(f"{graph.points[0].x:.2f}")


def test_summary_labels_describe_the_trajectory() -> None:
    graph = build_graph(series((0, 252.0), (30, 238.0)), THRESHOLD, INTERVAL)

    assert graph.latest_label == "238 lb on Mar 31"
    assert graph.net_label == "14 lb down since Mar 1"


def test_net_gain_is_described_as_up() -> None:
    graph = build_graph(series((0, 238.0), (30, 252.0)), THRESHOLD, INTERVAL)

    assert graph.net_label == "14 lb up since Mar 1"


def test_axis_labels_run_from_high_to_low() -> None:
    graph = build_graph(series((0, 260.0), (30, 210.0)), THRESHOLD, INTERVAL)

    labels = graph.axis_labels
    assert len(labels) == 3
    # Top of the plot has the smallest y and the largest weight.
    assert labels[0][0] < labels[1][0] < labels[2][0]
    assert float(labels[0][1]) > float(labels[2][1])


def test_interval_is_measured_in_whole_days() -> None:
    # Weighed Monday morning and the following Monday evening is "a week apart"
    # to a clinician, and 7.4 days to a computer. Whole days keep the rule from
    # depending on the hour of day the patient stepped on the scale.
    readings = [
        WeighIn(recorded=START, pounds=252.0),
        WeighIn(recorded=START + timedelta(days=7, hours=10), pounds=238.0),
    ]

    graph = build_graph(readings, THRESHOLD, INTERVAL)

    assert graph.segments[0].interval_days == 7
    assert graph.segments[0].flagged is True
    assert graph.segments[0].drop_label == "-14 lb in 7d"


def test_a_hair_under_eight_days_still_counts_as_seven() -> None:
    readings = [
        WeighIn(recorded=START, pounds=252.0),
        WeighIn(recorded=START + timedelta(days=7, hours=23), pounds=238.0),
    ]

    assert build_graph(readings, THRESHOLD, INTERVAL).flagged_count == 1


def test_a_full_eight_days_falls_outside_the_window() -> None:
    readings = [
        WeighIn(recorded=START, pounds=252.0),
        WeighIn(recorded=START + timedelta(days=8), pounds=238.0),
    ]

    assert build_graph(readings, THRESHOLD, INTERVAL).flagged_count == 0


def test_the_top_point_clears_the_axis_label_above_it() -> None:
    # Each weight is printed 9 user units above its dot, so the highest reading
    # needs at least that much headroom or its label lands on the top gridline.
    graph = build_graph(series((0, 246.0), (7, 223.0)), THRESHOLD, INTERVAL)

    highest = min(graph.points, key=lambda point: point.y)
    top_gridline_y = graph.axis_labels[0][0]
    assert highest.y - top_gridline_y >= 9
