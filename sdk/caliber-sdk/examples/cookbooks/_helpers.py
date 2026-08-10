"""Shared helpers for the runnable SDK cookbook examples."""

from __future__ import annotations

import os
from typing import Any

from caliber_sdk import CaliberClient


def get_recipe(caliber: CaliberClient, cookbook_id: str) -> Any:
    return {item.id: item for item in caliber.cookbooks.list()}[cookbook_id]


def configuration_blockers(recipe: Any) -> list[str]:
    return [
        str(check.get("label"))
        for check in recipe.unmet_checks
        if check.get("status") == "configuration_required"
    ]


def env_client() -> CaliberClient:
    return CaliberClient(
        os.environ["CALIBER_BASE_URL"],
        token=os.environ["CALIBER_TOKEN"],
    )
