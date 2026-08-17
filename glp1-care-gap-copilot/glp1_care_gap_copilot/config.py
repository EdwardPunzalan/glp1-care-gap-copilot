"""Plugin configuration parsed defensively from `self.secrets`.

Every clinical threshold lives here rather than in the detection code, so an
instance can retune the plugin without a code change. A missing or malformed
value never raises — it logs and falls back to the documented default, because
a bad secret must not stop the card from rendering.
"""

from dataclasses import dataclass
from typing import Any

from logger import log

DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS = 30
DEFAULT_FOLLOWUP_HORIZON_DAYS = 90
# Both generic and brand names, because Canvas stores the *brand* in the
# medication coding display ("Ozempic 4 mg tablet") — a generics-only list
# silently misses nearly every real GLP-1 patient. Confirmed on a live instance:
# a patient on Ozempic was scored out of scope until the brands were added.
DEFAULT_GLP1_MED_NAME_FRAGMENTS = (
    # semaglutide
    "semaglutide",
    "ozempic",
    "wegovy",
    "rybelsus",
    # tirzepatide
    "tirzepatide",
    "mounjaro",
    "zepbound",
    # liraglutide
    "liraglutide",
    "victoza",
    "saxenda",
    # dulaglutide
    "dulaglutide",
    "trulicity",
    # exenatide
    "exenatide",
    "byetta",
    "bydureon",
)
DEFAULT_OBESITY_ICD10_PREFIXES = ("E66", "Z68.4")

# The metabolic comorbidities that put a patient on the short lab interval.
# Which labs a patient needs is a clinical decision, so each group is explicit,
# configurable, and testable rather than inferred.
# E78 covers hypercholesterolemia, hyperlipidemia, and mixed dyslipidemia.
DEFAULT_HIGH_CHOLESTEROL_ICD10_PREFIXES = ("E78",)
# Both types: a type 1 patient on a GLP-1 needs the same monitoring.
DEFAULT_DIABETES_ICD10_PREFIXES = ("E11", "E10")
# R73.03 is prediabetes proper; R73.01/R73.09/R73.9 are the impaired-glucose
# and hyperglycemia codes that get used for the same picture in practice.
DEFAULT_PREDIABETES_ICD10_PREFIXES = ("R73",)
# E03 is acquired hypothyroidism, E02 subclinical iodine-deficiency, E89.0
# post-procedural. Presence of any adds TSH with reflex to T4.
DEFAULT_HYPOTHYROID_ICD10_PREFIXES = ("E03", "E02", "E89.0")

# How long a patient must have been on a GLP-1 before any lab is expected.
# Drawing a metabolic panel three weeks in measures the diet they were on
# before the drug, not the drug.
DEFAULT_GLP1_MIN_DAYS_FOR_LABS = 90
# How long a result stays current for a patient with a metabolic comorbidity.
DEFAULT_LAB_INTERVAL_DAYS = 90
# ...and for a patient whose only qualifying diagnosis is obesity. Obesity
# alone is a slower clinical picture; putting both tiers on 90 days would bury
# the patients who need watching under the ones who do not.
DEFAULT_LAB_INTERVAL_OBESITY_ONLY_DAYS = 365
DEFAULT_TASK_TITLE_PREFIX = "GLP-1 Copilot"

# Weigh-ins plotted on the chart summary trend graph. Six covers roughly six
# months of monthly weights — enough to read a trajectory without shrinking the
# points into noise on a narrow chart summary column.
DEFAULT_WEIGHT_TREND_POINTS = 6
# Pounds lost between two consecutive weigh-ins before the interval is flagged.
# Only losses count: rapid *loss* is the GLP-1 safety signal. Five pounds inside
# a single dosing week is roughly 1-2% of body weight for a typical patient on
# these drugs, which is the point at which the loss is outpacing the guideline
# rate rather than tracking it.
DEFAULT_WEIGHT_DROP_ALERT_LB = 5.0
# How close together those two weigh-ins must be for the loss to count as
# *rapid*. GLP-1s are dosed weekly, so a week is the natural unit: 14 lb between
# consecutive weekly weights is alarming, the same 14 lb across ten weeks is the
# drug working. Raise this if a practice weighs patients on a looser schedule —
# at 7 a pair 8 days apart is silently ignored.
DEFAULT_WEIGHT_DROP_MAX_INTERVAL_DAYS = 7.0

