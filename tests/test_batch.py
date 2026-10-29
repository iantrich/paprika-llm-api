"""add_groceries: one write for many items.

Adding a week of ingredients one at a time costs several requests each, and
every new item without an aisle downloads the ~1,700-entry ingredient-aisle
memory. That is the pattern that rate-limited the account before.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
from paprika_client import PaprikaAPIError, PaprikaClient  # noqa: E402

AISLES = [{"name": "1", "uid": "A1", "order_flag": 1},
          {"name": "5", "uid": "A5", "order_flag": 5},
          {"name": "Miscellaneous", "uid": "MISC", "order_flag": 9}]
MEMORY = [{"name": "eggs", "aisle_uid": "A1", "uid": "i1"},
          {"name": "soy sauce", "aisle_uid": "A5", "uid": "i2"}]


class Api:
    def __init__(self, groceries=()):
        self.groceries = list(groceries)
        self.calls = []

    async def __call__(self, method, path, **kwargs):
        self.calls.append((method, path.rstrip("/")))
        if method == "POST":
            return {"result": True}
        return {"result": {
            "/sync/groceryaisles": AISLES,
            "/sync/groceryingredients": MEMORY,
            "/sync/groceries": self.groceries,
        }[path.rstrip("/")]}

    def count(self, method, path=None):
        return sum(1 for m, p in self.calls if m == method and (path is None or p == path))


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    return c


async def run(client, api, items):
    sent = {}

    def capture(rows):
        sent["rows"] = rows
        return b"gz"

    with patch.object(client, "_make_authenticated_request", api), \
         patch.object(client, "_get_list_uuid_by_name", new_callable=AsyncMock, return_value="L1"), \
         patch.object(client, "_gzip_json", side_effect=capture):
        result = await client.add_groceries(items)
    return result, sent.get("rows")


@pytest.mark.asyncio
async def test_forty_items_cost_one_write_and_a_fixed_number_of_reads(client):
    api = Api()
    items = [{"item_name": f"thing {i}"} for i in range(40)]
    result, rows = await run(client, api, items)
    assert result["count"] == 40 and len(rows) == 40
    assert api.count("POST") == 1
    assert api.count("GET") == 3, api.calls  # groceries, aisles, memory -- once each
    assert api.count("GET", "/sync/groceryingredients") == 1


@pytest.mark.asyncio
async def test_memory_skipped_when_no_item_needs_it(client):
    api = Api(groceries=[{"uid": "g1", "name": "eggs", "list_uid": "L1",
                          "quantity": "1", "aisle": "1", "aisle_uid": "A1"}])
    await run(client, api, [{"item_name": "eggs"}, {"item_name": "flour", "aisle": "5"}])
    assert api.count("GET", "/sync/groceryingredients") == 0


@pytest.mark.asyncio
async def test_duplicates_in_the_request_merge_into_one_row(client):
    result, rows = await run(client, Api(), [
        {"item_name": "eggs", "quantity": "3"},
        {"item_name": "Eggs", "quantity": "2"},
    ])
    assert len(rows) == 1
    assert rows[0]["quantity"] == "5"
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_existing_item_matched_case_insensitively(client):
    api = Api(groceries=[{"uid": "g1", "name": "Milk", "list_uid": "L1",
                          "quantity": "1", "aisle": "1", "aisle_uid": "A1"}])
    result, rows = await run(client, api, [{"item_name": "milk", "quantity": "2"}])
    assert rows[0]["uid"] == "g1" and rows[0]["quantity"] == "3"
    assert result["added"][0]["was_on_list"] is True


@pytest.mark.asyncio
async def test_deleted_rows_are_not_reused(client):
    api = Api(groceries=[{"uid": "old", "name": "flour", "list_uid": "L1",
                          "quantity": "4", "deleted": True, "aisle_uid": "A5"}])
    result, rows = await run(client, api, [{"item_name": "flour"}])
    assert rows[0]["uid"] != "old"
    assert rows[0]["quantity"] == "1"
    assert rows[0]["deleted"] is False
    assert result["added"][0]["was_on_list"] is False


@pytest.mark.asyncio
async def test_other_lists_are_not_matched(client):
    api = Api(groceries=[{"uid": "g9", "name": "eggs", "list_uid": "OTHER", "quantity": "6"}])
    _, rows = await run(client, api, [{"item_name": "eggs"}])
    assert rows[0]["uid"] != "g9"


@pytest.mark.asyncio
async def test_one_bad_aisle_fails_the_whole_batch_without_writing(client):
    api = Api()
    with pytest.raises(PaprikaAPIError, match="Produce"):
        await run(client, api, [{"item_name": "eggs"}, {"item_name": "kale", "aisle": "Produce"}])
    assert api.count("POST") == 0


@pytest.mark.asyncio
async def test_empty_name_rejected_before_any_request(client):
    api = Api()
    with pytest.raises(PaprikaAPIError):
        await run(client, api, [{"item_name": "eggs"}, {"item_name": "  "}])
    assert api.calls == []


@pytest.mark.asyncio
async def test_empty_batch_makes_no_requests(client):
    api = Api()
    result, rows = await run(client, api, [])
    assert result == {"added": [], "count": 0} and api.calls == [] and rows is None


@pytest.mark.asyncio
async def test_each_item_reports_where_it_went(client):
    result, _ = await run(client, Api(), [
        {"item_name": "eggs"},                 # remembered -> 1
        {"item_name": "flour", "aisle": "5"},  # requested
        {"item_name": "dragonfruit"},          # default
    ])
    got = {a["name"]: (a["aisle"], a["aisle_source"]) for a in result["added"]}
    assert got == {"eggs": ("1", "remembered"), "flour": ("5", "requested"),
                   "dragonfruit": ("Miscellaneous", "default")}


@pytest.mark.asyncio
async def test_single_add_grocery_uses_the_batch_path(client):
    api = Api()
    with patch.object(client, "_make_authenticated_request", api), \
         patch.object(client, "_get_list_uuid_by_name", new_callable=AsyncMock, return_value="L1"), \
         patch.object(client, "_gzip_json", return_value=b"gz"):
        r = await client.add_grocery("soy sauce", quantity=2)
    assert r == {"name": "soy sauce", "quantity": "2", "aisle": "5", "aisle_source": "remembered"}
    assert api.count("POST") == 1
