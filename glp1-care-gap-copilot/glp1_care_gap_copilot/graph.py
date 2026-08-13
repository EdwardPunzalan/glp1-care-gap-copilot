"""Turns weigh-ins into plot coordinates for the weight-trend SVG.

Pure geometry — no database, no SDK, no rendering. Keeping the arithmetic here
means the scaling and the drop-detection can be tested directly on numbers
instead of by reading pixels out of generated markup.

The x axis is scaled by *elapsed time*, not by reading index. Evenly spacing the
points would draw an identical slope for 14 lb lost over three weeks and 14 lb
lost over eight months, which is exactly the distinction the graph exists to
make visible.
"""

from dataclasses import dataclass, field

from glp1_care_gap_copilot.weight_trend import WeighIn

VIEW_WIDTH = 400.0
VIEW_HEIGHT = 200.0
PAD_LEFT = 46.0
PAD_RIGHT = 16.0
PAD_TOP = 18.0
PAD_BOTTOM = 34.0

PLOT_LEFT = PAD_LEFT
PLOT_RIGHT = VIEW_WIDTH - PAD_RIGHT
PLOT_TOP = PAD_TOP
PLOT_BOTTOM = VIEW_HEIGHT - PAD_BOTTOM
PLOT_WIDTH = PLOT_RIGHT - PLOT_LEFT
PLOT_HEIGHT = PLOT_BOTTOM - PLOT_TOP

# Breathing room above and below the data so the extreme points are not welded
# to the frame edge. Asymmetric on purpose: each point's weight is printed
# *above* it, so the highest reading needs clearance for its own label or it
# collides with the top gridline and that axis label.
_RANGE_PADDING_TOP = 0.22
_RANGE_PADDING_BOTTOM = 0.08
# Half-spread used when every reading is identical and the range would be zero.
_FLAT_LINE_SPREAD = 5.0


@dataclass(frozen=True)
class Point:
    """One plotted weigh-in."""

    x: float
    y: float
    pounds: float
    weight_label: str
    date_label: str


@dataclass(frozen=True)
class Segment:
    """The stretch between two consecutive weigh-ins."""

    x1: float
    y1: float
    x2: float
    y2: float
    #: Pounds lost across the segment. Negative when the patient gained.
    drop: float
    #: Whole days elapsed between the two readings.
    interval_days: int
    flagged: bool
    band_x: float
    band_width: float
    label_x: float
    label_y: float
    drop_label: str


@dataclass(frozen=True)
class Graph:
    """Everything the template needs to draw the chart."""

    points: list[Point] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    axis_labels: list[tuple[float, str]] = field(default_factory=list)
    flagged_count: int = 0
    latest_label: str = ""
    net_label: str = ""

    @property
    def has_data(self) -> bool:
        """True when there is at least one weigh-in to draw."""
        return bool(self.points)

    @property
    def has_line(self) -> bool:
        """True when there are enough points to draw a line between any."""
        return len(self.points) > 1

    @property
    def polyline(self) -> str:
        """The full trend line as an SVG `points` attribute."""
        return " ".join(f"{point.x:.2f},{point.y:.2f}" for point in self.points)


def _date_label(weigh_in: WeighIn) -> str:
    return weigh_in.recorded.strftime("%b %-d")


def _weight_label(pounds: float) -> str:
    return f"{pounds:.0f}"


def build_graph(
    weigh_ins: list[WeighIn],
    drop_threshold_lb: float,
    max_interval_days: float,
) -> Graph:
    """Lay out `weigh_ins` and flag consecutive drops that are both large and fast.

    A segment is flagged only when **both** hold:

    - more than `drop_threshold_lb` was lost, and
    - the two readings are no more than `max_interval_days` apart.

    The interval test is what makes the flag mean "rapid". These patients dose
    weekly, so losing 14 lb between consecutive weekly weigh-ins is a safety
    signal, while losing the same 14 lb across ten weeks is the medication
    working as intended. Without the second test the graph would cry wolf on
    every successful course of treatment.

    Only *drops* are flagged. A gain of the same size is left unmarked.
    """
    if not weigh_ins:
        return Graph()

    pounds = [weigh_in.pounds for weigh_in in weigh_ins]
    low, high = min(pounds), max(pounds)
    if high - low < 0.01:
        low, high = low - _FLAT_LINE_SPREAD, high + _FLAT_LINE_SPREAD
    else:
        spread = high - low
        low = low - spread * _RANGE_PADDING_BOTTOM
        high = high + spread * _RANGE_PADDING_TOP
    span = high - low

    def y_for(value: float) -> float:
        # SVG's y grows downward, so a heavier reading sits nearer the top.
        return PLOT_BOTTOM - ((value - low) / span) * PLOT_HEIGHT

    first_moment = weigh_ins[0].recorded
    elapsed = (weigh_ins[-1].recorded - first_moment).total_seconds()

    def x_for(index: int, weigh_in: WeighIn) -> float:
        if len(weigh_ins) == 1:
            return PLOT_LEFT + PLOT_WIDTH / 2
        if elapsed <= 0:
            # Every reading shares a timestamp; fall back to even spacing.
            return PLOT_LEFT + (index / (len(weigh_ins) - 1)) * PLOT_WIDTH
        offset = (weigh_in.recorded - first_moment).total_seconds()
        return PLOT_LEFT + (offset / elapsed) * PLOT_WIDTH

    points = [
        Point(
            x=x_for(index, weigh_in),
            y=y_for(weigh_in.pounds),
            pounds=weigh_in.pounds,
            weight_label=_weight_label(weigh_in.pounds),
            date_label=_date_label(weigh_in),
        )
        for index, weigh_in in enumerate(weigh_ins)
    ]

    segments = []
    for (earlier, later), (previous, current) in zip(
        zip(weigh_ins, weigh_ins[1:]), zip(points, points[1:])
    ):
        drop = previous.pounds - current.pounds
        # Whole elapsed days, not a fraction. Two weigh-ins a clinician would
        # call "a week apart" are never exactly 168.000 hours apart, so
        # comparing the raw float against a 7-day window would flag or spare
        # them depending on the hour of day they happened to be weighed.
        interval_days = int((later.recorded - earlier.recorded).total_seconds() // 86400)
        flagged = drop > drop_threshold_lb and interval_days <= max_interval_days
        segments.append(
            Segment(
                x1=previous.x,
                y1=previous.y,
                x2=current.x,
                y2=current.y,
                drop=drop,
                interval_days=interval_days,
                flagged=flagged,
                band_x=previous.x,
                band_width=max(current.x - previous.x, 1.0),
                label_x=(previous.x + current.x) / 2,
                label_y=PLOT_TOP - 5,
                # Both halves of the rule, so the flag explains itself without
                # the reader having to measure the x axis.
                drop_label=f"-{drop:.0f} lb in {interval_days}d",
            )
        )

    axis_labels = [
        (y_for(high), _weight_label(high)),
        (y_for((high + low) / 2), _weight_label((high + low) / 2)),
        (y_for(low), _weight_label(low)),
    ]

    net = pounds[0] - pounds[-1]
    if len(pounds) > 1:
        direction = "down" if net > 0 else "up"
        net_label = f"{abs(net):.0f} lb {direction} since {points[0].date_label}"
    else:
        net_label = "single reading on record"

    return Graph(
        points=points,
        segments=segments,
        axis_labels=axis_labels,
        flagged_count=sum(1 for segment in segments if segment.flagged),
        latest_label=f"{_weight_label(pounds[-1])} lb on {points[-1].date_label}",
        net_label=net_label,
    )
