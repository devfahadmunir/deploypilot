import os

# In-memory tests have no API key, so give them the agent role (the default is viewer).
os.environ["MCP_ROLE"] = "agent"
