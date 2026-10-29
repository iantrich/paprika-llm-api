"""Rate limits during the recipe walk, and the persisted recipe cache.

A cold walk fetches every recipe one request at a time. Paprika refuses with
429 part-way through a large library, and the walk used to log each refusal
and carry on -- firing every remaining request into the same wall and
silently dropping those recipes from searches and the duplicate-name check.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "paprika_llm"))
from paprika_client import (  # noqa: E402
    PaprikaAPIError,
    PaprikaClient,
    PaprikaRateLimitError,
    _raise_for_status,
)


def index(n):
    return [{"uid": f"u{i}", "hash": f"h{i}"} for i in range(n)]


class Api:
    """Fake Paprika. Refuses recipe fetches from `limit_at` onward with a 429."""

    def __init__(self, n=10, limit_at=None, fail_uids=()):
        self.n, self.limit_at, self.fail_uids = n, limit_at, set(fail_uids)
        self.fetched, self.attempted, self.posts = [], [], 0

    async def __call__(self, method, path, **kwargs):
        if method == "POST":
            self.posts += 1
            return {"result": True}
        if path.rstrip("/") == "/sync/recipes":
            return {"result": index(self.n)}
        uid = path.split("/")[3]
        i = int(uid[1:])
        self.attempted.append(uid)
        if self.limit_at is not None and i >= self.limit_at:
            raise PaprikaRateLimitError("Request failed with status 429: slow down")
        if uid in self.fail_uids:
            raise PaprikaAPIError("Request failed with status 500")
        self.fetched.append(uid)
        return {"result": {"uid": uid, "name": f"Recipe {i}", "in_trash": i == 1}}


@pytest.fixture
def client():
    c = PaprikaClient("t@example.com", "pw")
    c.token = "tok"
    return c


def test_429_maps_to_the_rate_limit_error_and_nothing_else_does():
    with pytest.raises(PaprikaRateLimitError):
        _raise_for_status(429, "slow down")
    with pytest.raises(PaprikaAPIError) as exc:
        _raise_for_status(500)
    assert not isinstance(exc.value, PaprikaRateLimitError)


@pytest.mark.asyncio
async def test_walk_stops_at_the_first_429(client):
    api = Api(n=10, limit_at=4)
    with patch.object(client, "_make_authenticated_request", api):
        with pytest.raises(PaprikaRateLimitError, match="4 of 10"):
            await client._load_recipes_from_index(index(10), prune=True)
    assert api.fetched == ["u0", "u1", "u2", "u3"]
    # Count attempts, not successes: refused requests never succeed either way,
    # so only the attempt count shows whether the walk stopped.
    assert api.attempted == ["u0", "u1", "u2", "u3", "u4"], "nothing sent after the refusal"
    assert set(client._recipe_cache) == {"u0", "u1", "u2", "u3"}, "progress kept"


@pytest.mark.asyncio
async def test_next_call_resumes_rather_than_starting_over(client):
    first = Api(n=10, limit_at=4)
    with patch.object(client, "_make_authenticated_request", first):
        with pytest.raises(PaprikaRateLimitError):
            await client._load_recipes_from_index(index(10), prune=True)
    second = Api(n=10)
    with patch.object(client, "_make_authenticated_request", second):
        got = await client._load_recipes_from_index(index(10), prune=True)
    assert second.fetched == [f"u{i}" for i in range(4, 10)]
    assert len(got) == 10


@pytest.mark.asyncio
async def test_other_failures_still_skip_and_continue(client):
    api = Api(n=5, fail_uids={"u2"})
    with patch.object(client, "_make_authenticated_request", api):
        got = await client._load_recipes_from_index(index(5), prune=True)
    assert set(got) == {"u0", "u1", "u3", "u4"}


@pytest.mark.asyncio
async def test_progress_is_announced_even_when_cut_short(client):
    client.on_recipe_cache_changed = MagicMock()
    with patch.object(client, "_make_authenticated_request", Api(n=10, limit_at=4)):
        with pytest.raises(PaprikaRateLimitError):
            await client._load_recipes_from_index(index(10), prune=True)
    client.on_recipe_cache_changed.assert_called_once()


@pytest.mark.asyncio
async def test_no_announcement_when_nothing_changed(client):
    with patch.object(client, "_make_authenticated_request", Api(n=3)):
        await client._load_recipes_from_index(index(3), prune=True)
    client.on_recipe_cache_changed = MagicMock()
    with patch.object(client, "_make_authenticated_request", Api(n=3)):
        await client._load_recipes_from_index(index(3), prune=True)
    client.on_recipe_cache_changed.assert_not_called()


@pytest.mark.asyncio
async def test_title_lookup_does_not_hide_the_rate_limit(client):
    with patch.object(client, "_make_authenticated_request", Api(n=10, limit_at=0)):
        with pytest.raises(PaprikaRateLimitError):
            await client.get_recipe_by_title("Recipe 5")


@pytest.mark.asyncio
async def test_list_recipes_does_not_hide_the_rate_limit(client):
    with patch.object(client, "_make_authenticated_request", Api(n=10, limit_at=3)), \
         patch.object(client, "get_meal_plan", return_value=[]):
        with pytest.raises(PaprikaRateLimitError):
            await client.list_recipes(limit=1000)


@pytest.mark.asyncio
async def test_create_stops_before_writing_if_the_duplicate_check_is_cut_short(client):
    api = Api(n=10, limit_at=2)
    with patch.object(client, "_make_authenticated_request", api):
        with pytest.raises(PaprikaRateLimitError):
            await client.create_recipe(
                name="New", ingredients="1 egg", directions="Cook.", generate_image=False
            )
    assert api.posts == 0


# --- persistence --------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_import_round_trip_and_trashed_recipes_are_stubbed(client):
    with patch.object(client, "_make_authenticated_request", Api(n=4)):
        await client._load_recipes_from_index(index(4), prune=True)
    exported = client.export_recipe_cache()
    assert exported["recipes"]["u1"]["recipe"] == {"uid": "u1", "name": "Recipe 1", "in_trash": True}

    fresh = PaprikaClient("t@example.com", "pw")
    fresh.token = "tok"
    assert fresh.import_recipe_cache(exported) == 4
    api = Api(n=4)
    with patch.object(fresh, "_make_authenticated_request", api):
        got = await fresh._load_recipes_from_index(index(4), prune=True)
    assert api.fetched == [], "a restored cache costs no recipe fetches"
    assert got["u0"]["name"] == "Recipe 0"


def test_import_skips_malformed_entries(client):
    loaded = client.import_recipe_cache({"recipes": {
        "ok": {"hash": "h", "recipe": {"uid": "ok"}},
        "no_hash": {"recipe": {"uid": "x"}},
        "bad_recipe": {"hash": "h", "recipe": "nope"},
        "not_a_dict": "nope",
    }})
    assert loaded == 1 and set(client._recipe_cache) == {"ok"}


@pytest.mark.parametrize("garbage", [None, [], "x", {"recipes": "x"}, {}])
def test_import_of_garbage_loads_nothing(client, garbage):
    assert client.import_recipe_cache(garbage) == 0
