"""API-key authentication for the HTTP transport. Static keys, not OAuth."""

import hmac
import os

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError

# role -> (env variable holding its key, scopes that role gets)
ROLES = {
    "viewer": ("MCP_VIEWER_KEY", ["read"]),
    "agent": ("MCP_AGENT_KEY", ["read", "write"]),
}


class ApiKeyVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        for role, (env_name, scopes) in ROLES.items():
            key = os.getenv(env_name, "")
            # compare_digest takes constant time, so response timing leaks nothing about the key.
            if key and hmac.compare_digest(token.encode(), key.encode()):
                return AccessToken(token=token, client_id=role, scopes=scopes)
        return None  # unknown key -> 401


# issuer_url is mandatory in the SDK; with static keys there is no real issuer.
AUTH_SETTINGS = AuthSettings(issuer_url="http://127.0.0.1:8000", resource_server_url=None)


def caller() -> tuple[str, list[str]]:
    """(client id, scopes) of whoever is making the current request."""
    token = get_access_token()
    if token:
        return token.client_id, token.scopes
    # No token means stdio or in-memory (HTTP never gets this far without one).
    # Least privilege: read-only unless MCP_ROLE says otherwise.
    role = os.getenv("MCP_ROLE", "viewer")
    return f"local:{role}", ROLES.get(role, ROLES["viewer"])[1]


def require_write() -> None:
    client_id, scopes = caller()
    if "write" not in scopes:
        raise ToolError(f"Permission denied: '{client_id}' is read-only and cannot make changes.")
