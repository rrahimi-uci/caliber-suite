"""The collection guard that keeps a lone-surrogate parameter value off ``main`` (the Allure listener aborts the full run on one)."""

from __future__ import annotations

import pytest

from tests.conftest import lone_surrogate_in


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", False),
        ("emoji \U0001f600", False),
        ("", False),
        (7, False),
        (None, False),
        (["a", ("b", {"c": "d"})], False),
    ],
)
def test_ordinary_values_are_not_flagged(value: object, expected: bool) -> None:
    assert lone_surrogate_in(value) is expected


def test_a_lone_surrogate_is_flagged_alone_and_in_a_shallow_container() -> None:
    lone = "a" + chr(0xD800)
    assert lone_surrogate_in(lone) and lone_surrogate_in(chr(0xDFFF))
    assert (
        lone_surrogate_in([lone])
        and lone_surrogate_in(("x", {"k": lone}))
        and lone_surrogate_in({lone: 1})
    )
    assert not lone_surrogate_in(lone.encode("utf-8", "surrogatepass"))


def test_the_collection_hook_names_the_test_and_the_parameter() -> None:
    from types import SimpleNamespace

    from tests.conftest import pytest_collection_modifyitems

    def item(value: object) -> object:
        return SimpleNamespace(
            nodeid="tests/test_x.py::test_x[case]",
            callspec=SimpleNamespace(params={"value": value}),
            add_marker=lambda *_a, **_k: None,
            path=None,
        )

    pytest_collection_modifyitems(
        [item("fine"), SimpleNamespace(nodeid="n", path=None, add_marker=lambda *_a: None)]
    )
    with pytest.raises(
        pytest.UsageError, match=r"test_x\[case\]: parameter 'value' holds a lone surrogate"
    ):
        pytest_collection_modifyitems([item(chr(0xDFFF))])
