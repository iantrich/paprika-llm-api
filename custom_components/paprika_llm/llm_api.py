"""Paprika tools exposed to LLMs as a Home Assistant LLM API.

Home Assistant's Model Context Protocol Server integration serves every
registered LLM API at `/api/mcp/{api_id}`, with OAuth 2.0 + PKCE and remote
access handled by Home Assistant itself. Registering an API is therefore all
this integration has to do to become reachable by a cloud MCP client -- there
is no server, transport, tunnel or OAuth code of our own.

Non-Assist APIs on those per-API endpoints require an admin Home Assistant
user, enforced in mcp_server regardless of its `require_admin` option.

Parameter schemas are converted for MCP clients by `probatio.to_openapi`, so
the `description=` on each field is what the model reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import probatio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN, TOOL_PREFIX
from .paprika_client import (
    MealMatchAmbiguousError,
    MealNotFoundError,
    PaprikaAPIError,
    PaprikaClient,
    ValidationError,
)

_LOGGER = logging.getLogger(__name__)

API_PROMPT = (
    "Use these tools to plan meals and manage the user's Paprika grocery list.\n"
    "Weekly planning: choose recipes with search_recipes, schedule them with "
    "add_meal_to_plan, then call get_meal_plan_ingredients for the week. Merge "
    "the ingredient lines into what actually needs buying -- combine the same "
    "ingredient across meals, apply each meal's scale, and drop anything the "
    "in-stock pantry already covers. Before adding, show the user the list and "
    "what you are skipping as pantry, and get their confirmation. Then add "
    "everything in one add_groceries call rather than one add_grocery per item.\n"
    "Meals of kind 'note' are free text with no recipe; ask whether the note "
    "itself is something to buy.\n"
    "When the user says they already have something, offer to add it to the "
    "pantry with add_pantry_items so future lists skip it.\n"
    "Removals match exact names; read the list or pantry first and use the "
    "names as shown.\n"
    "To edit a recipe, read it with read_recipe first, show the user exactly "
    "what will change, and confirm before calling update_recipe.\n"
    "Grocery aisle names are the user's own (see get_grocery_aisles)."
)

# Field descriptions shared by several tools.
LIST_NAME = "Name of the grocery list. Omit to use the default list."
MEAL_TYPES = ["breakfast", "lunch", "dinner"]
DATE_HELP = "Date as YYYY-MM-DD. Natural forms like 'Nov 3' are accepted."

# Annotation presets. The ToolAnnotations defaults describe the least safe
# case -- a destructive write reaching outside Home Assistant -- so anything
# safer has to say so explicitly.
READ_ONLY = llm.ToolAnnotations(
    read_only=True, destructive=False, idempotent=True, open_world=True
)
WRITE_ADDITIVE = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=False, open_world=True
)
WRITE_DESTRUCTIVE = llm.ToolAnnotations(
    read_only=False, destructive=True, idempotent=True, open_world=True
)
REMOVE_ONE = llm.ToolAnnotations(
    read_only=False, destructive=True, idempotent=False, open_world=True
)
# Saving a pantry item twice leaves the same state: quantities are replaced,
# not added, so unlike add_groceries this is idempotent.
WRITE_UPSERT = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=True, open_world=True
)


class PaprikaTool(llm.Tool):
    """Base for Paprika tools.

    `llm.Tool` is a plain class, so holding the client on the instance is
    fine here -- unlike `llm.API`, which is a slotted dataclass.
    """

    integration = DOMAIN

    def __init__(self, client: PaprikaClient) -> None:
        """Store the authenticated Paprika client this tool calls through."""
        self._client = client

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Run the tool, translating client failures into tool errors.

        Returns a ToolResult rather than a bare dict: Home Assistant accepts a
        JSON object today but deprecates it, and it stops working in 2027.11.

        Every client exception is caught here. MealNotFoundError and friends
        are not PaprikaAPIError subclasses, and anything that escapes becomes
        a transport failure the model cannot explain to the user.
        """
        try:
            return llm.ToolResult(data=await self._run(tool_input.tool_args))
        except MealMatchAmbiguousError as err:
            names = ", ".join(
                repr(m.get("name")) for m in getattr(err, "matches", []) or []
            )
            raise HomeAssistantError(
                f"{err}. Candidates: {names or 'none'}. Ask the user which one."
            ) from err
        except (MealNotFoundError, ValidationError) as err:
            raise HomeAssistantError(str(err)) from err
        except PaprikaAPIError as err:
            raise HomeAssistantError(f"Paprika request failed: {err}") from err

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Perform the tool's work. Implemented by each tool."""
        raise NotImplementedError


