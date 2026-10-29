"""Editing a recipe must change only what was asked."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
from paprika_client import PaprikaClient  # noqa: E402

STORED = {
    "uid": "R1", "name": "Ground Beef Potatoes", "ingredients": "1 lb beef\n1 onion",
    "directions": "Cook.", "notes": "", "servings": "6",
    "created": "2025-03-01 12:00:00", "photo": "abc.jpg", "photo_hash": "PH",
    "categories": ["C1", "C2"], "rating": 5, "source": "Some Blog",
    "source_url": "https://example.com/r", "hash": "OLD",
}


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    return c


async def edit(client, **fields):
    posted = []

    async def api(method, path, **kwargs):
        if method == "GET":
            return {"result": dict(STORED)}
        return {"result": True}

    with patch.object(client, "_make_authenticated_request", api), \
         patch.object(client, "_gzip_json", side_effect=lambda r: posted.append(r) or b"gz"):
        await client.update_recipe_partial("R1", **fields)
    return posted[0]


@pytest.mark.asyncio
async def test_edit_changes_only_the_given_field(client):
    sent = await edit(client, notes="Less salt")
    assert sent["notes"] == "Less salt"
    for key in ("name", "ingredients", "directions", "servings", "photo", "photo_hash",
                "categories", "rating", "source", "source_url"):
        assert sent[key] == STORED[key], key


@pytest.mark.asyncio
async def test_edit_keeps_the_date_the_recipe_was_added(client):
    """It used to overwrite this, making every edit look like a new recipe."""
    sent = await edit(client, notes="Less salt")
    assert sent["created"] == STORED["created"]


@pytest.mark.asyncio
async def test_edit_recomputes_the_hash_so_sync_sees_it(client):
    sent = await edit(client, notes="Less salt")
    assert sent["hash"] != "OLD"
