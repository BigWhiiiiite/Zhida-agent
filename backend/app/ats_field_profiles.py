"""Compatibility entry point; all ATS hints live in the shared registry."""
from .ats_registry import policy_for, resolve_site_route


def field_profile_for_url(url: str) -> dict[str, object]:
    return policy_for(resolve_site_route(url).adapter).field_profile()