# ---------------------------------------------------------------- groceries


class GetGroceryListsTool(PaprikaTool):
    """List the grocery lists on the account."""

    name = f"{TOOL_PREFIX}get_grocery_lists"
    title = "Get Paprika grocery lists"
    description = (
        "List the grocery lists in Paprika, including which one is the default. "
        "Use this to discover list names before reading or adding items."
    )
    parameters = probatio.Schema({})
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return the account's grocery lists."""
        lists = await self._client.get_grocery_lists()
        return {
            "grocery_lists": [
                {
                    "name": entry.get("name"),
                    "uid": entry.get("uid"),
                    "is_default": bool(entry.get("is_default")),
                }
                for entry in lists
            ]
        }


class GetGroceryAislesTool(PaprikaTool):
    """List the grocery aisles defined on the account."""

    name = f"{TOOL_PREFIX}get_grocery_aisles"
    title = "Get Paprika grocery aisles"
    description = (
        "List the grocery aisles defined in Paprika, in store order. Aisle "
        "names are chosen by the user and may be numbers matching a store's "
        "layout (for example '1' to '6') rather than categories, so read them "
        "before choosing an aisle when adding groceries."
    )
    parameters = probatio.Schema({})
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return the account's aisles."""
        aisles = await self._client.get_grocery_aisles()
        return {"aisles": [a["name"] for a in aisles]}


