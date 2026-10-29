"""Constants for the Paprika LLM API integration."""

DOMAIN = "paprika"

# Identifier this API is registered under. It appears in the Model Context
# Protocol Server integration's "Control Home Assistant" selector, and in any
# conversation agent's LLM API picker.
LLM_API_ID = "paprika"
LLM_API_NAME = "Paprika"

# Tool names must be prefixed with the domain and two underscores. Home
# Assistant logs unprefixed names today and stops accepting them in 2027.3.
TOOL_PREFIX = f"{DOMAIN}__"
