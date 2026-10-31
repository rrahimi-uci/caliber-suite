"""The conformance suites run on the step fixtures' fixed date, never the date the suite runs on (B08.3)."""

import pytest


@pytest.fixture(autouse=True)
def _conformance_runs_on_the_fixtures_clock(fixed_step_clock: None) -> None:
    del fixed_step_clock
