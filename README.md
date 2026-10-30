# Paprika LLM API — Home Assistant

A Home Assistant custom integration that gives LLM clients, such as Claude,
tools for your [Paprika Recipe Manager](https://www.paprikaapp.com/) account:
recipes, meal plan, grocery lists and pantry.

It registers a Home Assistant **LLM API**. Home Assistant's
[Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
integration serves it to MCP clients, with sign-in handled by Home Assistant
and remote access through Home Assistant Cloud (Nabu Casa) or your own setup.

## What you can do

- **Plan meals:** search your recipes, schedule them on the meal plan, and add
  side dishes as notes.
- **Build the grocery list:** gather the ingredients for every planned meal,
  skip what's in your pantry, and add the rest in one go, sorted into your
  aisles.
- **Manage the pantry:** add items, mark them as run out, or remove them.
- **Manage recipes:** read, create and edit recipes, or import one from a
  recipe web page with its photo, source and link.

A typical weekly flow:

1. Pick recipes with `search_recipes` and schedule them with
   `add_meal_to_plan`; add sides with `add_note_to_plan`.
2. `get_meal_plan_ingredients` returns every planned meal's ingredient lines
   plus the in-stock pantry.
3. The model merges the ingredients, applies each meal's scale, drops what the
   pantry covers, and shows you the list before writing anything.
4. `add_groceries` adds the whole list in one request.

## Tools

| Tool | Behaviour |
|---|---|
| `paprika_llm__search_recipes` | read-only; lists everything when no query is given |
| `paprika_llm__read_recipe` | read-only; full ingredients, method and notes |
| `paprika_llm__create_recipe` | write; refuses a name that already exists |
| `paprika_llm__import_recipe_from_url` | preview by default; `save=true` writes, with the page's photo, source and link; refuses a page already imported |
| `paprika_llm__update_recipe` | write, **destructive**; changes only the fields given |
| `paprika_llm__list_meal_plan` | read-only; recipes and notes, in their order for the day |
| `paprika_llm__get_meal_plan_ingredients` | read-only; ingredients for planned meals, plus in-stock pantry |
| `paprika_llm__add_meal_to_plan` | write; refuses ambiguous recipe names |
| `paprika_llm__add_note_to_plan` | write; free-text entry such as a side dish, listed after the day's existing meals |
| `paprika_llm__remove_meal_from_plan` | write, **destructive**; removes a meal or note; requires a date, recipe or note text |
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

Recipes cannot be deleted through these tools.

## Details

### Recipes

Editing a recipe changes only the fields supplied; photo, categories, rating,
source and the date it was added are kept.

`import_recipe_from_url` reads the schema.org `Recipe` data that most recipe
sites publish. Times are converted to Paprika's style (`PT1H10M` →
`1 hr 10 mins`), a yield such as `1 loaf` goes into the notes, and the page's
photo is attached when it is a JPEG, PNG or WebP. Only public http(s)
addresses are fetched, including after redirects. Some publishers (Allrecipes,
Simply Recipes, Serious Eats) block automated requests; import those with the
Paprika app's own browser.

### Grocery aisles

`add_grocery` and `add_groceries` take an optional `aisle`, matched
case-insensitively against your own aisle names (`get_grocery_aisles` lists
them); `"aisle 5"` matches an aisle named `5`. An unknown aisle is an error
that lists the real ones. Without an aisle:

1. an item already on the list keeps its current aisle;
2. a new item goes where Paprika remembers that ingredient belongs;
3. otherwise it goes to `Miscellaneous`.

### Rate limits and caching

Paprika rate-limits its API. Recipes are cached in Home Assistant's
`.storage` and refreshed only when they change in Paprika, so after the first
load a search costs about one request. If Paprika refuses requests part-way
through a load, the integration keeps what it fetched, reports how far it got,
and continues on the next call. Removing the integration deletes the cache.

### Credentials

Your Paprika credentials are stored in the Home Assistant config entry. They
are never written to a file by this integration and never returned by any
tool.

## Requirements

- Home Assistant 2026.10 or newer
- The [Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
  integration
- A way for your client to reach Home Assistant, such as Home Assistant Cloud

## Installation

1. Add this repository to HACS as a custom integration and download it, or
   copy `custom_components/paprika_llm` into `config/custom_components/`.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Paprika LLM API**, and
   sign in with your Paprika account.
4. Connect your MCP client to `https://<your-home-assistant>/api/mcp/paprika_llm`
   and sign in with a Home Assistant administrator account.

This integration's domain is `paprika_llm`, so it can be installed alongside
[rbrunt/paprika-ha](https://github.com/rbrunt/paprika-ha), which shows the
meal plan as calendar entities.

## Development

```bash
pip install homeassistant==2026.10.2 mcp==1.28.1 aiohttp_sse==2.2.0 \
    hassil==3.12.1 home-assistant-intents==2026.10.6 gazetteer-matcher==1.1.0 \
    python-dateutil pytest pytest-asyncio
pytest -p no:anyio
```

`tests/test_tools.py` renders every tool through Home Assistant's own
`mcp_server` formatter, so a schema an MCP client cannot use, or a parameter
without a description, fails the suite.

## Attribution

`custom_components/paprika_llm/paprika_client.py` is based on
[paprika-mcp-python-server](https://github.com/sandordaroczi/paprika-mcp-python-server)
by Sándor Daróczi (MIT). Both copyright notices are retained in `LICENSE`.
