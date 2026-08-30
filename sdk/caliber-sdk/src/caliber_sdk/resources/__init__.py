"""Resource modules — typed façades over route groups."""

from ._base import Resource
from .assets import AgentsAPI, PromptsAPI, SkillsAPI, ToolsAPI
from .auth import AccountsAPI, AuthAPI, TokensAPI
from .integrations import (
    GatewayAPI,
    KnowledgeBasesAPI,
    McpServersAPI,
    ObjectStoreAPI,
    OpenApiIntegrationsAPI,
)
from .operations import (
    AriaAPI,
    AriaDraftsAPI,
    AriaSessionsAPI,
    AuditAPI,
    CookbooksAPI,
    EventsAPI,
    GateVerdictsAPI,
    JobsAPI,
    MemoryAPI,
    ObservabilityAPI,
    ReleasesAPI,
    ReviewQueuesAPI,
    SecretsAPI,
    SystemAPI,
)
from .projects import ProjectFilesAPI, ProjectsAPI
from .quality import EvalDatasetsAPI, EvaluationsAPI, JudgesAPI
from .raw import RawAPI
from .system import CapabilitiesAPI, MeAPI, SettingsAPI
from .workflows import (
    WorkflowPromotionsAPI,
    WorkflowRunFailed,
    WorkflowRunsAPI,
    WorkflowsAPI,
    WorkflowServicesAPI,
    WorkflowVersionsAPI,
)

__all__ = [
    "AccountsAPI",
    "AgentsAPI",
    "AriaAPI",
    "AriaDraftsAPI",
    "AriaSessionsAPI",
    "AuditAPI",
    "AuthAPI",
    "CapabilitiesAPI",
    "CookbooksAPI",
    "EvalDatasetsAPI",
    "EvaluationsAPI",
    "EventsAPI",
    "GateVerdictsAPI",
    "GatewayAPI",
    "JobsAPI",
    "JudgesAPI",
    "KnowledgeBasesAPI",
    "McpServersAPI",
    "MeAPI",
    "MemoryAPI",
    "ObjectStoreAPI",
    "ObservabilityAPI",
    "OpenApiIntegrationsAPI",
    "ProjectFilesAPI",
    "ProjectsAPI",
    "PromptsAPI",
    "RawAPI",
    "ReleasesAPI",
    "Resource",
    "ReviewQueuesAPI",
    "SecretsAPI",
    "SettingsAPI",
    "SkillsAPI",
    "SystemAPI",
    "TokensAPI",
    "ToolsAPI",
    "WorkflowPromotionsAPI",
    "WorkflowRunFailed",
    "WorkflowRunsAPI",
    "WorkflowServicesAPI",
    "WorkflowVersionsAPI",
    "WorkflowsAPI",
]
