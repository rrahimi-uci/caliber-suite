"""Resource modules — typed façades over route groups."""

from ._base import Resource
from .auth import AccountsAPI, AuthAPI, TokensAPI
from .projects import ProjectFilesAPI, ProjectsAPI
from .raw import RawAPI
from .system import CapabilitiesAPI, MeAPI, SettingsAPI

__all__ = [
    "AccountsAPI",
    "AuthAPI",
    "CapabilitiesAPI",
    "MeAPI",
    "ProjectFilesAPI",
    "ProjectsAPI",
    "RawAPI",
    "Resource",
    "SettingsAPI",
    "TokensAPI",
]
