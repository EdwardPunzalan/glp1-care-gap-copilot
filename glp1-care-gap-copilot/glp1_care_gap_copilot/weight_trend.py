"""Recent weigh-ins for the weight-trend graph.

Kept separate from `gaps.py` on purpose. The stale-weight *gap* only needs the
newest timestamp and deliberately accepts a BMI observation as evidence that
someone weighed the patient. The *graph* needs values, and mixing BMI (~32) into
a pound-scaled axis would wreck it — so this module matches `weight` only.

Canvas stores weight observations in **ounces** (`weight_oz` on the vitals
command), which is why values are normalized rather than plotted raw.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from canvas_sdk.v1.data.observation import Observation
from logger import log

WEIGHT_OBSERVATION_NAME = "weight"

# Multiplier from the recorded unit to pounds. Canvas's native unit for a weight
# observation is ounces; the others are accepted because an instance that
# imports vitals from an outside system may carry them.
_POUNDS_PER_UNIT = {
    "oz": 1.0 / 16.0,
    "ozs": 1.0 / 16.0,
    "ounce": 1.0 / 16.0,
    "ounces": 1.0 / 16.0,
    "lb": 1.0,
    "lbs": 1.0,
    "pound": 1.0,
    "pounds": 1.0,
    "kg": 2.2046226218,
    "kgs": 2.2046226218,
    "kilogram": 2.2046226218,
    "kilograms": 2.2046226218,
    "g": 1.0 / 453.59237,
    "gram": 1.0 / 453.59237,
    "grams": 1.0 / 453.59237,
}

# Assumed when the observation carries no units at all. Ounces is Canvas's
# documented storage unit for weight, and the SDK's own growth-chart example
# converts `obs.value` from ounces without consulting the units column.
_ASSUMED_UNIT = "oz"


@dataclass(frozen=True)
class WeighIn:
    """One weight reading, normalized to pounds."""

    recorded: datetime
    pounds: float


def _to_pounds(value: Any, units: Any) -> float | None:
    """Convert a recorded weight to pounds, or None if it cannot be trusted.

    An unrecognized unit is dropped rather than guessed at: showing a clinician
    a confidently wrong weight is worse than showing them a gap in the line.
    """
    try:
        magnitude = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if magnitude <= 0:
        return None

    unit = str(units or "").strip().lower().rstrip(".")
    if not unit:
        unit = _ASSUMED_UNIT
    multiplier = _POUNDS_PER_UNIT.get(unit)
    if multiplier is None:
        log.warning(
            f"[glp1-care-gap-copilot] weight observation has unrecognized units "
            f"{unit!r}; skipping that reading"
        )
        return None
    return magnitude * multiplier


def recent_weigh_ins(patient_id: str, limit: int) -> list[WeighIn]:
    """The patient's most recent weigh-ins, oldest first, at most `limit` of them.

    One query, bounded by `limit` rather than by how many times the patient has
    been weighed, so a patient with years of vitals costs the same as a new one.
    """
    if limit <= 0:
        return []

    rows = (
        Observation.objects.for_patient(patient_id)
        .filter(
            name__iexact=WEIGHT_OBSERVATION_NAME,
            deleted=False,
            entered_in_error__isnull=True,
            effective_datetime__isnull=False,
        )
        .exclude(value__isnull=True)
        .exclude(value="")
        .order_by("-effective_datetime")
        .values_list("effective_datetime", "value", "units")[:limit]
    )

    weigh_ins = []
    for recorded, value, units in rows:
        pounds = _to_pounds(value, units)
        if pounds is not None:
            weigh_ins.append(WeighIn(recorded=recorded, pounds=pounds))

    # The query takes the newest `limit`; the graph reads left-to-right in time.
    weigh_ins.reverse()
    return weigh_ins
