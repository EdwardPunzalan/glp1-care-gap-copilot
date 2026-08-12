"""Secrets parsing — a malformed value must degrade, never raise."""

import json
import logging
import pathlib

from glp1_care_gap_copilot.config import (
    REDACTED,
    SENSITIVE_VARIABLES,
    DEFAULT_DIABETES_ONLY_LAB_NAMES,
    DEFAULT_GLP1_MED_NAME_FRAGMENTS,
    DEFAULT_REQUIRED_LAB_NAMES,
    DEFAULT_LLM_MODEL,
    DEFAULT_TASK_TITLE_PREFIX,
    DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS,
    Config,
    _flag,
    _loggable,
)


def test_defaults_apply_when_no_secrets_are_set() -> None:
    config = Config.from_secrets({})

    assert config.weight_check_interval_days == DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS
    assert config.lab_interval_days == 90
    assert config.followup_horizon_days == 90
    assert config.glp1_med_name_fragments == DEFAULT_GLP1_MED_NAME_FRAGMENTS
    assert config.llm_model == DEFAULT_LLM_MODEL
    assert config.task_title_prefix == DEFAULT_TASK_TITLE_PREFIX
    assert config.outreach_team_dbid is None
    assert config.lab_partner_name == ""


def test_none_secrets_are_tolerated() -> None:
    assert Config.from_secrets(None).lab_interval_days == 90


def test_values_are_read_from_secrets() -> None:
    config = Config.from_secrets(
        {
            "WEIGHT_CHECK_INTERVAL_DAYS": "14",
            "LAB_INTERVAL_DAYS": " 45 ",
            "FOLLOWUP_HORIZON_DAYS": 60,
            "GLP1_MED_NAME_FRAGMENTS": "semaglutide, tirzepatide",
            "OBESITY_ICD10_PREFIXES": "E66",
            "OUTREACH_TEAM_DBID": "77",
            "LAB_PARTNER_NAME": "  Quest  ",
            "TASK_TITLE_PREFIX": "Copilot",
        }
    )

    assert config.weight_check_interval_days == 14
    assert config.lab_interval_days == 45
    assert config.followup_horizon_days == 60
    assert config.glp1_med_name_fragments == ("semaglutide", "tirzepatide")
    assert config.obesity_icd10_prefixes == ("E66",)
    assert config.outreach_team_dbid == 77
    assert config.lab_partner_name == "Quest"
    assert config.task_title_prefix == "Copilot"


def test_malformed_integers_fall_back_to_defaults() -> None:
    config = Config.from_secrets(
        {"WEIGHT_CHECK_INTERVAL_DAYS": "soon", "LAB_INTERVAL_DAYS": "-5"}
    )

    assert config.weight_check_interval_days == DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS
    assert config.lab_interval_days == 90


def test_zero_interval_falls_back_rather_than_flagging_everything() -> None:
    assert Config.from_secrets({"FOLLOWUP_HORIZON_DAYS": "0"}).followup_horizon_days == 90


def test_malformed_team_dbid_is_ignored() -> None:
    assert Config.from_secrets({"OUTREACH_TEAM_DBID": "team-a"}).outreach_team_dbid is None


def test_empty_list_secret_falls_back_to_default() -> None:
    config = Config.from_secrets({"GLP1_MED_NAME_FRAGMENTS": " , , "})

    assert config.glp1_med_name_fragments == DEFAULT_GLP1_MED_NAME_FRAGMENTS


def test_blank_declared_variable_uses_defaults_quietly() -> None:
    # A manifest-declared variable that was never configured arrives as "",
    # which is the ordinary default path — not a misconfiguration.
    config = Config.from_secrets(
        {
            "GLP1_MED_NAME_FRAGMENTS": "",
            "OBESITY_ICD10_PREFIXES": "",
            "REQUIRED_LAB_NAMES": "",
            "DIABETES_ONLY_LAB_NAMES": "",
        }
    )

    assert config.glp1_med_name_fragments == DEFAULT_GLP1_MED_NAME_FRAGMENTS
    assert config.required_lab_names == DEFAULT_REQUIRED_LAB_NAMES
    # Blank cannot mean "cleared" — it is indistinguishable from never-set.
    assert config.diabetes_only_lab_names == DEFAULT_DIABETES_ONLY_LAB_NAMES


def test_sentinel_clears_a_clearable_list() -> None:
    config = Config.from_secrets({"DIABETES_ONLY_LAB_NAMES": "none"})

    assert config.diabetes_only_lab_names == ()


def test_sentinel_is_not_honored_on_non_clearable_lists() -> None:
    # "none" is a literal entry for lists that must never be empty.
    config = Config.from_secrets({"GLP1_MED_NAME_FRAGMENTS": "none"})

    assert config.glp1_med_name_fragments == ("none",)


def test_every_sensitive_manifest_variable_is_redacted_in_logs() -> None:
    """Redaction must track the manifest, not the parser a variable happens to use."""
    manifest = json.loads(
        (
            pathlib.Path(__file__).parent.parent
            / "glp1_care_gap_copilot"
            / "CANVAS_MANIFEST.json"
        ).read_text()
    )
    declared_sensitive = {
        variable["name"] for variable in manifest["variables"] if variable.get("sensitive")
    }

    assert declared_sensitive <= SENSITIVE_VARIABLES, (
        "a variable marked sensitive in the manifest is not redacted in config logging"
    )


def test_sensitive_values_are_masked_in_log_output() -> None:
    assert _loggable("ANTHROPIC_API_KEY", "sk-secret-value") == REDACTED
    # Non-sensitive values stay visible — an operator needs them to fix a typo.
    assert _loggable("LAB_INTERVAL_DAYS", "ninety") == "'ninety'"


def test_a_malformed_sensitive_value_never_reaches_the_log(caplog) -> None:  # type: ignore[no-untyped-def]
    # ANTHROPIC_API_KEY routes through _text() today, which never logs. Guard the
    # case where a future sensitive variable is read through a logging parser.
    with caplog.at_level(logging.WARNING):
        _flag({"ANTHROPIC_API_KEY": "sk-live-do-not-log"}, "ANTHROPIC_API_KEY", True)

    assert "sk-live-do-not-log" not in caplog.text
    assert REDACTED in caplog.text


def test_llm_kill_switch_accepts_common_spellings() -> None:
    assert Config.from_secrets({"ENABLE_LLM_RATIONALE": "false"}).enable_llm_rationale is False
    assert Config.from_secrets({"ENABLE_LLM_RATIONALE": "0"}).enable_llm_rationale is False
    assert Config.from_secrets({"ENABLE_LLM_RATIONALE": "TRUE"}).enable_llm_rationale is True
    assert Config.from_secrets({"ENABLE_LLM_RATIONALE": True}).enable_llm_rationale is True
    # Unparseable values keep the documented default rather than silently disabling.
    assert Config.from_secrets({"ENABLE_LLM_RATIONALE": "maybe"}).enable_llm_rationale is True


def test_llm_is_unavailable_without_a_key() -> None:
    assert Config.from_secrets({}).llm_available() is False
    assert Config.from_secrets({"ANTHROPIC_API_KEY": "sk-test"}).llm_available() is True
    assert (
        Config.from_secrets(
            {"ANTHROPIC_API_KEY": "sk-test", "ENABLE_LLM_RATIONALE": "false"}
        ).llm_available()
        is False
    )
