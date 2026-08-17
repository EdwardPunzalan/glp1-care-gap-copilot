"""The monitoring hub: every GLP-1 patient on one page.

The chart card answers "what is wrong with *this* patient". This answers "who am
I watching, and what is missing" — which is the question a clinician actually
opens their day with.

**Everything is bulk-loaded.** The cohort, prescriptions, weights, labs and
appointments each come from exactly one query, joined in Python. The per-patient
alternative reads more naturally and would issue five queries per patient: at a
hundred patients that is five hundred queries for one page load, and the page
would get slower every time the practice grew. A fixed query count is the whole
design constraint here.

The high-risk band is deliberately *not* recomputed per patient either — it
reuses `scan_for_alerts`, which is bounded by recent weigh-ins. That also
guarantees the hub, the nightly task, and the chart banner can never disagree
about who is high risk.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from django.db.models import Q

from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.lab import LabValue
from canvas_sdk.v1.data.medication import Medication, Status
from canvas_sdk.v1.data.observation import Observation

from glp1_care_gap_copilot.config import Config
from glp1_care_gap_copilot.gaps import GAP_LABS_OVERDUE, GAP_NO_FOLLOWUP, GAP_STALE_WEIGHT
from glp1_care_gap_copilot.labs import BASE_REQUIREMENTS, TSH_REQUIREMENT
from glp1_care_gap_copilot.weight_trend import WEIGHT_OBSERVATION_NAME, _to_pounds

#: Weigh-ins drawn in a card's sparkline.
SPARK_POINTS = 8
#: Sparkline viewBox. Small and wide: it is a glance, not a chart to read values
#: off — the full graph is a click away on the chart.
SPARK_WIDTH = 220.0
SPARK_HEIGHT = 44.0
SPARK_PAD = 4.0


@dataclass(frozen=True)
class Prescription:
    """An active GLP-1 the patient is on."""

    name: str
    #: Nullable in practice. `Medication.start_date` is declared non-null on the
    #: SDK model, but real rows on a live instance come back as None — the model
    #: is a view over a schema that permits it. Trusting the declaration crashed
    #: the whole page on first contact with production data.
    started: datetime | None
    #: Precomputed for the page. Django templates cannot call a method with
    #: arguments, so anything needing `now` is resolved at load time.
    days_on_display: str = ""

    def days_on(self, now: datetime) -> int | None:
        """Whole days since this prescription started, if that is known."""
        if self.started is None:
            return None
        return max((now - self.started).days, 0)


@dataclass(frozen=True)
class MissingItem:
    """Something absent from the chart that a task can chase."""

    gap_key: str
    label: str
    #: What the task will say. Kept here so the page and the created task
    #: cannot drift apart.
    task_summary: str


@dataclass(frozen=True)
class WatchedPatient:
    """One card on the hub."""

    patient_id: str
    name: str
    prescriptions: tuple[Prescription, ...] = ()
    weights: tuple[tuple[datetime, float], ...] = ()
    last_labs: dict[str, datetime | None] = field(default_factory=dict)
    next_followup: datetime | None = None
    missing: tuple[MissingItem, ...] = ()
    #: Set when the safety scan flagged this patient; drives the red treatment.
    risk_reason: str = ""
    #: Lab recency chips, resolved at load time for the same reason as above.
    chips: tuple["LabChip", ...] = ()

    @property
    def is_high_risk(self) -> bool:
        """True when the safety rule flagged this patient."""
        return bool(self.risk_reason)

    @property
    def latest_weight(self) -> float | None:
        """Most recent weight in pounds."""
        return self.weights[-1][1] if self.weights else None

    @property
    def weight_change(self) -> float | None:
        """Pounds lost across the plotted window. Negative means gained."""
        if len(self.weights) < 2:
            return None
        return self.weights[0][1] - self.weights[-1][1]

    @property
    def spark(self) -> str:
        """The sparkline as an SVG `points` attribute."""
        return sparkline(self.weights)

    @property
    def spark_area(self) -> str:
        """The same line closed to the baseline, for the fill beneath it."""
        points = self.spark
        if not points:
            return ""
        first_x = points.split(" ", 1)[0].split(",")[0]
        last_x = points.rsplit(" ", 1)[-1].split(",")[0]
        floor = f"{SPARK_HEIGHT:.1f}"
        return f"{first_x},{floor} {points} {last_x},{floor}"

    @property
    def spark_last(self) -> tuple[float, float] | None:
        """Coordinates of the newest reading, so it can be marked."""
        points = self.spark
        if not points:
            return None
        x, y = points.rsplit(" ", 1)[-1].split(",")
        return float(x), float(y)


@dataclass(frozen=True)
class LabChip:
    """One lab's recency, rendered as a state chip rather than a raw date.

    "112d" tells a clinician more at a glance than "2026-04-26", and the state
    carries the judgement so the colour is never decoration.
    """

    label: str
    text: str
    #: One of "ok", "warn", "none" — drives the chip's colour.
    state: str


def lab_chips(
    patient: "WatchedPatient", config: Config, now: datetime
) -> list[LabChip]:
    """Recency chips for the labs this patient owes.

    TSH only appears when it has actually been resulted: the hub does not know
    whether this patient has a thyroid diagnosis without a per-patient query,
    and showing "TSH missing" to everyone would invent a gap the chart card
    would not agree with.
    """
    chips = []
    requirements = list(BASE_REQUIREMENTS)
    if patient.last_labs.get(TSH_REQUIREMENT.key):
        requirements.append(TSH_REQUIREMENT)

    for requirement in requirements:
        performed = patient.last_labs.get(requirement.key)
        short = requirement.label.replace("comprehensive or basic ", "")
        if performed is None:
            chips.append(LabChip(short, "none", "none"))
            continue
        days = max((now - performed).days, 0)
        state = "ok" if days <= config.lab_interval_days else "warn"
        chips.append(LabChip(short, f"{days}d", state))
    return chips


def sparkline(weights: tuple[tuple[datetime, float], ...]) -> str:
    """Plot weigh-ins into a tiny fixed-size polyline.

    Time-scaled on x, like the full graph, so an eight-month gap does not look
    like a week. A flat series is centred rather than divided by zero.
    """
    if len(weights) < 2:
        return ""
    values = [pounds for _, pounds in weights]
    low, high = min(values), max(values)
    span = high - low
    first = weights[0][0]
    elapsed = (weights[-1][0] - first).total_seconds()

    inner_w = SPARK_WIDTH - 2 * SPARK_PAD
    inner_h = SPARK_HEIGHT - 2 * SPARK_PAD

    points = []
    for index, (moment, pounds) in enumerate(weights):
        if elapsed > 0:
            ratio = (moment - first).total_seconds() / elapsed
        else:
            ratio = index / (len(weights) - 1)
        x = SPARK_PAD + ratio * inner_w
        if span < 0.01:
            y = SPARK_PAD + inner_h / 2
        else:
            # SVG y grows downward, so a heavier reading sits nearer the top.
            y = SPARK_PAD + inner_h - ((pounds - low) / span) * inner_h
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _cohort(config: Config, limit: int) -> dict[str, dict]:
    """Every patient on an active GLP-1, with the drug and when it started."""
    name_query = Q()
    for fragment in config.glp1_med_name_fragments:
        cleaned = fragment.strip()
        if cleaned:
            name_query |= Q(codings__display__icontains=cleaned)
    if not name_query:
        return {}

    rows = (
        Medication.objects.filter(
            name_query,
            status=Status.ACTIVE,
            deleted=False,
            entered_in_error__isnull=True,
        )
        .exclude(patient__isnull=True)
        .order_by("patient__id", "start_date")
        .values_list(
            "patient__id",
            "patient__first_name",
            "patient__last_name",
            "codings__display",
            "start_date",
        )
    )

    patients: dict[str, dict] = {}
    for patient_id, first, last, display, start in rows:
        key = str(patient_id)
        if key not in patients:
            if len(patients) >= limit:
                continue
            patients[key] = {
                "name": f"{first or ''} {last or ''}".strip() or "Unnamed patient",
                "prescriptions": [],
            }
        # One row per coding; a medication with several codings would otherwise
        # be listed once per coding on the card.
        if not any(p.name == display for p in patients[key]["prescriptions"]):
            patients[key]["prescriptions"].append(
                Prescription(name=display or "GLP-1 medication", started=start)
            )
    return patients


def _weights(patient_ids: list[str], since: datetime) -> dict[str, list]:
    """Recent weigh-ins per patient, oldest first, from one query."""
    rows = (
        Observation.objects.filter(
            patient__id__in=patient_ids,
            name__iexact=WEIGHT_OBSERVATION_NAME,
            deleted=False,
            entered_in_error__isnull=True,
            effective_datetime__isnull=False,
            effective_datetime__gte=since,
        )
        .exclude(value__isnull=True)
        .exclude(value="")
        .order_by("patient__id", "effective_datetime")
        .values_list("patient__id", "effective_datetime", "value", "units")
    )
    found: dict[str, list] = {}
    for patient_id, recorded, value, units in rows:
        pounds = _to_pounds(value, units)
        if pounds is None:
            continue
        found.setdefault(str(patient_id), []).append((recorded, pounds))
    # Keep only the newest few per patient — the sparkline is a glance.
    return {key: series[-SPARK_POINTS:] for key, series in found.items()}


def _labs(patient_ids: list[str]) -> dict[str, dict[str, datetime]]:
    """Most recent result date per lab requirement per patient, in one query."""
    requirements = (*BASE_REQUIREMENTS, TSH_REQUIREMENT)
    name_query = Q()
    for requirement in requirements:
        for alias in requirement.satisfied_by:
            name_query |= Q(codings__name__icontains=alias) | Q(
                test__ontology_test_name__icontains=alias
            )

    rows = LabValue.objects.filter(
        name_query,
        report__patient__id__in=patient_ids,
        report__deleted=False,
        report__entered_in_error__isnull=True,
        report__date_performed__isnull=False,
    ).values_list(
        "report__patient__id",
        "report__date_performed",
        "codings__name",
        "test__ontology_test_name",
    )

    found: dict[str, dict[str, datetime]] = {}
    for patient_id, performed, coding_name, test_name in rows:
        haystack = f"{coding_name or ''} {test_name or ''}".lower()
        for requirement in requirements:
            if not any(alias in haystack for alias in requirement.satisfied_by):
                continue
            per_patient = found.setdefault(str(patient_id), {})
            current = per_patient.get(requirement.key)
            if current is None or performed > current:
                per_patient[requirement.key] = performed
    return found


def _followups(patient_ids: list[str], now: datetime) -> dict[str, datetime]:
    """Soonest upcoming appointment per patient, in one query."""
    rows = (
        Appointment.objects.filter(
            patient__id__in=patient_ids,
            entered_in_error__isnull=True,
            start_time__gte=now,
        )
        .exclude(status=AppointmentProgressStatus.CANCELLED)
        .order_by("patient__id", "start_time")
        .values_list("patient__id", "start_time")
    )
    soonest: dict[str, datetime] = {}
    for patient_id, start in rows:
        soonest.setdefault(str(patient_id), start)
    return soonest


def _missing(
    weights: list,
    labs: dict[str, datetime],
    followup: datetime | None,
    prescriptions: list[Prescription],
    config: Config,
    now: datetime,
) -> tuple[MissingItem, ...]:
    """What this chart is missing, as items a task can chase.

    Only gaps a task can actually close are listed. The lab check mirrors the
    card's own rule — nothing is asked for before the time-on-therapy gate,
    because chasing a lab the plugin would not ask for on the chart would be
    the hub contradicting itself.
    """
    items: list[MissingItem] = []

    if not weights:
        items.append(
            MissingItem(GAP_STALE_WEIGHT, "No weight on file", "No weight ever recorded")
        )
    else:
        days = max((now - weights[-1][0]).days, 0)
        if days > config.weight_check_interval_days:
            items.append(
                MissingItem(
                    GAP_STALE_WEIGHT,
                    f"Weight {days} days old",
                    f"No weight recorded in {days} days",
                )
            )

    # Longest-running prescription decides the gate, matching the chart rule:
    # switching drugs does not restart the monitoring clock.
    # A prescription with no start date proves nothing about time on therapy,
    # so it is skipped rather than counted as day zero. If none of them has a
    # date, no lab is chased — the chart card would not chase one either.
    durations = [days for rx in prescriptions if (days := rx.days_on(now)) is not None]
    if durations and max(durations) >= config.glp1_min_days_for_labs:
        overdue = [
            requirement.label
            for requirement in BASE_REQUIREMENTS
            if labs.get(requirement.key) is None
            or (now - labs[requirement.key]).days > config.lab_interval_days
        ]
        if overdue:
            readable = ", ".join(overdue)
            items.append(
                MissingItem(
                    GAP_LABS_OVERDUE,
                    f"Labs due: {readable}",
                    f"Monitoring labs due: {readable}",
                )
            )

    if followup is None:
        items.append(
            MissingItem(
                GAP_NO_FOLLOWUP,
                "No follow-up booked",
                f"No follow-up appointment scheduled in the next "
                f"{config.followup_horizon_days} days",
            )
        )
    return tuple(items)


def load_hub(
    config: Config, risk_by_patient: dict[str, str], now: datetime | None = None
) -> list[WatchedPatient]:
    """Every watched patient, high risk first, then by name.

    `risk_by_patient` comes from the safety scan rather than being recomputed
    here, so the hub can never disagree with the banner or the nightly task.
    """
    moment = now or datetime.now(timezone.utc)
    cohort = _cohort(config, config.hub_max_patients)
    if not cohort:
        return []

    patient_ids = list(cohort)
    weights = _weights(patient_ids, moment - timedelta(days=config.hub_weight_days))
    labs = _labs(patient_ids)
    followups = _followups(patient_ids, moment)

    watched = []
    for patient_id, facts in cohort.items():
        series = weights.get(patient_id, [])
        patient_labs = labs.get(patient_id, {})
        followup = followups.get(patient_id)
        watched.append(
            WatchedPatient(
                patient_id=patient_id,
                name=facts["name"],
                prescriptions=tuple(facts["prescriptions"]),
                weights=tuple(series),
                last_labs={
                    requirement.key: patient_labs.get(requirement.key)
                    for requirement in (*BASE_REQUIREMENTS, TSH_REQUIREMENT)
                },
                next_followup=followup,
                missing=_missing(
                    series, patient_labs, followup, facts["prescriptions"], config, moment
                ),
                risk_reason=risk_by_patient.get(patient_id, ""),
            )
        )
        watched[-1] = _with_display(watched[-1], config, moment)

    watched.sort(key=lambda p: (not p.is_high_risk, p.name.lower()))
    return watched


def _with_display(
    patient: WatchedPatient, config: Config, now: datetime
) -> WatchedPatient:
    """Resolve everything the template cannot compute for itself."""
    prescriptions = []
    for rx in patient.prescriptions:
        days = rx.days_on(now)
        # Each unit is dropped once it stops being a number anyone counts. A
        # long-standing prescription on a live chart read "102 mo on therapy",
        # which is arithmetic the reader should not have to do.
        if days is None:
            label = ""
        elif days >= 730:
            label = f"{days // 365} yr on therapy"
        elif days >= 60:
            label = f"{days // 30} mo on therapy"
        else:
            label = f"{days} days on therapy"
        prescriptions.append(
            Prescription(name=rx.name, started=rx.started, days_on_display=label)
        )
    return WatchedPatient(
        patient_id=patient.patient_id,
        name=patient.name,
        prescriptions=tuple(prescriptions),
        weights=patient.weights,
        last_labs=patient.last_labs,
        next_followup=patient.next_followup,
        missing=patient.missing,
        risk_reason=patient.risk_reason,
        chips=tuple(lab_chips(patient, config, now)),
    )
