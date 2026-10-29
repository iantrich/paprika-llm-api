"""The Paprika LLM API integration.

Exposes Paprika recipe, grocery and meal-plan tools to LLM clients by
registering a Home Assistant LLM API. Home Assistant's Model Context Protocol
Server integration then serves them over `/api/mcp`, so this integration owns
no transport, no OAuth and no remote-access configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from . import llm_api
from .const import DOMAIN, LLM_API_ID, LLM_API_NAME
from .paprika_client import PaprikaAPIError, PaprikaClient

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Batch cache writes: a recipe walk changes many entries in quick succession.
CACHE_SAVE_DELAY = 30


@dataclass
class PaprikaRuntime:
    """What a loaded entry holds on to."""

    client: PaprikaClient
    cache_store: Store


type PaprikaConfigEntry = ConfigEntry[PaprikaRuntime]


def _cache_store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    """Storage for this entry's recipe cache, in Home Assistant's .storage.

    Persisting the cache is what keeps a restart from re-walking the whole
    recipe library -- one request per recipe -- which runs into Paprika's rate
    limit. The sync index hash keeps a loaded cache correct: a recipe changed
    while Home Assistant was down arrives with a new hash and is refetched.
    """
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.recipe_cache.{entry.entry_id}")


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

    cache_store = _cache_store(hass, entry)
    try:
        stored = await cache_store.async_load()
    except Exception:  # noqa: BLE001 - a bad cache file only costs a refetch
        _LOGGER.warning("Ignoring unreadable Paprika recipe cache")
        stored = None
    if stored:
        loaded = client.import_recipe_cache(stored)
        _LOGGER.debug("Loaded %s cached Paprika recipes", loaded)
    client.on_recipe_cache_changed = lambda: cache_store.async_delay_save(
        client.export_recipe_cache, CACHE_SAVE_DELAY
    )

    entry.runtime_data = PaprikaRuntime(client=client, cache_store=cache_store)
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
    runtime = entry.runtime_data
    # Write the cache now rather than leave it to the delayed save, which an
    # unload would otherwise abandon.
    await runtime.cache_store.async_save(runtime.client.export_recipe_cache())
    await runtime.client.close()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored recipe cache when the integration is removed."""
    await _cache_store(hass, entry).async_remove()
