"""caliber-sdk — a typed Python client for the CALIBER management API.

    from caliber_sdk import CaliberClient

    with CaliberClient("https://caliber.example.com", token="calpat_...") as caliber:
        print(caliber.whoami())

Install carries no server dependencies: no mlflow, no sqlalchemy, no starlette.
That is the reason this is a separate distribution rather than a module inside
the server package.

Stability follows the tiers the server publishes. ``client.stability`` reports
which API tags are ``ga``, ``beta``, or ``internal`` for the deployment you are
talking to, so a script can check rather than assume.
"""

from __future__ import annotations

from .auth import AuthProvider, NoAuth, TokenAuth, TrustedHeaderAuth
from .client import (
    ENV_BASE_URL,
    ENV_PROJECT,
    ENV_TOKEN,
    ENV_USER,
    CaliberClient,
)
from .errors import (
    CaliberAPIError,
    CaliberAuthenticationError,
    CaliberConfigError,
    CaliberConflictError,
    CaliberDecodeError,
    CaliberError,
    CaliberNotFoundError,
    CaliberPermissionError,
    CaliberRateLimitError,
    CaliberServerError,
    CaliberTransportError,
    CaliberValidationError,
)
from .models import ErrorBody, FieldError, Page, Stability
from .resources import RawAPI, WorkflowRunFailed
from .transport import API_PREFIX, Response, Transport
from .waiters import (
    FAILURE_STATES,
    TERMINAL_STATES,
    WaitFailed,
    WaitTimeout,
    wait_for,
    wait_for_terminal_state,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "API_PREFIX",
    "ENV_BASE_URL",
    "ENV_PROJECT",
    "ENV_TOKEN",
    "ENV_USER",
    "FAILURE_STATES",
    "TERMINAL_STATES",
    "AuthProvider",
    "CaliberAPIError",
    "CaliberAuthenticationError",
    "CaliberClient",
    "CaliberConfigError",
    "CaliberConflictError",
    "CaliberDecodeError",
    "CaliberError",
    "CaliberNotFoundError",
    "CaliberPermissionError",
    "CaliberRateLimitError",
    "CaliberServerError",
    "CaliberTransportError",
    "CaliberValidationError",
    "ErrorBody",
    "FieldError",
    "NoAuth",
    "Page",
    "RawAPI",
    "Response",
    "Stability",
    "TokenAuth",
    "Transport",
    "TrustedHeaderAuth",
    "WaitFailed",
    "WaitTimeout",
    "WorkflowRunFailed",
    "__version__",
    "wait_for",
    "wait_for_terminal_state",
]
