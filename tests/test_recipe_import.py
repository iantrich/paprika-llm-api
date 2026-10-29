"""Importing a recipe from a web page."""
import json
import socket
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
from custom_components.paprika_llm import recipe_import  # noqa: E402
from custom_components.paprika_llm.recipe_import import (  # noqa: E402
    RecipeImportError,
    _duration,
    _servings,
    parse_recipe,
)
from test_tools import PaprikaClient, _call, tools  # noqa: E402

URL = "https://www.example.com/recipes/sheet-pan-chicken/"


def page(*blocks):
    scripts = "".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>'
        for b in blocks
    )
    return f"<html><head>{scripts}</head><body>hi</body></html>"


RECIPE = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Sheet Pan Chicken &amp; Veggies",
    "description": "<p>Easy weeknight dinner.</p>",
    "recipeYield": ["4", "4 servings"],
    "prepTime": "PT15M",
    "cookTime": "PT1H10M",
    "totalTime": "PT1H25M",
    "image": [{"@type": "ImageObject", "url": "/img/chicken.jpg"}],
    "publisher": {"@type": "Organization", "name": "Example Kitchen"},
    "recipeIngredient": ["1 lb chicken thighs", " 2 cups broccoli ", "", "2 tbsp oil (, peanut or canola)"],
    "recipeInstructions": [
        {"@type": "HowToSection", "name": "Prep", "itemListElement": [
            {"@type": "HowToStep", "text": "Heat oven to 425&deg;F."},
        ]},
        {"@type": "HowToStep", "text": "Roast <b>25 minutes</b>."},
    ],
}


# ------------------------------------------------------------------ parsing

def test_parses_a_plain_recipe():
    got = parse_recipe(page(RECIPE), URL)
    assert got["name"] == "Sheet Pan Chicken & Veggies"
    assert got["description"] == "Easy weeknight dinner."
    assert got["ingredients"] == ["1 lb chicken thighs", "2 cups broccoli", "2 tbsp oil (peanut or canola)"]
    assert got["directions"] == "Prep:\nHeat oven to 425°F.\nRoast 25 minutes."
    assert got["servings"] == "4" and got["yield_text"] == ""
    assert (got["prep_time"], got["cook_time"], got["total_time"]) == (
        "15 mins", "1 hr 10 mins", "1 hr 25 mins")
    assert got["source"] == "Example Kitchen"
    assert got["source_url"] == URL
    assert got["image_url"] == "https://www.example.com/img/chicken.jpg"


def test_finds_the_recipe_inside_a_graph_after_a_broken_block():
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "page"},
        {**RECIPE, "@type": ["Recipe", "NewsArticle"]},
    ]}
    got = parse_recipe(page("{not json", {"@type": "Organization"}, graph), URL)
    assert got["name"] == "Sheet Pan Chicken & Veggies"


def test_string_instructions_and_no_publisher():
    r = {**RECIPE, "recipeInstructions": "Mix.<br>Bake.\n\nEat.", "image": "a.png"}
    del r["publisher"]
    got = parse_recipe(page(r), URL)
    assert got["directions"] == "Mix.\nBake.\nEat."
    assert got["source"] == "example.com"
    assert got["image_url"] == "https://www.example.com/recipes/sheet-pan-chicken/a.png"


def test_page_without_recipe_data_is_refused():
    with pytest.raises(RecipeImportError, match="No recipe data"):
        parse_recipe(page({"@type": "Article"}), URL)
    with pytest.raises(RecipeImportError, match="No recipe data"):
        parse_recipe("<html>just text</html>", URL)


def test_recipe_without_ingredients_is_refused():
    with pytest.raises(RecipeImportError, match="no ingredients"):
        parse_recipe(page({**RECIPE, "recipeIngredient": []}), URL)


@pytest.mark.parametrize("iso,text", [
    ("PT20M", "20 mins"), ("PT1H", "1 hr"), ("PT2H5M", "2 hrs 5 mins"),
    ("PT90M", "1 hr 30 mins"), ("P1DT2H", "26 hrs"), ("PT0S", ""),
    ("20 minutes", ""), (None, ""),
])
def test_durations(iso, text):
    assert _duration(iso) == text


@pytest.mark.parametrize("value,expected", [
    ("6", ("6", "")), ("6 servings", ("6", "6 servings")), (8, ("8", "")),
    (["1 loaf"], ("1", "1 loaf")), ("Makes a dozen", ("", "Makes a dozen")),
    (None, ("", "")),
])
def test_servings(value, expected):
    assert _servings(value) == expected


# ------------------------------------------------------------------ fetching

