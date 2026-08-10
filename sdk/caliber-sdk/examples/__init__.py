"""Executable examples for the CALIBER SDK.

Every example is a function taking a configured :class:`CaliberClient`, not a
script that builds its own. That is what makes them testable: ``tests/
test_examples.py`` runs each one against a stub server, so a snippet published
in the docs cannot drift from working code.

Run one against a real deployment:

    from caliber_sdk import CaliberClient
    from examples.quickstart import quickstart

    with CaliberClient("https://caliber.example.com", token="calpat_...") as c:
        quickstart(c)
"""
