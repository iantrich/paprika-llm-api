"""Paprika tools exposed to LLMs as a Home Assistant LLM API.

Home Assistant's Model Context Protocol Server integration serves every
registered LLM API at `/api/mcp/{api_id}`, with OAuth 2.0 + PKCE and remote
access handled by Home Assistant itself. Registering an API is therefore all
this integration has to do to become reachable by a cloud MCP client -- there
is no server, transport, tunnel or OAuth code of our own.

Non-Assist APIs on those per-API endpoints require an admin Home Assistant
user, enforced in mcp_server regardless of its `require_admin` option.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import probatio
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import DOMAIN, TOOL_PREFIX
from .paprika_client import PaprikaAPIError, PaprikaClient

_LOGGER = logging.getLogger(__name__)

API_PROMPT = (
    "Use these tools to read and manage the user's Paprika recipe collection, "
    "grocery lists and meal plan. Grocery list names are matched "
    "case-insensitively; omit the list name to use the default list."
)

# Every grocery tool accepts an optional list name. Paprika accounts usually
# have one list, so omitting it and letting the client resolve the default is
# the common path.
LIST_NAME_DESCRIPTION = (
    "Name of the grocery list. Omit to use the account's default list."
)

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
    ) -> JsonObjectType:
        """Run the tool, translating Paprika failures into tool errors."""
        try:
            return await self._run(tool_input.tool_args)
        except PaprikaAPIError as err:
            # Raised as a tool error rather than left to escape as a transport
            # failure, so the model can explain itself to the user.
            raise HomeAssistantError(f"Paprika request failed: {err}") from err

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        """Perform the tool's work. Implemented by each tool."""
        raise NotImplementedError


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


class GetGroceriesTool(PaprikaTool):
    """Read every item on a grocery list."""

    name = f"{TOOL_PREFIX}get_groceries"
    title = "Get Paprika grocery items"
    description = (
        "Read all items currently on a Paprika grocery list, with quantities. "
        "This is the tool to use for 'what's on my grocery list?'."
    )
    parameters = probatio.Schema(
        {probatio.Optional("grocery_list_name"): str}
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
                {"name": item.get("name", ""), "quantity": item.get("quantity", "")}
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
            probatio.Required("item_name"): str,
            probatio.Optional("grocery_list_name"): str,
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


class AddGroceryTool(PaprikaTool):
    """Add an item to a grocery list."""

    name = f"{TOOL_PREFIX}add_grocery"
    title = "Add a Paprika grocery item"
    description = (
        "Add an item to a Paprika grocery list. If the item is already on the "
        "list, the quantity is added to what is there rather than replacing it."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("item_name"): str,
            # Coerce rather than str: an LLM asked to "add 2 milks" emits a
            # JSON number, which a bare str rejects before the tool runs.
            # Verified against probatio 0.13.0 for int, float and str.
            probatio.Optional("quantity", default="1"): probatio.Coerce(str),
            probatio.Optional("grocery_list_name"): str,
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
        )
        return {
            "added": True,
            "grocery_list": args.get("grocery_list_name") or "default",
            "item": result,
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
        {probatio.Optional("grocery_list_name"): str}
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
        return llm.APIInstance(
            api=self,
            api_prompt=API_PROMPT,
            llm_context=llm_context,
            tools=_build_tools(self.client),
        )


def _build_tools(client: PaprikaClient) -> list[llm.Tool]:
    """Return every Paprika tool, bound to the given client.

    Groceries are complete. Recipes and meal planning are ported next.
    """
    return [
        GetGroceryListsTool(client),
        GetGroceriesTool(client),
        GetGroceryTool(client),
        AddGroceryTool(client),
        ClearGroceryListTool(client),
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
