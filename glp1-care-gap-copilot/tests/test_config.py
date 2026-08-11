"""Secrets parsing — a malformed value must degrade, never raise."""

from glp1_care_gap_copilot.config import (
    DEFAULT_GLP1_MED_NAME_FRAGMENTS,
    DEFAULT_LLM_MODEL,
    DEFAULT_TASK_TITLE_PREFIX,
    DEFAULT_WEIGHT_CHECK_INTERVAL_DAYS,
    Config,
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
