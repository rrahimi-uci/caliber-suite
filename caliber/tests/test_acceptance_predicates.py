"""B09.1 (#779): acceptance predicates are pure, deterministic judgements over frozen observations.

The Harborlight set accepts the happy path, rejects a false success (a downstream that answered but left the case at the
version the effect was proposed against) and fails closed on anything missing or malformed. The predicate contract itself
(ids, rule ids, one criteria version, a result that is the predicate's own) is checked at the door.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from caliber.acceptance import (
    CRITERIA_VERSION,
    HARBORLIGHT_V1,
    CriterionResult,
    Observations,
    Predicate,
    PredicateError,
    evaluate_all,
)
from caliber.acceptance import predicates as predicates_module

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "acceptance_observations.json").read_text("utf-8")
)
SOURCE_DIR = Path(predicates_module.__file__).parent
EXISTS = "document_request_exists"
ATTACHED = "attached_to_expected_version"


def _fixture(name: str) -> dict[str, Any]:
    return copy.deepcopy(FIXTURES[name])


def _observations(name: str = "happy_path", **edits: Any) -> Observations:
    data = _fixture(name)
    for dotted, value in edits.items():
        *path, leaf = dotted.split("__")
        node = data
        for part in path:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if value is _MISSING:
            del node[leaf]
        elif isinstance(node, list):
            node[int(leaf)] = value
        else:
            node[leaf] = value
    return Observations(data)


_MISSING = object()


def _verdicts(observations: Observations) -> dict[str, tuple[bool, str]]:
    return {
        r.predicate_id: (r.passed, r.evidence_ref)
        for r in evaluate_all(HARBORLIGHT_V1, observations)
    }


def _ok(predicate_id: str = "p1", version: str = "v1") -> Predicate:
    return Predicate(
        id=predicate_id,
        criteria_version=version,
        rule_ids=["rule-one"],
        evaluate=lambda _o: CriterionResult(predicate_id, True, "/x"),
    )


# --- the oracle ------------------------------------------------------------------------------------------------------


def test_pure_and_deterministic() -> None:
    observations = _observations()
    snapshot = observations.as_dict()
    first = evaluate_all(HARBORLIGHT_V1, observations)
    second = evaluate_all(list(HARBORLIGHT_V1), Observations(_fixture("happy_path")))
    assert first == second
    assert observations.as_dict() == snapshot  # judging changes nothing
    assert [(r.predicate_id, r.passed) for r in first] == [(EXISTS, True), (ATTACHED, True)]


def test_happy_path_passes_both_with_pointers_to_the_deciding_request() -> None:
    assert _verdicts(_observations()) == {
        EXISTS: (True, "/case_record/document_requests/0"),
        ATTACHED: (True, "/case_record/document_requests/0/case_version"),
    }


def test_stale_case_version_is_a_rejected_false_success() -> None:
    """The request is listed (the downstream answered) but the case never advanced: only the version criterion fails."""
    verdicts = _verdicts(_observations("stale_case_version"))
    assert verdicts[EXISTS] == (True, "/case_record/document_requests/0")
    assert verdicts[ATTACHED] == (False, "/case_record/document_requests/0/case_version")
    assert not all(passed for passed, _ in verdicts.values())


def test_no_request_and_no_receipt_fails_both() -> None:
    assert _verdicts(_observations("no_request")) == {
        EXISTS: (False, "/receipt/downstream_request_id"),
        ATTACHED: (False, "/receipt/downstream_request_id"),
    }


def test_a_receipt_for_a_request_the_record_does_not_list_fails_both() -> None:
    """The false success: a receipt names a request, the case record has none."""
    observations = _observations("no_request", receipt={"downstream_request_id": "DR-0001"})
    assert _verdicts(observations) == {
        EXISTS: (False, "/case_record/document_requests"),
        ATTACHED: (False, "/case_record/document_requests"),
    }


def test_the_set_is_one_criteria_version_with_cited_rules() -> None:
    assert [p.id for p in HARBORLIGHT_V1] == [EXISTS, ATTACHED]
    assert {p.criteria_version for p in HARBORLIGHT_V1} == {CRITERIA_VERSION}
    assert all(p.rule_ids and isinstance(p.rule_ids, tuple) for p in HARBORLIGHT_V1)
    assert len({r for p in HARBORLIGHT_V1 for r in p.rule_ids}) == 2


# --- exact boundaries --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "stored", "passed"),
    [
        (1, 2, True),  # the version the effect produces
        (1, 1, False),  # the downstream did not advance the case
        (1, 3, False),  # something else moved it too
        (1, 0, False),
        (0, 1, True),  # lowest expected version
        (0, 0, False),
        (9999999999, 10000000000, True),  # no overflow in the comparison
    ],
)
def test_attached_boundary(expected: int, stored: int, passed: bool) -> None:
    observations = _observations(
        proposal__expected_version=expected, case_record__document_requests__0__case_version=stored
    )
    assert _verdicts(observations)[ATTACHED][0] is passed


LISTED = [
    {"request_id": "DR-0000", "document_type": "id_card", "case_version": 2},
    {"request_id": "DR-0001", "document_type": "pay_statement", "case_version": 1},
    {"request_id": "DR-0002", "document_type": "pay_statement", "case_version": 2},
]


def test_without_a_receipt_nothing_is_accepted() -> None:
    data = _fixture("happy_path")
    del data["receipt"]
    data["case_record"]["document_requests"] = copy.deepcopy(LISTED)
    assert _verdicts(Observations(data)) == {
        EXISTS: (False, "/receipt/downstream_request_id"),
        ATTACHED: (False, "/receipt/downstream_request_id"),
    }


@pytest.mark.parametrize(
    ("receipt_id", "exists", "attached"),
    [
        # DR-0002 is the committed request, at the version the effect produces
        (
            "DR-0002",
            (True, "/case_record/document_requests/2"),
            (True, "/case_record/document_requests/2/case_version"),
        ),
        # DR-0001 is another proposal's request: it exists, but not at expected + 1
        (
            "DR-0001",
            (True, "/case_record/document_requests/1"),
            (False, "/case_record/document_requests/1/case_version"),
        ),
        # a request of another type is never the effect, even when the receipt names it
        (
            "DR-0000",
            (False, "/case_record/document_requests"),
            (False, "/case_record/document_requests"),
        ),
        # a receipt that names a request the record does not list is a false success
        (
            "DR-9999",
            (False, "/case_record/document_requests"),
            (False, "/case_record/document_requests"),
        ),
    ],
)
def test_a_receipt_pins_the_request_that_is_judged(
    receipt_id: str, exists: tuple[bool, str], attached: tuple[bool, str]
) -> None:
    data = _fixture("happy_path")
    data["receipt"] = {"downstream_request_id": receipt_id}
    data["case_record"]["document_requests"] = copy.deepcopy(LISTED)
    assert _verdicts(Observations(data)) == {EXISTS: exists, ATTACHED: attached}


def test_a_refused_second_proposal_is_not_accepted_on_the_first_ones_request() -> None:
    """Both proposals ask for a pay statement; the first committed (version 2), the second was refused as stale."""
    first_only = _fixture("happy_path")
    second = copy.deepcopy(first_only)
    assert all(passed for passed, _ in _verdicts(Observations(first_only)).values())
    # the refused proposal has no receipt: the first one's request must not be enough
    del second["receipt"]
    assert _verdicts(Observations(second)) == {
        EXISTS: (False, "/receipt/downstream_request_id"),
        ATTACHED: (False, "/receipt/downstream_request_id"),
    }
    # nor a receipt for a request the record never listed
    second["receipt"] = {"downstream_request_id": "DR-0002"}
    assert [passed for passed, _ in _verdicts(Observations(second)).values()] == [False, False]


@pytest.mark.parametrize(
    "receipt",
    [
        _MISSING,
        None,
        {},
        {"downstream_request_id": ""},
        {"downstream_request_id": None},
        {"downstream_request_id": 1},
        [],
        "DR-0001",
    ],
)
def test_a_receipt_without_a_usable_request_id_fails_both(receipt: object) -> None:
    observations = _observations(receipt=receipt)
    assert _verdicts(observations) == {
        EXISTS: (False, "/receipt/downstream_request_id"),
        ATTACHED: (False, "/receipt/downstream_request_id"),
    }


def test_attached_ignores_a_request_of_another_type_at_the_right_version() -> None:
    observations = _observations(case_record__document_requests__0__document_type="id_card")
    assert _verdicts(observations) == {
        EXISTS: (False, "/case_record/document_requests"),
        ATTACHED: (False, "/case_record/document_requests"),
    }


def test_document_type_matches_exactly() -> None:
    for stored in ("Pay_Statement", "pay_statement ", " pay_statement", "pay_statement2", ""):
        observations = _observations(case_record__document_requests__0__document_type=stored)
        assert _verdicts(observations)[EXISTS][0] is False, stored


# --- fail closed on missing and malformed observations ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "exists_ref", "attached_ref"),
    [
        (
            {"proposal__document_type": _MISSING},
            "/proposal/document_type",
            "/proposal/document_type",
        ),
        ({"proposal__document_type": ""}, "/proposal/document_type", "/proposal/document_type"),
        ({"proposal__document_type": 7}, "/proposal/document_type", "/proposal/document_type"),
        ({"proposal__document_type": None}, "/proposal/document_type", "/proposal/document_type"),
        ({"proposal__expected_version": _MISSING}, None, "/proposal/expected_version"),
        ({"proposal__expected_version": None}, None, "/proposal/expected_version"),
        ({"proposal__expected_version": "1"}, None, "/proposal/expected_version"),
        ({"proposal__expected_version": 1.0}, None, "/proposal/expected_version"),
        ({"proposal__expected_version": True}, None, "/proposal/expected_version"),
        ({"proposal__expected_version": -1}, None, "/proposal/expected_version"),
        (
            {"case_record__document_requests": _MISSING},
            "/case_record/document_requests",
            "/case_record/document_requests",
        ),
        (
            {"case_record__document_requests": {"0": {}}},
            "/case_record/document_requests",
            "/case_record/document_requests",
        ),
        (
            {"case_record__document_requests": "pay_statement"},
            "/case_record/document_requests",
            "/case_record/document_requests",
        ),
        (
            {"case_record__document_requests": None},
            "/case_record/document_requests",
            "/case_record/document_requests",
        ),
    ],
)
def test_missing_or_malformed_values_fail_closed(
    edit: dict[str, Any], exists_ref: str | None, attached_ref: str
) -> None:
    verdicts = _verdicts(_observations(**edit))
    if exists_ref is None:  # a version problem does not make the request any less listed
        assert verdicts[EXISTS] == (True, "/case_record/document_requests/0")
    else:
        assert verdicts[EXISTS] == (False, exists_ref)
    assert verdicts[ATTACHED] == (False, attached_ref)


@pytest.mark.parametrize("stored", [None, "2", 2.0, True, [2], {"v": 2}])
def test_a_stored_version_that_is_not_an_integer_never_matches(stored: object) -> None:
    observations = _observations(case_record__document_requests__0__case_version=stored)
    assert _verdicts(observations)[ATTACHED] == (
        False,
        "/case_record/document_requests/0/case_version",
    )


def test_a_boolean_never_stands_in_for_the_version_one() -> None:
    observations = _observations(
        proposal__expected_version=0, case_record__document_requests__0__case_version=True
    )
    assert _verdicts(observations)[ATTACHED][0] is False


def test_missing_stored_version_fails_closed() -> None:
    observations = _observations(case_record__document_requests__0__case_version=_MISSING)
    assert _verdicts(observations)[ATTACHED] == (
        False,
        "/case_record/document_requests/0/case_version",
    )


@pytest.mark.parametrize("entry", [None, 5, "pay_statement", ["pay_statement"]])
def test_a_request_entry_that_is_not_a_mapping_is_not_a_request(entry: object) -> None:
    data = _fixture("happy_path")
    data["case_record"]["document_requests"] = [entry]
    assert _verdicts(Observations(data)) == {
        EXISTS: (False, "/case_record/document_requests"),
        ATTACHED: (False, "/case_record/document_requests"),
    }


@pytest.mark.parametrize("section", ["proposal", "case_record"])
@pytest.mark.parametrize("value", [None, [], "x", 3])
def test_a_missing_or_non_mapping_section_fails_both(section: str, value: object) -> None:
    data = _fixture("happy_path")
    data[section] = value
    verdicts = _verdicts(Observations(data))
    assert [passed for passed, _ in verdicts.values()] == [False, False]
    del data[section]
    assert [passed for passed, _ in _verdicts(Observations(data)).values()] == [False, False]


def test_an_empty_observation_set_fails_both_without_raising() -> None:
    assert [r.passed for r in evaluate_all(HARBORLIGHT_V1, Observations({}))] == [False, False]


def test_a_json_pointer_escapes_its_reserved_characters() -> None:
    data = _fixture("happy_path")
    data["a/b~c"] = {"d": float("1.5")}
    assert Observations(data).as_dict()["a/b~c"] == {"d": 1.5}
    with pytest.raises(PredicateError, match=r"/a~1b~0c/d is not a finite number"):
        Observations({"a/b~c": {"d": float("nan")}})


# --- the proposal's case, and ambiguity -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        {"proposal__case_id": "MORT-2026-000185"},  # a record of another case
        {"proposal__case_id": _MISSING},
        {"proposal__case_id": ""},
        {"proposal__case_id": "", "case_record__case_id": ""},  # equal, but not a case
        {"proposal__case_id": None},
        {"proposal__case_id": 184},
        {"case_record__case_id": _MISSING},
        {"case_record__case_id": ""},
        {"case_record__case_id": None},
        {"case_record__case_id": "mort-2026-000184"},  # case ids compare exactly
        {"case_record__case_id": "MORT-2026-000184 "},
    ],
)
def test_a_record_of_another_or_an_unnamed_case_fails_both(edit: dict[str, Any]) -> None:
    assert _verdicts(_observations(**edit)) == {
        EXISTS: (False, "/case_record/case_id"),
        ATTACHED: (False, "/case_record/case_id"),
    }


def test_the_same_case_id_on_both_sides_is_judged() -> None:
    other = _observations(proposal__case_id="OTHER", case_record__case_id="OTHER")
    assert all(passed for passed, _ in _verdicts(other).values())


def test_a_case_record_that_is_not_a_mapping_has_no_case_id() -> None:
    data = _fixture("happy_path")
    data["case_record"] = ["MORT-2026-000184"]
    assert [ref for _, ref in _verdicts(Observations(data)).values()] == [
        "/case_record/case_id"
    ] * 2


def test_a_request_id_listed_twice_is_ambiguous_and_fails_closed() -> None:
    data = _fixture("happy_path")
    data["case_record"]["document_requests"] = [
        {"request_id": "DR-0001", "document_type": "pay_statement", "case_version": 2},
        {"request_id": "DR-0001", "document_type": "pay_statement", "case_version": 7},
    ]
    assert _verdicts(Observations(data)) == {
        EXISTS: (False, "/case_record/document_requests/1/request_id"),
        ATTACHED: (False, "/case_record/document_requests/1/request_id"),
    }
    # the duplicate is ambiguous whatever it is a duplicate of, even another type's entry
    data["case_record"]["document_requests"][1]["document_type"] = "id_card"
    assert [passed for passed, _ in _verdicts(Observations(data)).values()] == [False, False]


def test_entries_without_a_usable_request_id_are_not_duplicates() -> None:
    data = _fixture("happy_path")
    data["case_record"]["document_requests"] += [
        {"document_type": "id_card", "case_version": 3},
        {"request_id": "", "document_type": "id_card", "case_version": 3},
        {"request_id": "", "document_type": "id_card", "case_version": 3},
        {"request_id": 5, "document_type": "id_card", "case_version": 3},
        {"request_id": 5, "document_type": "id_card", "case_version": 3},
        "not a request",
    ]
    assert all(passed for passed, _ in _verdicts(Observations(data)).values())


def test_the_receipts_request_without_a_document_type_is_not_a_match() -> None:
    data = _fixture("happy_path")
    del data["case_record"]["document_requests"][0]["document_type"]
    assert _verdicts(Observations(data)) == {
        EXISTS: (False, "/case_record/document_requests"),
        ATTACHED: (False, "/case_record/document_requests"),
    }


# --- observations are frozen --------------------------------------------------------------------------------------------


def test_observations_are_frozen_all_the_way_down() -> None:
    observations = _observations()
    with pytest.raises(TypeError):
        observations["extra"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        observations["proposal"]["expected_version"] = 9  # type: ignore[index]
    requests = observations["case_record"]["document_requests"]
    assert isinstance(requests, tuple)
    with pytest.raises(TypeError):
        requests[0]["case_version"] = 9
    with pytest.raises(AttributeError):
        observations._data = {}  # type: ignore[misc]
    with pytest.raises(AttributeError):
        observations.other = 1  # type: ignore[attr-defined]


def test_the_source_is_copied_not_aliased() -> None:
    data = _fixture("happy_path")
    observations = Observations(data)
    data["proposal"]["expected_version"] = 99
    data["case_record"]["document_requests"].clear()
    assert observations == Observations(_fixture("happy_path"))
    assert _verdicts(observations)[ATTACHED][0] is True


def test_as_dict_is_a_plain_independent_copy() -> None:
    observations = _observations()
    copied = observations.as_dict()
    assert copied == _fixture("happy_path")
    assert type(copied["proposal"]) is dict
    assert type(copied["case_record"]["document_requests"]) is list
    copied["proposal"]["expected_version"] = 99
    assert observations["proposal"]["expected_version"] == 1
    assert json.dumps(copied, sort_keys=True)  # JSON-serialisable


def test_mapping_protocol() -> None:
    observations = _observations()
    assert set(observations) == {"proposal", "receipt", "case_record"}
    assert len(observations) == 3
    assert "proposal" in observations
    assert observations != _observations("stale_case_version")


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ({"a": object()}, "/a is a object"),
        ({"a": {1, 2}}, "/a is a set"),
        ({"a": b"x"}, "/a is a bytes"),
        ({"a": float("inf")}, "/a is not a finite number"),
        ({"a": float("-inf")}, "/a is not a finite number"),
        ({"a": [1, {"b": float("nan")}]}, "/a/1/b is not a finite number"),
        ({1: "x"}, "has a key that is not a string"),
        ({"a": {2: "x"}}, "/a has a key that is not a string"),
    ],
)
def test_observations_that_are_not_json_are_refused(value: dict[Any, Any], text: str) -> None:
    with pytest.raises(PredicateError, match=text) as caught:
        Observations(value)
    assert caught.value.code == "invalid_observations"


@pytest.mark.parametrize("value", [None, [], "x", 3, [("a", 1)]])
def test_observations_need_a_mapping(value: object) -> None:
    with pytest.raises(PredicateError) as caught:
        Observations(value)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_observations"


def _nested(levels: int) -> dict[str, Any]:
    """A mapping nested ``levels`` deep (the top mapping is level 1) with a scalar in the innermost one."""
    node: dict[str, Any] = {"leaf": 1}
    for _ in range(levels - 1):
        node = {"n": node}
    return node


def test_depth_is_bounded_exactly_and_a_cycle_is_refused() -> None:
    limit = predicates_module.MAX_DEPTH
    assert limit == 32
    assert Observations(_nested(limit))  # 32 levels of mappings: accepted
    with pytest.raises(PredicateError, match="nested deeper than 32 levels"):
        Observations(_nested(limit + 1))
    with pytest.raises(PredicateError, match="nested deeper"):
        Observations({"a": _nested(limit)})  # the same structure one level lower
    as_lists: Any = [1]
    for _ in range(limit - 2):
        as_lists = [as_lists]
    assert Observations({"a": as_lists})  # lists count as levels too: 1 + (limit - 1) levels
    with pytest.raises(PredicateError, match="nested deeper"):
        Observations({"a": [as_lists]})
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(PredicateError, match="nested deeper"):
        Observations(cyclic)


def test_nested_sequences_are_frozen_to_tuples() -> None:
    observations = Observations({"a": [[1, 2], (3,), []], "b": ((4,),)})
    assert observations["a"] == ((1, 2), (3,), ())
    assert observations["b"] == ((4,),)
    assert observations.as_dict() == {"a": [[1, 2], [3], []], "b": [[4]]}


def test_a_str_or_int_subclass_cannot_spoof_a_comparison() -> None:
    class Always(str):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = str.__hash__

    class AlwaysInt(int):
        def __eq__(self, other: object) -> bool:
            return True

        __hash__ = int.__hash__

    for hostile in (Always("anything"), AlwaysInt(2)):
        with pytest.raises(PredicateError, match="not JSON-shaped") as caught:
            Observations({"a": [hostile]})
        assert caught.value.code == "invalid_observations"
    with pytest.raises(PredicateError, match="key that is not a string"):
        Observations({Always("proposal"): 1})
    data = _fixture("happy_path")
    data["case_record"]["document_requests"][0]["document_type"] = Always("pay_statement")
    with pytest.raises(PredicateError):
        Observations(data)


def test_a_hostile_container_becomes_a_predicate_error() -> None:
    class Boom(dict):  # type: ignore[type-arg]
        def items(self) -> Any:
            raise RuntimeError("secret detail 0xBEEF")

    class BoomList(list):  # type: ignore[type-arg]
        def __iter__(self) -> Any:
            raise RuntimeError("secret detail 0xBEEF")

    for hostile in (Boom(a=1), BoomList([1])):
        with pytest.raises(PredicateError, match="could not be read") as caught:
            Observations({"a": hostile})
        assert caught.value.code == "invalid_observations"
        assert "0xBEEF" not in str(caught.value)
        assert isinstance(caught.value.__cause__, RuntimeError)
    with pytest.raises(PredicateError, match="could not be read"):
        Observations(Boom(a=1))


def test_an_error_message_names_the_path_but_never_quotes_a_value() -> None:
    secret = "do-not-print-me-0xBEEF"
    with pytest.raises(PredicateError) as caught:
        Observations({"k": {"v": [secret, float("nan")]}})
    assert "/k/v/1" in str(caught.value)
    assert secret not in str(caught.value)
    with pytest.raises(PredicateError) as caught:
        Observations({"k": {"v": object(), "w": secret}})
    assert secret not in str(caught.value)


# --- the predicate contract ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "Upper", "-lead", "has space", "x" * 129, 3, None])
def test_predicate_id_is_validated(bad: object) -> None:
    with pytest.raises(PredicateError) as caught:
        Predicate(id=bad, criteria_version="v1", rule_ids=["r1"], evaluate=lambda o: None)  # type: ignore[arg-type,return-value]
    assert caught.value.code == "invalid_predicate"


def test_predicate_id_length_boundary() -> None:
    assert _ok("a" * 128).id == "a" * 128


@pytest.mark.parametrize("bad", ["", "V1", "x" * 65, ".v1", 1, None])
def test_criteria_version_is_validated(bad: object) -> None:
    with pytest.raises(PredicateError) as caught:
        Predicate(id="p1", criteria_version=bad, rule_ids=["r1"], evaluate=lambda o: None)  # type: ignore[arg-type,return-value]
    assert caught.value.code == "invalid_predicate"


def test_criteria_version_length_boundary() -> None:
    assert _ok(version="v" * 64).criteria_version == "v" * 64


@pytest.mark.parametrize(
    "bad",
    [
        [],
        (),
        "r1",
        None,
        5,
        {"r1"},
        [""],
        ["Bad Rule"],
        [1],
        ["r1", "r1"],
        ["r1", None],
        ["x" * 129],
    ],
)
def test_rule_ids_are_validated(bad: object) -> None:
    with pytest.raises(PredicateError) as caught:
        Predicate(id="p1", criteria_version="v1", rule_ids=bad, evaluate=lambda o: None)  # type: ignore[arg-type,return-value]
    assert caught.value.code == "invalid_predicate"


def test_evaluate_must_be_callable() -> None:
    with pytest.raises(PredicateError) as caught:
        Predicate(id="p1", criteria_version="v1", rule_ids=["r1"], evaluate="nope")  # type: ignore[arg-type]
    assert caught.value.code == "invalid_predicate"


def test_rule_ids_given_as_a_list_are_stored_immutably() -> None:
    rules = ["r1", "r2"]
    predicate = Predicate(id="p1", criteria_version="v1", rule_ids=rules, evaluate=lambda o: None)  # type: ignore[arg-type,return-value]
    rules.append("r3")
    assert predicate.rule_ids == ("r1", "r2")
    with pytest.raises(dataclasses.FrozenInstanceError):
        predicate.id = "p2"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("args", "text"),
    [
        (("", True, "/x"), "predicate_id"),
        (("Bad Id", True, "/x"), "predicate_id"),
        ((3, True, "/x"), "predicate_id"),
        (("p1", 1, "/x"), "passed must be a bool"),
        (("p1", None, "/x"), "passed must be a bool"),
        (("p1", "yes", "/x"), "passed must be a bool"),
        (("p1", True, ""), "JSON pointer"),
        (("p1", True, "x"), "JSON pointer"),
        (("p1", True, None), "JSON pointer"),
    ],
)
def test_criterion_result_is_validated(args: tuple[Any, ...], text: str) -> None:
    with pytest.raises(PredicateError, match=text) as caught:
        CriterionResult(*args)
    assert caught.value.code == "invalid_result"


def test_criterion_result_is_frozen_and_comparable() -> None:
    result = CriterionResult("p1", False, "/a")
    assert result == CriterionResult("p1", False, "/a")
    assert result != CriterionResult("p1", True, "/a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = True  # type: ignore[misc]


# --- evaluate_all -------------------------------------------------------------------------------------------------------


def test_evaluate_all_keeps_order_and_does_not_stop_at_a_failure() -> None:
    seen: list[str] = []

    def make(pid: str, passed: bool) -> Predicate:
        def evaluate(_o: Observations) -> CriterionResult:
            seen.append(pid)
            return CriterionResult(pid, passed, "/x")

        return Predicate(id=pid, criteria_version="v1", rule_ids=["r1"], evaluate=evaluate)

    results = evaluate_all([make("c", False), make("a", True), make("b", False)], Observations({}))
    assert [(r.predicate_id, r.passed) for r in results] == [
        ("c", False),
        ("a", True),
        ("b", False),
    ]
    assert seen == ["c", "a", "b"]


def test_evaluate_all_accepts_any_sequence_and_returns_a_fresh_list() -> None:
    observations = _observations()
    as_tuple = evaluate_all(HARBORLIGHT_V1, observations)
    as_list = evaluate_all(list(HARBORLIGHT_V1), observations)
    assert as_tuple == as_list
    assert as_tuple is not as_list
    as_tuple.clear()
    assert len(evaluate_all(HARBORLIGHT_V1, observations)) == 2


@pytest.mark.parametrize("raw", [{"proposal": {}}, [], None, "x"])
def test_evaluate_all_needs_frozen_observations(raw: object) -> None:
    with pytest.raises(PredicateError) as caught:
        evaluate_all(HARBORLIGHT_V1, raw)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_observations"


def test_evaluate_all_refuses_an_empty_set() -> None:
    for empty in ([], ()):
        with pytest.raises(PredicateError) as caught:
            evaluate_all(empty, Observations({}))
        assert caught.value.code == "empty_predicate_set"


@pytest.mark.parametrize("bad", [None, 5, object()])
def test_evaluate_all_refuses_a_non_sequence(bad: object) -> None:
    with pytest.raises(PredicateError) as caught:
        evaluate_all(bad, Observations({}))  # type: ignore[arg-type]
    assert caught.value.code == "invalid_predicate"


@pytest.mark.parametrize("bad", [["not a predicate"], [_ok(), None], "p1", [lambda o: None]])
def test_evaluate_all_refuses_a_non_predicate(bad: Any) -> None:
    with pytest.raises(PredicateError) as caught:
        evaluate_all(bad, Observations({}))
    assert caught.value.code == "invalid_predicate"


def test_evaluate_all_refuses_a_repeated_id() -> None:
    with pytest.raises(PredicateError) as caught:
        evaluate_all([_ok("p1"), _ok("p1")], Observations({}))
    assert caught.value.code == "duplicate_predicate_id"


def test_evaluate_all_refuses_mixed_criteria_versions() -> None:
    with pytest.raises(PredicateError) as caught:
        evaluate_all([_ok("p1", "v1"), _ok("p2", "v2")], Observations({}))
    assert caught.value.code == "mixed_criteria_versions"
    assert len(evaluate_all([_ok("p1", "v2"), _ok("p2", "v2")], Observations({}))) == 2


def test_nothing_is_evaluated_when_the_set_is_refused() -> None:
    calls: list[str] = []

    def evaluate(_o: Observations) -> CriterionResult:
        calls.append("called")
        return CriterionResult("p1", True, "/x")

    first = Predicate(id="p1", criteria_version="v1", rule_ids=["r1"], evaluate=evaluate)
    with pytest.raises(PredicateError):
        evaluate_all([first, _ok("p2", "v2")], Observations({}))
    assert calls == []


def test_a_raising_predicate_is_an_error_not_a_verdict() -> None:
    def boom(_o: Observations) -> CriterionResult:
        raise RuntimeError("secret detail 0xBEEF")

    predicate = Predicate(id="p1", criteria_version="v1", rule_ids=["r1"], evaluate=boom)
    with pytest.raises(PredicateError) as caught:
        evaluate_all([predicate], Observations({}))
    assert caught.value.code == "predicate_failed"
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "0xBEEF" not in str(caught.value)
    assert "RuntimeError" in str(caught.value)


@pytest.mark.parametrize(
    "returned",
    [None, True, ("p1", True, "/x"), {"predicate_id": "p1"}, CriterionResult("other", True, "/x")],
)
def test_a_result_that_is_not_the_predicates_own_is_refused(returned: object) -> None:
    predicate = Predicate(
        id="p1", criteria_version="v1", rule_ids=["r1"], evaluate=lambda _o: returned
    )  # type: ignore[arg-type,return-value]
    with pytest.raises(PredicateError) as caught:
        evaluate_all([predicate], Observations({}))
    assert caught.value.code == "invalid_result"


def test_error_codes_are_the_published_set() -> None:
    assert {
        "invalid_predicate",
        "invalid_observations",
        "invalid_result",
        "empty_predicate_set",
        "duplicate_predicate_id",
        "mixed_criteria_versions",
        "predicate_failed",
    } == predicates_module.CODES
    assert str(PredicateError("invalid_result", "m")) == "invalid_result: m"
    assert isinstance(PredicateError("invalid_result", "m"), ValueError)


# --- purity of the module -----------------------------------------------------------------------------------------------

#: Everything the acceptance package may import: the standard library modules a pure function of its input needs.
ALLOWED_IMPORTS = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "math",
    "re",
    "types",
    "typing",
    "caliber.acceptance.predicates",
}
FORBIDDEN_TOP_LEVEL = {
    "httpx",
    "requests",
    "sqlalchemy",
    "socket",
    "urllib",
    "http",
    "time",
    "datetime",
    "random",
    "os",
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not used in this package"
            found.add(node.module or "")
    return found


@pytest.mark.parametrize("module", sorted(p.name for p in SOURCE_DIR.glob("*.py")))
def test_the_acceptance_package_imports_no_io_library(module: str) -> None:
    imports = _imports(SOURCE_DIR / module)
    assert imports, module
    assert not {name.split(".")[0] for name in imports} & FORBIDDEN_TOP_LEVEL, imports
    assert imports <= ALLOWED_IMPORTS, sorted(imports - ALLOWED_IMPORTS)


def test_importing_the_package_does_not_load_an_http_or_database_client() -> None:
    """In a fresh interpreter that has not imported caliber's own database layer, the package pulls in none of them."""
    import subprocess

    # The package's modules refer to themselves as ``caliber.acceptance.predicates``; stub the parent so no other
    # caliber module (and so no client library) is loaded by the import under test.
    stub = (
        "import sys, types;"
        "sys.modules['caliber'] = types.ModuleType('caliber');"
        "sys.modules['caliber'].__path__ = [];"
        "sys.modules['caliber.acceptance'] = types.ModuleType('caliber.acceptance');"
        "sys.modules['caliber.acceptance'].__path__ = [];"
        "import importlib.util;"
        "spec = importlib.util.spec_from_file_location('caliber.acceptance.predicates', sys.argv[1]);"
        "mod = importlib.util.module_from_spec(spec);"
        "sys.modules['caliber.acceptance.predicates'] = mod;"
        "spec.loader.exec_module(mod);"
        "print(sorted(m for m in ('httpx', 'requests', 'sqlalchemy') if m in sys.modules))"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", stub, str(SOURCE_DIR / "predicates.py")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
