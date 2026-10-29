"""Read a recipe from a web page.

Nearly every recipe site embeds its recipe as schema.org JSON-LD for search
engines, so that is what is parsed here -- the same data Paprika's own browser
import relies on. Pages without it are reported as such rather than guessed
at from the visible HTML.

The URL comes from a language model, so fetching is restricted to public
http(s) hosts: a link that resolves to the local network, including after a
redirect, is refused rather than fetched from inside Home Assistant.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from .paprika_client import ValidationError

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
# An honest user agent. Sites that block automated requests are reported
# as blocked; working around their bot protection is not this tool's job.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; paprika-llm-api; "
        "+https://github.com/iantrich/paprika-llm-api)"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}
IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


class RecipeImportError(ValidationError):
    """The page could not be fetched or holds no recipe."""


# ------------------------------------------------------------------ fetching


async def _check_public(url: str) -> None:
    """Refuse anything but http(s) to a host that resolves only to public IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RecipeImportError(f"Only http(s) recipe links can be imported: {url}")
    host = parsed.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as err:
        raise RecipeImportError(f"Could not resolve {host}: {err}") from err
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise RecipeImportError(
                f"{host} is not a public web address; only public recipe "
                "sites can be imported."
            )


async def _get(
    session: aiohttp.ClientSession, url: str, limit: int
) -> tuple[str, str, bytes]:
    """GET a public URL, following redirects by hand so each hop is checked.

    Returns the final URL, its content type and the body.
    """
    for _ in range(MAX_REDIRECTS + 1):
        await _check_public(url)
        async with session.get(
            url,
            headers=HEADERS,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308) and "Location" in resp.headers:
                url = urljoin(url, resp.headers["Location"])
                continue
            if resp.status != 200:
                raise RecipeImportError(
                    f"The site answered HTTP {resp.status} for {url}. Some "
                    "sites block automated requests; the user can still import "
                    "it from the Paprika app's browser."
                )
            body = await resp.content.read(limit + 1)
            if len(body) > limit:
                raise RecipeImportError(f"{url} is too large to import.")
            return url, resp.content_type or "", body
    raise RecipeImportError(f"Too many redirects fetching {url}.")


async def fetch_page(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Return the final URL and HTML text of a recipe page."""
    final_url, _, body = await _get(session, url.strip(), MAX_PAGE_BYTES)
    return final_url, body.decode("utf-8", errors="replace")


async def fetch_image(
    session: aiohttp.ClientSession, url: str
) -> tuple[bytes, str] | None:
    """Return a recipe photo's bytes and file extension, or None.

    A missing or unusable photo never stops an import.
    """
    try:
        _, content_type, body = await _get(session, url, MAX_IMAGE_BYTES)
    except (RecipeImportError, aiohttp.ClientError, asyncio.TimeoutError):
        return None
    ext = IMAGE_TYPES.get(content_type.lower())
    return (body, ext) if ext and body else None


# ------------------------------------------------------------------- parsing


class _JsonLdCollector(HTMLParser):
    """Collect the text of every <script type="application/ld+json">."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and any(
            k == "type" and (v or "").strip().lower() == "application/ld+json"
            for k, v in attrs
        ):
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._buf is not None:
            self.blocks.append("".join(self._buf))
            self._buf = None


def _is_recipe(node: Any) -> bool:
    kind = node.get("@type") if isinstance(node, dict) else None
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.lower() == "recipe" for k in kinds)


def _find_recipe(node: Any) -> dict[str, Any] | None:
    """Depth-first search for the first Recipe object, including in @graph."""
    if isinstance(node, list):
        for item in node:
            if (found := _find_recipe(item)) is not None:
                return found
    elif isinstance(node, dict):
        if _is_recipe(node):
            return node
        for key in ("@graph", "mainEntity", "mainEntityOfPage"):
            if key in node and (found := _find_recipe(node[key])) is not None:
                return found
    return None