class GetGroceriesTool(PaprikaTool):
    """Read every item on a grocery list."""

    name = f"{TOOL_PREFIX}get_groceries"
    title = "Get Paprika grocery items"
    description = (
        "Read all items currently on a Paprika grocery list, with quantities "
        "and aisles. This is the tool to use for 'what's on my grocery list?'."
    )
    parameters = probatio.Schema(
        {probatio.Optional("grocery_list_name", description=LIST_NAME): str}
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return the items on the requested list."""
        list_name = args.get("grocery_list_name")
        items = await self._client.get_groceries(grocery_list_name=list_name)
        return {
            "grocery_list": list_name or "default",
            "count": len(items),
            "items": [
                {
                    "name": item.get("name", ""),
                    "quantity": item.get("quantity", ""),
                    "aisle": item.get("aisle", ""),
                }
                for item in items
            ],
        }


class GetGroceryTool(PaprikaTool):
    """Look up one item on a grocery list."""

    name = f"{TOOL_PREFIX}get_grocery"
    title = "Get a Paprika grocery item"
    description = (
        "Look up a single item on a Paprika grocery list by name, to check "
        "whether it is already there and in what quantity."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("item_name", description="Item to look up."): str,
            probatio.Optional("grocery_list_name", description=LIST_NAME): str,
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return the matching item, or report that there is none."""
        item_name = args["item_name"]
        list_name = args.get("grocery_list_name")
        item = await self._client.get_grocery(
            item_name=item_name, grocery_list_name=list_name
        )
        if item is None:
            # Absence is an answer, not a failure.
            return {
                "found": False,
                "item_name": item_name,
                "grocery_list": list_name or "default",
            }
        return {
            "found": True,
            "grocery_list": list_name or "default",
            "item": {
                "name": item.get("name", ""),
                "quantity": item.get("quantity", ""),
                "purchased": bool(item.get("purchased")),
                "aisle": item.get("aisle"),
                "uid": item.get("uid"),
            },
        }


# Coerce rather than str for quantity and aisle: a model asked to "add 2
# milks" emits a JSON number, and numbered aisles invite {"aisle": 5}. A bare
# str rejects both before the tool runs. Verified against probatio 0.13.0,
# the version Home Assistant pins.
_ITEM_FIELDS = {
    probatio.Required("item_name", description="Grocery item, e.g. 'eggs'."): str,
    probatio.Optional(
        "quantity",
        default="1",
        description="Amount to add, as a number or with a unit, e.g. '2' or '1 lb'.",
    ): probatio.Coerce(str),
    probatio.Optional(
        "aisle",
        description=(
            "Aisle name from get_grocery_aisles. Omit to keep an existing "
            "item's aisle or use the aisle Paprika remembers for it."
        ),
    ): probatio.Coerce(str),
}


class AddGroceryTool(PaprikaTool):
    """Add one item to a grocery list."""

    name = f"{TOOL_PREFIX}add_grocery"
    title = "Add a Paprika grocery item"
    description = (
        "Add a single item to a Paprika grocery list. If the item is already on "
        "the list, the quantity is added to what is there. For more than one "
        "item, use add_groceries instead."
    )
    parameters = probatio.Schema(
        {
            **_ITEM_FIELDS,
            probatio.Optional("grocery_list_name", description=LIST_NAME): str,
        }
    )
    # Not idempotent: calling twice adds the quantity twice.
    annotations = WRITE_ADDITIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Add the item and report the resulting state."""
        result = await self._client.add_grocery(
            item_name=args["item_name"],
            quantity=args.get("quantity", "1"),
            grocery_list_name=args.get("grocery_list_name"),
            aisle=args.get("aisle"),
        )
        return {
            "added": True,
            "grocery_list": args.get("grocery_list_name") or "default",
            "item": result,
        }


class AddGroceriesTool(PaprikaTool):
    """Add many items to a grocery list in one write."""

    name = f"{TOOL_PREFIX}add_groceries"
    title = "Add Paprika grocery items"
    description = (
        "Add several items to a Paprika grocery list in a single write -- use "
        "this for a whole shopping list. Items already on the list have the "
        "quantity added to what is there; repeated items in the request are "
        "merged. An unknown aisle name fails the whole request without "
        "writing anything."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("items", description="Items to add."): [_ITEM_FIELDS],
            probatio.Optional("grocery_list_name", description=LIST_NAME): str,
        }
    )
    annotations = WRITE_ADDITIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Add all items and report where each one landed."""
        result = await self._client.add_groceries(
            items=args["items"], grocery_list_name=args.get("grocery_list_name")
        )
        return {
            "grocery_list": args.get("grocery_list_name") or "default",
            **result,
        }


class ClearGroceryListTool(PaprikaTool):
    """Remove every item from a grocery list."""

    name = f"{TOOL_PREFIX}clear_grocery_list"
    title = "Clear a Paprika grocery list"
    description = (
        "Remove every item from a Paprika grocery list. This cannot be undone "
        "from here, so confirm with the user before calling it."
    )
    parameters = probatio.Schema(
        {probatio.Optional("grocery_list_name", description=LIST_NAME): str}
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Clear the list and report how many items were removed."""
        list_name = args.get("grocery_list_name")

        # Counted before clearing so the model can tell the user what went,
        # which a bare boolean from the client cannot convey.
        try:
            before = len(await self._client.get_groceries(grocery_list_name=list_name))
        except PaprikaAPIError:
            before = None

        cleared = await self._client.clear_grocery_list(grocery_list_name=list_name)
        return {
            "cleared": bool(cleared),
            "grocery_list": list_name or "default",
            "items_removed": before,
        }


class RemoveGroceriesTool(PaprikaTool):
    """Remove specific items from a grocery list."""

    name = f"{TOOL_PREFIX}remove_groceries"
    title = "Remove Paprika grocery items"
    description = (
        "Remove specific items from a Paprika grocery list, matched by exact "
        "name (case-insensitive) as shown by get_groceries. Names that match "
        "nothing are reported back rather than guessed at. To empty the whole "
        "list, use clear_grocery_list instead."
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "names", description="Item names exactly as they appear on the list."
            ): [str],
            probatio.Optional("grocery_list_name", description=LIST_NAME): str,
        }
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Remove the items and report what was and wasn't found."""
        result = await self._client.remove_groceries(
            names=args["names"], grocery_list_name=args.get("grocery_list_name")
        )
        return {"grocery_list": args.get("grocery_list_name") or "default", **result}


# ------------------------------------------------------------------- pantry


class GetPantryTool(PaprikaTool):
    """Read the pantry."""

    name = f"{TOOL_PREFIX}get_pantry"
    title = "Get Paprika pantry"
    description = (
        "List what is in the user's Paprika pantry. By default only items "
        "marked in stock, which are the ones a shopping list can skip."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "in_stock_only",
                default=True,
                description="Only items marked in stock. False includes ones marked as run out.",
            ): bool,
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return pantry items."""
        items = await self._client.get_pantry(
            in_stock_only=args.get("in_stock_only", True)
        )
        return {"count": len(items), "items": items}


class AddPantryItemsTool(PaprikaTool):
    """Add or update pantry items."""

    name = f"{TOOL_PREFIX}add_pantry_items"
    title = "Add Paprika pantry items"
    description = (
        "Add items to the Paprika pantry, or update ones already there, in a "
        "single write. Use it when the user says they already have something, "
        "so future shopping lists skip it. An item already in the pantry is "
        "updated rather than duplicated. Set in_stock to false to mark an item "
        "as run out without removing it."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("items", description="Pantry items to save."): [
                {
                    probatio.Required("name", description="Ingredient, e.g. 'yellow mustard'."): str,
                    probatio.Optional(
                        "quantity",
                        description=(
                            "How much is on hand, e.g. '2 jars'. Replaces the "
                            "stored amount; omit to leave it unchanged."
                        ),
                    ): probatio.Coerce(str),
                    probatio.Optional(
                        "aisle",
                        description="Aisle name from get_grocery_aisles. Omit to keep or infer it.",
                    ): probatio.Coerce(str),
                    probatio.Optional(
                        "in_stock",
                        default=True,
                        description="Whether the item is on hand. False marks it as run out.",
                    ): bool,
                }
            ],
        }
    )
    annotations = WRITE_UPSERT

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Save the items and report each one."""
        return await self._client.add_pantry_items(items=args["items"])


class RemovePantryItemsTool(PaprikaTool):
    """Remove pantry items."""

    name = f"{TOOL_PREFIX}remove_pantry_items"
    title = "Remove Paprika pantry items"
    description = (
        "Remove items from the Paprika pantry entirely, matched by exact name "
        "(case-insensitive) as shown by get_pantry. Names that match nothing "
        "are reported back rather than guessed at. To record that something "
        "has simply run out, prefer add_pantry_items with in_stock false."
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "names", description="Pantry item names exactly as get_pantry shows them."
            ): [str],
        }
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Remove the items and report what was and wasn't found."""
        return await self._client.remove_pantry_items(names=args["names"])


# ------------------------------------------------------------------ recipes


class SearchRecipesTool(PaprikaTool):
    """Find recipes to plan with."""

    name = f"{TOOL_PREFIX}search_recipes"
    title = "Search Paprika recipes"
    description = (
        "Search the user's Paprika recipes, or list them all when no query is "
        "given. Each result includes when it was last planned, which helps "
        "vary the week's meals. Ingredient text is not returned; use "
        "get_meal_plan_ingredients once meals are planned."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "query",
                description="Text to look for, e.g. 'chicken'. Omit to list every recipe.",
            ): str,
            probatio.Optional(
                "search_fields",
                default="all",
                description="Where to look for the query.",
            ): probatio.In(["name", "ingredients", "description", "all"]),
            probatio.Optional(
                "limit", default=50, description="Maximum results."
            ): probatio.Coerce(int),
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return matching recipes, compactly."""
        # list_recipes is served from the hash-keyed recipe cache, so repeated
        # searches during a planning session cost one index request each.
        recipes = await self._client.list_recipes(limit=1000)

        query = (args.get("query") or "").strip().casefold()
        fields = args.get("search_fields", "all")
        searched = (
            ["name", "ingredients", "description"] if fields == "all" else [fields]
        )

        def matches(recipe: dict[str, Any]) -> bool:
            if not query:
                return True
            return any(
                query in str(recipe.get(field) or "").casefold() for field in searched
            )

        found = sorted(
            (r for r in recipes if matches(r)),
            key=lambda r: str(r.get("name", "")).casefold(),
        )
        limit = max(1, int(args.get("limit", 50)))
        return {
            "query": args.get("query"),
            "total_matches": len(found),
            "recipes": [
                {
                    "name": r.get("name"),
                    "uid": r.get("uid"),
                    "total_time": r.get("total_time") or None,
                    "servings": r.get("servings") or None,
                    "rating": r.get("rating") or None,
                    "last_planned": r.get("last_planned"),
                }
                for r in found[:limit]
            ],
        }


# Fields a recipe can be created with or edited. Ingredients are taken as a
# list (one per line in Paprika) so the model never has to escape newlines.
_RECIPE_TEXT_FIELDS = {
    probatio.Optional("description", description="Short description."): str,
    probatio.Optional("notes", description="Cooking notes."): str,
    probatio.Optional(
        "servings", description="Number of servings, as a whole number, e.g. '4'."
    ): probatio.Coerce(str),
    probatio.Optional("prep_time", description="Prep time, e.g. '15 mins'."): str,
    probatio.Optional("cook_time", description="Cook time, e.g. '30 mins'."): str,
    probatio.Optional("difficulty", description="Easy, Medium or Hard."): str,
}


def _recipe_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Map tool arguments onto client fields, joining ingredient lines."""
    fields = {
        key: args[key]
        for key in (
            "name", "directions", "description", "notes",
            "servings", "prep_time", "cook_time", "difficulty",
        )
        if key in args
    }
    if "ingredients" in args:
        fields["ingredients"] = "\n".join(
            line.strip() for line in args["ingredients"] if line.strip()
        )
    return fields


class ReadRecipeTool(PaprikaTool):
    """Read one recipe in full."""

    name = f"{TOOL_PREFIX}read_recipe"
    title = "Read a Paprika recipe"
    description = (
        "Read a recipe's full content -- ingredients, directions, notes, times. "
        "Use this before editing a recipe, so the edit starts from what is "
        "actually there."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional("uid", description="Recipe uid from search_recipes (preferred)."): str,
            probatio.Optional("title", description="Exact recipe title, if no uid."): str,
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return the recipe, or report that it was not found."""
        if args.get("uid"):
            recipe = await self._client.get_recipe_by_uid(args["uid"])
        elif args.get("title"):
            recipe = await self._client.get_recipe_by_title(args["title"])
        else:
            raise ValidationError("Provide a recipe uid or title.")
        if not recipe:
            return {"found": False, "uid": args.get("uid"), "title": args.get("title")}
        return {
            "found": True,
            "uid": recipe.get("uid"),
            "name": recipe.get("name"),
            "ingredients": [
                line.strip()
                for line in str(recipe.get("ingredients") or "").splitlines()
                if line.strip()
            ],
            "directions": recipe.get("directions") or "",
            "description": recipe.get("description") or "",
            "notes": recipe.get("notes") or "",
            "servings": recipe.get("servings") or "",
            "prep_time": recipe.get("prep_time") or "",
            "cook_time": recipe.get("cook_time") or "",
            "total_time": recipe.get("total_time") or "",
            "difficulty": recipe.get("difficulty") or "",
            "source": recipe.get("source") or "",
            "source_url": recipe.get("source_url") or "",
            "rating": recipe.get("rating") or None,
        }


class CreateRecipeTool(PaprikaTool):
    """Add a new recipe."""

    name = f"{TOOL_PREFIX}create_recipe"
    title = "Create a Paprika recipe"
    description = (
        "Add a new recipe to Paprika. A recipe with exactly the same name is "
        "refused, so check with search_recipes first; to change an existing "
        "recipe use update_recipe instead."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("name", description="Recipe name."): str,
            probatio.Required(
                "ingredients", description="Ingredient lines, e.g. ['2 cups flour', '3 eggs']."
            ): [str],
            probatio.Required(
                "directions", description="Method, as text; separate steps with newlines."
            ): str,
            **_RECIPE_TEXT_FIELDS,
        }
    )
    annotations = WRITE_ADDITIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Create the recipe and return its uid."""
        fields = _recipe_payload(args)
        # No image generation: the integration runs without a Replicate key,
        # and a paid image call is not something to trigger implicitly.
        recipe = await self._client.create_recipe(generate_image=False, **fields)
        return {"created": True, "uid": recipe.get("uid"), "name": recipe.get("name")}


class UpdateRecipeTool(PaprikaTool):
    """Edit an existing recipe."""

    name = f"{TOOL_PREFIX}update_recipe"
    title = "Edit a Paprika recipe"
    description = (
        "Change an existing recipe. Send only the fields that change; "
        "everything else -- including photo, categories, rating and source -- "
        "is kept. Supplying ingredients replaces the whole ingredient list, so "
        "read the recipe first and send the full edited list. Show the user the "
        "change and confirm before calling this."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("uid", description="Uid of the recipe to edit."): str,
            probatio.Optional("name", description="New name."): str,
            probatio.Optional(
                "ingredients", description="The complete new ingredient list."
            ): [str],
            probatio.Optional("directions", description="The complete new method."): str,
            **_RECIPE_TEXT_FIELDS,
        }
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Apply the edit and report which fields changed."""
        fields = _recipe_payload(args)
        if not fields:
            raise ValidationError("Nothing to change: give at least one field.")
        # update_recipe_partial fetches the stored recipe and changes only these
        # fields. The client's update_recipe rebuilds the recipe from scratch
        # and would drop photo, categories, rating and source -- never use it.
        recipe = await self._client.update_recipe_partial(args["uid"], **fields)
        return {
            "updated": True,
            "uid": recipe.get("uid", args["uid"]),
            "name": recipe.get("name"),
            "changed_fields": sorted(fields),
        }


# --------------------------------------------------------------- meal plan

_WINDOW_FIELDS = {
    probatio.Optional("start_date", description=f"First day. {DATE_HELP}"): str,
    probatio.Optional("end_date", description=f"Last day. {DATE_HELP}"): str,
    probatio.Optional(
        "num_days",
        description="Days from today, when no dates are given. Defaults to 10.",
    ): probatio.Coerce(int),
    probatio.Optional(
        "meal_type", description="Only this meal. Omit for all meals."
    ): probatio.In(MEAL_TYPES),
}


class ListMealPlanTool(PaprikaTool):
    """Read the meal plan."""

    name = f"{TOOL_PREFIX}list_meal_plan"
    title = "List Paprika meal plan"
    description = (
        "List planned meals in a date window. Meals of kind 'note' are free "
        "text with no recipe attached."
    )
    parameters = probatio.Schema(_WINDOW_FIELDS)
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return the planned meals."""
        meals = await self._client.get_meal_plan(
            num_days=args.get("num_days"),
            meal_type=args.get("meal_type"),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
        )
        return {
            "meals": [
                {
                    "date": m.get("date"),
                    "meal_type": m.get("type"),
                    "name": str(m.get("meal", "")).strip(),
                    "kind": "recipe" if m.get("meal_ID") else "note",
                    "recipe_uid": m.get("meal_ID") or None,
                    "scale": m.get("scale"),
                }
                for m in meals
            ]
        }


class GetMealPlanIngredientsTool(PaprikaTool):
    """Gather what's needed to build a shopping list for planned meals."""

    name = f"{TOOL_PREFIX}get_meal_plan_ingredients"
    title = "Get ingredients for planned meals"
    description = (
        "For every meal planned in a date window, return its recipe's "
        "ingredient lines, plus everything the user has in stock in their "
        "Paprika pantry. Use this to build the week's shopping list: merge "
        "ingredients across meals, apply each meal's scale, drop what the "
        "pantry covers, confirm with the user, then call add_groceries once."
    )
    parameters = probatio.Schema(_WINDOW_FIELDS)
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Return planned meals with ingredients, and the in-stock pantry."""
        return await self._client.get_meal_plan_ingredients(
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
            num_days=args.get("num_days"),
            meal_type=args.get("meal_type"),
        )


class AddMealToPlanTool(PaprikaTool):
    """Schedule a recipe."""

    name = f"{TOOL_PREFIX}add_meal_to_plan"
    title = "Add a meal to the Paprika plan"
    description = (
        "Schedule a recipe on a date. Identify the recipe by uid (from "
        "search_recipes) or by name; an ambiguous name is refused with the "
        "candidates rather than guessed. Without a date, the next day with no "
        "meal of that type is used."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional("recipe_uid", description="Recipe uid from search_recipes."): str,
            probatio.Optional("recipe_name", description="Recipe name, if no uid."): str,
            probatio.Optional("date", description=DATE_HELP): str,
            probatio.Optional(
                "meal_type", default="dinner", description="Which meal."
            ): probatio.In(MEAL_TYPES),
        }
    )
    annotations = WRITE_ADDITIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Add the meal and report what was scheduled."""
        if not args.get("recipe_uid") and not args.get("recipe_name"):
            raise ValidationError("Provide recipe_uid or recipe_name.")
        result = await self._client.add_meal_to_plan(
            meal=args.get("recipe_name"),
            meal_id=args.get("recipe_uid"),
            date=args.get("date"),
            meal_type=args.get("meal_type", "dinner"),
        )
        return {
            "scheduled": True,
            "date": result.get("date"),
            "meal_type": result.get("type"),
            "recipe_name": result.get("meal"),
            "recipe_uid": result.get("meal_ID"),
        }


class RemoveMealFromPlanTool(PaprikaTool):
    """Unschedule one meal."""

    name = f"{TOOL_PREFIX}remove_meal_from_plan"
    title = "Remove a meal from the Paprika plan"
    description = (
        "Remove one planned meal. Identify it by date, by recipe, or both; "
        "giving both is the safest. This removes the meal from the plan, not "
        "the recipe."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional("date", description=DATE_HELP): str,
            probatio.Optional("recipe_uid", description="Recipe uid of the meal."): str,
            probatio.Optional("recipe_name", description="Name of the meal."): str,
            probatio.Optional(
                "meal_type", default="dinner", description="Which meal."
            ): probatio.In(MEAL_TYPES),
        }
    )
    annotations = REMOVE_ONE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Remove the meal and report which one went."""
        # The client treats no identifier at all as "the latest planned meal".
        # That is too loose for a model to trigger by omission, so it is
        # refused here.
        if not (args.get("date") or args.get("recipe_uid") or args.get("recipe_name")):
            raise ValidationError(
                "Say which meal to remove: give a date, a recipe, or both."
            )
        result = await self._client.remove_meal_from_plan(
            meal=args.get("recipe_name"),
            meal_id=args.get("recipe_uid"),
            date=args.get("date"),
            meal_type=args.get("meal_type", "dinner"),
        )
        return {
            "removed": True,
            "date": result.get("date"),
            "meal_type": result.get("type"),
            "name": result.get("meal"),
        }


# --------------------------------------------------------------------- API


@dataclass(slots=True, kw_only=True)
class PaprikaLLMAPI(llm.API):
    """The Paprika API, as offered to LLM clients.

    `llm.API` is a slotted dataclass, so `client` must be a declared field --
    assigning it to an instance afterwards raises AttributeError.
    """

    client: PaprikaClient

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Build the tool set for one request."""
        # Built per request, so the date is always current. A model planning
        # "this week" has no other reliable source for today's date.
        today = dt_util.now()
        prompt = f"Today is {today:%A, %Y-%m-%d}.\n{API_PROMPT}"
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=_build_tools(self.client),
        )


def _build_tools(client: PaprikaClient) -> list[llm.Tool]:
    """Return every Paprika tool, bound to the given client."""
    return [
        tool(client)
        for tool in (
            # groceries
            GetGroceryListsTool,
            GetGroceryAislesTool,
            GetGroceriesTool,
            GetGroceryTool,
            AddGroceryTool,
            AddGroceriesTool,
            RemoveGroceriesTool,
            ClearGroceryListTool,
            # pantry
            GetPantryTool,
            AddPantryItemsTool,
            RemovePantryItemsTool,
            # recipes
            SearchRecipesTool,
            ReadRecipeTool,
            CreateRecipeTool,
            UpdateRecipeTool,
            # meal plan
            ListMealPlanTool,
            GetMealPlanIngredientsTool,
            AddMealToPlanTool,
            RemoveMealFromPlanTool,
        )
    ]


def async_register(
    hass: HomeAssistant, api_id: str, api_name: str, client: PaprikaClient
) -> Any:
    """Register the Paprika LLM API and return the unregister callback."""
    _LOGGER.debug("Registering Paprika LLM API as %s", api_id)
    return llm.async_register_api(
        hass,
        PaprikaLLMAPI(hass=hass, id=api_id, name=api_name, client=client),
    )
