"""Aisle handling in add_grocery.

add_grocery used to hardcode every item to "Produce" with a fixed aisle_uid
that need not exist on the account, and re-adding an item moved it there.
These tests pin the replacement: requested -> existing -> remembered ->
Miscellaneous.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# paprika_client is standalone; import it directly so the integration's
# __init__ (which needs Home Assistant) is not loaded.
sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
from paprika_client import PaprikaAPIError, PaprikaClient  # noqa: E402

AISLES = [
    {"name": "1", "uid": "A1", "order_flag": 24},
    {"name": "5", "uid": "A5", "order_flag": 30},
    {"name": "Aldi Finds", "uid": "AF", "order_flag": 33},
    {"name": "Miscellaneous", "uid": "MISC", "order_flag": 34},
]
INGREDIENTS = [
    {"name": "beef chuck roast", "aisle_uid": "A1", "uid": "i1"},
    {"name": "baking soda", "aisle_uid": "A5", "uid": "i2"},
    {"name": "gravy mix", "aisle_uid": None, "uid": "i3"},
]
OLD_HARDCODED = "F94467760BF4BC6B9521FFA9329D0F1DBCCA0F5AC0808BD8552FB375A565FB9E"


class FakeApi:
    def __init__(self, groceries=(), aisles=AISLES, ingredients=INGREDIENTS):
        self.groceries = list(groceries)
        self.aisles = list(aisles)
        self.ingredients = list(ingredients)
        self.calls = []

    async def __call__(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "POST":
            return {"result": True}
        if path.rstrip("/") == "/sync/groceryaisles":
            return {"result": self.aisles}
        if path.rstrip("/") == "/sync/groceryingredients":
            return {"result": self.ingredients}
        if path.rstrip("/") == "/sync/groceries":
            return {"result": self.groceries}
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    return c


async def _add(client, api, **kwargs):
    captured = {}

    def capture(payload):
        captured["payload"] = payload
        return b"gz"

    with patch.object(client, "_make_authenticated_request", api), patch.object(
        client, "_get_list_uuid_by_name", new_callable=AsyncMock, return_value="L1"
    ), patch.object(client, "_gzip_json", side_effect=capture):
        result = await client.add_grocery(**kwargs)
    return result, captured["payload"][0]


# --- explicit aisle -------------------------------------------------------

@pytest.mark.asyncio
async def test_requested_aisle_is_used(client):
    result, row = await _add(client, FakeApi(), item_name="flour", aisle="5")
    assert (row["aisle"], row["aisle_uid"]) == ("5", "A5")
    assert result["aisle_source"] == "requested"


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["aisle 5", "Aisle 5", " 5 ", "AISLE 5"])
async def test_requested_aisle_tolerates_aisle_prefix_and_case(client, spelling):
    _, row = await _add(client, FakeApi(), item_name="flour", aisle=spelling)
    assert row["aisle_uid"] == "A5"


@pytest.mark.asyncio
async def test_requested_aisle_name_is_case_insensitive(client):
    _, row = await _add(client, FakeApi(), item_name="snacks", aisle="aldi finds")
    assert (row["aisle"], row["aisle_uid"]) == ("Aldi Finds", "AF")


@pytest.mark.asyncio
async def test_unknown_aisle_raises_and_lists_real_ones(client):
    api = FakeApi()
    with pytest.raises(PaprikaAPIError) as exc:
        await _add(client, api, item_name="flour", aisle="Produce")
    msg = str(exc.value)
    assert "Produce" in msg and "'Aldi Finds'" in msg and "'Miscellaneous'" in msg
    assert not any(m == "POST" for m, _ in api.calls), "must not write on a bad aisle"


@pytest.mark.asyncio
async def test_requested_aisle_moves_an_existing_item(client):
    existing = {"uid": "g1", "name": "flour", "list_uid": "L1", "quantity": "1",
                "aisle": "1", "aisle_uid": "A1"}
    _, row = await _add(client, FakeApi(groceries=[existing]), item_name="flour", aisle="5")
    assert row["aisle_uid"] == "A5"


# --- no aisle given -------------------------------------------------------

@pytest.mark.asyncio
async def test_existing_item_keeps_its_aisle(client):
    """The old code moved every re-added item to Produce."""
    existing = {"uid": "g1", "name": "toilet paper", "list_uid": "L1",
                "quantity": "1", "aisle": "Aldi Finds", "aisle_uid": "AF"}
    api = FakeApi(groceries=[existing])
    result, row = await _add(client, api, item_name="toilet paper")
    assert (row["aisle"], row["aisle_uid"]) == ("Aldi Finds", "AF")
    assert result["aisle_source"] == "existing"
    assert ("GET", "/sync/groceryingredients/") not in api.calls


@pytest.mark.asyncio
async def test_new_item_uses_remembered_aisle(client):
    result, row = await _add(client, FakeApi(), item_name="Beef Chuck Roast")
    assert (row["aisle"], row["aisle_uid"]) == ("1", "A1")
    assert result["aisle_source"] == "remembered"


@pytest.mark.asyncio
async def test_remembered_entry_without_aisle_falls_through(client):
    result, row = await _add(client, FakeApi(), item_name="gravy mix")
    assert row["aisle_uid"] == "MISC"
    assert result["aisle_source"] == "default"


@pytest.mark.asyncio
async def test_remembered_aisle_that_no_longer_exists_is_ignored(client):
    api = FakeApi(ingredients=[{"name": "kale", "aisle_uid": "DELETED", "uid": "x"}])
    result, row = await _add(client, api, item_name="kale")
    assert row["aisle_uid"] == "MISC" and result["aisle_source"] == "default"


@pytest.mark.asyncio
async def test_unknown_new_item_goes_to_miscellaneous(client):
    result, row = await _add(client, FakeApi(), item_name="dragonfruit")
    assert (row["aisle"], row["aisle_uid"]) == ("Miscellaneous", "MISC")
    assert result["aisle_source"] == "default"


@pytest.mark.asyncio
async def test_no_miscellaneous_aisle_leaves_it_unset(client):
    api = FakeApi(aisles=[a for a in AISLES if a["name"] != "Miscellaneous"])
    _, row = await _add(client, api, item_name="dragonfruit")
    assert (row["aisle"], row["aisle_uid"]) == ("", "")


# --- the original bug -----------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"item_name": "dragonfruit"},
    {"item_name": "beef chuck roast"},
    {"item_name": "flour", "aisle": "5"},
])
async def test_hardcoded_produce_aisle_is_gone(client, kwargs):
    _, row = await _add(client, FakeApi(), **kwargs)
    assert row["aisle_uid"] != OLD_HARDCODED
    assert row["aisle"] != "Produce"


# --- get_groceries reports the real aisle ---------------------------------

@pytest.mark.asyncio
async def test_get_groceries_resolves_aisle_from_uid_not_stale_text(client):
    """Paprika re-files an unknown aisle_uid without fixing the text."""
    stale = {"uid": "g1", "name": "milk", "list_uid": "L1", "quantity": "2",
             "aisle": "Produce", "aisle_uid": "MISC"}
    with patch.object(client, "_make_authenticated_request", FakeApi(groceries=[stale])), \
         patch.object(client, "_get_list_uuid_by_name", new_callable=AsyncMock, return_value="L1"):
        items = await client.get_groceries()
    assert items == [{"name": "milk", "quantity": "2", "aisle": "Miscellaneous"}]
