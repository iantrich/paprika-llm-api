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

Built around one workflow: **plan the week's meals, then put everything
they need on the grocery list, minus what's already in the pantry.**

1. Pick recipes with `search_recipes` and schedule them with `add_meal_to_plan`.
2. `get_meal_plan_ingredients` returns every planned meal's ingredient lines
   plus the in-stock pantry, in a handful of requests.
3. The model merges ingredients across meals, applies each meal's scale, drops
   what the pantry covers, and shows you the result — including what it
   skipped — before writing anything.
4. `add_groceries` writes the whole list in one request.

When you say you already have something, it can go into the pantry with
`add_pantry_items` so later lists skip it. Removals — from the list or the
pantry — match exact names only, and anything that doesn't match is reported
back rather than guessed at.

Ingredient lines are free text (`2 tablespoons soy sauce` against a pantry
entry `soy sauce`), so merging and pantry matching are left to the model's
judgment rather than a string-matching parser. The tools do data access.

| Tool | Behaviour |
|---|---|
| `paprika_llm__search_recipes` | read-only; lists everything when no query is given |
| `paprika_llm__read_recipe` | read-only; full ingredients, method and notes |
| `paprika_llm__create_recipe` | write; refuses a name that already exists |
| `paprika_llm__update_recipe` | write, **destructive**; changes only the fields given |
| `paprika_llm__list_meal_plan` | read-only |
| `paprika_llm__get_meal_plan_ingredients` | read-only; ingredients for planned meals, plus in-stock pantry |
| `paprika_llm__add_meal_to_plan` | write; refuses ambiguous recipe names rather than guessing |
| `paprika_llm__remove_meal_from_plan` | write, **destructive**; requires a date or recipe |
| `paprika_llm__get_grocery_lists` | read-only |
| `paprika_llm__get_grocery_aisles` | read-only |
| `paprika_llm__get_groceries` | read-only |
| `paprika_llm__get_grocery` | read-only |
| `paprika_llm__add_grocery` | write, additive; optional `aisle` |
| `paprika_llm__add_groceries` | write, additive; many items in one request |
| `paprika_llm__remove_groceries` | write, **destructive**; exact names, near-misses reported |
| `paprika_llm__clear_grocery_list` | write, **destructive** |
| `paprika_llm__get_pantry` | read-only; in-stock items by default |
| `paprika_llm__add_pantry_items` | write; adds or updates, can mark items as run out |
| `paprika_llm__remove_pantry_items` | write, **destructive**; exact names, near-misses reported |

Meals planned as free-text notes (no recipe attached) are returned as kind
`note` with no ingredients, not dropped.

Recipe edits fetch the stored recipe and change only the fields supplied, so
photo, categories, rating and source survive, and the date the recipe was
added is left alone. Creating a recipe never triggers image generation.

`delete_recipe` is deliberately omitted and will stay omitted.

### Request budget

The Paprika API rate-limits. `add_groceries` reads the list, the aisles and
the ingredient-aisle memory once each and posts every row together, so a
40-item shopping list is four requests rather than well over a hundred.

Recipe listing is cached on the sync index hash. The first search after Home
Assistant starts walks the whole recipe index once (one request per recipe,
trashed ones included); later searches cost a single request.

### Aisles

`add_grocery` and `add_groceries` take an optional `aisle`, matched
case-insensitively against the account's own aisle names (which may be numbers
like `1`–`6` rather than categories — `get_grocery_aisles` lists them).
`"aisle 5"` matches an aisle named `5`. An unknown aisle is an error that
lists the real ones, and in a batch it fails the whole request before
anything is written. Aisles are never created implicitly.

Without an aisle:

1. an item already on the list **keeps its current aisle**;
2. a new item goes where Paprika remembers that ingredient belongs
   (`/sync/groceryingredients/` — the same memory the Paprika apps sort by);
3. otherwise it goes to `Miscellaneous`.

## Not the same as `rbrunt/paprika-ha`

This integration uses the domain `paprika_llm`, deliberately. The unofficial
[Paprika Recipes](https://github.com/rbrunt/paprika-ha) integration uses
`paprika`, and two integrations cannot share a domain — HACS installs both into
`custom_components/<domain>/`, so a collision silently overwrites one with the
other and Home Assistant then hands one integration's config entry to the
other's code.

The two do different things and can be installed side by side: that one
exposes the meal plan as calendar entities; this one exposes tools to MCP
clients.

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

## Development

```bash
pip install homeassistant==2026.10.2 mcp==1.28.1 aiohttp_sse==2.2.0 \
    hassil==3.12.1 home-assistant-intents==2026.10.6 gazetteer-matcher==1.1.0 \
    python-dateutil pytest pytest-asyncio
pytest -p no:anyio
```

`tests/test_tools.py` renders every tool through Home Assistant's own
`mcp_server` formatter, so a schema the client cannot use, or a parameter
without a description, fails the suite.

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
