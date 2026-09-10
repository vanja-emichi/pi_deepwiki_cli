"""Shared pytest fixture: a CliRunner.

click 8.2+ always separates streams: `result.stdout` is provably clean,
`result.stderr` holds diagnostics, `result.output` is the combined view.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()
