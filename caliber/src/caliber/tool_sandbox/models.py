"""Pydantic models for sandboxed tool execution and tests."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunStatus = Literal["completed", "failed", "timed_out"]
TestSuiteStatus = Literal["passed", "failed", "timed_out"]


class ToolSandboxRunRequest(BaseModel):
    """Execute one user-authored Python callable in the sandbox."""

    model_config = ConfigDict(extra="forbid")

    #: Authored Python. Mutually exclusive with ``module_path`` — exactly one of the
    #: two says where the callable comes from.
    source_code: str = Field(default="", max_length=200_000)
    #: An installed module an admin registered, imported **inside the subprocess**
    #: rather than in the control plane (C8). Empty means source mode.
    module_path: str = Field(default="", max_length=512)
    callable_name: str = Field(min_length=1, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)
    #: Positional arguments, kept separate from ``input`` so ordering survives.
    args: list[Any] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0, le=120)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ToolSandboxRunRequest:
        """Refuse an ambiguous request rather than silently preferring one mode.

        A request carrying both would run *something*, and which one is not obvious
        from the call site — precisely the kind of quiet ambiguity that makes an
        execution boundary hard to reason about.
        """
        if bool(self.source_code.strip()) == bool(self.module_path.strip()):
            raise ValueError("exactly one of 'source_code' or 'module_path' is required")
        return self


class ToolSandboxRunResult(BaseModel):
    """Result of one sandboxed callable invocation."""

    status: RunStatus
    output: Any | None = None
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float


class ToolSandboxTestCase(BaseModel):
    """One example-based test for a tool callable."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    expected: Any | None = None
    compare_output: bool = True


class ToolSandboxTestSuiteRequest(BaseModel):
    """Run a set of example tests against one user-authored callable."""

    model_config = ConfigDict(extra="forbid")

    source_code: str = Field(min_length=1, max_length=200_000)
    callable_name: str = Field(min_length=1, max_length=128)
    tests: list[ToolSandboxTestCase] = Field(min_length=1, max_length=100)
    timeout_seconds: float | None = Field(default=None, gt=0, le=120)


class ToolSandboxTestCaseResult(BaseModel):
    """Result of one sandboxed tool test case."""

    name: str
    status: Literal["passed", "failed"]
    output: Any | None = None
    expected: Any | None = None
    error: str | None = None
    duration_ms: float


class ToolSandboxTestSuiteResult(BaseModel):
    """Result of a sandboxed tool test suite."""

    status: TestSuiteStatus
    tests: list[ToolSandboxTestCaseResult] = Field(default_factory=list)
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: float


__all__ = [
    "RunStatus",
    "TestSuiteStatus",
    "ToolSandboxRunRequest",
    "ToolSandboxRunResult",
    "ToolSandboxTestCase",
    "ToolSandboxTestCaseResult",
    "ToolSandboxTestSuiteRequest",
    "ToolSandboxTestSuiteResult",
]
