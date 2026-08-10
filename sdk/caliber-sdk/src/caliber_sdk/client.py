"""The root client — what a developer constructs first.

    from caliber_sdk import CaliberClient

    with CaliberClient("https://caliber.example.com", token="calpat_...") as caliber:
        print(caliber.capabilities()["sdk_stability"]["ga"])

"Simple first" from the SDK principles: the common case is a URL and a token.
Everything else -- project scoping, retries, a shared httpx client, a custom
auth provider -- is a keyword argument with a sensible default.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from .auth import AuthProvider, NoAuth, TokenAuth, TrustedHeaderAuth
from .errors import CaliberConfigError
from .resources import (
    AriaAPI,
    AuditAPI,
    AuthAPI,
    CapabilitiesAPI,
    CookbooksAPI,
    EvalDatasetsAPI,
    EvaluationsAPI,
    EventsAPI,
    GatewayAPI,
    JobsAPI,
    JudgesAPI,
    KnowledgeBasesAPI,
    McpServersAPI,
    MeAPI,
    ObjectStoreAPI,
    ObservabilityAPI,
    ProjectsAPI,
    PromptsAPI,
    RawAPI,
    ReleasesAPI,
    ReviewQueuesAPI,
    SecretsAPI,
    SettingsAPI,
    SkillsAPI,
    ToolsAPI,
    WorkflowsAPI,
)
from .transport import Transport

#: Environment variables the client falls back to, so a script that runs in CI
#: does not have to thread configuration through its own argument parsing.
ENV_BASE_URL = "CALIBER_BASE_URL"
ENV_TOKEN = "CALIBER_TOKEN"
ENV_PROJECT = "CALIBER_PROJECT"
ENV_USER = "CALIBER_USER"


class CaliberClient:
    """A connection to one CALIBER deployment."""

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
        http_client: httpx.Client | None = None,
    ) -> None:
        resolved_url = (base_url or os.environ.get(ENV_BASE_URL) or "").strip()
        if not resolved_url:
            raise CaliberConfigError(f"base_url is required (pass it, or set {ENV_BASE_URL})")

        self._transport = Transport(
            resolved_url,
            auth=auth or self._auth_from(token, user, proxy_secret),
            project=project or os.environ.get(ENV_PROJECT) or None,
            timeout=timeout,
            max_retries=max_retries,
            verify=verify,
            client=http_client,
        )
        self.raw = RawAPI(self._transport)
        #: Typed resource modules. ``raw`` stays available for anything not
        #: yet modelled, so the SDK is never the reason something is
        #: unreachable.
        self.auth = AuthAPI(self._transport)
        self.me = MeAPI(self._transport)
        self.capabilities_api = CapabilitiesAPI(self._transport)
        self.settings = SettingsAPI(self._transport)
        self.projects = ProjectsAPI(self._transport)
        self.prompts = PromptsAPI(self._transport)
        self.skills = SkillsAPI(self._transport)
        self.tools = ToolsAPI(self._transport)
        self.workflows = WorkflowsAPI(self._transport)
        self.datasets = EvalDatasetsAPI(self._transport)
        self.judges = JudgesAPI(self._transport)
        self.evaluations = EvaluationsAPI(self._transport)
        #: Beta surfaces. Real and supported, but their shapes are still
        #: moving -- check ``client.stability`` before depending on one.
        self.mcp_servers = McpServersAPI(self._transport)
        self.gateway = GatewayAPI(self._transport)
        self.knowledge_bases = KnowledgeBasesAPI(self._transport)
        self.object_store = ObjectStoreAPI(self._transport)
        self.jobs = JobsAPI(self._transport)
        self.review_queues = ReviewQueuesAPI(self._transport)
        self.aria = AriaAPI(self._transport)
        self.releases = ReleasesAPI(self._transport)
        self.observability = ObservabilityAPI(self._transport)
        self.audit = AuditAPI(self._transport)
        self.events = EventsAPI(self._transport)
        self.cookbooks = CookbooksAPI(self._transport)
        self.secrets = SecretsAPI(self._transport)

    @staticmethod
    def _auth_from(token: str | None, user: str | None, proxy_secret: str | None) -> AuthProvider:
        """Pick a credential, preferring the explicit one.

        A token beats a trusted header when both are present: the token is a
        real credential and the header is only an assertion, so silently
        preferring the weaker one would be the wrong surprise.
        """
        resolved_token = (token or os.environ.get(ENV_TOKEN) or "").strip()
        if resolved_token:
            return TokenAuth(resolved_token)
        resolved_user = (user or os.environ.get(ENV_USER) or "").strip()
        if resolved_user:
            return TrustedHeaderAuth(resolved_user, proxy_secret=proxy_secret)
        return NoAuth()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> CaliberClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- discovery ---------------------------------------------------------

    def capabilities(self) -> Any:
        """Runtime feature flags and the SDK stability tiers.

        The cheap half of feature detection: it answers "may I call this?"
        without downloading the full OpenAPI document.
        """
        return self._transport.get("/capabilities").data

    def openapi(self) -> Any:
        """The management OpenAPI document, generated from the live routes."""
        return self._transport.get("/openapi.json").data

    def whoami(self) -> Any:
        """The identity and scopes CALIBER resolved for this client's credential.

        The first call worth making when a script gets an unexpected 403: it
        distinguishes "wrong credential" from "right credential, wrong scope".

        It reports identity rather than requiring it, so an invalid or revoked
        credential does **not** raise here -- it returns ``user_id:
        "anonymous"`` with no scopes. Check the value; do not rely on an
        exception to detect a bad token.
        """
        return self._transport.get("/me").data

    def health(self) -> Any:
        return self._transport.get("/health").data

    def bootstrap_csrf(self) -> str | None:
        """Fetch a CSRF token up front.

        Rarely needed: the transport fetches one automatically when a write is
        refused for want of it. Exposed for callers who would rather pay that
        round trip at startup than on their first write.
        """
        return self._transport.bootstrap_csrf()

    @property
    def stability(self) -> dict[str, list[str]]:
        """Tags grouped by ``ga`` / ``beta`` / ``internal``."""
        payload = self.capabilities()
        tiers = payload.get("sdk_stability") if isinstance(payload, dict) else None
        return tiers if isinstance(tiers, dict) else {}

    def __repr__(self) -> str:
        return f"CaliberClient(base_url={self._transport.base_url!r})"


__all__ = ["ENV_BASE_URL", "ENV_PROJECT", "ENV_TOKEN", "ENV_USER", "CaliberClient"]
