"""Google OAuth for the MCP endpoint, restricted to allowlisted emails (ported from mailhub)."""

import logging

from fastmcp.server.auth.providers.google import GoogleProvider

from cashbook_mcp.config import Secrets, Settings

log = logging.getLogger("cashbook-mcp")


class AllowlistGoogleProvider(GoogleProvider):
    """Google login that only accepts tokens for allowlisted, verified emails.

    Anyone can complete the Google login, so the check has to happen on every
    request, not just at sign-in.
    """

    def __init__(self, *, allowed_emails: frozenset[str], **kwargs):
        super().__init__(**kwargs)
        self.allowed_emails = allowed_emails

    async def load_access_token(self, token):
        access_token = await super().load_access_token(token)
        if access_token is None:
            return None
        claims = access_token.claims or {}
        email = str(claims.get("email", "")).lower()
        if email in self.allowed_emails and claims.get("email_verified") in (True, "true"):
            return access_token
        log.warning("rejected token for email=%r", email[:200])
        return None


def build_auth(settings: Settings, secrets: Secrets) -> AllowlistGoogleProvider:
    return AllowlistGoogleProvider(
        client_id=secrets.google_client_id,
        client_secret=secrets.google_client_secret,
        base_url=settings.public_url,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        allowed_client_redirect_uris=list(settings.allowed_redirect_uris),
        jwt_signing_key=secrets.session_key,
        # "consent" makes Google return a refresh token on every login; without it Google
        # sends one only on the client's first authorization and sessions end after 1 h.
        extra_authorize_params={"access_type": "offline", "prompt": "consent select_account"},
        allowed_emails=settings.allowed_emails,
    )
