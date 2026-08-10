"""Resource modules — typed façades over route groups."""

from ._base import Resource
from .assets import PromptsAPI, SkillsAPI, ToolsAPI
from .auth import AccountsAPI, AuthAPI, TokensAPI
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
    "AuthAPI",
    "CapabilitiesAPI",
    "EvalDatasetsAPI",
    "EvaluationsAPI",
    "JudgesAPI",
    "MeAPI",
    "ProjectFilesAPI",
    "ProjectsAPI",
    "PromptsAPI",
    "RawAPI",
    "Resource",
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