# How close a warning sign has to sit to the rapid drop before the two are
# treated as one clinical picture. Measured either side of the drop's later
# weigh-in: a patient may report the symptom at the visit that discovers the
# loss, or a few weeks later when they finally call in. Too wide and a summer
# drop pairs with an autumn symptom that has nothing to do with it.
DEFAULT_SAFETY_WINDOW_DAYS = 30
# How many of the seven warning signs must be present alongside the drop. One,
# per the requesting clinician: this is a safety net, so it errs toward calling
# the patient.
DEFAULT_SAFETY_MIN_FINDINGS = 1

# How far back the panel-wide scan looks for new weigh-ins. A rapid drop can only
# appear when a new weight lands, so this is what makes the scan complete rather
# than a sample. Two days for a nightly job: enough overlap that a single failed
# run does not create a gap, small enough that the scan stays cheap.
DEFAULT_ALERT_SCAN_LOOKBACK_DAYS = 2
# Safety valve on one scan, not a paging cursor. Sized well above any realistic
# day's weigh-ins; hitting it is logged as a sign the lookback needs revisiting.
DEFAULT_ALERT_SCAN_MAX_PATIENTS = 500

# Patients shown on the monitoring hub. The page bulk-loads a fixed number of
# queries regardless of this, but a thousand cards is a page nobody reads.
DEFAULT_HUB_MAX_PATIENTS = 100
# How far back the hub reads weigh-ins for its sparklines.
DEFAULT_HUB_WEIGHT_DAYS = 365

# Maps an expected lab name to the exact lab-partner order code to order for it.
# Deliberately empty by default and never inferred: a partner catalog carries
# many near-identical variants of the same panel (XPC Lab lists 8 comprehensive
# metabolic panels), and choosing between them is a clinical and contractual
# decision for the practice, not something this plugin should guess.
DEFAULT_LAB_TEST_ORDER_CODES: dict[str, str] = {}

# Sentinel a deployment sets to mean "no entries" on a clearable list, since a
# blank value is indistinguishable from an unconfigured one.
EMPTY_LIST_SENTINEL = "none"

# Variables whose value must never reach the logs, whatever goes wrong parsing
# them. Redaction is keyed on the variable name rather than left to the choice
# of parser, so a value declared sensitive in the manifest stays masked even if
# it is later read through a parser that echoes malformed input.
#
# Empty today — the only sensitive variable was the LLM API key, removed with
# that feature. Kept wired up, and held to the manifest by a test, so marking a
# future variable sensitive is safe by default rather than a leak waiting to
# happen.
SENSITIVE_VARIABLES: frozenset[str] = frozenset()
REDACTED = "<redacted>"

def _loggable(key: str, raw: Any) -> str:
    """Render a variable's value for a log message, masking sensitive ones."""
    if key in SENSITIVE_VARIABLES:
        return REDACTED
    return repr(raw)


def _text(secrets: dict[str, Any], key: str, default: str = "") -> str:
    value = secrets.get(key)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _positive_int(secrets: dict[str, Any], key: str, default: int) -> int:
    raw = secrets.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning(
            f"[glp1-care-gap-copilot] {key}={_loggable(key, raw)} is not an integer; "
            f"using {default}"
        )
        return default
    if value <= 0:
        log.warning(f"[glp1-care-gap-copilot] {key}={value} must be positive; using {default}")
        return default
    return value


def _positive_float(secrets: dict[str, Any], key: str, default: float) -> float:
    raw = secrets.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log.warning(
            f"[glp1-care-gap-copilot] {key}={_loggable(key, raw)} is not a number; "
            f"using {default}"
        )
        return default
    if value <= 0:
        log.warning(f"[glp1-care-gap-copilot] {key}={value} must be positive; using {default}")
        return default
    return value


