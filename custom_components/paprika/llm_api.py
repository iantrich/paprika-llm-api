"""Paprika tools exposed to LLMs as a Home Assistant LLM API.

Home Assistant's Model Context Protocol Server integration serves whichever
LLM APIs are registered here over `/api/mcp`, with OAuth 2.0 + PKCE and
remote access handled by Home Assistant itself. Registering an API is
therefore all this integration has to do to become reachable by a cloud MCP
client -- there is no server, transport, tunnel or OAuth code of our own.
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
    "grocery lists and meal plan. Recipe titles are matched case-sensitively; "
    "if a title is ambiguous the tool will say so rather than guess."
)


class PaprikaTool(llm.Tool):
    """Base for Paprika tools.

    `llm.Tool` is a plain class, so holding the client on the instance is
    fine here -- unlike `llm.API`, which is a slotted dataclass.
    """

    def __init__(self, client: PaprikaClient) -> None:
        """Store the authenticated Paprika client this tool calls through."""
        self._client = client


class GetGroceryListsTool(PaprikaTool):
    """List the grocery lists on the account."""

    name = f"{TOOL_PREFIX}get_grocery_lists"
    title = "Get Paprika grocery lists"
    description = (
        "List the grocery lists in Paprika, including which one is the default. "
        "Use this to discover list names before reading or adding items."
    )
    parameters = probatio.Schema({})
    # ToolAnnotations defaults describe the least safe case -- a write that is
    # destructive and reaches outside Home Assistant -- so a read must opt in.
    annotations = llm.ToolAnnotations(
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
    )
    integration = DOMAIN

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return the account's grocery lists."""
        try:
            lists = await self._client.get_grocery_lists()
        except PaprikaAPIError as err:
            # Surfaced to the model as a tool error rather than a transport
            # failure, which is what lets it explain itself to the user.
            raise HomeAssistantError(f"Paprika request failed: {err}") from err

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

    Only one tool for now: this is a deliberate spike proving the whole chain
    (custom API registration -> MCP server exposure -> remote access -> OAuth
    from the client) before the remaining tools are ported over.
    """
    return [GetGroceryListsTool(client)]


def async_register(
    hass: HomeAssistant, api_id: str, api_name: str, client: PaprikaClient
) -> Any:
    """Register the Paprika LLM API and return the unregister callback."""
    _LOGGER.debug("Registering Paprika LLM API as %s", api_id)
    return llm.async_register_api(
        hass,
        PaprikaLLMAPI(hass=hass, id=api_id, name=api_name, client=client),
    )
