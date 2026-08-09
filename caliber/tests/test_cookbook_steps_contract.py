"""The catalog's guided steps stay in step with the cookbook READMEs.

The steps in ``cookbook_catalog._RECIPE_STEPS`` are the addressable form of the
recipes written in ``docs-site/cookbooks/<slug>/README.md``. Two copies of the
same procedure drift: a README gains a step, the checklist does not, and the
in-app guide quietly stops matching the documentation it was derived from.

These tests pin the count against the README's numbered list, and pin every
route to one the SPA actually registers -- a checklist that navigates nowhere is
worse than no checklist, because it looks like a working affordance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from caliber.workflows.cookbook_catalog import (
    RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION,
    build_cookbook_catalog,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COOKBOOKS = REPO_ROOT / "docs-site" / "cookbooks"
APP_TSX = REPO_ROOT / "caliber" / "caliber-ui" / "src" / "App.tsx"

#: ``1. **Title.** body`` at the top level of a ``## Recipe`` section.
NUMBERED_STEP = re.compile(r"^\d+\. ", flags=re.MULTILINE)


def _recipes() -> list[dict]:
    return build_cookbook_catalog()["recipes"]


def _readme_for(recipe: dict) -> Path:
    """The cookbook folder is ``<id>-<some-slug>``, not ``<id>-<recipe slug>``."""
    matches = sorted(COOKBOOKS.glob(f"{recipe['id']}-*"))
    assert len(matches) == 1, f"expected exactly one folder for cookbook {recipe['id']}: {matches}"
    return matches[0] / "README.md"


def _numbered_steps_in_readme(readme: Path) -> list[str]:
    text = readme.read_text(encoding="utf-8")
    if "\n## Recipe" not in text:
        return []
    body = text.split("\n## Recipe", 1)[1]
    # Stop at the next top-level heading so "Demo evidence" lists are excluded.
    body = re.split(r"\n## ", body, maxsplit=1)[0]
    return NUMBERED_STEP.findall(body)


def test_every_recipe_has_steps() -> None:
    recipes = _recipes()
    assert len(recipes) == 16
    for recipe in recipes:
        assert recipe["steps"], f"cookbook {recipe['id']} has no guided steps"


def test_step_ids_are_positional_and_unique() -> None:
    """Ids are derived from position, so stored progress cannot be reassigned."""
    for recipe in _recipes():
        expected = [f"{recipe['id']}.{n}" for n in range(1, len(recipe["steps"]) + 1)]
        assert [step["id"] for step in recipe["steps"]] == expected


@pytest.mark.parametrize("recipe", _recipes(), ids=lambda item: item["id"])
def test_step_count_matches_the_readme(recipe: dict) -> None:
    """One catalog step per numbered README step.

    The count, not the wording: the README carries field values and API
    fallbacks that deliberately do not appear in a checklist.
    """
    readme = _readme_for(recipe)
    numbered = _numbered_steps_in_readme(readme)

    if recipe["id"] in RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION:
        assert not numbered, (
            f"cookbook {recipe['id']} is listed as having no '## Recipe' section, but its "
            f"README now defines {len(numbered)} numbered steps -- remove it from "
            "RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION and pin its real count"
        )
        return

    assert numbered, f"{readme.relative_to(REPO_ROOT)} has no numbered '## Recipe' steps"
    assert len(recipe["steps"]) == len(numbered), (
        f"cookbook {recipe['id']}: README defines {len(numbered)} steps, "
        f"the catalog defines {len(recipe['steps'])}"
    )


def test_every_step_route_is_one_the_spa_registers() -> None:
    """A step's route must resolve, or the checklist navigates into a 404."""
    registered = set(re.findall(r'path="([^"]+)"', APP_TSX.read_text(encoding="utf-8")))
    assert registered, "parsed no routes out of App.tsx; the assertion below would be vacuous"

    used = {step["route"] for recipe in _recipes() for step in recipe["steps"]}
    assert used, "no step declared a route"
    assert used <= registered, (
        f"steps route to paths the SPA does not register: {sorted(used - registered)}"
    )


def test_the_exempt_list_names_only_real_recipes() -> None:
    """A stale exemption would silently disable the count check for a recipe."""
    known = {recipe["id"] for recipe in _recipes()}
    assert known >= RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION, (
        f"exempt ids that are not recipes: "
        f"{sorted(RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION - known)}"
    )
