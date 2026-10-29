"""The integration's storage wiring for the recipe cache."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
import custom_components.paprika_llm as integration  # noqa: E402


class FakeStore:
    instances = []

    def __init__(self, hass, version, key):
        self.key, self.loaded_value = key, None
        self.async_load = AsyncMock(side_effect=lambda: self.loaded_value)
        self.async_save = AsyncMock()
        self.async_remove = AsyncMock()
        self.async_delay_save = MagicMock()
        FakeStore.instances.append(self)


@pytest.fixture
def env():
    FakeStore.instances = []
    client = MagicMock()
    client.authenticate = AsyncMock()
    client.close = AsyncMock()
    client.import_recipe_cache = MagicMock(return_value=3)
    client.export_recipe_cache = MagicMock(return_value={"recipes": {}})
    entry = MagicMock()
    entry.entry_id = "E1"
    entry.data = {"username": "u@example.com", "password": "pw"}
    with patch.object(integration, "Store", FakeStore), \
         patch.object(integration, "PaprikaClient", return_value=client), \
         patch.object(integration.llm_api, "async_register", return_value=lambda: None):
        yield client, entry


async def setup(entry, stored=None):
    original = FakeStore.__init__

    def init(self, *a):
        original(self, *a)
        self.loaded_value = stored

    with patch.object(FakeStore, "__init__", init):
        assert await integration.async_setup_entry(MagicMock(), entry)
    return FakeStore.instances[-1]


@pytest.mark.asyncio
async def test_stored_cache_is_loaded_into_the_client(env):
    client, entry = env
    store = await setup(entry, stored={"recipes": {"u": {}}})
    assert store.key == "paprika_llm.recipe_cache.E1"
    client.import_recipe_cache.assert_called_once_with({"recipes": {"u": {}}})


@pytest.mark.asyncio
async def test_empty_store_imports_nothing(env):
    client, entry = env
    await setup(entry, stored=None)
    client.import_recipe_cache.assert_not_called()


@pytest.mark.asyncio
async def test_cache_changes_schedule_a_delayed_save(env):
    client, entry = env
    store = await setup(entry)
    client.on_recipe_cache_changed()
    store.async_delay_save.assert_called_once_with(
        client.export_recipe_cache, integration.CACHE_SAVE_DELAY
    )


@pytest.mark.asyncio
async def test_unload_writes_the_cache_then_closes(env):
    client, entry = env
    store = await setup(entry)
    entry.runtime_data = integration.PaprikaRuntime(client=client, cache_store=store)
    assert await integration.async_unload_entry(MagicMock(), entry)
    store.async_save.assert_awaited_once_with({"recipes": {}})
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_unreadable_store_is_ignored(env):
    client, entry = env
    original = FakeStore.__init__

    def init(self, *a):
        original(self, *a)
        self.async_load = AsyncMock(side_effect=ValueError("corrupt"))

    with patch.object(FakeStore, "__init__", init):
        assert await integration.async_setup_entry(MagicMock(), entry)
    client.import_recipe_cache.assert_not_called()


@pytest.mark.asyncio
async def test_removing_the_integration_deletes_the_cache(env):
    _, entry = env
    await integration.async_remove_entry(MagicMock(), entry)
    FakeStore.instances[-1].async_remove.assert_awaited_once()
