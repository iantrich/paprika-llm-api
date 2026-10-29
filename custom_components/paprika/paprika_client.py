import asyncio
import base64
import gzip
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

from dateutil import parser as date_parser

import aiohttp

logger = logging.getLogger(__name__)

# A fuzzy name match at or above this ratio is confident enough to act on
# without asking. Below it, more than one candidate means we refuse rather
# than silently schedule the wrong recipe.
STRONG_NAME_MATCH_RATIO = 0.85

# Two candidates whose similarity differs by less than this are not
# meaningfully distinguishable; refuse rather than pick one.
AMBIGUOUS_MATCH_MARGIN = 0.05


class PaprikaAPIError(Exception):
    """Custom exception for Paprika API errors."""

    pass


class ValidationError(Exception):
    """Custom exception for input validation errors."""

    pass


class MealNotFoundError(Exception):
    """Exception raised when a meal/recipe cannot be found."""

    pass


class MealMatchAmbiguousError(Exception):
    """Exception raised when multiple potential meal matches are found."""

    def __init__(self, message: str, matches: List[Dict[str, Any]]):
        super().__init__(message)
        self.matches = matches


class PaprikaClient:
    """Client for interacting with the Paprika Recipe Manager API."""

    BASE_URL = "https://paprikaapp.com/api"

    def __init__(
        self,
        username: str,
        password: str,
        image_generator: Optional[Any] = None
    ):
        """
        Initialize the Paprika client.

        Args:
            username: Paprika account email
            password: Paprika account password
            image_generator: Optional ImageGenerator instance for AI image generation
        """
        self.username = username
        self.password = password
        # uid -> (index hash, recipe data). The /sync/recipes index carries only
        # {uid, hash}, so any question about recipe names means fetching each one
        # individually. The hash is the API's own change token: unchanged hash
        # means unchanged recipe, so it can be served from here instead.
        self._recipe_cache: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        # Serialises cache fills so two concurrent callers on a cold cache do not
        # both walk the whole library.
        self._recipe_cache_lock = asyncio.Lock()
        self.token: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.image_generator = image_generator

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self.session

    async def authenticate(self) -> Optional[str]:
        """
        Authenticate with Paprika API and get access token.

        Returns:
            Authentication token

        Raises:
            PaprikaAPIError: If authentication fails
        """
        session = await self._get_session()

        login_data = {"email": self.username, "password": self.password}

        try:
            # Use v1 API for authentication (v2 returns "Unrecognized client")
            async with session.post(
                f"{self.BASE_URL}/v1/account/login",
                data=login_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status != 200:
                    raise PaprikaAPIError(f"Login failed with status {response.status}")

                result = await response.json()

                if "result" not in result or "token" not in result["result"]:
                    raise PaprikaAPIError("Invalid login response format")

                self.token = result["result"]["token"]
                logger.info("Successfully authenticated with Paprika API")
                return self.token

        except aiohttp.ClientError as e:
            raise PaprikaAPIError(f"Network error during authentication: {str(e)}")
        except json.JSONDecodeError:
            raise PaprikaAPIError("Invalid JSON response from login endpoint")

    async def _make_authenticated_request(
        self, method: str, endpoint: str, **kwargs
    ) -> Dict[str, Any]:
        """
        Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            **kwargs: Additional arguments for aiohttp request

        Returns:
            JSON response data

        Raises:
            PaprikaAPIError: If request fails
        """
        if not self.token:
            await self.authenticate()

        session = await self._get_session()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with session.request(
                method, f"{self.BASE_URL}/v2{endpoint}", headers=headers, **kwargs
            ) as response:
                if response.status == 401:
                    # Token might be expired, re-authenticate
                    await self.authenticate()
                    headers["Authorization"] = f"Bearer {self.token}"

                    async with session.request(
                        method,
                        f"{self.BASE_URL}/v2{endpoint}",
                        headers=headers,
                        **kwargs,
                    ) as retry_response:
                        if retry_response.status != 200:
                            raise PaprikaAPIError(
                                f"Request failed with status {retry_response.status}"
                            )

                        return await retry_response.json()

                elif response.status != 200:
                    error_text = await response.text()
                    raise PaprikaAPIError(
                        f"Request failed with status {response.status}: {error_text}"
                    )

                return await response.json()

        except aiohttp.ClientError as e:
            raise PaprikaAPIError(f"Network error: {str(e)}")

    async def _make_v1_basic_auth_request(
        self, method: str, endpoint: str, **kwargs
    ) -> Dict[str, Any]:
        """Make a v1 API request with HTTP Basic Auth (used for meal plan endpoints)."""
        session = await self._get_session()
        headers = kwargs.pop("headers", {})

        credentials = f"{self.username}:{self.password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        headers["Authorization"] = f"Basic {encoded_credentials}"

        try:
            async with session.request(
                method, f"{self.BASE_URL}/v1{endpoint}", headers=headers, **kwargs
            ) as response:
                if response.status not in (200, 201, 204):
                    error_text = await response.text()
                    raise PaprikaAPIError(
                        f"Request failed with status {response.status}: {error_text}"
                    )

                if response.status == 204:
                    return {}

                try:
                    return await response.json()
                except aiohttp.ContentTypeError:
                    return {}

        except aiohttp.ClientError as e:
            raise PaprikaAPIError(f"Network error: {str(e)}")

    def _generate_uuid(self) -> str:
        """Generate a new uppercase UUID."""
        return str(uuid.uuid4()).upper()

    def _calculate_hash(self, recipe_dict: Dict[str, Any]) -> str:
        """
        Calculate SHA256 hash for a recipe object.

        Args:
            recipe_dict: Recipe data dictionary

        Returns:
            Hex-encoded SHA256 hash
        """
        # Remove hash field and sort keys for consistent hashing
        data = {k: v for k, v in recipe_dict.items() if k != "hash"}
        json_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(json_str.encode()).hexdigest()

    def _parse_quantity(self, quantity_str: str) -> Tuple[float, Optional[str]]:
        if not quantity_str or not isinstance(quantity_str, str):
            return (0.0, None)

        quantity_str = quantity_str.strip()
        if not quantity_str:
            return (0.0, None)

        match = re.match(r"^(-?\d+\.?\d*)\s*(.*)$", quantity_str)
        if match:
            numeric_value = float(match.group(1))
            unit = match.group(2).strip() if match.group(2) else None
            return (numeric_value, unit if unit else None)

        try:
            return (float(quantity_str), None)
        except ValueError:
            return (0.0, None)

    def _add_quantities(self, quantity1: str, quantity2: str) -> Tuple[str, bool]:
        if not isinstance(quantity1, str):
            quantity1 = str(quantity1)
        if not isinstance(quantity2, str):
            quantity2 = str(quantity2)

        value1, unit1 = self._parse_quantity(quantity1)
        value2, unit2 = self._parse_quantity(quantity2)

        result_unit = unit1 if unit1 else unit2
        result_value = value1 + value2

        if result_value < 0:
            raise PaprikaAPIError(
                f"Cannot subtract {quantity2} from {quantity1}: result would be negative"
            )

        if result_unit:
            if result_value == 1.0 and result_unit.endswith("s"):
                display_unit = result_unit[:-1]
            else:
                display_unit = result_unit
            if result_value == int(result_value):
                result_str = f"{int(result_value)} {display_unit}"
            else:
                result_str = f"{result_value} {display_unit}"
        else:
            if result_value == int(result_value):
                result_str = str(int(result_value))
            else:
                result_str = str(result_value)

        return (result_str, True)

    def _find_recipe_by_name(
        self, name: str, recipes: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        name_lower = name.lower()

        for recipe in recipes:
            if recipe.get("name", "").lower() == name_lower:
                return recipe

        matches = []
        for recipe in recipes:
            recipe_name = recipe.get("name", "").lower()
            ratio = SequenceMatcher(None, name_lower, recipe_name).ratio()
            if ratio > 0.6 or name_lower in recipe_name:
                matches.append((ratio, recipe))

        if not matches:
            raise MealNotFoundError(f"No recipe found matching '{name}'")

        matches.sort(key=lambda x: x[0], reverse=True)

        best_ratio, best_recipe = matches[0]
        if len(matches) > 1:
            runner_up_ratio = matches[1][0]
            # Refuse both when nothing is a confident match AND when the top
            # two are so close that picking the higher one is a coin flip
            # (e.g. "Chicken Curr" against "Chicken Curry" and
            # "Chicken Currys"). A strong best score is not on its own a
            # reason to act when an equally strong runner-up exists.
            if (
                best_ratio < STRONG_NAME_MATCH_RATIO
                or (best_ratio - runner_up_ratio) < AMBIGUOUS_MATCH_MARGIN
            ):
                raise MealMatchAmbiguousError(
                    f"Multiple recipes match '{name}'",
                    matches=[m[1] for m in matches[:5]],
                )

        return best_recipe

    def _parse_flexible_date(self, date_str: str) -> str:
        """Parse a date string into "YYYY-MM-DD".

        ISO 8601 is tried first and exactly: it is unambiguous, and passing it
        to dateutil's heuristic parser reorders month and day. Everything else
        falls back to dateutil with US month-first convention.
        """
        date_str = date_str.strip()

        try:
            return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            pass

        try:
            parsed = date_parser.parse(date_str, dayfirst=False)
            return parsed.strftime("%Y-%m-%d")
        except Exception as e:
            raise ValueError(f"Cannot parse date '{date_str}': {str(e)}")

    def _format_date_readable(self, date_str: str) -> str:
        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d")
            return parsed.strftime("%A, %B %d, %Y")
        except Exception:
            return date_str

    def _get_next_free_day(
        self, current_plan: List[Dict[str, Any]], meal_type: str = "dinner"
    ) -> str:
        planned_dates = set()
        for entry in current_plan:
            if entry.get("type", "").lower() == meal_type.lower():
                planned_dates.add(entry.get("date", ""))

        today = datetime.now().date()
        candidate = today + timedelta(days=1)
        for _ in range(365):
            candidate_str = candidate.strftime("%Y-%m-%d")
            if candidate_str not in planned_dates:
                return candidate_str
            candidate += timedelta(days=1)

        return (today + timedelta(days=1)).strftime("%Y-%m-%d")

    def _gzip_json(self, data: Dict[str, Any]) -> bytes:
        """
        Compress JSON data with gzip.

        Args:
            data: Data to compress

        Returns:
            Gzipped JSON bytes
        """
        json_str = json.dumps(data)
        return gzip.compress(json_str.encode("utf-8"))

    async def _download_image(self, image_url: str) -> Optional[tuple[bytes, str]]:
        """
        Download an image from URL and return raw bytes + hash.

        Args:
            image_url: URL of the image to download

        Returns:
            Tuple of (raw image bytes, SHA256 hash in UPPERCASE),
            or None if download fails
        """
        try:
            session = await self._get_session()
            async with session.get(image_url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    # Calculate SHA256 hash of the raw image data (UPPERCASE for Paprika compatibility)
                    photo_hash = hashlib.sha256(image_data).hexdigest().upper()
                    logger.info(f"Successfully downloaded image ({len(image_data)} bytes, hash: {photo_hash[:8]}...)")
                    return (image_data, photo_hash)
                else:
                    logger.warning(f"Failed to download image: HTTP {response.status}")
                    return None
        except Exception as e:
            logger.warning(f"Error downloading image from {image_url}: {e}")
            return None

    async def _prepare_image_for_recipe(
        self,
        recipe: Dict[str, Any],
        image_url: str
    ) -> Optional[tuple[bytes, str]]:
        """
        Download image and prepare recipe metadata for photo upload.

        Paprika requires uploading photos as multipart file data:
        - photo: filename (generated UUID)
        - photo_hash: SHA256 hash of image bytes (UPPERCASE)
        - photo_large: filename for larger version
        - photo_url: can be left as None

        Args:
            recipe: Recipe object to modify (modified in-place)
            image_url: URL of the image to download

        Returns:
            Tuple of (image_bytes, filename) if successful, None otherwise
        """
        # Download image data
        photo_result = await self._download_image(image_url)

        if photo_result:
            image_bytes, photo_hash = photo_result

            # Generate a filename (UUID format like Paprika uses)
            photo_filename = f"{self._generate_uuid()}.png"

            # Set recipe metadata for photo
            # Note: photo_hash must be calculated BEFORE setting photo field
            recipe["photo"] = photo_filename
            recipe["photo_hash"] = photo_hash
            recipe["photo_large"] = photo_filename  # Can be description, we use filename
            recipe["photo_url"] = None  # Leave as None, we're uploading the file
            recipe["image_url"] = None  # Not used by Paprika

            logger.info(f"Prepared image for recipe: {recipe.get('name', 'Unknown')} (filename: {photo_filename})")
            return (image_bytes, photo_filename)
        else:
            logger.warning(f"Failed to download image from: {image_url}")
            return None

    def _create_recipe_object(
        self,
        name: str,
        ingredients: str,
        directions: str,
        description: str = "",
        notes: str = "",
        servings: str = "",
        prep_time: str = "",
        cook_time: str = "",
        difficulty: str = "",
        uid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a recipe object with all required fields.

        Args:
            name: Recipe name
            ingredients: Recipe ingredients
            directions: Cooking directions
            description: Recipe description
            notes: Additional notes
            servings: Number of servings
            prep_time: Preparation time
            cook_time: Cooking time
            difficulty: Difficulty level
            uid: Recipe UID (generated if not provided)

        Returns:
            Complete recipe object
        """
        recipe = {
            "uid": uid or self._generate_uuid(),
            "name": name,
            "ingredients": ingredients,
            "directions": directions,
            "description": description,
            "notes": notes,
            "servings": servings,
            "prep_time": prep_time,
            "cook_time": cook_time,
            "total_time": "",
            "difficulty": difficulty,
            "source": "",
            "source_url": "",
            "categories": [],
            "rating": 0,
            "nutritional_info": "",
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "in_trash": False,
            "is_pinned": False,
            "on_favorites": False,
            "on_grocery_list": False,
            "image_url": "",
            "photo": "",
            "photo_hash": "",
            "photo_large": None,
            "photo_url": None,
            "scale": None,
        }

        # Calculate and set hash
        recipe["hash"] = self._calculate_hash(recipe)
        return recipe

    async def get_recipe_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        """
        Get a single recipe by UID.

        Args:
            uid: Recipe UID

        Returns:
            Recipe data if found, None otherwise

        Raises:
            PaprikaAPIError: If request fails
        """
        try:
            response = await self._make_authenticated_request(
                "GET", f"/sync/recipe/{uid}/"
            )

            recipe_data = response.get("result", {})

            if not recipe_data or recipe_data.get("in_trash", False):
                return None

            logger.info(f"Successfully fetched recipe by UID: {uid}")
            return recipe_data

        except Exception as e:
            logger.error(f"Failed to get recipe by UID {uid}: {str(e)}")
            return None

    async def _load_recipes_from_index(
        self,
        index_entries: List[Dict[str, Any]],
        prune: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        """Resolve /sync/recipes index entries to full recipe bodies.

        Only recipes whose index hash differs from the cached one are fetched.
        An unchanged library therefore costs a single index request instead of
        one request per recipe, which is what made create_recipe and
        list_recipes rate-limit an account of any size.

        Freshness comes from the index, which is always fetched live by the
        caller: a recipe edited on another device arrives with a new hash and
        is re-fetched. A stale entry cannot be served, because entries are only
        consulted when their uid appears in the current index.

        Args:
            index_entries: entries from GET /sync/recipes, each {uid, hash}
            prune: drop cached uids absent from index_entries. Only pass True
                when index_entries is the COMPLETE index; pruning against a
                truncated slice would evict live entries.

        Returns:
            uid -> recipe data, skipping any recipe that failed to fetch.
        """
        async with self._recipe_cache_lock:
            if prune:
                live = {e["uid"] for e in index_entries}
                for gone in self._recipe_cache.keys() - live:
                    del self._recipe_cache[gone]

            resolved: Dict[str, Dict[str, Any]] = {}
            fetched = 0
            for entry in index_entries:
                uid = entry["uid"]
                entry_hash = entry.get("hash")

                cached = self._recipe_cache.get(uid)
                if cached is not None and entry_hash is not None and cached[0] == entry_hash:
                    resolved[uid] = cached[1]
                    continue

                try:
                    recipe_response = await self._make_authenticated_request(
                        "GET", f"/sync/recipe/{uid}/"
                    )
                except Exception as e:
                    logger.warning(f"Failed to fetch recipe {uid}: {str(e)}")
                    continue

                recipe_data = recipe_response.get("result", {})
                if not isinstance(recipe_data, dict):
                    # Matches the pre-cache behaviour: a malformed recipe body
                    # is skipped rather than failing the whole listing.
                    logger.warning(f"Unexpected payload for recipe {uid}; skipping")
                    continue

                fetched += 1
                if entry_hash is not None:
                    self._recipe_cache[uid] = (entry_hash, recipe_data)
                resolved[uid] = recipe_data

            logger.info(
                f"Resolved {len(resolved)} recipes "
                f"({fetched} fetched, {len(resolved) - fetched} from cache)"
            )
            return resolved

    async def get_recipe_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """
        Get a single recipe by exact title match (case-sensitive).

        Args:
            title: Recipe title to search for

        Returns:
            Recipe data if found, None otherwise

        Raises:
            PaprikaAPIError: If request fails
        """
        try:
            # Get all recipes and filter by title
            response = await self._make_authenticated_request("GET", "/sync/recipes")
            recipe_list = response.get("result", [])

            if not recipe_list:
                return None

            # recipe_list is the complete index here, so the cache can be pruned.
            recipes = await self._load_recipes_from_index(recipe_list, prune=True)

            # Search for recipe with matching title
            for recipe_info in recipe_list:
                recipe_data = recipes.get(recipe_info["uid"])
                if recipe_data is None:
                    continue

                # Check for exact title match (case-sensitive) and not in trash
                if (
                    recipe_data.get("name") == title
                    and not recipe_data.get("in_trash", False)
                ):
                    logger.info(f"Successfully found recipe by title: {title}")
                    return recipe_data

            logger.info(f"No recipe found with title: {title}")
            return None

        except Exception as e:
            logger.error(f"Failed to get recipe by title {title}: {str(e)}")
            raise PaprikaAPIError(f"Failed to get recipe by title: {str(e)}")

    async def recipe_title_exists(self, title: str) -> Optional[str]:
        """
        Check if a recipe with the given title already exists.

        Args:
            title: Recipe title to check

        Returns:
            Recipe UID if title exists, None otherwise

        Raises:
            PaprikaAPIError: If request fails
        """
        recipe = await self.get_recipe_by_title(title)
        return recipe.get("uid") if recipe else None

    def _validate_recipe_name(self, name: str) -> None:
        """
        Validate recipe name.

        Args:
            name: Recipe name to validate

        Raises:
            ValidationError: If name is invalid
        """
        if not name:
            raise ValidationError("Recipe name cannot be empty")

        if not isinstance(name, str):
            raise ValidationError("Recipe name must be a string")

        name_stripped = name.strip()
        if not name_stripped:
            raise ValidationError("Recipe name cannot be empty or whitespace only")

        if len(name_stripped) > 200:
            raise ValidationError("Recipe name must be 200 characters or less")

    def _validate_ingredients(self, ingredients: Any) -> None:
        """
        Validate ingredients.

        Args:
            ingredients: Ingredients to validate (string or list)

        Raises:
            ValidationError: If ingredients are invalid
        """
        if not ingredients:
            raise ValidationError("Ingredients must be provided")

        # Accept both string and list formats
        if isinstance(ingredients, list):
            if not ingredients:
                raise ValidationError("Ingredients list cannot be empty")
            for i, ingredient in enumerate(ingredients):
                if not isinstance(ingredient, str) or not ingredient.strip():
                    raise ValidationError(
                        f"Ingredient at position {i} must be a non-empty string"
                    )
        elif isinstance(ingredients, str):
            if not ingredients.strip():
                raise ValidationError("Ingredients cannot be empty or whitespace only")
        else:
            raise ValidationError("Ingredients must be a string or list of strings")

    def _validate_directions(self, directions: str) -> None:
        """
        Validate recipe directions.

        Args:
            directions: Directions to validate

        Raises:
            ValidationError: If directions are invalid
        """
        if not directions:
            raise ValidationError("Recipe directions cannot be empty")

        if not isinstance(directions, str):
            raise ValidationError("Recipe directions must be a string")

        if not directions.strip():
            raise ValidationError(
                "Recipe directions cannot be empty or whitespace only"
            )

    def _validate_numeric_field(
        self, value: Any, field_name: str, min_val: int = 0
    ) -> None:
        """
        Validate numeric fields (servings, times).

        Args:
            value: Value to validate
            field_name: Name of the field (for error messages)
            min_val: Minimum allowed value (default: 0)

        Raises:
            ValidationError: If value is invalid
        """
        if value is None or value == "":
            return  # Optional fields can be empty

        # Try to convert string to int
        if isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                raise ValidationError(
                    f"{field_name} must be a number, got: '{value}'"
                )

        if not isinstance(value, int):
            raise ValidationError(f"{field_name} must be an integer")

        if value < min_val:
            if min_val == 0:
                raise ValidationError(f"{field_name} cannot be negative")
            else:
                raise ValidationError(
                    f"{field_name} must be at least {min_val}, got: {value}"
                )

    def _validate_string_field(
        self, value: Any, field_name: str, required: bool = False
    ) -> None:
        """
        Validate optional string fields.

        Args:
            value: Value to validate
            field_name: Name of the field (for error messages)
            required: Whether field is required

        Raises:
            ValidationError: If value is invalid
        """
        if value is None or value == "":
            if required:
                raise ValidationError(f"{field_name} is required")
            return  # Optional fields can be empty

        if not isinstance(value, str):
            raise ValidationError(f"{field_name} must be a string")

    async def delete_recipe(self, uid: str) -> Dict[str, Any]:
        """
        Delete a recipe by moving it to trash (soft delete).

        Args:
            uid: Recipe UID to delete

        Returns:
            Deleted recipe data with confirmation

        Raises:
            PaprikaAPIError: If deletion fails or recipe not found
        """
        try:
            # First, get the existing recipe
            recipe_data = await self.get_recipe_by_uid(uid)

            if not recipe_data:
                raise PaprikaAPIError(f"Recipe with UID '{uid}' not found")

            # Check if already in trash
            if recipe_data.get("in_trash", False):
                return {
                    "already_deleted": True,
                    "name": recipe_data.get("name", ""),
                    "uid": uid,
                }

            # Set in_trash to True
            recipe_data["in_trash"] = True
            recipe_data["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe_data["hash"] = self._calculate_hash(recipe_data)

            # Gzip the recipe data
            gzipped_data = self._gzip_json(recipe_data)

            # Create multipart form data
            data = aiohttp.FormData()
            data.add_field(
                "data", gzipped_data, content_type="application/octet-stream"
            )

            await self._make_authenticated_request(
                "POST", f"/sync/recipe/{uid}/", data=data
            )

            logger.info(f"Successfully deleted recipe: {recipe_data['name']}")
            return recipe_data

        except Exception as e:
            logger.error(f"Failed to delete recipe {uid}: {str(e)}")
            raise PaprikaAPIError(f"Failed to delete recipe: {str(e)}")

    async def create_recipe(
        self,
        name: str,
        ingredients: str,
        directions: str,
        description: str = "",
        notes: str = "",
        servings: str = "",
        prep_time: str = "",
        cook_time: str = "",
        difficulty: str = "",
        generate_image: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a new recipe in Paprika.

        Args:
            name: Recipe name
            ingredients: Recipe ingredients (one per line)
            directions: Cooking directions
            description: Recipe description
            notes: Additional notes
            servings: Number of servings
            prep_time: Preparation time
            cook_time: Cooking time
            difficulty: Difficulty level
            generate_image: Whether to auto-generate AI food photography (default: True)

        Returns:
            Created recipe data

        Raises:
            ValidationError: If input validation fails
            PaprikaAPIError: If creation fails or duplicate title exists
        """
        # Validate required fields
        self._validate_recipe_name(name)
        self._validate_ingredients(ingredients)
        self._validate_directions(directions)

        # Validate optional string fields
        self._validate_string_field(description, "Description")
        self._validate_string_field(notes, "Notes")
        self._validate_string_field(difficulty, "Difficulty")

        # Validate numeric fields
        if servings:
            self._validate_numeric_field(servings, "Servings", min_val=1)
        if prep_time:
            # For time fields, we accept string format like "15 mins"
            # Just validate it's a string if provided
            self._validate_string_field(prep_time, "Prep time")
        if cook_time:
            self._validate_string_field(cook_time, "Cook time")

        # Check for duplicate title
        existing_uid = await self.recipe_title_exists(name)
        if existing_uid:
            logger.warning(f"Duplicate recipe title detected: {name} (UID: {existing_uid})")
            raise PaprikaAPIError(
                f"Recipe '{name}' already exists (UID: {existing_uid}). "
                "Please choose a different name or use update_recipe to modify the existing one."
            )

        # Generate AI image BEFORE creating recipe (if enabled)
        image_url = None
        image_data = None
        if generate_image and self.image_generator:
            try:
                logger.info(f"Generating AI image for recipe: {name}")
                image_url = await self.image_generator.generate_food_image(
                    name, description
                )

                if not image_url:
                    logger.warning(f"Image generation returned no URL for: {name}")
            except Exception as e:
                # Never fail recipe creation due to image generation
                logger.warning(
                    f"Image generation failed for '{name}': {e}",
                    exc_info=True
                )

        # Create recipe object
        recipe = self._create_recipe_object(
            name=name,
            ingredients=ingredients,
            directions=directions,
            description=description,
            notes=notes,
            servings=servings,
            prep_time=prep_time,
            cook_time=cook_time,
            difficulty=difficulty,
        )

        # Prepare image for upload if generation succeeded
        photo_filename = None
        if image_url:
            image_result = await self._prepare_image_for_recipe(recipe, image_url)
            if image_result:
                image_data, photo_filename = image_result

        # Calculate hash after all fields are set
        recipe["hash"] = self._calculate_hash(recipe)

        # Create multipart form data with recipe + optional photo
        data = aiohttp.FormData()
        data.add_field("data", self._gzip_json(recipe), content_type="application/octet-stream")

        # Add photo_upload field if we have an image (required for Paprika to accept photos)
        if image_data and photo_filename:
            data.add_field(
                "photo_upload",
                image_data,
                filename=photo_filename,
                content_type="image/png"
            )
            logger.info(f"Uploading recipe with photo_upload field: {photo_filename}")

        try:
            await self._make_authenticated_request(
                "POST", f"/sync/recipe/{recipe['uid']}/", data=data
            )

            if image_url:
                logger.info(f"Successfully created recipe '{name}' with AI-generated image")
            else:
                logger.info(f"Successfully created recipe: {name}")

            return recipe

        except Exception as e:
            logger.error(f"Failed to create recipe {name}: {str(e)}")
            raise PaprikaAPIError(f"Failed to create recipe: {str(e)}")

    async def update_recipe(
        self,
        uid: str,
        name: str,
        ingredients: str,
        directions: str,
        description: str = "",
        notes: str = "",
        servings: str = "",
        prep_time: str = "",
        cook_time: str = "",
        difficulty: str = "",
    ) -> Dict[str, Any]:
        """
        Update an existing recipe in Paprika.

        Args:
            uid: Recipe UID to update
            name: Recipe name
            ingredients: Recipe ingredients
            directions: Cooking directions
            description: Recipe description
            notes: Additional notes
            servings: Number of servings
            prep_time: Preparation time
            cook_time: Cooking time
            difficulty: Difficulty level

        Returns:
            Updated recipe data

        Raises:
            PaprikaAPIError: If update fails
        """
        recipe = self._create_recipe_object(
            name=name,
            ingredients=ingredients,
            directions=directions,
            description=description,
            notes=notes,
            servings=servings,
            prep_time=prep_time,
            cook_time=cook_time,
            difficulty=difficulty,
            uid=uid,
        )

        # Gzip the recipe data
        gzipped_data = self._gzip_json(recipe)

        # Create multipart form data
        data = aiohttp.FormData()
        data.add_field("data", gzipped_data, content_type="application/octet-stream")

        try:
            await self._make_authenticated_request(
                "POST", f"/sync/recipe/{uid}/", data=data
            )

            logger.info(f"Successfully updated recipe: {name}")
            return recipe

        except Exception as e:
            logger.error(f"Failed to update recipe {name}: {str(e)}")
            raise PaprikaAPIError(f"Failed to update recipe: {str(e)}")

    async def update_recipe_partial(self, uid: str, **kwargs) -> Dict[str, Any]:
        """
        Partially update an existing recipe in Paprika.
        Only updates the fields that are provided.

        Args:
            uid: Recipe UID to update
            **kwargs: Fields to update (name, ingredients, directions, etc.)

        Returns:
            Updated recipe data

        Raises:
            ValidationError: If input validation fails
            PaprikaAPIError: If update fails
        """
        # Validate UID
        if not uid or not isinstance(uid, str) or not uid.strip():
            raise ValidationError("Recipe UID is required and must be a non-empty string")

        # Validate provided fields
        for field, value in kwargs.items():
            if value is None:
                continue  # Skip None values

            # Validate even if empty string (some fields shouldn't be empty)
            if field == "name":
                self._validate_recipe_name(value)
            elif field == "ingredients":
                self._validate_ingredients(value)
            elif field == "directions":
                self._validate_directions(value)
            elif field in ["description", "notes", "difficulty", "prep_time", "cook_time"]:
                if value != "":  # Only validate non-empty optional fields
                    self._validate_string_field(value, field.replace("_", " ").title())
            elif field == "servings":
                if value != "":  # Only validate non-empty servings
                    self._validate_numeric_field(value, "Servings", min_val=1)

        try:
            # First, get the existing recipe
            response = await self._make_authenticated_request(
                "GET", f"/sync/recipe/{uid}/"
            )
            existing_recipe = response.get("result", {})

            if not existing_recipe:
                raise PaprikaAPIError(f"Recipe with UID {uid} not found")

            # Update only the provided fields
            for field, value in kwargs.items():
                if value is not None and value != "":
                    existing_recipe[field] = value

            # Recalculate hash and update timestamp
            existing_recipe["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            existing_recipe["hash"] = self._calculate_hash(existing_recipe)

            # Gzip the recipe data
            gzipped_data = self._gzip_json(existing_recipe)

            # Create multipart form data
            data = aiohttp.FormData()
            data.add_field(
                "data", gzipped_data, content_type="application/octet-stream"
            )

            await self._make_authenticated_request(
                "POST", f"/sync/recipe/{uid}/", data=data
            )

            logger.info(
                f"Successfully partially updated recipe: {existing_recipe['name']}"
            )
            return existing_recipe

        except Exception as e:
            logger.error(f"Failed to partially update recipe {uid}: {str(e)}")
            raise PaprikaAPIError(f"Failed to partially update recipe: {str(e)}")

    async def list_recipes(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        List recipes from Paprika. Each recipe may include a last_planned date
        (YYYY-MM-DD) if it appears in the meal plan history.

        Args:
            limit: Maximum number of recipes to return

        Returns:
            List of recipe data

        Raises:
            PaprikaAPIError: If listing fails
        """
        try:
            # Fetch meal plan history to compute last_planned dates
            meal_plan = []
            try:
                today = datetime.now().date()
                start_date = (today - timedelta(days=366)).strftime("%Y-%m-%d")
                end_date = today.strftime("%Y-%m-%d")
                meal_plan = await self.get_meal_plan(
                    start_date=start_date, end_date=end_date, meal_type=None
                )
            except Exception as e:
                logger.warning(f"Failed to fetch meal plan for last_planned: {str(e)}")

            recipe_last_planned: Dict[str, str] = {}
            for meal_entry in meal_plan:
                meal_id = meal_entry.get("meal_ID", "")
                meal_date = meal_entry.get("date", "")
                if meal_id and meal_date:
                    existing = recipe_last_planned.get(meal_id, "")
                    if meal_date > existing:
                        recipe_last_planned[meal_id] = meal_date

            # First get the list of recipe UIDs
            response = await self._make_authenticated_request("GET", "/sync/recipes")
            recipe_list = response.get("result", [])

            if not recipe_list:
                return []

            recipe_list = recipe_list[:limit]

            # recipe_list was truncated to `limit` above, so it is NOT the
            # complete index and must not be used to prune the cache.
            resolved = await self._load_recipes_from_index(recipe_list, prune=False)

            recipes = []
            for recipe_info in recipe_list:
                uid = recipe_info["uid"]
                recipe_data = resolved.get(uid)
                if recipe_data is None:
                    continue

                if recipe_data.get("in_trash", False):
                    continue

                if uid in recipe_last_planned:
                    recipe_data["last_planned"] = recipe_last_planned[uid]

                recipes.append(recipe_data)

            logger.info(f"Successfully fetched {len(recipes)} recipes")
            return recipes

        except Exception as e:
            logger.error(f"Failed to list recipes: {str(e)}")
            raise PaprikaAPIError(f"Failed to list recipes: {str(e)}")

    async def regenerate_recipe_image(
        self,
        uid: str,
        custom_instructions: str = ""
    ) -> Dict[str, Any]:
        """
        Regenerate AI image for an existing recipe with optional custom styling.

        Args:
            uid: Recipe UID
            custom_instructions: Optional styling instructions
                (e.g., "rustic style, less cream visible")

        Returns:
            Updated recipe data with new image

        Raises:
            PaprikaAPIError: If regeneration fails or recipe not found
            ValueError: If image_generator is not configured
        """
        if not self.image_generator:
            raise ValueError(
                "Image generator not configured. "
                "Set REPLICATE_API_TOKEN environment variable."
            )

        try:
            # Get existing recipe
            recipe_data = await self.get_recipe_by_uid(uid)

            if not recipe_data:
                raise PaprikaAPIError(f"Recipe with UID '{uid}' not found")

            recipe_name = recipe_data.get("name", "")
            recipe_description = recipe_data.get("description", "")

            # Generate new image with custom instructions
            logger.info(
                f"Regenerating image for recipe: {recipe_name} "
                f"(instructions: {custom_instructions or 'none'})"
            )

            if custom_instructions:
                image_url = await self.image_generator.generate_custom_food_image(
                    recipe_name=recipe_name,
                    description=recipe_description,
                    custom_instructions=custom_instructions,
                )
            else:
                image_url = await self.image_generator.generate_food_image(
                    recipe_name=recipe_name, description=recipe_description
                )

            if not image_url:
                raise PaprikaAPIError(
                    f"Image generation failed for recipe '{recipe_name}'"
                )

            # Prepare image for upload
            image_result = await self._prepare_image_for_recipe(recipe_data, image_url)

            if not image_result:
                raise PaprikaAPIError(
                    f"Failed to download image from URL: {image_url}"
                )

            image_bytes, photo_filename = image_result

            # Update timestamp and recalculate hash
            recipe_data["created"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            recipe_data["hash"] = self._calculate_hash(recipe_data)

            # Debug: Log what we're uploading
            logger.info(
                f"Uploading recipe with photo_upload: "
                f"photo='{recipe_data.get('photo')}', "
                f"photo_hash='{recipe_data.get('photo_hash')}', "
                f"photo_large='{recipe_data.get('photo_large')}'"
            )

            # Create multipart form data with recipe + photo
            data = aiohttp.FormData()
            data.add_field(
                "data", self._gzip_json(recipe_data), content_type="application/octet-stream"
            )
            data.add_field(
                "photo_upload",
                image_bytes,
                filename=photo_filename,
                content_type="image/png"
            )

            await self._make_authenticated_request(
                "POST", f"/sync/recipe/{uid}/", data=data
            )

            logger.info(f"Successfully regenerated image for recipe: {recipe_name}")
            return recipe_data

        except Exception as e:
            logger.error(f"Failed to regenerate image for recipe {uid}: {str(e)}")
            raise PaprikaAPIError(f"Failed to regenerate image: {str(e)}")

    async def get_meal_plan(
        self,
        num_days: Optional[int] = None,
        meal_type: Optional[str] = "dinner",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get meal plan entries from Paprika.

        Args:
            num_days: Days from today (default: 10, ignored if start_date/end_date provided)
            meal_type: Filter by meal type (default: "dinner", None for all)
            start_date: Start date YYYY-MM-DD (optional)
            end_date: End date YYYY-MM-DD (optional)

        Returns:
            List of meal plan entries with meal, meal_ID, date, type fields
        """
        try:
            try:
                response = await self._make_v1_basic_auth_request("GET", "/sync/meals")
            except PaprikaAPIError as e:
                if "404" in str(e):
                    response = await self._make_v1_basic_auth_request("GET", "/sync/meals/")
                else:
                    raise

            meal_plan_data = response.get("result", {})
            if isinstance(meal_plan_data, list):
                all_entries = meal_plan_data
            else:
                all_entries = meal_plan_data.get("mealplan", meal_plan_data.get("meals", []))

            if not all_entries:
                return []

            today = datetime.now().date()

            if start_date and end_date:
                try:
                    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
                    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
                except ValueError as e:
                    raise ValueError(f"Invalid date format. Expected YYYY-MM-DD: {str(e)}")
            elif start_date:
                try:
                    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d").date()
                    end_date_obj = today + timedelta(days=num_days if num_days is not None else 10)
                except ValueError as e:
                    raise ValueError(f"Invalid start_date format. Expected YYYY-MM-DD: {str(e)}")
            elif end_date:
                try:
                    start_date_obj = today
                    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d").date()
                except ValueError as e:
                    raise ValueError(f"Invalid end_date format. Expected YYYY-MM-DD: {str(e)}")
            else:
                num_days_val = num_days if num_days is not None else 10
                start_date_obj = today
                end_date_obj = today + timedelta(days=num_days_val)

            meal_type_map = {1: "breakfast", 2: "dinner", 3: "lunch"}
            reverse_meal_type_map = {"breakfast": 1, "dinner": 2, "lunch": 3}

            filtered_entries = []
            for entry in all_entries:
                entry_date_str = entry.get("date", "")
                if not entry_date_str:
                    continue

                try:
                    if " " in entry_date_str:
                        entry_date = datetime.strptime(entry_date_str.split()[0], "%Y-%m-%d").date()
                        date_str_for_result = entry_date_str.split()[0]
                    else:
                        entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
                        date_str_for_result = entry_date_str

                    if start_date_obj <= entry_date <= end_date_obj:
                        entry_type = entry.get("type")
                        entry_type_str = None
                        if isinstance(entry_type, int):
                            entry_type_str = meal_type_map.get(entry_type, "dinner")
                        elif isinstance(entry_type, str):
                            entry_type_str = entry_type.lower()

                        if meal_type is None:
                            include_entry = True
                        else:
                            meal_type_int = reverse_meal_type_map.get(meal_type.lower())
                            include_entry = (
                                entry_type_str == meal_type.lower()
                                or entry_type == meal_type_int
                            )

                        if include_entry:
                            recipe_uid = (
                                entry.get("recipe_uid")
                                or entry.get("recipe_id")
                                or entry.get("meal_id")
                            )
                            recipe_name = (
                                entry.get("name")
                                or entry.get("recipe_name")
                                or entry.get("meal")
                            )
                            filtered_entries.append({
                                "meal": recipe_name or "Unknown",
                                "meal_ID": recipe_uid or "",
                                "date": date_str_for_result,
                                "type": entry_type_str or "dinner",
                            })
                except ValueError:
                    continue

            filtered_entries.sort(key=lambda x: x.get("date", ""))
            logger.info(f"Successfully fetched {len(filtered_entries)} meal plan entries")
            return filtered_entries

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to get meal plan: {error_msg}")
            raise PaprikaAPIError(f"Failed to get meal plan: {error_msg}")

    async def add_meal_to_plan(
        self,
        meal: Optional[str] = None,
        meal_id: Optional[str] = None,
        date: Optional[str] = None,
        meal_type: str = "dinner",
    ) -> Dict[str, Any]:
        """Add a meal to the meal plan."""
        recipe_uid = meal_id
        recipe_name = None

        if meal and not meal_id:
            recipes = await self.list_recipes(limit=1000)
            matched_recipe = self._find_recipe_by_name(meal, recipes)
            recipe_uid = matched_recipe["uid"]
            recipe_name = matched_recipe["name"]
        elif meal_id:
            try:
                recipe_response = await self._make_authenticated_request(
                    "GET", f"/sync/recipe/{meal_id}/"
                )
                recipe_data = recipe_response.get("result", {})
                recipe_name = recipe_data.get("name", "")
            except Exception:
                recipe_name = "Unknown Recipe"

        if not recipe_uid:
            raise MealNotFoundError("Either meal name or meal_id must be provided")

        if date:
            parsed_date = self._parse_flexible_date(date)
        else:
            current_plan = await self.get_meal_plan(num_days=365, meal_type=None)
            parsed_date = self._get_next_free_day(current_plan, meal_type)

        meal_entry_id = self._generate_uuid()
        meal_type_map = {"breakfast": 1, "dinner": 2, "lunch": 3}
        meal_type_int = meal_type_map.get(meal_type.lower(), 2)
        date_with_time = f"{parsed_date} 00:00:00"

        meal_entry = {
            "uid": meal_entry_id,
            "recipe_uid": recipe_uid,
            "name": recipe_name or "Unknown",
            "date": date_with_time,
            "type": meal_type_int,
            "order_flag": 0,
            "deleted": False,
        }

        try:
            meals_array = [meal_entry]
            gzipped_data = self._gzip_json(meals_array)
            data = aiohttp.FormData()
            data.add_field(
                "data", gzipped_data, content_type="application/octet-stream", filename="data"
            )
            await self._make_v1_basic_auth_request("POST", "/sync/meals/", data=data)

            logger.info(f"Successfully added meal '{recipe_name}' to meal plan on {parsed_date}")
            return {
                "meal": recipe_name or "Unknown",
                "meal_ID": recipe_uid,
                "date": parsed_date,
                "type": meal_type,
            }

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to add meal to plan: {error_msg}")
            raise PaprikaAPIError(f"Failed to add meal to plan: {error_msg}")

    async def remove_meal_from_plan(
        self,
        meal: Optional[str] = None,
        meal_id: Optional[str] = None,
        date: Optional[str] = None,
        meal_type: str = "dinner",
    ) -> Dict[str, Any]:
        """Remove a meal from the meal plan."""
        current_plan = await self.get_meal_plan(num_days=365, meal_type=None)

        if not current_plan:
            raise MealNotFoundError("No meals found in meal plan")

        meal_to_remove = None

        if not meal and not meal_id and not date:
            sorted_plan = sorted(
                current_plan,
                # reverse=True sorts True first, so the key must be True for
                # the entry whose type MATCHES: with `!=` the sort actively
                # preferred the wrong meal type and deleted it.
                key=lambda x: (x.get("date", ""), x.get("type", "").lower() == meal_type.lower()),
                reverse=True,
            )
            meal_to_remove = sorted_plan[0]
        elif date:
            parsed_date = self._parse_flexible_date(date)
            matching_meals = [
                m for m in current_plan
                if m.get("date") == parsed_date
                and (meal_type is None or m.get("type", "").lower() == meal_type.lower())
            ]
            if meal or meal_id:
                search_value = (meal or "").lower() if meal else meal_id
                matching_meals = [
                    m for m in matching_meals
                    if (m.get("meal", "").lower() == search_value)
                    or (m.get("meal_ID", "") == search_value)
                ]
            if not matching_meals:
                raise MealNotFoundError(f"No meal found on {parsed_date} matching the criteria")
            meal_to_remove = matching_meals[0]
        elif meal or meal_id:
            search_value = (meal or "").lower() if meal else meal_id
            matching_meals = [
                m for m in current_plan
                if (m.get("meal", "").lower() == search_value)
                or (m.get("meal_ID", "") == search_value)
            ]
            if date:
                parsed_date = self._parse_flexible_date(date)
                matching_meals = [m for m in matching_meals if m.get("date") == parsed_date]
            if not matching_meals:
                raise MealNotFoundError(f"No meal found matching '{search_value}'")
            if len(matching_meals) > 1:
                matching_meals.sort(key=lambda x: x.get("date", ""), reverse=True)
            meal_to_remove = matching_meals[0]

        if not meal_to_remove:
            raise MealNotFoundError("No meal found matching the criteria")

        entry_date = meal_to_remove.get("date", "")
        entry_type = meal_to_remove.get("type", "")
        entry_recipe_uid = meal_to_remove.get("meal_ID", "")

        try:
            response = await self._make_v1_basic_auth_request("GET", "/sync/meals/")
            meal_plan_data = response.get("result", {})
            if isinstance(meal_plan_data, list):
                all_entries = meal_plan_data
            else:
                all_entries = meal_plan_data.get("mealplan", meal_plan_data.get("meals", []))

            meal_type_map = {1: "breakfast", 2: "dinner", 3: "lunch"}
            active_entries = [e for e in all_entries if not e.get("deleted", False)]

            entry_to_delete = None
            for entry in active_entries:
                entry_date_check = entry.get("date", "")
                entry_date_check_clean = (
                    entry_date_check.split()[0] if " " in entry_date_check else entry_date_check
                )
                entry_type_raw = entry.get("type", "")
                if isinstance(entry_type_raw, int):
                    entry_type_check = meal_type_map.get(entry_type_raw, "dinner")
                elif isinstance(entry_type_raw, str):
                    entry_type_check = entry_type_raw.lower()
                else:
                    entry_type_check = "dinner"
                entry_recipe_check = (
                    entry.get("recipe_uid") or entry.get("recipe_id") or entry.get("meal_id")
                )
                if (
                    entry_date_check_clean == entry_date
                    and entry_type_check == entry_type.lower()
                    and entry_recipe_check == entry_recipe_uid
                ):
                    entry_to_delete = entry
                    break

            if not entry_to_delete:
                raise MealNotFoundError("Meal plan entry not found for deletion")

            entry_id = entry_to_delete.get("uid") or entry_to_delete.get("id") or entry_recipe_uid

            if not entry_id:
                raise MealNotFoundError("Meal plan entry UID not found for deletion")

            soft_delete_entry = {"uid": entry_id, "deleted": True}
            meals_array = [soft_delete_entry]
            gzipped_data = self._gzip_json(meals_array)
            data = aiohttp.FormData()
            data.add_field(
                "data", gzipped_data, content_type="application/octet-stream", filename="data"
            )
            await self._make_v1_basic_auth_request("POST", "/sync/meals/", data=data)

            logger.info(
                f"Successfully removed meal '{meal_to_remove.get('meal')}' from meal plan on {entry_date}"
            )
            return {
                "result": "Meal removed",
                "meal": meal_to_remove.get("meal", "Unknown"),
                "meal_ID": meal_to_remove.get("meal_ID", ""),
                "date": entry_date,
                "type": entry_type,
            }

        except (MealNotFoundError, MealMatchAmbiguousError):
            # These carry information the handler layer maps to a specific
            # message; wrapping them in PaprikaAPIError below would hide it.
            raise
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to remove meal from plan: {error_msg}")
            raise PaprikaAPIError(f"Failed to remove meal from plan: {error_msg}")

    async def get_grocery_lists(self) -> List[Dict[str, Any]]:
        try:
            response = await self._make_authenticated_request(
                "GET", "/sync/grocerylists"
            )
            grocery_lists = response.get("result", [])

            if not grocery_lists:
                return []

            result = []
            for grocery_list in grocery_lists:
                if "name" in grocery_list:
                    result.append(
                        {
                            "name": grocery_list["name"],
                            "is_default": grocery_list.get("is_default", False),
                        }
                    )

            logger.info(f"Found {len(result)} grocery lists")
            return result

        except Exception as e:
            logger.error(f"Failed to get grocery lists: {str(e)}")
            raise PaprikaAPIError(f"Failed to get grocery lists: {str(e)}")

    async def get_default_list_uuid(self) -> Optional[str]:
        try:
            response = await self._make_authenticated_request(
                "GET", "/sync/grocerylists"
            )
            grocery_lists = response.get("result", [])

            if not grocery_lists:
                raise PaprikaAPIError("No grocery lists found")

            for grocery_list in grocery_lists:
                if grocery_list.get("is_default"):
                    uid = grocery_list.get("uid")
                    logger.info(
                        f"Found default grocery list: {grocery_list.get('name')} (UID: {uid})"
                    )
                    return uid

            raise PaprikaAPIError("No default grocery list found")

        except Exception as e:
            logger.error(f"Failed to get default grocery list UUID: {str(e)}")
            raise PaprikaAPIError(f"Failed to get default grocery list: {str(e)}")

    async def _get_list_uuid_by_name(
        self, grocery_list_name: Optional[str] = None
    ) -> str:
        if grocery_list_name is None:
            return await self.get_default_list_uuid()

        try:
            response = await self._make_authenticated_request(
                "GET", "/sync/grocerylists"
            )
            grocery_lists = response.get("result", [])

            for grocery_list in grocery_lists:
                if grocery_list.get("name") == grocery_list_name:
                    return grocery_list.get("uid")

            valid_names = []
            for grocery_list in grocery_lists:
                if "name" in grocery_list:
                    valid_names.append(grocery_list["name"])

            if valid_names:
                valid_names_str = ", ".join([f"'{name}'" for name in valid_names])
                raise PaprikaAPIError(
                    f"Grocery list '{grocery_list_name}' not found. Valid grocery lists are: {valid_names_str}"
                )
            else:
                raise PaprikaAPIError(
                    f"Grocery list '{grocery_list_name}' not found. No grocery lists are available."
                )

        except Exception as e:
            if isinstance(e, PaprikaAPIError):
                raise
            logger.error(f"Failed to get grocery list UUID: {str(e)}")
            raise PaprikaAPIError(f"Failed to get grocery list UUID: {str(e)}")

    async def get_groceries(
        self, grocery_list_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        try:
            list_uuid = await self._get_list_uuid_by_name(grocery_list_name)
            response = await self._make_authenticated_request("GET", "/sync/groceries")
            groceries = response.get("result", [])

            filtered_groceries = []
            for grocery in groceries:
                if grocery.get("list_uid") == list_uuid:
                    filtered_groceries.append(
                        {
                            "name": grocery.get("name", ""),
                            "quantity": grocery.get("quantity", ""),
                        }
                    )

            logger.info(f"Found {len(filtered_groceries)} groceries in list")
            return filtered_groceries

        except Exception as e:
            logger.error(f"Failed to get groceries: {str(e)}")
            raise PaprikaAPIError(f"Failed to get groceries: {str(e)}")

    async def get_grocery(
        self, item_name: str, grocery_list_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        try:
            list_uuid = await self._get_list_uuid_by_name(grocery_list_name)
            response = await self._make_authenticated_request("GET", "/sync/groceries")
            groceries = response.get("result", [])

            for grocery in groceries:
                grocery_name = grocery.get("name", "").strip()
                if (
                    grocery_name == item_name.strip()
                    and grocery.get("list_uid") == list_uuid
                ):
                    return grocery

            return None

        except Exception as e:
            logger.error(f"Failed to get grocery item '{item_name}': {str(e)}")
            raise PaprikaAPIError(f"Failed to get grocery item: {str(e)}")

    async def add_grocery(
        self,
        item_name: str,
        quantity: Union[int, str] = 1,
        grocery_list_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not item_name or not item_name.strip():
            raise PaprikaAPIError("Item name cannot be empty")

        try:
            quantity_str = str(quantity)

            list_uuid = await self._get_list_uuid_by_name(grocery_list_name)
            response = await self._make_authenticated_request("GET", "/sync/groceries")
            groceries = response.get("result", [])

            existing_grocery = None
            for grocery in groceries:
                grocery_name = grocery.get("name", "").strip()
                if (
                    grocery_name == item_name.strip()
                    and grocery.get("list_uid") == list_uuid
                ):
                    existing_grocery = grocery
                    break

            if existing_grocery:
                grocery_uid = existing_grocery.get("uid")
                current_quantity_str = str(existing_grocery.get("quantity", "0"))
            else:
                grocery_uid = self._generate_uuid()
                current_quantity_str = "0"

            final_quantity_str, _ = self._add_quantities(
                current_quantity_str, quantity_str
            )

            grocery_data = [
                {
                    "uid": grocery_uid,
                    "name": item_name.strip(),
                    "ingredient": item_name.strip(),
                    "quantity": final_quantity_str,
                    "recipe_uid": None,
                    "order_flag": 316,
                    "purchased": False,
                    "aisle": "Produce",
                    "recipe": None,
                    "instruction": "",
                    "separate": False,
                    "aisle_uid": "F94467760BF4BC6B9521FFA9329D0F1DBCCA0F5AC0808BD8552FB375A565FB9E",
                    "list_uid": list_uuid,
                }
            ]

            gzipped_data = self._gzip_json(grocery_data)
            data = aiohttp.FormData()
            data.add_field(
                "data",
                BytesIO(gzipped_data),
                filename="grocery.json.gz",
                content_type="application/octet-stream",
            )

            await self._make_authenticated_request("POST", "/sync/groceries", data=data)

            logger.info(
                f"Successfully added {quantity_str} '{item_name}' (total: {final_quantity_str})"
            )
            return {
                "name": item_name.strip(),
                "quantity": final_quantity_str,
            }

        except PaprikaAPIError:
            raise
        except Exception as e:
            logger.error(f"Failed to add grocery '{item_name}': {str(e)}")
            raise PaprikaAPIError(f"Failed to add grocery: {str(e)}")

    async def clear_grocery_list(self, grocery_list_name: Optional[str] = None) -> bool:
        try:
            list_uuid = await self._get_list_uuid_by_name(grocery_list_name)
            response = await self._make_authenticated_request("GET", "/sync/groceries")
            groceries = response.get("result", [])

            groceries_to_delete = []
            for grocery in groceries:
                if grocery.get("list_uid") == list_uuid:
                    groceries_to_delete.append(grocery)

            if not groceries_to_delete:
                logger.info("Grocery list is already empty")
                return True

            grocery_data_list = []
            for grocery in groceries_to_delete:
                grocery_item = {
                    "uid": grocery.get("uid"),
                    "name": grocery.get("name", ""),
                    "ingredient": grocery.get("ingredient", ""),
                    "quantity": "0",
                    "recipe_uid": grocery.get("recipe_uid"),
                    "order_flag": grocery.get("order_flag", 316),
                    "purchased": False,
                    "aisle": grocery.get("aisle", "Produce"),
                    "recipe": grocery.get("recipe"),
                    "instruction": grocery.get("instruction", ""),
                    "separate": grocery.get("separate", False),
                    "aisle_uid": grocery.get(
                        "aisle_uid",
                        "F94467760BF4BC6B9521FFA9329D0F1DBCCA0F5AC0808BD8552FB375A565FB9E",
                    ),
                    "list_uid": list_uuid,
                    # Paprika's sync API soft-deletes; quantity "0" alone
                    # leaves the row on the list. Same flag remove_meal_from_plan uses.
                    "deleted": True,
                }
                grocery_data_list.append(grocery_item)

            gzipped_data = self._gzip_json(grocery_data_list)
            data = aiohttp.FormData()
            data.add_field(
                "data",
                BytesIO(gzipped_data),
                filename="grocery.json.gz",
                content_type="application/octet-stream",
            )

            await self._make_authenticated_request("POST", "/sync/groceries", data=data)

            logger.info(
                f"Successfully cleared {len(groceries_to_delete)} items from grocery list"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to clear grocery list: {str(e)}")
            raise PaprikaAPIError(f"Failed to clear grocery list: {str(e)}")

    async def close(self):
        """Close the HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
