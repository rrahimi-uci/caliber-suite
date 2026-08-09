"""Resource modules — typed façades over route groups."""

from ._base import Resource
from .raw import RawAPI

__all__ = ["RawAPI", "Resource"]
