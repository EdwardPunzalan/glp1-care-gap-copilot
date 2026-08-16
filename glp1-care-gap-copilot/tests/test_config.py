"""Secrets parsing — a malformed value must degrade, never raise."""

import json
import logging
import pathlib

import glp1_care_gap_copilot.config as config_module
from glp1_care_gap_copilot.config import (
    REDACTED,
    SENSITIVE_VARIABLES,
    DEFAULT_GLP1_MED_NAME_FRAGMENTS,
    DEFAULT_HYPOTHYROID_ICD10_PREFIXES,
    DEFAULT_TASK_TITLE_PREFIX,
    DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS,
    Config,
    _positive_int,
    _loggable,
)


def test_defaults_apply_when_no_secrets_are_set() -> None:
    config = Config.from_secrets({})

    assert config.weight_check_interval_days == DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS
    assert config.lab_interval_days == 90
    assert config.followup_horizon_days == 90
    assert config.glp1_med_name_fragments == DEFAULT_GLP1_MED_NAME_FRAGMENTS
    assert config.task_title_prefix == DEFAULT_TASK_TITLE_PREFIX
    assert config.outreach_team_dbid is None
    assert config.lab_partner_names == ()


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
    assert config.lab_partner_names == ("Quest",)
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
            "HYPOTHYROID_ICD10_PREFIXES": "",
        }
    )

    assert config.glp1_med_name_fragments == DEFAULT_GLP1_MED_NAME_FRAGMENTS
    # Blank cannot mean "cleared" — it is indistinguishable from never-set.
    assert config.hypothyroid_icd10_prefixes == DEFAULT_HYPOTHYROID_ICD10_PREFIXES


def test_sentinel_clears_a_clearable_list() -> None:
    # Clearing the thyroid prefixes is how a practice switches TSH off.
    config = Config.from_secrets({"HYPOTHYROID_ICD10_PREFIXES": "none"})

    assert config.hypothyroid_icd10_prefixes == ()


def test_sentinel_is_not_honored_on_non_clearable_lists() -> None:
    # "none" is a literal entry for lists that must never be empty.
    config = Config.from_secrets({"GLP1_MED_NAME_FRAGMENTS": "none"})

    assert config.glp1_med_name_fragments == ("none",)


def test_lab_test_order_codes_parses_a_map() -> None:
    config = Config.from_secrets(
        {"LAB_TEST_ORDER_CODES": "Hemoglobin A1c:496, comprehensive metabolic panel:10231"}
    )

    # Names are lowercased so lookup matches the configured lab names.
    assert config.lab_test_order_codes == {
        "hemoglobin a1c": "496",
        "comprehensive metabolic panel": "10231",
    }


def test_lab_test_order_codes_defaults_to_empty() -> None:
    assert Config.from_secrets({}).lab_test_order_codes == {}
    assert Config.from_secrets({"LAB_TEST_ORDER_CODES": ""}).lab_test_order_codes == {}


def test_malformed_code_map_entries_are_dropped_not_guessed() -> None:
    config = Config.from_secrets(
        {"LAB_TEST_ORDER_CODES": "lipid panel:7600,, no-colon-here, :991, trailing:"}
    )

    # Only the well-formed entry survives — an empty slot from a stray comma,
    # a missing separator, and a missing side are all dropped rather than guessed.
    assert config.lab_test_order_codes == {"lipid panel": "7600"}


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


def test_sensitive_values_are_masked_in_log_output(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # No variable is sensitive today, so exercise the mechanism directly: this is
    # what protects the next sensitive variable someone adds.
    monkeypatch.setattr(config_module, "SENSITIVE_VARIABLES", frozenset({"SOME_TOKEN"}))

    assert _loggable("SOME_TOKEN", "shh-secret-value") == REDACTED
    # Non-sensitive values stay visible — an operator needs them to fix a typo.
    assert _loggable("LAB_INTERVAL_DAYS", "ninety") == "'ninety'"


def test_a_malformed_sensitive_value_never_reaches_the_log(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(config_module, "SENSITIVE_VARIABLES", frozenset({"SOME_TOKEN"}))

    with caplog.at_level(logging.WARNING):
        _positive_int({"SOME_TOKEN": "shh-do-not-log"}, "SOME_TOKEN", 30)

    assert "shh-do-not-log" not in caplog.text
    assert REDACTED in caplog.text





def test_weight_trend_settings_have_documented_defaults() -> None:
    config = Config.from_secrets({})

    assert config.weight_trend_points == 6
    assert config.weight_drop_alert_lb == 5.0


def test_weight_trend_settings_are_configurable() -> None:
    config = Config.from_secrets(
        {"WEIGHT_TREND_POINTS": "10", "WEIGHT_DROP_ALERT_LB": "7.5"}
    )

    assert config.weight_trend_points == 10
    assert config.weight_drop_alert_lb == 7.5


def test_a_non_numeric_drop_threshold_falls_back_to_the_default(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING):
        config = Config.from_secrets({"WEIGHT_DROP_ALERT_LB": "ten pounds"})

    assert config.weight_drop_alert_lb == 5.0
    assert "is not a number" in caplog.text


def test_a_non_positive_drop_threshold_falls_back_to_the_default(caplog) -> None:  # type: ignore[no-untyped-def]
    # Zero would flag every interval, including a patient who gained.
    with caplog.at_level(logging.WARNING):
        config = Config.from_secrets({"WEIGHT_DROP_ALERT_LB": "0"})

    assert config.weight_drop_alert_lb == 5.0
    assert "must be positive" in caplog.text


def test_the_drop_window_defaults_to_a_week() -> None:
    # GLP-1s are dosed weekly, so a week is the natural comparison window.
    assert Config.from_secrets({}).weight_drop_max_interval_days == 7.0


def test_the_drop_window_is_configurable() -> None:
    config = Config.from_secrets({"WEIGHT_DROP_MAX_INTERVAL_DAYS": "14"})

    assert config.weight_drop_max_interval_days == 14.0


def test_a_malformed_drop_window_falls_back_to_the_default(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING):
        config = Config.from_secrets({"WEIGHT_DROP_MAX_INTERVAL_DAYS": "weekly"})

    assert config.weight_drop_max_interval_days == 7.0
    assert "is not a number" in caplog.text


def test_safety_settings_have_documented_defaults() -> None:
    config = Config.from_secrets({})

    assert config.safety_window_days == 30
    assert config.safety_min_findings == 1


def test_safety_settings_are_configurable() -> None:
    config = Config.from_secrets(
        {"SAFETY_WINDOW_DAYS": "90", "SAFETY_MIN_FINDINGS": "2"}
    )

    assert config.safety_window_days == 90
    assert config.safety_min_findings == 2


def test_a_non_positive_safety_window_falls_back_to_the_default(caplog) -> None:  # type: ignore[no-untyped-def]
    # A zero-day window would pair a drop only with same-day findings, which in
    # practice means never firing at all.
    with caplog.at_level(logging.WARNING):
        config = Config.from_secrets({"SAFETY_WINDOW_DAYS": "0"})

    assert config.safety_window_days == 30
    assert "must be positive" in caplog.text
