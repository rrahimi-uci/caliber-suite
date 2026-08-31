"""The async client, and an honest statement of what it covers.

``AsyncCaliberClient`` gives you the transport, the raw surface, async waiters,
and the two typed surfaces where being async actually changes the outcome:
long-running work you poll, and the event stream you hold open.

It does **not** mirror all twenty-odd typed resource modules. That is a decision,
not an omission. Two hand-written copies of the same resource tree is two places
for a path to be renamed and one place for it to be forgotten, and this
repository's history includes several wrong paths caught only by driving the SDK
against the real server -- a second, less-exercised copy would carry exactly that
risk with none of that coverage.

So the split follows where async earns its complexity:

* **Streaming.** ``events.stream()`` holds a connection open. Consuming that
  synchronously occupies a thread for as long as you want events, and this is
  the case async exists for.
* **Concurrency.** Submitting forty workflow runs and awaiting them together is
  one coroutine per run instead of forty threads.
* **Everything else.** ``client.raw`` reaches any endpoint with the same auth,
  retries, envelope handling, and typed errors. Use the synchronous client for
  the typed convenience, or decode ``raw`` yourself with the models this package
  already ships -- ``caliber_sdk.models.decode`` works on any payload.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from ..auth import AuthProvider
from ..client import ENV_BASE_URL, ENV_PROJECT, CaliberClient
from ..errors import CaliberConfigError
from ..models._decode import decode, decode_list
from ..models.core import Capabilities, Identity, WorkflowRunCapabilities
from ..models.operations import Job
from ..models.workflows import FAILED_RUN_STATES, WorkflowRun
from ..resources.workflows import WorkflowRunFailed
from .transport import AsyncTransport
from .waiters import wait_for


class AsyncCaliberClient:
    """An asynchronous connection to one CALIBER deployment.

    Constructed exactly like :class:`caliber_sdk.CaliberClient`, including the
    same environment fallbacks and the same credential precedence -- a token
    beats a trusted header, because the token is a real credential and the header
    is only an assertion.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        user: str | None = None,
        proxy_secret: str | None = None,
        auth: AuthProvider | None = None,
        project: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        verify: bool | str = True,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_url = (base_url or os.environ.get(ENV_BASE_URL) or "").strip()
        if not resolved_url:
            raise CaliberConfigError(f"base_url is required (pass it, or set {ENV_BASE_URL})")

        self._transport = AsyncTransport(
            resolved_url,
            # Reused from the sync client rather than reimplemented: the
            # precedence rule between a token, a trusted header, and no
            # credential at all is a decision, and having it in one place is
            # what stops the two clients from authenticating differently.
            auth=auth or CaliberClient._auth_from(token, user, proxy_secret),
            project=project or os.environ.get(ENV_PROJECT) or None,
            timeout=timeout,
            max_retries=max_retries,
            verify=verify,
            http_client=http_client,
        )
        self.raw = AsyncRawAPI(self._transport)
        self.me = AsyncMeAPI(self._transport)
        self.capabilities_info = AsyncCapabilitiesAPI(self._transport)
        self.workflows = AsyncWorkflowRunsAPI(self._transport)
        self.jobs = AsyncJobsAPI(self._transport)
        self.events = AsyncEventsAPI(self._transport)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def __aenter__(self) -> AsyncCaliberClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    # -- deprecated aliases -------------------------------------------------
    #
    # Mirrors CaliberClient.capabilities_api -- see AD-6 (client.py).
    # Scheduled for removal in 0.2.0, alongside the sync client's alias.

    @property
    def capabilities_api(self) -> AsyncCapabilitiesAPI:
        """Deprecated alias for :attr:`capabilities_info`."""
        warnings.warn(
            "AsyncCaliberClient.capabilities_api is deprecated and will be "
            "removed in caliber-sdk 0.2.0; use .capabilities_info instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.capabilities_info


class _AsyncResource:
    """Shared plumbing: unwrap the envelope, hand back the payload."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.get(path, **kwargs)).data

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.post(path, **kwargs)).data


class AsyncRawAPI(_AsyncResource):
    """Any endpoint, with the SDK's auth, retries, and typed errors.

    The reason the typed coverage here can stay narrow without the client being
    limiting: nothing in CALIBER is unreachable from an async caller.
    """

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._get(path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self._post(path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.put(path, **kwargs)).data

    async def patch(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.patch(path, **kwargs)).data

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.delete(path, **kwargs)).data

    async def download(self, path: str, **kwargs: Any) -> bytes:
        return await self._transport.download(path, **kwargs)

    def paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None, limit: int = 100
    ) -> AsyncIterator[Any]:
        """Not a coroutine: an async generator, so it is iterated rather than awaited."""
        return self._transport.paginate(path, params=params, limit=limit)


class AsyncMeAPI(_AsyncResource):
    async def get(self) -> Identity:
        """Reports identity rather than requiring it: a bad credential returns
        an anonymous identity, not an exception."""
        return decode(Identity, await self._get("/me"))


class AsyncCapabilitiesAPI(_AsyncResource):
    async def get(self) -> Capabilities:
        payload = await self._get("/capabilities")
        capabilities = decode(Capabilities, payload)
        if isinstance(payload, dict):
            capabilities.workflow_runs = decode(
                WorkflowRunCapabilities, payload.get("workflow_runs")
            )
        return capabilities


class AsyncWorkflowRunsAPI(_AsyncResource):
    """Submit runs and await them.

    The surface async is for on the request side: forty concurrent
    ``submit_and_wait`` calls are forty coroutines rather than forty threads.
    """

    async def submit(
        self,
        *,
        workflow_version_id: str | None = None,
        workflow_id: str | None = None,
        alias: str | None = None,
        input: Any = None,
        idempotency_key: str | None = None,
        **options: Any,
    ) -> WorkflowRun:
        body: dict[str, Any] = {**options}
        for key, value in (
            ("workflow_version_id", workflow_version_id),
            ("workflow_id", workflow_id),
            ("alias", alias),
            ("input", input),
            ("idempotency_key", idempotency_key),
        ):
            if value is not None:
                body[key] = value
        return decode(WorkflowRun, await self._post("/workflow-runs", json=body))

    async def get(self, run_id: str) -> WorkflowRun:
        return decode(WorkflowRun, await self._get(f"/workflow-runs/{run_id}"))

    async def list(self, workflow_id: str, *, status: str | None = None) -> list[WorkflowRun]:
        """Runs of one workflow.

        Scoped because the server has no unscoped listing: ``/workflow-runs`` is
        POST-only. An earlier SDK method implying otherwise returned 405.
        """
        params = {"status": status} if status else None
        return decode_list(
            WorkflowRun, await self._get(f"/workflows/{workflow_id}/runs", params=params)
        )

    async def cancel(self, run_id: str) -> WorkflowRun:
        return decode(WorkflowRun, await self._post(f"/workflow-runs/{run_id}/cancel"))

    async def wait(
        self, run_id: str, *, timeout: float = 900.0, raise_on_failure: bool = True, **options: Any
    ) -> WorkflowRun:
        run = await wait_for(
            lambda: self.get(run_id),
            is_done=lambda item: item.is_terminal,
            timeout=timeout,
            **options,
        )
        if raise_on_failure and run.status in FAILED_RUN_STATES:
            raise WorkflowRunFailed(run)
        return run


class AsyncJobsAPI(_AsyncResource):
    """Background jobs, and waiting on ones that stop for a person."""

    async def get(self, job_id: str) -> Job:
        return decode(Job, await self._get(f"/jobs/{job_id}"))

    async def list(self, *, status: str | None = None) -> list[Job]:
        params = {"status": status} if status else None
        return decode_list(Job, await self._get("/jobs", params=params))

    async def wait(self, job_id: str, *, timeout: float = 900.0, **options: Any) -> Job:
        """Return when the job finishes *or* stops for a human.

        ``candidate_ready`` is a resting state: applying the candidate is a
        person's decision, so the job will never advance on its own and polling
        past it would spend the whole timeout on the expected outcome.
        """
        return await wait_for(
            lambda: self.get(job_id),
            is_done=lambda job: job.is_terminal or job.awaits_human,
            timeout=timeout,
            **options,
        )


class AsyncEventsAPI(_AsyncResource):
    """The reason this module exists."""

    def stream(self, **params: Any) -> AsyncIterator[str]:
        """Yield raw server-sent-event lines as they arrive.

        Unparsed on purpose: the event vocabulary grows with the server, and a
        decoder compiled into this SDK would reject events added after it
        shipped -- exactly when a consumer most needs to see them.

        Returns an async iterator rather than a coroutine, so it is used with
        ``async for`` and never awaited.
        """
        return self._transport.stream_lines("/events/stream", params=params or None)


__all__ = [
    "AsyncCaliberClient",
    "AsyncCapabilitiesAPI",
    "AsyncEventsAPI",
    "AsyncJobsAPI",
    "AsyncMeAPI",
    "AsyncRawAPI",
    "AsyncWorkflowRunsAPI",
]
