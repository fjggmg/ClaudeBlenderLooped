"""Shared exception types and user-facing remediation text."""

from __future__ import annotations


class AuthError(RuntimeError):
    """Raised when the underlying claude CLI cannot authenticate."""


AUTH_REMEDIATION = (
    "The claude CLI could not authenticate (HTTP 401).\n"
    "blendahbot drives the standalone `claude` CLI, which needs its OWN long-lived "
    "subscription token — the Claude Desktop app's in-session auth is not shared with "
    "external processes, and `claude setup-token` does NOT write it to a file by itself.\n\n"
    "Fix it once (browser 'approve'), then you're hands-off:\n"
    "  • Double-click  start.bat auth   — or —\n"
    "  • Run:  blendahbot --auth\n"
    "Then verify with:  blendahbot --selftest\n\n"
    "Alternatively, set ANTHROPIC_API_KEY to use the API directly (pay-per-token)."
)


def looks_like_auth_failure(text: str) -> bool:
    blob = (text or "").lower()
    return any(
        k in blob
        for k in (
            "invalid authentication",
            "failed to authenticate",
            "authentication_error",
            "401",
            "invalid api key",
            "oauth",
        )
    )
