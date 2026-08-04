#!/usr/bin/env python3
"""Export the public Cookbook capability inventory for documentation builds."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CALIBER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CALIBER_ROOT.parent
sys.path.insert(0, str(CALIBER_ROOT / "src"))

from caliber.workflows.cookbook_catalog import build_cookbook_catalog  # noqa: E402

OUTPUT = REPO_ROOT / "docs-site" / "cookbooks" / "capabilities.json"


def main() -> None:
    catalog = build_cookbook_catalog()
    payload = {
        "schema_version": 1,
        "catalog_version": catalog["catalog_version"],
        "source": "caliber.workflows.cookbook_catalog",
        "recipes": [
            {
                key: recipe[key]
                for key in (
                    "id",
                    "slug",
                    "title",
                    "summary",
                    "capabilities",
                    "prerequisites",
                    "activation_requires_review",
                )
            }
            for recipe in catalog["recipes"]
        ],
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(OUTPUT)


if __name__ == "__main__":
    main()
