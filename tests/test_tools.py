"""The tool layer, exercised against real Home Assistant code.

Schemas are rendered through mcp_server's own _format_tool -- the exact
function that builds what an MCP client receives -- so a schema that
probatio cannot convert, or a missing field description, fails here rather
than in front of the model.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.mcp_server.server import _format_tool
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

sys.path.insert(0, str(Path(__file__).parents[1]))
from custom_components.paprika_llm import llm_api  # noqa: E402
from custom_components.paprika_llm.paprika_client import (  # noqa: E402
    MealMatchAmbiguousError,
    MealNotFoundError,
    PaprikaAPIError,
    PaprikaClient,
    PaprikaRateLimitError,
)

EXPECTED = {
    "get_grocery_lists", "get_grocery_aisles", "get_groceries", "get_grocery",
    "add_grocery", "add_groceries", "remove_groceries", "clear_grocery_list",
    "get_pantry", "add_pantry_items", "remove_pantry_items", "search_recipes",
    "read_recipe", "create_recipe", "import_recipe_from_url", "update_recipe",
    "list_meal_plan", "get_meal_plan_ingredients", "add_meal_to_plan",
    "add_note_to_plan",
    "remove_meal_from_plan",
}


@pytest.fixture
def client():
    return MagicMock(spec_set=PaprikaClient)


def tools(client):
    return {t.name.removeprefix("paprika_llm__"): t for t in llm_api._build_tools(client)}


def _call(tool, **args):
    """Call a tool the way Home Assistant does: arguments go through the tool's
    schema first, so defaults and coercions apply before the tool runs."""
    validated = tool.parameters(args)
    return tool.async_call(None, llm.ToolInput(tool_name=tool.name, tool_args=validated), None)


# --- shape ----------------------------------------------------------------

def test_tool_set(client):
    built = llm_api._build_tools(client)
    names = [t.name for t in built]
    assert len(names) == len(set(names))
    assert set(tools(client)) == EXPECTED
    for t in built:
        assert t.name.startswith("paprika_llm__"), t.name
        assert t.integration == "paprika_llm"
        assert t.description and t.title


def test_every_schema_converts_and_documents_every_field(client):
    """Rendered exactly as mcp_server renders it for the client."""
    for name, tool in tools(client).items():
        spec = _format_tool(tool, None)
        props = spec.inputSchema["properties"]

        def walk(properties, path):
            for key, prop in properties.items():
                assert prop.get("description"), f"{name}: {path}{key} has no description"
                nested = prop.get("items", {}).get("properties")
                if nested:
                    walk(nested, f"{path}{key}[].")

        walk(props, "")


def test_annotations_reach_the_client(client):
    t = tools(client)
    hints = {n: _format_tool(t[n], None).annotations for n in t}
    for n in ("get_groceries", "get_meal_plan_ingredients", "search_recipes", "list_meal_plan"):
        assert hints[n].readOnlyHint is True and hints[n].destructiveHint is False
    assert hints["clear_grocery_list"].destructiveHint is True
    assert hints["remove_groceries"].destructiveHint is True
    assert hints["remove_pantry_items"].destructiveHint is True
    assert hints["get_pantry"].readOnlyHint is True
    assert hints["read_recipe"].readOnlyHint is True
    assert hints["update_recipe"].destructiveHint is True
    assert hints["create_recipe"].destructiveHint is False
    assert hints["add_pantry_items"].destructiveHint is False
    assert hints["add_pantry_items"].idempotentHint is True
    assert hints["remove_meal_from_plan"].destructiveHint is True
    assert hints["add_groceries"].idempotentHint is False


def test_add_groceries_schema_is_a_list_of_items(client):
    spec = _format_tool(tools(client)["add_groceries"], None).inputSchema
    items = spec["properties"]["items"]
    assert items["type"] == "array"
    assert items["items"]["required"] == ["item_name"]
    assert spec["required"] == ["items"]


def test_numeric_quantity_and_aisle_are_accepted_in_a_batch(client):
    schema = tools(client)["add_groceries"].parameters
    out = schema({"items": [{"item_name": "eggs", "quantity": 5, "aisle": 1}]})
    assert out["items"][0] == {"item_name": "eggs", "quantity": "5", "aisle": "1"}


# --- behaviour ------------------------------------------------------------

@pytest.mark.asyncio
async def test_results_are_tool_results(client):
    client.get_grocery_aisles = AsyncMock(return_value=[{"name": "5", "uid": "A5"}])
    result = await _call(tools(client)["get_grocery_aisles"])
    assert isinstance(result, llm.ToolResult)
    assert result.data == {"aisles": ["5"]}


@pytest.mark.asyncio
async def test_add_groceries_passes_items_through(client):
    client.add_groceries = AsyncMock(return_value={"added": [], "count": 0})
    items = [{"item_name": "eggs", "quantity": "5"}]
    result = await _call(tools(client)["add_groceries"], items=items)
    client.add_groceries.assert_awaited_once_with(items=items, grocery_list_name=None)
    assert result.data["count"] == 0


@pytest.mark.asyncio
async def test_ambiguous_recipe_lists_candidates(client):
    client.add_meal_to_plan = AsyncMock(side_effect=MealMatchAmbiguousError(
        "Multiple recipes match 'Pasta'",
        matches=[{"name": "Pasta Bolognese"}, {"name": "Pasta Carbonara"}],
    ))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(tools(client)["add_meal_to_plan"], recipe_name="Pasta")
    assert "Pasta Bolognese" in str(exc.value) and "Pasta Carbonara" in str(exc.value)


@pytest.mark.asyncio
async def test_meal_not_found_is_a_tool_error_not_an_escape(client):
    client.remove_meal_from_plan = AsyncMock(side_effect=MealNotFoundError("No meal on 2026-11-02"))
    with pytest.raises(HomeAssistantError):
        await _call(tools(client)["remove_meal_from_plan"], date="2026-11-02")


@pytest.mark.asyncio
async def test_remove_requires_an_identifier(client):
    """The client removes 'the latest meal' when given nothing; a model must not."""
    client.remove_meal_from_plan = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await _call(tools(client)["remove_meal_from_plan"], meal_type="dinner")
    client.remove_meal_from_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_meal_requires_a_recipe(client):
    client.add_meal_to_plan = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await _call(tools(client)["add_meal_to_plan"], date="2026-11-02")
    client.add_meal_to_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_errors_are_tool_errors(client):
    client.get_groceries = AsyncMock(side_effect=PaprikaAPIError("429"))
    with pytest.raises(HomeAssistantError, match="429"):
        await _call(tools(client)["get_groceries"])


@pytest.mark.asyncio
async def test_search_filters_and_stays_compact(client):
    client.list_recipes = AsyncMock(return_value=[
        {"name": "Chicken Fried Rice", "uid": "1", "ingredients": "chicken\nrice",
         "description": "", "last_planned": "2026-10-28", "servings": "4"},
        {"name": "Pizza", "uid": "2", "ingredients": "dough", "description": "",
         "last_planned": None},
    ])
    t = tools(client)["search_recipes"]
    hit = (await _call(t, query="RICE")).data
    assert [r["name"] for r in hit["recipes"]] == ["Chicken Fried Rice"]
    assert "ingredients" not in hit["recipes"][0]
    everything = (await _call(t)).data
    assert everything["total_matches"] == 2


@pytest.mark.asyncio
async def test_prompt_carries_todays_date(client):
    api = llm_api.PaprikaLLMAPI(hass=MagicMock(), id="paprika_llm", name="Paprika", client=client)
    ctx = MagicMock(spec=llm.LLMContext)
    instance = await api.async_get_api_instance(ctx)
    assert instance.api_prompt.startswith("Today is ")
    assert len(instance.tools) == len(EXPECTED)


# --- recipes --------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_sends_only_given_fields_through_the_partial_path(client):
    client.update_recipe_partial = AsyncMock(return_value={"uid": "R1", "name": "Chili"})
    client.update_recipe = AsyncMock()
    result = await _call(tools(client)["update_recipe"], uid="R1",
                         notes="Use less salt", ingredients=["2 lb beef", " ", "1 onion"])
    client.update_recipe_partial.assert_awaited_once_with(
        "R1", notes="Use less salt", ingredients="2 lb beef\n1 onion")
    client.update_recipe.assert_not_awaited()  # the full-replace path drops fields
    assert result.data["changed_fields"] == ["ingredients", "notes"]


@pytest.mark.asyncio
async def test_update_with_nothing_to_change_is_refused(client):
    client.update_recipe_partial = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await _call(tools(client)["update_recipe"], uid="R1")
    client.update_recipe_partial.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_joins_ingredients_and_never_generates_images(client):
    client.create_recipe = AsyncMock(return_value={"uid": "NEW", "name": "Test"})
    result = await _call(tools(client)["create_recipe"], name="Test",
                         ingredients=["1 cup flour", "2 eggs"], directions="Mix.", servings=4)
    kwargs = client.create_recipe.await_args.kwargs
    assert kwargs["generate_image"] is False
    assert kwargs["ingredients"] == "1 cup flour\n2 eggs"
    assert kwargs["servings"] == "4"
    assert result.data == {"created": True, "uid": "NEW", "name": "Test"}


@pytest.mark.asyncio
async def test_read_recipe_splits_ingredients_and_reports_missing(client):
    client.get_recipe_by_uid = AsyncMock(side_effect=[
        {"uid": "R1", "name": "Rice", "ingredients": "1 cup rice\n\n2 cups water\n"},
        None,
    ])
    t = tools(client)["read_recipe"]
    got = (await _call(t, uid="R1")).data
    assert got["found"] is True and got["ingredients"] == ["1 cup rice", "2 cups water"]
    assert (await _call(t, uid="GONE")).data["found"] is False
    with pytest.raises(HomeAssistantError):
        await _call(t)


@pytest.mark.asyncio
async def test_rate_limit_tells_the_model_to_wait(client):
    client.list_recipes = AsyncMock(side_effect=PaprikaRateLimitError("loaded 4 of 124"))
    with pytest.raises(HomeAssistantError) as exc:
        await _call(tools(client)["search_recipes"])
    assert "4 of 124" in str(exc.value) and "do not retry immediately" in str(exc.value)


@pytest.mark.asyncio
async def test_rewrapped_rate_limit_is_still_recognised(client):
    client.create_recipe = AsyncMock(side_effect=PaprikaAPIError(
        "Failed to create recipe: Request failed with status 429: slow down"))
    with pytest.raises(HomeAssistantError, match="rate-limiting"):
        await _call(tools(client)["create_recipe"], name="X", ingredients=["a"], directions="b")
