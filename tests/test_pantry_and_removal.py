"""Pantry writes and grocery removal."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
from paprika_client import PaprikaAPIError, PaprikaClient  # noqa: E402

AISLES = [{"name": "2", "uid": "A2", "order_flag": 2},
          {"name": "Miscellaneous", "uid": "MISC", "order_flag": 9}]
MEMORY = [{"name": "ketchup", "aisle_uid": "A2", "uid": "i1"}]


class Api:
    def __init__(self, pantry=(), groceries=()):
        self.pantry, self.groceries = list(pantry), list(groceries)
        self.calls = []
        self.posted = []

    async def __call__(self, method, path, **kwargs):
        path = path.rstrip("/")
        self.calls.append((method, path))
        if method == "POST":
            return {"result": True}
        return {"result": {"/sync/pantry": self.pantry, "/sync/groceries": self.groceries,
                           "/sync/groceryaisles": AISLES,
                           "/sync/groceryingredients": MEMORY}[path]}

    def posts(self):
        return sum(1 for m, _ in self.calls if m == "POST")


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    return c


async def run(client, api, coro_name, *args, **kwargs):
    sent = []
    with patch.object(client, "_make_authenticated_request", api), \
         patch.object(client, "_get_list_uuid_by_name", new_callable=AsyncMock, return_value="L1"), \
         patch.object(client, "_gzip_json", side_effect=lambda rows: sent.append(rows) or b"gz"):
        result = await getattr(client, coro_name)(*args, **kwargs)
    return result, (sent[0] if sent else None)


def row(name, **kw):
    base = {"uid": f"u-{name}", "ingredient": name, "in_stock": True, "quantity": "",
            "aisle": "Miscellaneous", "aisle_uid": "MISC", "purchase_date": "2026-02-15 00:00:00"}
    return {**base, **kw}


# --- add_pantry_items -----------------------------------------------------

@pytest.mark.asyncio
async def test_new_items_saved_in_one_write_and_in_stock(client):
    api = Api()
    result, rows = await run(client, api, "add_pantry_items",
                             [{"name": "ketchup"}, {"name": "capers", "quantity": 2}])
    assert api.posts() == 1 and len(rows) == 2
    assert all(r["in_stock"] is True and r["deleted"] is False for r in rows)
    got = {r["name"]: (r["aisle"], r["aisle_source"], r["was_in_pantry"]) for r in result["saved"]}
    assert got == {"ketchup": ("2", "remembered", False), "capers": ("Miscellaneous", "default", False)}
    assert rows[1]["quantity"] == "2"


@pytest.mark.asyncio
async def test_existing_item_is_updated_not_duplicated(client):
    api = Api(pantry=[row("Soy Sauce", in_stock=False, quantity="1 bottle", aisle="2", aisle_uid="A2")])
    result, rows = await run(client, api, "add_pantry_items", [{"name": "soy sauce"}])
    assert len(rows) == 1 and rows[0]["uid"] == "u-Soy Sauce"
    assert rows[0]["in_stock"] is True
    assert rows[0]["quantity"] == "1 bottle", "quantity left alone when not given"
    assert rows[0]["aisle_uid"] == "A2", "keeps its aisle"
    assert rows[0]["purchase_date"] != "2026-02-15 00:00:00", "restock updates purchase date"
    assert result["saved"][0]["was_in_pantry"] is True


@pytest.mark.asyncio
async def test_quantity_is_replaced_not_added(client):
    api = Api(pantry=[row("rice", quantity="2 lb")])
    _, rows = await run(client, api, "add_pantry_items", [{"name": "rice", "quantity": "5 lb"}])
    assert rows[0]["quantity"] == "5 lb"


@pytest.mark.asyncio
async def test_in_stock_false_marks_run_out(client):
    api = Api(pantry=[row("honey")])
    result, rows = await run(client, api, "add_pantry_items", [{"name": "honey", "in_stock": False}])
    assert rows[0]["in_stock"] is False and rows[0]["uid"] == "u-honey"
    assert result["saved"][0]["in_stock"] is False


@pytest.mark.asyncio
async def test_deleted_pantry_rows_are_not_revived(client):
    api = Api(pantry=[row("salt", deleted=True)])
    _, rows = await run(client, api, "add_pantry_items", [{"name": "salt"}])
    assert rows[0]["uid"] != "u-salt"


@pytest.mark.asyncio
async def test_later_mention_of_same_item_wins(client):
    _, rows = await run(client, Api(), "add_pantry_items",
                        [{"name": "eggs", "quantity": "6"}, {"name": "Eggs", "quantity": "12"}])
    assert len(rows) == 1 and rows[0]["quantity"] == "12"


@pytest.mark.asyncio
async def test_bad_aisle_writes_nothing(client):
    api = Api()
    with pytest.raises(PaprikaAPIError, match="Produce"):
        await run(client, api, "add_pantry_items", [{"name": "kale", "aisle": "Produce"}])
    assert api.posts() == 0


@pytest.mark.asyncio
async def test_empty_name_rejected_before_any_request(client):
    api = Api()
    with pytest.raises(PaprikaAPIError):
        await run(client, api, "add_pantry_items", [{"name": " "}])
    assert api.calls == []


# --- remove_pantry_items --------------------------------------------------

@pytest.mark.asyncio
async def test_remove_pantry_matches_exact_names_case_insensitively(client):
    api = Api(pantry=[row("serve with pasta"), row("potatoes or veggies"), row("olive oil")])
    result, rows = await run(client, api, "remove_pantry_items",
                             ["Serve With Pasta", "potatoes or veggies", "potatoes"])
    assert api.posts() == 1
    assert {r["uid"] for r in rows} == {"u-serve with pasta", "u-potatoes or veggies"}
    assert all(r["deleted"] is True for r in rows)
    assert result["not_found"] == ["potatoes"], "near-misses are reported, not guessed"
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_remove_pantry_nothing_matched_writes_nothing(client):
    api = Api(pantry=[row("olive oil")])
    result, rows = await run(client, api, "remove_pantry_items", ["oil"])
    assert api.posts() == 0 and rows is None
    assert result == {"removed": [], "not_found": ["oil"], "count": 0}


# --- remove_groceries -----------------------------------------------------

def g(name, list_uid="L1", **kw):
    return {"uid": f"g-{name}", "name": name, "list_uid": list_uid, "quantity": "1", **kw}


@pytest.mark.asyncio
async def test_remove_groceries_one_write(client):
    api = Api(groceries=[g("yellow mustard"), g("milk"), g("ketchup")])
    result, rows = await run(client, api, "remove_groceries", ["Yellow Mustard", "ketchup"])
    assert api.posts() == 1
    assert {r["uid"] for r in rows} == {"g-yellow mustard", "g-ketchup"}
    assert all(r["deleted"] is True for r in rows)
    assert result["removed"] == ["yellow mustard", "ketchup"] and result["not_found"] == []


@pytest.mark.asyncio
async def test_remove_groceries_only_touches_the_requested_list(client):
    api = Api(groceries=[g("milk", list_uid="OTHER")])
    result, rows = await run(client, api, "remove_groceries", ["milk"])
    assert rows is None and result["not_found"] == ["milk"]


@pytest.mark.asyncio
async def test_remove_groceries_ignores_already_deleted(client):
    api = Api(groceries=[g("eggs", deleted=True)])
    result, rows = await run(client, api, "remove_groceries", ["eggs"])
    assert rows is None and result["not_found"] == ["eggs"]


@pytest.mark.asyncio
async def test_remove_groceries_empty_request_makes_no_requests(client):
    api = Api()
    result, _ = await run(client, api, "remove_groceries", ["", "  "])
    assert api.calls == [] and result["count"] == 0
