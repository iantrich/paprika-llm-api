"""Notes on the meal plan (side dishes) and entry ordering within a day."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
sys.path.insert(0, str(Path(__file__).parent))
from paprika_client import PaprikaClient  # noqa: E402
from test_tools import PaprikaAPIError as PackagePaprikaAPIError  # noqa: E402
from test_tools import PaprikaClient as PackageClient  # noqa: E402
from test_tools import _call, tools  # noqa: E402

DAY = "2026-11-02"
# Raw rows as /sync/meals returns them: a main dish and one side already planned.
RAW = [
    {"uid": "M1", "recipe_uid": "R1", "date": f"{DAY} 00:00:00", "type": 2,
     "name": "Cashew Chicken", "order_flag": 0, "scale": None},
    {"uid": "M2", "recipe_uid": None, "date": f"{DAY} 00:00:00", "type": 2,
     "name": "Jasmine Rice", "order_flag": 1, "scale": None},
    {"uid": "M3", "recipe_uid": None, "date": f"{DAY} 00:00:00", "type": 3,
     "name": "Leftovers", "order_flag": 4, "scale": None},
]


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    c.posted = []

    async def v1(method, path, **kwargs):
        if method == "GET":
            return {"result": RAW}
        c.posted.append(path)
        return {"result": True}

    real_gzip = c._gzip_json

    def spy(data):
        c.sent = data
        return real_gzip(data)

    c._make_v1_basic_auth_request = v1
    c._gzip_json = spy
    return c


@pytest.mark.asyncio
async def test_note_is_an_entry_without_a_recipe_after_existing_sides(client):
    out = await client.add_note_to_plan("Steamed Broccoli", DAY)
    [entry] = client.sent
    assert entry["recipe_uid"] is None
    assert entry["name"] == "Steamed Broccoli"
    assert entry["date"] == f"{DAY} 00:00:00"
    assert entry["type"] == 2
    assert entry["order_flag"] == 2, "goes after the main (0) and the rice (1)"
    assert entry["deleted"] is False
    assert client.posted == ["/sync/meals/"]
    assert out == {"meal": "Steamed Broccoli", "meal_ID": None, "date": DAY, "type": "dinner"}


@pytest.mark.asyncio
async def test_order_counts_only_the_same_meal_type(client):
    await client.add_note_to_plan("Fruit", "2026-11-02", meal_type="breakfast")
    assert client.sent[0]["order_flag"] == 0, "nothing planned for breakfast"
    assert client.sent[0]["type"] == 1


@pytest.mark.asyncio
async def test_note_on_an_empty_day_is_first(client):
    await client.add_note_to_plan("Pizza", "2026-11-06")
    assert client.sent[0]["order_flag"] == 0


@pytest.mark.asyncio
async def test_recipe_added_to_a_busy_day_is_appended_too(client):
    client._make_authenticated_request = AsyncMock(
        return_value={"result": {"name": "Garlic Bread"}}
    )
    await client.add_meal_to_plan(meal_id="R9", date=DAY)
    [entry] = client.sent
    assert entry["recipe_uid"] == "R9"
    assert entry["name"] == "Garlic Bread"
    assert entry["order_flag"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["", "   "])
async def test_blank_note_is_refused_before_any_request(client, note):
    with pytest.raises(Exception, match="Note text is required"):
        await client.add_note_to_plan(note, DAY)
    assert client.posted == []


@pytest.mark.asyncio
async def test_meal_plan_reports_order(client):
    meals = await client.get_meal_plan(start_date=DAY, end_date=DAY)
    assert [(m["meal"], m["order"]) for m in meals] == [
        ("Cashew Chicken", 0), ("Jasmine Rice", 1)
    ]


@pytest.mark.asyncio
async def test_tool_requires_a_date():
    client = MagicMock(spec_set=PackageClient)
    client.add_note_to_plan = AsyncMock()
    with pytest.raises(Exception):
        await _call(tools(client)["add_note_to_plan"], note="Side Salad")
    client.add_note_to_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_passes_note_through():
    client = MagicMock(spec_set=PackageClient)
    client.add_note_to_plan = AsyncMock(return_value={
        "meal": "Side Salad", "meal_ID": None, "date": DAY, "type": "dinner"})
    out = (await _call(tools(client)["add_note_to_plan"], note="Side Salad", date=DAY)).data
    client.add_note_to_plan.assert_awaited_once_with(
        note="Side Salad", date=DAY, meal_type="dinner")
    assert out == {"scheduled": True, "date": DAY, "meal_type": "dinner", "note": "Side Salad"}


@pytest.mark.asyncio
async def test_tool_reports_rate_limit_from_the_day_lookup():
    # The tool layer imports the client through the package, so its error
    # classes are the package ones, not the top-level module used above.
    client = MagicMock(spec_set=PackageClient)
    client.add_note_to_plan = AsyncMock(side_effect=PackagePaprikaAPIError(
        "Failed to get meal plan: Request failed with status 429"))
    with pytest.raises(HomeAssistantError, match="rate-limiting"):
        await _call(tools(client)["add_note_to_plan"], note="Corn", date=DAY)
