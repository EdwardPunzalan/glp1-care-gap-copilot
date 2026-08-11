"""Plugin configuration parsed defensively from `self.secrets`.

Every clinical threshold lives here rather than in the detection code, so an
instance can retune the plugin without a code change. A missing or malformed
value never raises — it logs and falls back to the documented default, because
a bad secret must not stop the card from rendering.
"""

from dataclasses import dataclass
from typing import Any

from logger import log

DEFAULT_LLM_MODEL = "claude-sonnet-5"
DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS = 30
DEFAULT_LAB_INTERVAL_DAYS = 90
DEFAULT_FOLLOWUP_HORIZON_DAYS = 90
DEFAULT_GLP1_MED_NAME_FRAGMENTS = (
    "semaglutide",
    "tirzepatide",
    "liraglutide",
    "dulaglutide",
    "exenatide",
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

_TRUE_VALUES = frozenset({"true", "1", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"false", "0", "no", "n", "off"})


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
        log.warning(f"[glp1-care-gap-copilot] {key}={raw!r} is not an integer; using {default}")
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
        log.warning(f"[glp1-care-gap-copilot] {key}={raw!r} is not an integer; ignoring")
        return None


def _flag(secrets: dict[str, Any], key: str, default: bool) -> bool:
    raw = secrets.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    log.warning(f"[glp1-care-gap-copilot] {key}={raw!r} is not a boolean; using {default}")
    return default


def _csv(
    secrets: dict[str, Any],
    key: str,
    default: tuple[str, ...],
    allow_empty: bool = False,
) -> tuple[str, ...]:
    """Parse a comma-separated secret.

    `allow_empty` distinguishes lists where clearing the value is a meaningful
    instruction (drop the conditional-lab rule entirely) from lists where an
    empty value would silently disable detection and almost certainly means the
    secret was set by mistake.
    """
    raw = secrets.get(key)
    if raw is None:
        return default
    items = tuple(part.strip() for part in str(raw).split(",") if part.strip())
    if not items:
        if allow_empty:
            return ()
        log.warning(f"[glp1-care-gap-copilot] {key} is empty; using defaults")
        return default
    return items


@dataclass(frozen=True)
class Config:
    """Resolved plugin settings for a single card render."""

    enable_llm_rationale: bool
    anthropic_api_key: str
    llm_model: str
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
            enable_llm_rationale=_flag(secrets, "ENABLE_LLM_RATIONALE", True),
            anthropic_api_key=_text(secrets, "ANTHROPIC_API_KEY"),
            llm_model=_text(secrets, "LLM_MODEL", DEFAULT_LLM_MODEL),
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
                allow_empty=True,
            ),
            diabetes_icd10_prefixes=_csv(
                secrets, "DIABETES_ICD10_PREFIXES", DEFAULT_DIABETES_ICD10_PREFIXES
            ),
            outreach_team_dbid=_optional_int(secrets, "OUTREACH_TEAM_DBID"),
            lab_partner_name=_text(secrets, "LAB_PARTNER_NAME"),
            task_title_prefix=_text(secrets, "TASK_TITLE_PREFIX", DEFAULT_TASK_TITLE_PREFIX),
        )

    def llm_available(self) -> bool:
        """Whether the LLM rationale path is both enabled and configured."""
        return self.enable_llm_rationale and bool(self.anthropic_api_key)
