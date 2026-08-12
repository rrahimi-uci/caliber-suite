"""Resource modules — typed façades over route groups."""

from ._base import Resource
from .assets import PromptsAPI, SkillsAPI, ToolsAPI
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
    AuditAPI,
    CookbooksAPI,
    EventsAPI,
    JobsAPI,
    ObservabilityAPI,
    ReleasesAPI,
    ReviewQueuesAPI,
    SecretsAPI,
)
from .projects import ProjectFilesAPI, ProjectsAPI
from .quality import EvalDatasetsAPI, EvaluationsAPI, JudgesAPI
from .raw import RawAPI
from .system import CapabilitiesAPI, MeAPI, SettingsAPI
from .workflows import (
    WorkflowRunFailed,
    WorkflowRunsAPI,
    WorkflowsAPI,
    WorkflowServicesAPI,
    WorkflowVersionsAPI,
)

__all__ = [
    "AccountsAPI",
    "AriaAPI",
    "AuditAPI",
    "AuthAPI",
    "CapabilitiesAPI",
    "CookbooksAPI",
    "EvalDatasetsAPI",
    "EvaluationsAPI",
    "EventsAPI",
    "GatewayAPI",
    "JobsAPI",
    "JudgesAPI",
    "KnowledgeBasesAPI",
    "McpServersAPI",
    "MeAPI",
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
    "TokensAPI",
    "ToolsAPI",
    "WorkflowRunFailed",
    "WorkflowRunsAPI",
    "WorkflowServicesAPI",
    "WorkflowVersionsAPI",
    "WorkflowsAPI",
]
