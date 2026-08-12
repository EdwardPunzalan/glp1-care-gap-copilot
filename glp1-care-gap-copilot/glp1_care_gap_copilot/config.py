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
DEFAULT_LAB_INTERVAL_DAYS = 90
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
DEFAULT_REQUIRED_LAB_NAMES = (
    "hemoglobin a1c",
    "comprehensive metabolic panel",
    "lipid panel",
)
# Labs in this set are only expected when the patient carries a matching
# diagnosis. Which labs a patient needs is a clinical decision, so the rule is
# explicit, configurable, and testable rather than delegated to the LLM.
DEFAULT_DIABETES_ONLY_LAB_NAMES = ("hemoglobin a1c",)
DEFAULT_DIABETES_ICD10_PREFIXES = ("E11",)
DEFAULT_TASK_TITLE_PREFIX = "GLP-1 Copilot"

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


def _optional_int(secrets: dict[str, Any], key: str) -> int | None:
    raw = secrets.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning(f"[glp1-care-gap-copilot] {key}={_loggable(key, raw)} is not an integer; ignoring")
        return None




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
    required_lab_names: tuple[str, ...]
    diabetes_only_lab_names: tuple[str, ...]
    diabetes_icd10_prefixes: tuple[str, ...]
    outreach_team_dbid: int | None
    lab_partner_name: str
    task_title_prefix: str

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
            required_lab_names=_csv(
                secrets, "REQUIRED_LAB_NAMES", DEFAULT_REQUIRED_LAB_NAMES
            ),
            diabetes_only_lab_names=_csv(
                secrets,
                "DIABETES_ONLY_LAB_NAMES",
                DEFAULT_DIABETES_ONLY_LAB_NAMES,
                clearable=True,
            ),
            diabetes_icd10_prefixes=_csv(
                secrets, "DIABETES_ICD10_PREFIXES", DEFAULT_DIABETES_ICD10_PREFIXES
            ),
            outreach_team_dbid=_optional_int(secrets, "OUTREACH_TEAM_DBID"),
            lab_partner_name=_text(secrets, "LAB_PARTNER_NAME"),
            task_title_prefix=_text(secrets, "TASK_TITLE_PREFIX", DEFAULT_TASK_TITLE_PREFIX),
        )
