"""Reading weigh-ins out of Canvas: unit conversion, ordering, and exclusions."""

from canvas_sdk.test_utils.factories import PatientFactory

from glp1_care_gap_copilot.weight_trend import recent_weigh_ins
from tests.factories import add_observation, add_weight, days_ago


def pounds_for(patient_id: str, limit: int = 6) -> list[float]:
    """Recorded weights in pounds, oldest first."""
    return [round(weigh_in.pounds, 4) for weigh_in in recent_weigh_ins(patient_id, limit)]


def test_ounces_are_converted_to_pounds() -> None:
    # Canvas stores weight in ounces, so a 240 lb patient is 3840 on the row.
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(1), value="3840", units="oz")

    assert pounds_for(str(patient.id)) == [240.0]


def test_pounds_are_taken_as_recorded() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(1), value="240", units="lbs")

    assert pounds_for(str(patient.id)) == [240.0]


def test_kilograms_are_converted() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(1), value="100", units="kg")

    assert pounds_for(str(patient.id)) == [220.4623]


def test_missing_units_are_assumed_to_be_ounces() -> None:
    # Ounces is Canvas's storage unit; the SDK's own growth-chart example
    # converts from ounces without consulting the units column.
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(1), value="3840", units="")

    assert pounds_for(str(patient.id)) == [240.0]


def test_unrecognized_units_are_dropped_rather_than_guessed() -> None:
    # A confidently wrong weight is worse than a gap in the line.
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(2), value="3840", units="oz")
    add_observation(patient, "weight", days_ago(1), value="17", units="stone")

    assert pounds_for(str(patient.id)) == [240.0]


def test_non_numeric_and_non_positive_values_are_dropped() -> None:
    patient = PatientFactory.create()
    add_observation(patient, "weight", days_ago(3), value="unknown", units="oz")
    add_observation(patient, "weight", days_ago(2), value="0", units="oz")
    add_observation(patient, "weight", days_ago(1), value="-16", units="oz")

    assert pounds_for(str(patient.id)) == []


def test_readings_come_back_oldest_first() -> None:
    patient = PatientFactory.create()
    add_weight(patient, 250.0, days_ago(60))
    add_weight(patient, 244.0, days_ago(30))
    add_weight(patient, 238.0, days_ago(1))

    assert pounds_for(str(patient.id)) == [250.0, 244.0, 238.0]


def test_only_the_most_recent_readings_are_returned() -> None:
    patient = PatientFactory.create()
    for index, weight in enumerate([300.0, 290.0, 280.0, 270.0, 260.0, 250.0, 240.0]):
        add_weight(patient, weight, days_ago(70 - index * 10))

    # Newest six, still oldest-first: the 300 lb reading falls off the left.
    assert pounds_for(str(patient.id), limit=6) == [
        290.0,
        280.0,
        270.0,
        260.0,
        250.0,
        240.0,
    ]


def test_a_non_positive_limit_reads_nothing() -> None:
    patient = PatientFactory.create()
    add_weight(patient, 240.0, days_ago(1))

    assert recent_weigh_ins(str(patient.id), 0) == []
    assert recent_weigh_ins(str(patient.id), -1) == []


def test_bmi_observations_are_not_plotted_as_weights() -> None:
    # The stale-weight *gap* accepts a BMI as evidence someone was weighed;
    # the graph must not, or a BMI of 32 lands on a pound-scaled axis.
    patient = PatientFactory.create()
    add_weight(patient, 240.0, days_ago(2))
    add_observation(patient, "bmi", days_ago(1), value="32", units="")

    assert pounds_for(str(patient.id)) == [240.0]


def test_deleted_readings_are_excluded() -> None:
    patient = PatientFactory.create()
    add_weight(patient, 240.0, days_ago(2))
    add_weight(patient, 999.0, days_ago(1), deleted=True)

    assert pounds_for(str(patient.id)) == [240.0]


def test_blank_values_are_excluded() -> None:
    patient = PatientFactory.create()
    add_weight(patient, 240.0, days_ago(2))
    add_observation(patient, "weight", days_ago(1), value="", units="oz")

    assert pounds_for(str(patient.id)) == [240.0]


def test_another_patients_weights_are_never_read() -> None:
    patient = PatientFactory.create()
    other = PatientFactory.create()
    add_weight(patient, 240.0, days_ago(1))
    add_weight(other, 180.0, days_ago(1))

    assert pounds_for(str(patient.id)) == [240.0]


def test_a_patient_with_no_weights_reads_empty() -> None:
    patient = PatientFactory.create()

    assert recent_weigh_ins(str(patient.id), 6) == []
