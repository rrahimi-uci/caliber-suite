"""Auth providers: header shape, validation, and credential redaction."""

from __future__ import annotations

import pytest

from caliber_sdk import CaliberConfigError, NoAuth, TokenAuth, TrustedHeaderAuth


def test_token_auth_sends_a_bearer_header() -> None:
    assert TokenAuth("calpat_abc").headers() == {"Authorization": "Bearer calpat_abc"}


def test_token_auth_never_renders_the_token() -> None:
    """A repr lands in logs and tracebacks -- exactly where a secret must not."""
    auth = TokenAuth("calpat_supersecret")
    assert "supersecret" not in repr(auth)


@pytest.mark.parametrize("bad", ["", "   "])
def test_token_must_not_be_empty(bad: str) -> None:
    with pytest.raises(CaliberConfigError):
        TokenAuth(bad)


def test_trusted_header_auth_sends_the_user_and_optional_proxy_secret() -> None:
    assert TrustedHeaderAuth("@alice").headers() == {"X-CALIBER-User": "@alice"}
    with_secret = TrustedHeaderAuth("@alice", proxy_secret="s3cret").headers()
    assert with_secret["X-CALIBER-Proxy-Secret"] == "s3cret"


def test_trusted_header_auth_rejects_a_comma() -> None:
    """The server parses scope user-lists as comma separated and rejects these.

    Failing here turns a confusing 401 into a clear configuration error.
    """
    with pytest.raises(CaliberConfigError):
        TrustedHeaderAuth("@alice,@admin")


def test_trusted_header_auth_never_renders_the_proxy_secret() -> None:
    assert "s3cret" not in repr(TrustedHeaderAuth("@alice", proxy_secret="s3cret"))


def test_no_auth_sends_nothing() -> None:
    assert NoAuth().headers() == {}
