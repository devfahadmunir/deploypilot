"""API-key authentication for the HTTP transport. Static keys, not OAuth."""

import hmac
import os

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

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
