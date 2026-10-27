"""Context inputs that are pinned, hashed and authorized before a run uses them (B02/B07; ADR 0001-0003).

Today this package holds the rule-bundle loader (:mod:`caliber.context.rule_bundle`). The context manifest and the
source authorization that build on it arrive with B07; nothing here depends on them, on a run, or on a database.
"""