def _text(value: Any) -> str:
    """Plain text from a JSON-LD string: entities decoded, tags dropped."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(html.unescape(value))  # some sites double-encode
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def _instructions(value: Any) -> list[str]:
    """Flatten recipeInstructions into lines, keeping section headings."""
    if isinstance(value, str):
        return [line for line in _text(value).splitlines() if line]
    if isinstance(value, dict):
        if str(value.get("@type", "")).lower() == "howtosection":
            heading = _text(value.get("name"))
            steps = _instructions(value.get("itemListElement"))
            return ([f"{heading}:"] if heading else []) + steps
        return _instructions(value.get("text") or value.get("name"))
    if isinstance(value, list):
        return [line for item in value for line in _instructions(item)]
    return []


def _duration(value: Any) -> str:
    """ISO 8601 duration ('PT1H10M') to Paprika's style ('1 hr 10 mins')."""
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+(?:\.\d+)?S)?)?",
        value.strip().upper(),
    )
    if not match:
        return ""
    days, hours, mins = (int(g) if g else 0 for g in match.groups())
    hours += days * 24
    hours, mins = hours + mins // 60, mins % 60
    parts = []
    if hours:
        parts.append(f"{hours} hr" + ("s" if hours > 1 else ""))
    if mins:
        parts.append(f"{mins} min" + ("s" if mins > 1 else ""))
    return " ".join(parts)


def _servings(value: Any) -> tuple[str, str]:
    """Split recipeYield into a whole-number servings and the original text.

    Paprika's servings field takes a number; yields like '1 loaf' keep their
    text so nothing is lost.
    """
    items = value if isinstance(value, list) else [value]
    texts = [_text(v) for v in items if _text(v)]
    for text in texts:
        if match := re.match(r"\s*(\d+)", text):
            number = int(match.group(1))
            if number >= 1:
                return str(number), "" if text.strip().isdigit() else text
    return "", texts[0] if texts else ""


def _image(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return next((url for url in map(_image, value) if url), "")
    if isinstance(value, dict):
        return _image(value.get("url") or value.get("contentUrl"))
    return ""


def _source(recipe: dict[str, Any], page_url: str) -> str:
    """The site or publisher name Paprika shows as the recipe's source."""
    publisher = recipe.get("publisher")
    if isinstance(publisher, list) and publisher:
        publisher = publisher[0]
    if isinstance(publisher, dict) and _text(publisher.get("name")):
        return _text(publisher["name"])
    return (urlparse(page_url).hostname or "").removeprefix("www.")


def parse_recipe(page_html: str, page_url: str) -> dict[str, Any]:
    """Extract the page's schema.org Recipe as Paprika recipe fields."""
    collector = _JsonLdCollector()
    collector.feed(page_html)
    recipe = None
    for block in collector.blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue  # one malformed block must not hide a good one
        if (recipe := _find_recipe(data)) is not None:
            break
    if recipe is None:
        raise RecipeImportError(
            "No recipe data found on that page. It may not be a recipe page, "
            "or the site does not publish structured recipe data."
        )

    ingredients = [
        # Some recipe plugins leave an empty field before a note: "(, peanut)".
        re.sub(r"\(\s*,\s*", "(", line)
        for line in (_text(i) for i in recipe.get("recipeIngredient") or [])
        if line
    ]
    directions = _instructions(recipe.get("recipeInstructions"))
    if not ingredients or not directions:
        raise RecipeImportError(
            "The page's recipe data has no "
            + ("ingredients" if not ingredients else "directions")
            + ", so it cannot be imported as-is."
        )
    servings, yield_text = _servings(recipe.get("recipeYield"))
    image = _image(recipe.get("image"))
    return {
        "name": _text(recipe.get("name")) or "Imported recipe",
        "ingredients": ingredients,
        "directions": "\n".join(directions),
        "description": _text(recipe.get("description")),
        "servings": servings,
        "yield_text": yield_text,
        "prep_time": _duration(recipe.get("prepTime")),
        "cook_time": _duration(recipe.get("cookTime")),
        "total_time": _duration(recipe.get("totalTime")),
        "source": _source(recipe, page_url),
        "source_url": page_url,
        "image_url": urljoin(page_url, image) if image else "",
    }
