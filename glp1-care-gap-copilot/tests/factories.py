"""Record builders for models the SDK does not yet ship a factory for.

Each helper fills the NOT NULL columns the plugin does not care about so that
tests only state the fields relevant to the rule under test.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from canvas_sdk.test_utils.factories import (
    LabReportFactory,
    LabTestFactory,
    LabValueFactory,
    MedicationFactory,
)
from canvas_sdk.v1.data.appointment import Appointment, AppointmentProgressStatus
from canvas_sdk.v1.data.condition import ClinicalStatus, Condition, ConditionCoding
from canvas_sdk.v1.data.medication import MedicationCoding, Status
from canvas_sdk.v1.data.observation import Observation
from canvas_sdk.v1.data.patient import Patient
from canvas_sdk.v1.data.questionnaire import (
    Interview,
    InterviewQuestionResponse,
    Question,
    Questionnaire,
    QuestionnaireQuestionMap,
    ResponseOption,
    ResponseOptionSet,
)
from canvas_sdk.v1.data.user import CanvasUser

from glp1_care_gap_copilot.safety_signals import FINDINGS, QUESTIONNAIRE_CODE


def now() -> datetime:
    """A stable 'current time' for threshold assertions."""
    return datetime.now(timezone.utc)


def days_ago(count: int) -> datetime:
    """A timestamp `count` days before now."""
    return now() - timedelta(days=count)


def days_ahead(count: int) -> datetime:
    """A timestamp `count` days after now."""
    return now() + timedelta(days=count)


def add_medication(
    patient: Patient, display: str, status: str = Status.ACTIVE, **kwargs: Any
) -> None:
    """Give the patient a medication whose coding displays `display`."""
    medication = MedicationFactory.create(
        patient=patient, status=status, deleted=False, **kwargs
    )
    MedicationCoding.objects.create(
        medication=medication,
        system="http://www.nlm.nih.gov/research/umls/rxnorm",
        code="000000",
        display=display,
        user_selected=False,
    )


def add_condition(
    patient: Patient,
    code: str,
    clinical_status: str = ClinicalStatus.ACTIVE,
    display: str = "condition",
    onset_date: date | None = None,
) -> None:
    """Give the patient a condition coded with `code`.

    `onset_date` matters to the safety rule, which pairs a coded finding with a
    rapid weight drop only when the two sit inside the same window.
    """
    condition = Condition.objects.create(
        patient=patient,
        clinical_status=clinical_status,
        deleted=False,
        onset_date=onset_date or date(2024, 1, 1),
        resolution_date=date(2024, 1, 1),
        surgical=False,
        notes="",
    )
    ConditionCoding.objects.create(
        condition=condition,
        system="ICD-10",
        code=code,
        display=display,
        user_selected=False,
    )


def add_observation(
    patient: Patient,
    name: str,
    effective_datetime: datetime,
    value: str = "200",
    units: str = "lbs",
    **kwargs: Any,
) -> Observation:
    """Record an observation such as a weight or BMI."""
    fields: dict[str, Any] = {
        "patient": patient,
        "name": name,
        "value": value,
        "units": units,
        "category": "vital-signs",
        "deleted": False,
        "note_id": 0,
        "effective_datetime": effective_datetime,
    }
    fields.update(kwargs)
    return Observation.objects.create(**fields)


def add_weight(
    patient: Patient, pounds: float, effective_datetime: datetime, **kwargs: Any
) -> Observation:
    """Record a weight the way Canvas stores it — in ounces."""
    return add_observation(
        patient,
        "weight",
        effective_datetime,
        value=str(int(round(pounds * 16))),
        units="oz",
        **kwargs,
    )


def _safety_questionnaire() -> Questionnaire:
    """The shipped GLP-1 Safety Check, created once per test database."""
    existing = Questionnaire.objects.filter(code=QUESTIONNAIRE_CODE).first()
    if existing is not None:
        return existing
    questionnaire = Questionnaire.objects.create(
        status="AC",
        name="GLP-1 Safety Check",
        expected_completion_time=60.0,
        can_originate_in_charting=True,
        use_case_in_charting="SA",
        scoring_function_name="",
        scoring_code_system="",
        scoring_code="",
        code_system="INTERNAL",
        code=QUESTIONNAIRE_CODE,
        search_tags="",
        use_in_shx=False,
        carry_forward="",
    )
    option_set = ResponseOptionSet.objects.create(
        status="AC", name="Yes/No", code_system="INTERNAL", code="YN",
        type="SING", use_in_shx=False,
    )
    for code, name, ordering in (("Y", "Yes", 0), ("N", "No", 1)):
        ResponseOption.objects.create(
            response_option_set=option_set, status="AC", name=name, code=code,
            code_description=name, value=name, ordering=ordering,
        )
    for finding in FINDINGS:
        question = Question.objects.create(
            status="AC", name=finding.label, acknowledge_only=False,
            show_prologue=False, code_system="INTERNAL", code=finding.question_code,
            response_option_set=option_set,
        )
        QuestionnaireQuestionMap.objects.create(
            questionnaire=questionnaire, question=question, status="AC"
        )
    return questionnaire


def complete_safety_check(
    patient: Patient,
    positive_question_codes: tuple[str, ...],
    created: datetime,
    committed: bool = True,
) -> Interview:
    """Record a completed GLP-1 Safety Check.

    Every question is answered; `positive_question_codes` get "Y" and the rest
    get "N", so a test can assert that a *negative* answer is genuinely
    distinguished from an unanswered one.
    """
    questionnaire = _safety_questionnaire()
    committer = (
        CanvasUser.objects.create(email="clinician@example.test", phone_number="")
        if committed
        else None
    )
    interview = Interview.objects.create(
        patient=patient, committer=committer, deleted=False, status="AC",
        name="GLP-1 Safety Check", language_id=0, use_case_in_charting="SA",
        note_id=0, appointment_id=0, progress_status="F",
    )
    interview.questionnaires.add(questionnaire)
    # `created` is auto_now_add, so it has to be forced after the insert.
    Interview.objects.filter(dbid=interview.dbid).update(created=created)

    for question in Question.objects.filter(
        questionnairequestionmap__questionnaire=questionnaire
    ):
        positive = question.code in positive_question_codes
        option = ResponseOption.objects.get(
            response_option_set=question.response_option_set,
            code="Y" if positive else "N",
        )
        InterviewQuestionResponse.objects.create(
            interview=interview, questionnaire=questionnaire, question=question,
            response_option=option, status="AC",
            response_option_value=option.value,
            questionnaire_state="", interview_state="", comment="",
        )
    interview.refresh_from_db()
    return interview


def add_appointment(
    patient: Patient,
    start_time: datetime,
    status: str = AppointmentProgressStatus.CONFIRMED,
) -> Appointment:
    """Book an appointment for the patient."""
    return Appointment.objects.create(
        patient=patient,
        start_time=start_time,
        duration_minutes=30,
        status=status,
        comment="",
        description="",
        telehealth_instructions_sent=False,
    )


def add_lab_result(patient: Patient, lab_name: str, performed_at: datetime) -> None:
    """Record a resulted lab of the given name for the patient."""
    report = LabReportFactory.create(
        patient=patient, date_performed=performed_at, deleted=False
    )
    test = LabTestFactory.create(report=report, ontology_test_name=lab_name)
    LabValueFactory.create(report=report, test=test, value="5.4", units="%")
