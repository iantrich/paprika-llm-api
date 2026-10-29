"""get_meal_plan_ingredients and get_pantry."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
from paprika_client import PaprikaClient  # noqa: E402

PANTRY = [
    {"ingredient": "soy sauce", "in_stock": True, "quantity": ""},
    {"ingredient": "flour", "in_stock": False, "quantity": ""},
    {"ingredient": "salt", "in_stock": True, "deleted": True},
    {"ingredient": "  ", "in_stock": True},
    {"ingredient": "Black Pepper ", "in_stock": True, "quantity": "1 jar"},
]
RECIPES = {
    "R1": {"name": "Chicken Fried Rice", "servings": "4",
           "ingredients": "25oz chicken breast\n\n  2 tablespoons soy sauce \n3 eggs\n"},
}


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    return c


def pantry_api():
    async def api(method, path, **kwargs):
        assert path.rstrip("/") == "/sync/pantry"
        return {"result": PANTRY}
    return api


@pytest.mark.asyncio
async def test_pantry_defaults_to_in_stock_only(client):
    with patch.object(client, "_make_authenticated_request", pantry_api()):
        got = await client.get_pantry()
    assert [p["name"] for p in got] == ["soy sauce", "Black Pepper"]


@pytest.mark.asyncio
async def test_pantry_can_include_out_of_stock(client):
    with patch.object(client, "_make_authenticated_request", pantry_api()):
        got = await client.get_pantry(in_stock_only=False)
    assert "flour" in [p["name"] for p in got]
    assert "salt" not in [p["name"] for p in got], "deleted rows never count"


async def gather(client, meals, recipes=RECIPES):
    fetch = AsyncMock(side_effect=lambda uid: recipes.get(uid))
    with patch.object(client, "get_meal_plan", new_callable=AsyncMock, return_value=meals), \
         patch.object(client, "get_recipe_by_uid", fetch), \
         patch.object(client, "_make_authenticated_request", pantry_api()):
        out = await client.get_meal_plan_ingredients(num_days=7)
    return out, fetch


@pytest.mark.asyncio
async def test_recipe_meals_carry_clean_ingredient_lines(client):
    out, _ = await gather(client, [
        {"meal": "Chicken Fried Rice", "meal_ID": "R1", "date": "2026-10-28", "type": "dinner", "scale": None},
    ])
    meal = out["meals"][0]
    assert meal["kind"] == "recipe"
    assert meal["ingredients"] == ["25oz chicken breast", "2 tablespoons soy sauce", "3 eggs"]
    assert meal["recipe_name"] == "Chicken Fried Rice" and meal["servings"] == "4"
    assert out["pantry_in_stock"] == ["soy sauce", "Black Pepper"]


@pytest.mark.asyncio
async def test_notes_are_reported_not_dropped(client):
    out, fetch = await gather(client, [
        {"meal": "Asparagus ", "meal_ID": "", "date": "2026-10-29", "type": "dinner"},
    ])
    assert out["meals"] == [{"date": "2026-10-29", "meal_type": "dinner", "name": "Asparagus",
                             "scale": None, "kind": "note", "ingredients": []}]
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_recipe_planned_twice_is_fetched_once(client):
    meals = [{"meal": "Chicken Fried Rice", "meal_ID": "R1", "date": d, "type": "dinner"}
             for d in ("2026-10-28", "2026-11-01")]
    out, fetch = await gather(client, meals)
    assert len(out["meals"]) == 2
    fetch.assert_awaited_once_with("R1")


@pytest.mark.asyncio
async def test_unresolvable_recipe_is_flagged(client):
    out, _ = await gather(client, [{"meal": "Gone", "meal_ID": "DEAD", "date": "2026-10-28", "type": "dinner"}])
    assert out["meals"][0]["kind"] == "missing_recipe"
    assert out["meals"][0]["ingredients"] == []


@pytest.mark.asyncio
async def test_scale_is_passed_through(client):
    out, _ = await gather(client, [
        {"meal": "Chicken Fried Rice", "meal_ID": "R1", "date": "2026-10-28", "type": "dinner", "scale": "2"},
    ])
    assert out["meals"][0]["scale"] == "2"


@pytest.mark.asyncio
async def test_window_defaults_to_all_meal_types(client):
    with patch.object(client, "get_meal_plan", new_callable=AsyncMock, return_value=[]) as gmp, \
         patch.object(client, "_make_authenticated_request", pantry_api()):
        await client.get_meal_plan_ingredients(start_date="2026-11-02", end_date="2026-11-08")
    assert gmp.await_args.kwargs["meal_type"] is None
