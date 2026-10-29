# Paprika LLM API — Home Assistant

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

**Groceries are complete. Recipes and meal planning are next.**

| Tool | Behaviour |
|---|---|
| `paprika_llm__get_grocery_lists` | read-only |
| `paprika_llm__get_grocery_aisles` | read-only |
| `paprika_llm__get_groceries` | read-only |
| `paprika_llm__get_grocery` | read-only |
| `paprika_llm__add_grocery` | write, additive, not idempotent; optional `aisle` |
| `paprika_llm__clear_grocery_list` | write, **destructive** |

Each tool declares Home Assistant `ToolAnnotations`. The defaults describe the
least safe case, so reads opt in to `read_only` explicitly and
`clear_grocery_list` is marked `destructive` for clients that surface it.

### Aisles

`add_grocery` takes an optional `aisle`, matched case-insensitively against
the account's own aisle names (which may be numbers like `1`–`6` rather than
categories — `get_grocery_aisles` lists them). `"aisle 5"` matches an aisle
named `5`. An unknown aisle is an error that lists the real ones; aisles are
never created implicitly.

Without an aisle:

1. an item already on the list **keeps its current aisle**;
2. a new item goes where Paprika remembers that ingredient belongs
   (`/sync/groceryingredients/` — the same memory the Paprika apps sort by);
3. otherwise it goes to `Miscellaneous`.

Still to port: the six recipe tools (`list_recipes`, `read_recipe`,
`search_recipes`, `filter_recipes_by_ingredient`, `filter_recipes_by_time`,
`create_recipe`, `update_recipe`) and the three meal-plan tools
(`list_meal_plan`, `add_meal_to_plan`, `remove_meal_from_plan`).

`delete_recipe` is deliberately omitted and will stay omitted.

## Requirements

- Home Assistant 2026.10 or newer
- The [Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
  integration, configured to expose this API
- A way for the client to reach your instance (Home Assistant Cloud is the
  least troublesome)

## Installation

1. Copy `custom_components/paprika_llm` into your Home Assistant `config/custom_components/`
   directory, or add this repository to HACS as a custom integration.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Paprika LLM API**,
   and sign in with your Paprika account.
4. Open the **Model Context Protocol Server** integration's options and enable
   the **Paprika** API under *Control Home Assistant* (or leave *Expose all LLM
   APIs* on).

Your Paprika credentials are held in Home Assistant's config entry store. They
are never written to a file by this integration and are never returned by any
tool, so they cannot reach a model's context.

## Attribution

`custom_components/paprika_llm/paprika_client.py` is vendored from
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