def addrinfo(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in ips]


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, ctype="text/html"):
        self.status, self.headers, self.content_type = status, headers or {}, ctype
        self.content = MagicMock()
        self.content.read = AsyncMock(return_value=body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def session_returning(*responses):
    session = MagicMock()
    session.get = MagicMock(side_effect=list(responses))
    return session


def resolve(mapping):
    async def getaddrinfo(host, port, **kwargs):
        return addrinfo(*mapping[host])
    loop = MagicMock()
    loop.getaddrinfo = getaddrinfo
    return patch.object(recipe_import.asyncio, "get_running_loop", return_value=loop)


@pytest.mark.asyncio
async def test_fetches_a_public_page():
    session = session_returning(FakeResp(body=b"<html>ok</html>"))
    with resolve({"www.example.com": ["93.184.216.34"]}):
        final, text = await recipe_import.fetch_page(session, URL)
    assert (final, text) == (URL, "<html>ok</html>")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://192.168.68.52:8123/api/states", "http://localhost/", "file:///etc/passwd",
    "http://[::1]/",
])
async def test_local_and_non_http_addresses_are_never_fetched(url):
    session = session_returning()
    with resolve({"192.168.68.52": ["192.168.68.52"], "localhost": ["127.0.0.1"],
                  "::1": ["::1"]}):
        with pytest.raises(RecipeImportError):
            await recipe_import.fetch_page(session, url)
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_redirect_to_the_local_network_is_refused_before_following():
    session = session_returning(
        FakeResp(status=302, headers={"Location": "http://homeassistant.local:8123/"}))
    with resolve({"www.example.com": ["93.184.216.34"],
                  "homeassistant.local": ["192.168.68.52"]}):
        with pytest.raises(RecipeImportError, match="not a public"):
            await recipe_import.fetch_page(session, URL)
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_blocked_site_gets_an_explanation():
    session = session_returning(FakeResp(status=403))
    with resolve({"www.example.com": ["93.184.216.34"]}):
        with pytest.raises(RecipeImportError, match="HTTP 403.*Paprika app"):
            await recipe_import.fetch_page(session, URL)


@pytest.mark.asyncio
async def test_oversized_page_is_refused():
    big = b"x" * (recipe_import.MAX_PAGE_BYTES + 1)
    session = session_returning(FakeResp(body=big))
    with resolve({"www.example.com": ["93.184.216.34"]}):
        with pytest.raises(RecipeImportError, match="too large"):
            await recipe_import.fetch_page(session, URL)


@pytest.mark.asyncio
async def test_image_of_unknown_type_is_skipped_not_fatal():
    session = session_returning(FakeResp(body=b"GIF89a", ctype="image/gif"))
    with resolve({"www.example.com": ["93.184.216.34"]}):
        assert await recipe_import.fetch_image(session, URL + "a.gif") is None


# ---------------------------------------------------------------------- tool

@pytest.fixture
def client():
    c = MagicMock(spec_set=PaprikaClient)
    c._get_session = AsyncMock(return_value=MagicMock())
    c.list_recipes = AsyncMock(return_value=[])
    c.create_recipe = AsyncMock(return_value={"uid": "NEW", "name": "Sheet Pan Chicken & Veggies"})
    return c


def fetched(html=None, photo=(b"jpegbytes", "jpg")):
    return (
        patch("custom_components.paprika_llm.llm_api.fetch_page",
              AsyncMock(return_value=(URL, html or page(RECIPE)))),
        patch("custom_components.paprika_llm.llm_api.fetch_image",
              AsyncMock(return_value=photo)),
    )


@pytest.mark.asyncio
async def test_default_is_a_preview_that_writes_nothing(client):
    p1, p2 = fetched()
    with p1, p2:
        out = (await _call(tools(client)["import_recipe_from_url"], url=URL)).data
    assert out["saved"] is False
    assert out["preview"]["ingredients"][:2] == ["1 lb chicken thighs", "2 cups broccoli"]
    assert out["preview"]["has_photo"] is True
    client.create_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_creates_with_source_photo_and_yield_note(client):
    r = {**RECIPE, "recipeYield": "6 cookies"}
    p1, p2 = fetched(page(r))
    with p1, p2:
        out = (await _call(tools(client)["import_recipe_from_url"], url=URL, save=True,
                           name=" Chicken Night ")).data
    assert out == {"saved": True, "uid": "NEW", "name": "Sheet Pan Chicken & Veggies",
                   "photo_attached": True}
    kw = client.create_recipe.await_args.kwargs
    assert kw["name"] == "Chicken Night"
    assert kw["ingredients"].startswith("1 lb chicken thighs\n2 cups broccoli\n")
    assert kw["servings"] == "6" and kw["notes"] == "Yield: 6 cookies"
    assert kw["source"] == "Example Kitchen" and kw["source_url"] == URL
    assert kw["total_time"] == "1 hr 25 mins"
    assert kw["photo"] == (b"jpegbytes", "jpg")
    assert kw["generate_image"] is False


@pytest.mark.asyncio
async def test_photo_failure_still_saves(client):
    p1, p2 = fetched(photo=None)
    with p1, p2:
        out = (await _call(tools(client)["import_recipe_from_url"], url=URL, save=True)).data
    assert out["photo_attached"] is False
    assert client.create_recipe.await_args.kwargs["photo"] is None


@pytest.mark.asyncio
async def test_page_already_in_the_library_is_refused(client):
    client.list_recipes.return_value = [
        {"uid": "OLD", "name": "Chicken", "source_url": "http://example.com/recipes/sheet-pan-chicken"}]
    p1, p2 = fetched()
    with p1, p2, pytest.raises(HomeAssistantError, match="already in the library as 'Chicken'"):
        await _call(tools(client)["import_recipe_from_url"], url=URL, save=True)
    client.create_recipe.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_without_recipe_is_a_tool_error(client):
    p1, p2 = fetched("<html>no recipe</html>")
    with p1, p2, pytest.raises(HomeAssistantError, match="No recipe data"):
        await _call(tools(client)["import_recipe_from_url"], url=URL)
