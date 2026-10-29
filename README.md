# Paprika Recipe Manager — Home Assistant LLM API

Exposes your [Paprika Recipe Manager](https://www.paprikaapp.com/) recipes,
grocery lists and meal plan to LLM clients as a Home Assistant **LLM API**.

Home Assistant's
[Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
integration serves the tools over `/api/mcp`, so a cloud client such as Claude
can reach them through Home Assistant Cloud (Nabu Casa) with no tunnel, no
reverse proxy and no port forwarding.

## Why this shape

This integration contains **no MCP server**. It registers an LLM API; Home
Assistant is the MCP server. That means Home Assistant owns:

| Concern | Who handles it |
|---|---|
| MCP transport | `mcp_server` integration |
| OAuth 2.0 + PKCE, Client ID Metadata Documents | Home Assistant |
| Remote access over HTTPS | Home Assistant Cloud, or your own method |
| Credential storage | Home Assistant config entries |
| Process lifecycle | Home Assistant |

The alternative — running a standalone MCP server — means building and
operating all of the above yourself.

## Status

**Early. One tool, deliberately.**

`paprika__get_grocery_lists` exists to prove the whole chain end to end:
custom LLM API registration → exposure via `mcp_server` → remote reachability
→ OAuth from the client. The remaining tools are ported once that holds.

## Requirements

- Home Assistant 2026.10 or newer
- The [Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
  integration, configured to expose this API
- A way for the client to reach your instance (Home Assistant Cloud is the
  least troublesome)

## Installation

1. Copy `custom_components/paprika` into your Home Assistant `config/custom_components/`
   directory, or add this repository to HACS as a custom integration.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Paprika Recipe Manager**,
   and sign in with your Paprika account.
4. Open the **Model Context Protocol Server** integration's options and enable
   the **Paprika** API under *Control Home Assistant* (or leave *Expose all LLM
   APIs* on).

Your Paprika credentials are held in Home Assistant's config entry store. They
are never written to a file by this integration and are never returned by any
tool, so they cannot reach a model's context.

## Attribution

`custom_components/paprika/paprika_client.py` is vendored from
[paprika-mcp-python-server](https://github.com/sandordaroczi/paprika-mcp-python-server)
by Sándor Daróczi (MIT), via
[iantrich/paprika-mcp-python-server](https://github.com/iantrich/paprika-mcp-python-server),
which carries additional fixes including:

- ISO 8601 dates no longer parsed day-first (`2025-11-03` was read as 11 March)
- `remove_meal_from_plan()` no longer prefers the wrong meal type
- Ambiguous recipe titles raise instead of silently picking one
- `clear_grocery_list` corrected (wrong endpoint, then a missing delete flag)
- Recipe lookups cached on the sync index hash, turning a ~356-request library
  walk into one request

Both copyright notices are retained in `LICENSE`.