def _optional_int(secrets: dict[str, Any], key: str) -> int | None:
    raw = secrets.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning(f"[glp1-care-gap-copilot] {key}={_loggable(key, raw)} is not an integer; ignoring")
        return None




def _code_map(secrets: dict[str, Any], key: str) -> dict[str, str]:
    """Parse a `lab name:order code` map, e.g. `hemoglobin a1c:496, lipid panel:7600`.

    Entries that are malformed or missing a side are dropped with a warning
    rather than guessed at — an unmapped lab simply gets no order button.
    """
    raw = secrets.get(key)
    if raw is None or str(raw).strip() == "":
        return dict(DEFAULT_LAB_TEST_ORDER_CODES)
    mapping: dict[str, str] = {}
    for entry in str(raw).split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, separator, code = entry.rpartition(":")
        name, code = name.strip().lower(), code.strip()
        if not separator or not name or not code:
            log.warning(
                f"[glp1-care-gap-copilot] {key} entry {entry!r} is not "
                "'lab name:order code'; ignoring it"
            )
            continue
        mapping[name] = code
    return mapping


def _csv(
    secrets: dict[str, Any],
    key: str,
    default: tuple[str, ...],
    clearable: bool = False,
) -> tuple[str, ...]:
    """Parse a comma-separated secret.

    A variable declared in the manifest but never configured arrives as `""`,
    not as absent — so a blank value cannot be distinguished from "never set"
    and must mean "use the default". Lists that a deployment may legitimately
    want *empty* therefore need an explicit sentinel: `clearable` lists accept
    the literal `none` to mean "no entries".
    """
    raw = secrets.get(key)
    # Blank is the ordinary default path on a fresh install, so it stays silent.
    # Warning here fired on every chart render, for every patient.
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip()
    if clearable and text.lower() == EMPTY_LIST_SENTINEL:
        return ()
    items = tuple(part.strip() for part in text.split(",") if part.strip())
    if not items:
        log.warning(
            f"[glp1-care-gap-copilot] {key}={_loggable(key, raw)} has no usable entries; "
            "using defaults"
        )
        return default
    return items


