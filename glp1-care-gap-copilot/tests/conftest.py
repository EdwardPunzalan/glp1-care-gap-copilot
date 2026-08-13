"""Shared test setup.

`render_to_string` refuses to run outside a plugin: it walks back up the call
stack for a frame whose module globals carry `__is_plugin__`, then resolves
templates under `PLUGIN_DIRECTORY / <plugin name>`. The plugin runner injects
that marker at import time, so tests have to stand it up themselves — otherwise
every template-rendering test dies with "called from outside a plugin" and the
only way to cover the section is to mock the renderer, which would stop the
template itself from ever being exercised.
"""

from pathlib import Path
from typing import Iterator

import pytest

import canvas_sdk.utils.plugins as plugin_utils
from glp1_care_gap_copilot.handlers import weight_trend_handler

# `PLUGIN_DIRECTORY / glp1_care_gap_copilot / templates/...` has to land on the
# real template, so the directory is the repo root that holds the inner package.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def plugin_context(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make handler modules look like installed plugin code to the SDK."""
    monkeypatch.setattr(plugin_utils, "PLUGIN_DIRECTORY", str(PLUGIN_ROOT))
    monkeypatch.setitem(weight_trend_handler.__dict__, "__is_plugin__", True)
    yield
