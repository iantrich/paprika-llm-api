"""The Paprika LLM API integration.

Exposes Paprika recipe, grocery and meal-plan tools to LLM clients by
registering a Home Assistant LLM API. Home Assistant's Model Context Protocol
Server integration then serves them over `/api/mcp`, so this integration owns
no transport, no OAuth and no remote-access configuration.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from . import llm_api
from .const import LLM_API_ID, LLM_API_NAME
from .paprika_client import PaprikaAPIError, PaprikaClient

_LOGGER = logging.getLogger(__name__)

type PaprikaConfigEntry = ConfigEntry[PaprikaClient]


async def async_setup_entry(hass: HomeAssistant, entry: PaprikaConfigEntry) -> bool:
    """Authenticate with Paprika and register the LLM API."""
    client = PaprikaClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    try:
        await client.authenticate()
    except PaprikaAPIError as err:
        # The client closes its own session when authentication fails, so
        # there is nothing to clean up here.
        if "401" in str(err) or "login failed" in str(err).lower():
            raise ConfigEntryAuthFailed(f"Paprika rejected the credentials: {err}") from err
        raise ConfigEntryNotReady(f"Could not reach Paprika: {err}") from err
    except Exception as err:  # noqa: BLE001 - surface anything else as retryable
        await client.close()
        raise ConfigEntryNotReady(f"Could not set up Paprika: {err}") from err

    entry.runtime_data = client
    entry.async_on_unload(
        llm_api.async_register(hass, LLM_API_ID, LLM_API_NAME, client)
    )

    _LOGGER.info("Paprika LLM API registered as %s", LLM_API_ID)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PaprikaConfigEntry) -> bool:
    """Deregister the API and close the client session.

    The client is closed here and awaited rather than in an async_on_unload
    callback: a fire-and-forget close task can be abandoned if Home Assistant
    is shutting down, leaking the aiohttp session.
    """
    await entry.runtime_data.close()
    return True