@dataclass(frozen=True)
class Config:
    """Resolved plugin settings for a single card render."""

    weight_check_interval_days: int
    lab_interval_days: int
    followup_horizon_days: int
    glp1_med_name_fragments: tuple[str, ...]
    obesity_icd10_prefixes: tuple[str, ...]
    high_cholesterol_icd10_prefixes: tuple[str, ...]
    prediabetes_icd10_prefixes: tuple[str, ...]
    hypothyroid_icd10_prefixes: tuple[str, ...]
    glp1_min_days_for_labs: int
    lab_interval_obesity_only_days: int
    diabetes_icd10_prefixes: tuple[str, ...]
    outreach_team_dbid: int | None
    lab_partner_names: tuple[str, ...]
    lab_test_order_codes: dict[str, str]
    task_title_prefix: str
    weight_trend_points: int
    weight_drop_alert_lb: float
    weight_drop_max_interval_days: float
    safety_window_days: int
    safety_min_findings: int
    alert_scan_lookback_days: int
    alert_scan_max_patients: int
    hub_max_patients: int
    hub_weight_days: int

    @classmethod
    def from_secrets(cls, secrets: dict[str, Any] | None) -> "Config":
        """Build a config from plugin secrets, substituting defaults for bad input."""
        secrets = secrets or {}
        return cls(
            weight_check_interval_days=_positive_int(
                secrets, "WEIGHT_CHECK_INTERVAL_DAYS", DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS
            ),
            lab_interval_days=_positive_int(
                secrets, "LAB_INTERVAL_DAYS", DEFAULT_LAB_INTERVAL_DAYS
            ),
            followup_horizon_days=_positive_int(
                secrets, "FOLLOWUP_HORIZON_DAYS", DEFAULT_FOLLOWUP_HORIZON_DAYS
            ),
            glp1_med_name_fragments=_csv(
                secrets, "GLP1_MED_NAME_FRAGMENTS", DEFAULT_GLP1_MED_NAME_FRAGMENTS
            ),
            obesity_icd10_prefixes=_csv(
                secrets, "OBESITY_ICD10_PREFIXES", DEFAULT_OBESITY_ICD10_PREFIXES
            ),
            # Every comorbidity group is clearable with the `none` sentinel: a
            # practice that does not want, say, pre-diabetes to pull patients
            # onto the short interval needs a way to say so, and blank cannot
            # mean "cleared" — it is indistinguishable from never-configured.
            diabetes_icd10_prefixes=_csv(
                secrets,
                "DIABETES_ICD10_PREFIXES",
                DEFAULT_DIABETES_ICD10_PREFIXES,
                clearable=True,
            ),
            high_cholesterol_icd10_prefixes=_csv(
                secrets,
                "HIGH_CHOLESTEROL_ICD10_PREFIXES",
                DEFAULT_HIGH_CHOLESTEROL_ICD10_PREFIXES,
                clearable=True,
            ),
            prediabetes_icd10_prefixes=_csv(
                secrets,
                "PREDIABETES_ICD10_PREFIXES",
                DEFAULT_PREDIABETES_ICD10_PREFIXES,
                clearable=True,
            ),
            # Cleared, this switches TSH off entirely.
            hypothyroid_icd10_prefixes=_csv(
                secrets,
                "HYPOTHYROID_ICD10_PREFIXES",
                DEFAULT_HYPOTHYROID_ICD10_PREFIXES,
                clearable=True,
            ),
            glp1_min_days_for_labs=_positive_int(
                secrets, "GLP1_MIN_DAYS_FOR_LABS", DEFAULT_GLP1_MIN_DAYS_FOR_LABS
            ),
            lab_interval_obesity_only_days=_positive_int(
                secrets,
                "LAB_INTERVAL_OBESITY_ONLY_DAYS",
                DEFAULT_LAB_INTERVAL_OBESITY_ONLY_DAYS,
            ),
            outreach_team_dbid=_optional_int(secrets, "OUTREACH_TEAM_DBID"),
            # A comma-separated list, first entry the default. A single name
            # is the same string it always was, so clinics using one lab need
            # no config change and see no change on the card.
            lab_partner_names=_csv(secrets, "LAB_PARTNER_NAME", ()),
            lab_test_order_codes=_code_map(secrets, "LAB_TEST_ORDER_CODES"),
            task_title_prefix=_text(secrets, "TASK_TITLE_PREFIX", DEFAULT_TASK_TITLE_PREFIX),
            weight_trend_points=_positive_int(
                secrets, "WEIGHT_TREND_POINTS", DEFAULT_WEIGHT_TREND_POINTS
            ),
            weight_drop_alert_lb=_positive_float(
                secrets, "WEIGHT_DROP_ALERT_LB", DEFAULT_WEIGHT_DROP_ALERT_LB
            ),
            weight_drop_max_interval_days=_positive_float(
                secrets,
                "WEIGHT_DROP_MAX_INTERVAL_DAYS",
                DEFAULT_WEIGHT_DROP_MAX_INTERVAL_DAYS,
            ),
            safety_window_days=_positive_int(
                secrets, "SAFETY_WINDOW_DAYS", DEFAULT_SAFETY_WINDOW_DAYS
            ),
            safety_min_findings=_positive_int(
                secrets, "SAFETY_MIN_FINDINGS", DEFAULT_SAFETY_MIN_FINDINGS
            ),
            alert_scan_lookback_days=_positive_int(
                secrets, "ALERT_SCAN_LOOKBACK_DAYS", DEFAULT_ALERT_SCAN_LOOKBACK_DAYS
            ),
            alert_scan_max_patients=_positive_int(
                secrets, "ALERT_SCAN_MAX_PATIENTS", DEFAULT_ALERT_SCAN_MAX_PATIENTS
            ),
            hub_max_patients=_positive_int(
                secrets, "HUB_MAX_PATIENTS", DEFAULT_HUB_MAX_PATIENTS
            ),
            hub_weight_days=_positive_int(
                secrets, "HUB_WEIGHT_DAYS", DEFAULT_HUB_WEIGHT_DAYS
            ),
        )
