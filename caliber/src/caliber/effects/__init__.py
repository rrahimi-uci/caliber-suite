"""Governed external-effect services and evidence records.

The dispatcher exports are lazy because configuration imports effect helpers
while the SQLAlchemy model package imports configuration.  Eagerly importing
the service here would create a package-initialization cycle for every server
startup, including code paths that never dispatch an effect.
"""

__all__ = [
    "BACKENDS",
    "EffectOutcome",
    "EffectRefused",
    "UnsupportedEffectPath",
    "dispatch",
    "submit_proposal",
]


def __getattr__(name: str) -> object:
    if name in __all__:
        from caliber.effects import dispatcher  # noqa: PLC0415

        return getattr(dispatcher, name)
    raise AttributeError(name)
