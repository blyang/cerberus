"""Check primitives: registry, severity, status, context, result.

Each check is an async function decorated with @register that takes a CheckContext
and returns a CheckResult. Multi-step checks return sub_steps for the renderer.
"""
from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any


class Severity(str, Enum):
    BLOCKING = "Blocking"
    RECOMMENDED = "Recommended"
    CONDITIONAL = "Conditional"


class Env(str, Enum):
    """Deployment environment of the URL under test.

    Operators pick one at run time; checks declare which envs they're meaningful in via
    `applicable_envs` on @register. Mismatched checks short-circuit to N/A — preprod can't
    legitimately verify production CDN cache headers, sitemap availability, etc.
    """
    PRODUCTION = "production"
    UAT = "uat"
    PREPROD = "preprod"
    STAGING = "staging"
    LOCAL = "local"


ALL_ENVS = frozenset(Env)


class Status(str, Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    PASS = "Pass"
    FAIL = "Fail"
    NEEDS_REVIEW = "Needs Review"
    MANUAL = "Manual"
    NA = "N/A"


@dataclasses.dataclass
class SubStep:
    label: str
    status: Status
    detail: str = ""
    instruction: str = ""  # populated when status is MANUAL


@dataclasses.dataclass
class CheckResult:
    status: Status
    summary: str = ""
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
    sub_steps: list[SubStep] = dataclasses.field(default_factory=list)
    instruction: str = ""  # populated when status is MANUAL

    @classmethod
    def from_substeps(cls, summary: str, sub_steps: list[SubStep]) -> "CheckResult":
        statuses = {s.status for s in sub_steps}
        if Status.FAIL in statuses:
            status = Status.FAIL
        elif Status.MANUAL in statuses or Status.NEEDS_REVIEW in statuses:
            status = Status.NEEDS_REVIEW
        elif Status.NA in statuses and statuses <= {Status.NA, Status.PASS}:
            status = Status.PASS if Status.PASS in statuses else Status.NA
        elif statuses == {Status.PASS}:
            status = Status.PASS
        else:
            status = Status.NEEDS_REVIEW
        return cls(status=status, summary=summary, sub_steps=sub_steps)


@dataclasses.dataclass
class CheckSpec:
    id: str
    section: str
    severity: Severity
    title: str
    func: Callable[["CheckContext"], Awaitable[CheckResult]]
    default_estimate_ms: int = 1000
    applicable_envs: frozenset[Env] = ALL_ENVS


@dataclasses.dataclass
class CheckContext:
    """Shared context handed to every check function. Heavy resources are lazy."""
    url: str
    site_config: Any  # cerberus.config.SiteConfig
    run_id: str = ""  # populated by the runner for evidence persistence (screenshots, etc.)
    environment: Env = Env.PRODUCTION
    cache: dict[str, Any] = dataclasses.field(default_factory=dict)

    async def get_or_set(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Memoize a coroutine result on this run's cache."""
        if key in self.cache:
            value = self.cache[key]
            if isinstance(value, asyncio.Task):
                return await value
            return value
        task = asyncio.ensure_future(factory())
        self.cache[key] = task
        result = await task
        self.cache[key] = result
        return result


_REGISTRY: list[CheckSpec] = []


def register(
    id: str,
    section: str,
    severity: Severity,
    title: str,
    estimate_ms: int = 1000,
    applicable_envs: frozenset[Env] | set[Env] | None = None,
) -> Callable[[Callable[[CheckContext], Awaitable[CheckResult]]], Callable[[CheckContext], Awaitable[CheckResult]]]:
    envs = frozenset(applicable_envs) if applicable_envs is not None else ALL_ENVS

    def deco(func: Callable[[CheckContext], Awaitable[CheckResult]]) -> Callable[[CheckContext], Awaitable[CheckResult]]:
        _REGISTRY.append(
            CheckSpec(
                id=id,
                section=section,
                severity=severity,
                title=title,
                func=func,
                default_estimate_ms=estimate_ms,
                applicable_envs=envs,
            )
        )
        return func
    return deco


def all_checks() -> list[CheckSpec]:
    """Stable order: A1, A2, ..., B1, ..., H3."""
    def sort_key(spec: CheckSpec) -> tuple[str, int, str]:
        section = spec.id[0]
        rest = spec.id[1:]
        digits = ""
        suffix = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                digits += ch
            else:
                suffix = rest[i:]
                break
        return (section, int(digits or "0"), suffix)
    return sorted(_REGISTRY, key=sort_key)


def find_check(check_id: str) -> CheckSpec | None:
    for spec in _REGISTRY:
        if spec.id == check_id:
            return spec
    return None
