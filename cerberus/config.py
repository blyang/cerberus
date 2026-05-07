"""Site config loader. Reads site_config.yaml; provides per-host lookup."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from urllib.parse import urlparse

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "site_config.yaml"


@dataclasses.dataclass
class HostConfig:
    host: str
    apex_host: str | None = None
    sitemap_url: str | None = None
    robots_url: str | None = None
    pipeline_url_prefixes: list[str] = dataclasses.field(default_factory=list)
    allowed_bots: list[str] = dataclasses.field(default_factory=list)
    # If set, the G checks ignore hreflang alternates whose primary language code (`pt-br` → `pt`,
    # `zh-CN` → `zh`) is not in this list. Use to mute G failures for locales the page advertises
    # but the site does not actually serve. None = no filtering (all advertised locales evaluated).
    supported_languages: list[str] | None = None
    explicit: bool = False  # True if host was found in config; False if inferred

    def robots_url_resolved(self) -> str:
        if self.robots_url:
            return self.robots_url
        return f"https://{self.host}/robots.txt"


@dataclasses.dataclass
class VisionProviderConfig:
    provider: str
    model: str
    api_key_env: str
    base_url: str | None = None


@dataclasses.dataclass
class VisionConfig:
    enabled: bool
    primary: VisionProviderConfig | None
    fallback: VisionProviderConfig | None
    confidence_threshold: float
    per_run_budget_usd: float


@dataclasses.dataclass
class SiteConfig:
    raw: dict
    hosts: dict[str, dict]
    defaults: dict
    gsc_credentials_path: str | None
    default_url: str | None
    vision: VisionConfig | None = None

    def for_url(self, url: str) -> HostConfig:
        host = urlparse(url).hostname or ""
        host = host.lower()
        if host in self.hosts:
            entry = self.hosts[host]
            sl = entry.get("supported_languages")
            return HostConfig(
                host=host,
                apex_host=entry.get("apex_host") or _infer_apex(host),
                sitemap_url=entry.get("sitemap_url"),
                robots_url=entry.get("robots_url"),
                pipeline_url_prefixes=list(entry.get("pipeline_url_prefixes", [])),
                allowed_bots=list(entry.get("allowed_bots") or self.defaults.get("allowed_bots", [])),
                supported_languages=[s.lower() for s in sl] if sl is not None else None,
                explicit=True,
            )
        return HostConfig(
            host=host,
            apex_host=_infer_apex(host),
            allowed_bots=list(self.defaults.get("allowed_bots", [])),
            explicit=False,
        )


def _infer_apex(host: str) -> str | None:
    """Strip leading www. (or single subdomain) to derive apex. None if can't infer safely."""
    if not host or host.count(".") < 1:
        return None
    if host.startswith("www."):
        return host[4:]
    # If the host has 3+ labels and no www, also try first-label-strip as a guess.
    parts = host.split(".")
    if len(parts) >= 3:
        return ".".join(parts[1:])
    return None


def load(path: Path | None = None) -> SiteConfig:
    p = path or CONFIG_PATH
    if not p.exists():
        return SiteConfig(raw={}, hosts={}, defaults={}, gsc_credentials_path=None, default_url=None)
    raw = yaml.safe_load(p.read_text()) or {}
    hosts = {k.lower(): v for k, v in (raw.get("hosts") or {}).items()}
    return SiteConfig(
        raw=raw,
        hosts=hosts,
        defaults=raw.get("defaults") or {},
        gsc_credentials_path=raw.get("gsc_credentials_path"),
        default_url=raw.get("default_url"),
        vision=_parse_vision(raw.get("vision")),
    )


def _parse_vision(d: dict | None) -> VisionConfig | None:
    if not d:
        return None
    def _provider(sub: dict | None) -> VisionProviderConfig | None:
        if not sub:
            return None
        return VisionProviderConfig(
            provider=sub.get("provider", ""),
            model=sub.get("model", ""),
            api_key_env=sub.get("api_key_env", ""),
            base_url=sub.get("base_url"),
        )
    return VisionConfig(
        enabled=bool(d.get("enabled")),
        primary=_provider(d.get("primary")),
        fallback=_provider(d.get("fallback")),
        confidence_threshold=float(d.get("confidence_threshold", 0.70)),
        per_run_budget_usd=float(d.get("per_run_budget_usd", 0.10)),
    )
