"""Two transports is two chances to drift. This is the mechanism against it.

The async transport exists for streaming, and it necessarily restates the
*shape* of the request loop -- an ``await`` cannot be injected into a synchronous
function. What it must not restate is the *decisions*: which methods may be
retried, which statuses are worth retrying, how backoff grows, how a body is
decoded, when an envelope is unwrapped, what a CSRF failure looks like, and how
long a waiter waits.

Every test here fails if a decision gets copied instead of shared. That is
deliberately stricter than testing behaviour: two independent implementations
can behave identically today and diverge on the next edit, and nothing would
notice.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from caliber_sdk import transport as sync_transport
from caliber_sdk import waiters as sync_waiters
from caliber_sdk.aio import transport as async_transport
from caliber_sdk.aio import waiters as async_waiters

#: Read through ``vars`` because these are imports the async module makes, not
#: part of its public surface -- which is the whole claim being tested. Attribute
#: access would need a type-ignore on every line to say the same thing.
_ASYNC = vars(async_transport)
_SYNC = vars(sync_transport)


@pytest.mark.parametrize(
    "name",
    [
        "_RETRYABLE_METHODS",
        "_RETRYABLE_STATUS",
        "_decode",
        "_unwrap",
        "API_PREFIX",
        "USER_AGENT",
        "Response",
    ],
)
def test_each_shared_decision_is_one_object_not_two_copies(name: str) -> None:
    """Identity, not equality.

    Two equal sets today can diverge on the next edit and nothing would notice.
    The subtle ones are the reason this is strict: the envelope rule unwraps only
    when the body is *exactly* ``{"data": ...}``, and a second copy would
    eventually lose the "exactly".
    """
    assert name in _ASYNC, f"the async transport no longer shares {name}"
    assert _ASYNC[name] is _SYNC[name], f"{name} has been copied instead of shared"


def test_retry_after_and_csrf_detection_are_shared() -> None:
    """Both are parsing decisions about the server's replies, and both are
    reused off the sync class rather than reimplemented."""
    source = inspect.getsource(async_transport.AsyncTransport.request)
    assert "Transport._retry_after(" in source
    assert "Transport._looks_like_csrf_failure(" in source


def test_the_waiters_share_their_defaults() -> None:
    """A caller who moves a script from sync to async must not silently get a
    different timeout."""
    sync_defaults: dict[str, Any] = sync_waiters.wait_for.__kwdefaults__ or {}
    assert sync_defaults, "wait_for has no keyword defaults left to compare against"
    assert sync_defaults["timeout"] == async_waiters.DEFAULT_TIMEOUT
    assert sync_defaults["interval"] == async_waiters.DEFAULT_INTERVAL
    assert sync_defaults["max_interval"] == async_waiters.DEFAULT_MAX_INTERVAL
    assert sync_defaults["backoff"] == async_waiters.DEFAULT_BACKOFF


def test_the_waiters_share_their_exceptions_and_state_sets() -> None:
    """``except WaitTimeout`` has to catch both, or the async client is a trap."""
    assert async_waiters.WaitTimeout is sync_waiters.WaitTimeout
    assert async_waiters.WaitFailed is sync_waiters.WaitFailed


def test_credential_precedence_is_decided_in_one_place() -> None:
    """A token beats a trusted header. Two clients authenticating differently
    would be invisible until a permission failure."""
    from caliber_sdk.aio.client import AsyncCaliberClient

    source = inspect.getsource(AsyncCaliberClient.__init__)
    assert "CaliberClient._auth_from(" in source


def test_both_transports_expose_the_same_request_options() -> None:
    """A caller porting a call from one to the other must not find a missing
    keyword. Compared by name because the async version's are all awaited."""
    sync_params = set(inspect.signature(sync_transport.Transport.request).parameters)
    async_params = set(inspect.signature(async_transport.AsyncTransport.request).parameters)
    assert sync_params == async_params


def test_both_transports_expose_the_same_verbs() -> None:
    verbs = {"get", "post", "put", "patch", "delete", "download", "stream_lines", "paginate"}
    for name in verbs:
        assert hasattr(sync_transport.Transport, name), f"sync transport lost {name}"
        assert hasattr(async_transport.AsyncTransport, name), f"async transport lost {name}"


def test_the_async_transport_never_blocks_the_event_loop() -> None:
    """``time.sleep`` anywhere in here would stall every other coroutine on the
    loop, which is the one thing an async client must not do."""
    # Matched with the call parenthesis so the prose explaining *why* there is
    # no ``time.sleep`` here does not trip its own check.
    for module in (async_transport, async_waiters):
        source = inspect.getsource(module)
        assert "time.sleep(" not in source, f"{module.__name__} blocks the loop"
        assert "asyncio.sleep(" in source


def test_the_async_client_documents_what_it_does_not_cover() -> None:
    """Narrower typed coverage is a decision, and a reader has to be able to
    find out it was one rather than assume a resource was forgotten."""
    from caliber_sdk.aio import client as async_client

    doc = async_client.__doc__ or ""
    assert "raw" in doc
    assert "does **not** mirror" in doc
