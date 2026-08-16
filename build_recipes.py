#!/usr/bin/env python3
"""
Sync script: 1Password Recipes vault -> recipes.html / index.html

Usage:
    python3 build_recipes.py <raw_vault_export.json>

<raw_vault_export.json> must be a JSON array of items in the nested
1Password export format:
    [
      {
        "uuid": "...",
        "overview": {"title": "...", "ainfo": "...", "tags": [...]},
        "details": {"notesPlain": "...", "source": "..."},
        "image": "optional-filename.jpg",
        "createdAt": "...", "updatedAt": "..."
      },
      ...
    ]

What it does:
  1. Loads categories.json (the durable "which category does this recipe
     belong in" cache -- category can't be reliably derived from the raw
     1Password data alone, so once a human/Claude assigns one it's kept).
  1b. Loads photos.json (the durable uuid -> featured-photo-filename(s) cache.
     1Password itself never supplies these; a photo is only ever added by
     dropping a file in the repo and adding an entry here, so it survives
     every daily sync unchanged). Each entry can be a single filename string
     (one photo) or a JSON array of filenames (multiple photos -- rendered
     as a carousel in the recipe modal). The first photo in the list is
     always used as the card-grid background image.
  2. Derives every other field automatically from the raw item data.
  3. Rebuilds the RECIPES array and rewrites it into recipes.html and
     index.html (only touches the `const RECIPES = [...]` line).
  4. Writes NEW_RECIPES_REVIEW.md listing any recipe whose category was
     guessed rather than confirmed, and any recipe that was removed
     because it's no longer in the vault.
  5. Prints "CHANGED" or "UNCHANGED" so the caller knows whether to
     git commit/push.

Edit categories.json to correct a guessed category, then re-run --
the correction sticks permanently. Edit photos.json (uuid -> filename in
the repo root, e.g. "pork-tenderloin.jpg", or a list of filenames, e.g.
["smoked-brisket.jpg", "smoked-brisket-2.jpg"]) to attach or change a
recipe's featured photo(s) -- the first is shown as the card background
on the table view; all of them are shown as a carousel at the top of the
recipe modal.
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).parent
CATEGORIES_FILE = REPO / "categories.json"
PHOTOS_FILE = REPO / "photos.json"
RAW_JSON_FILE = REPO / "recipes.json"
REVIEW_FILE = REPO / "NEW_RECIPES_REVIEW.md"
HTML_FILES = [REPO / "recipes.html", REPO / "index.html"]

VALID_CATEGORIES = [
    "sourdough", "smoked", "breads", "asian", "soups", "pasta", "tacos",
    "breakfast", "mains", "sides", "salads", "appetizers", "sauces",
    "rubs", "desserts", "reference",
]

# Ordered keyword fallback -- only used for BRAND NEW recipes that have no
# entry yet in categories.json. First match wins. Imperfect on purpose --
# anything guessed here gets flagged in NEW_RECIPES_REVIEW.md for a human
# to confirm or correct in categories.json.
KEYWORD_RULES = [
    ("sourdough", ["sourdough"]),
    ("smoked", ["smoked", "smoker", "blackstone", "tomahawk", "barbacoa",
                "brisket", "ribs", "pulled pork", "tri-tip", "ribeye",
                "wings", "beer batter"]),
    ("breads", ["baguette", "naan", "pizza dough", "buns", "bread",
                "dough"]),
    ("asian", ["banh mi", "pho", "adobo", "thai", "vietnamese",
               "indonesian", "sesame", "suka", "curry", "egg rolls",
               "skewers", "ba tay so"]),
    ("sauces", ["sauce", "pesto", "vinaigrette", "pickle", "relish",
                "hollandaise"]),
    ("desserts", ["pie", "cake", "cookie", "cookies", "dessert"]),
    ("sides", ["salad", "carrots", "cabbage", "corn", "rice", "fries",
               "potato", "slaw", "asparagus"]),
]


def guess_category(title: str) -> str:
    t = title.lower()
    for cat, keywords in KEYWORD_RULES:
        if any(k in t for k in keywords):
            return cat
    return "mains"


MIN_PREVIEW_LEN = 9  # skip trivially short bullets like "Sugar" / "Milk"


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # bold
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text)  # italics
    return text.strip()


def derive_attribution_and_preview(notes: str):
    """Look for a whole-line italic or bold metadata line near the top of
    the notes, e.g. *Grandma Marty's Recipe*, *Source: The Sourdough
    Journey*, **Yield:** one loaf, or **Makes 4 Servings**. If it matches
    "X's Recipe...", X becomes the attribution too.

    Otherwise fall back to the first "substantial" ingredient-style
    bullet or numbered step (skipping trivially short ones like "Sugar"
    or "Milk"), stripped of markdown.
    """
    lines = notes.split("\n")
    for line in lines[:5]:
        s = line.strip()
        # Whole-line italic: starts with * immediately followed by a
        # non-space, non-* character (rules out bullets like "* **Milk**"
        # and lines that merely end in an italic aside).
        m = re.match(r"^\*(?![\s*])(.*)\*$", s)
        if m:
            inner = m.group(1).strip()
            name_m = re.match(r"^(.+?)'s Recipe\b", inner)
            attribution = name_m.group(1).strip() if name_m else ""
            return attribution, inner
        # Whole-line bold metadata, e.g. "**Yield:** one loaf" or
        # "**Makes 4 Servings**".
        m = re.match(r"^\*\*(.+?)\*\*(.*)$", s)
        if m and s.startswith("**") and not s.startswith("* "):
            inner = (m.group(1) + m.group(2)).strip()
            inner = _strip_markdown(inner)
            if inner:
                return "", inner

    # Fallback: first substantial bullet ("* text") or numbered step
    # ("1. text"), stripped of markdown. Short bullets (bare ingredient
    # names with no detail) are skipped in favor of the next one.
    candidates = []
    for line in lines:
        s = line.strip()
        item = None
        if s.startswith("* "):
            item = s[2:].strip()
        else:
            nm = re.match(r"^\d+\.\s+(.+)$", s)
            if nm:
                item = nm.group(1).strip()
        if item:
            item = _strip_markdown(item)
            if item:
                candidates.append(item)

    for item in candidates:
        if len(item) >= MIN_PREVIEW_LEN:
            return "", item
    if candidates:
        return "", candidates[0]
    return "", ""


def build_recipe(item: dict, categories: dict, review: list, photos: dict) -> dict:
    uuid = item["uuid"]
    overview = item.get("overview", {})
    details = item.get("details", {})
    # .strip() guards against stray leading/trailing whitespace in the
    # 1Password title field (e.g. " Smoked Asparagus" with a leading
    # space) -- harmless-looking in 1Password itself, but it broke every
    # client-side sort option on the site: title sort put it first via
    # localeCompare, and both the "Updated" sort's stable-sort tie-
    # breaking and the "Rating" sort's unrated-fallback both fall back to
    # comparing titles, so the same stray-space recipe won every tie too.
    title = overview.get("title", "").strip()
    ainfo = (overview.get("ainfo") or title).strip()
    tags = overview.get("tags", [])
    notes = details.get("notesPlain", "")
    cookbook = details.get("source") == "The Gruenberg Family Cook Book"
    attribution, preview = derive_attribution_and_preview(notes)

    if uuid in categories:
        # A confirmed category always wins, even if a tag like "reference"
        # would otherwise suggest something else -- tags can serve double
        # duty (e.g. "reference" meaning "informational" without meaning
        # the recipe belongs in the Reference section).
        category = categories[uuid]["category"]
    elif "reference" in tags:
        category = "reference"
    else:
        category = guess_category(title)
        review.append((title, category))

    categories[uuid] = {"title": title, "category": category}

    recipe = {
        "uuid": uuid,
        "title": title,
        "ainfo": ainfo,
        "category": category,
        "tags": tags,
        "cookbook": cookbook,
        "attribution": attribution,
        "preview": preview,
        "notes": notes,
        "updatedAt": item.get("updatedAt", ""),
    }
    if item.get("image"):
        recipe["image"] = item["image"]
    if uuid in photos:
        photo_list = photos[uuid]
        if isinstance(photo_list, str):
            photo_list = [photo_list]
        recipe["photos"] = photo_list
        recipe["photo"] = photo_list[0]
    return recipe


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 build_recipes.py <raw_vault_export.json>")
        sys.exit(1)

    raw_path = Path(sys.argv[1])
    raw_items = json.loads(raw_path.read_text(encoding="utf-8"))

    categories = {}
    if CATEGORIES_FILE.exists():
        categories = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))

    photos = {}
    if PHOTOS_FILE.exists():
        photos = json.loads(PHOTOS_FILE.read_text(encoding="utf-8"))

    seen_uuids = {item["uuid"] for item in raw_items}
    removed = [c["title"] for u, c in categories.items() if u not in seen_uuids]

    review = []
    by_uuid = {item["uuid"]: build_recipe(item, categories, review, photos) for item in raw_items}

    # Preserve the existing display order (read from the current HTML) so a
    # sync doesn't reshuffle every category on the live site. Recipes that
    # are brand new get appended at the end, alphabetically among themselves.
    existing_order = []
    for html_path in HTML_FILES:
        if html_path.exists():
            html = html_path.read_text(encoding="utf-8")
            m = re.search(r"const RECIPES = (\[.*?\]);\n", html, re.S)
            if m:
                try:
                    existing_order = [r["uuid"] for r in json.loads(m.group(1))]
                except (json.JSONDecodeError, KeyError):
                    existing_order = []
                break

    ordered_uuids = [u for u in existing_order if u in by_uuid]
    new_uuids = sorted(
        (u for u in by_uuid if u not in existing_order),
        key=lambda u: by_uuid[u]["title"].lower(),
    )
    recipes = [by_uuid[u] for u in ordered_uuids + new_uuids]

    # Drop categories.json / photos.json entries for recipes no longer in
    # the vault.
    categories = {u: c for u, c in categories.items() if u in seen_uuids}
    photos = {u: p for u, p in photos.items() if u in seen_uuids}

    new_json = json.dumps(recipes, ensure_ascii=False, separators=(", ", ": "))

    changed = False
    for html_path in HTML_FILES:
        if not html_path.exists():
            continue
        html = html_path.read_text(encoding="utf-8")
        m = re.search(r"const RECIPES = (\[.*?\]);\n", html, re.S)
        if not m:
            print(f"WARNING: could not find RECIPES array in {html_path}")
            continue
        if m.group(1) != new_json:
            changed = True
            html = html[:m.start(1)] + new_json + html[m.end(1):]
            html_path.write_text(html, encoding="utf-8")

    CATEGORIES_FILE.write_text(
        json.dumps(categories, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    PHOTOS_FILE.write_text(
        json.dumps(photos, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    RAW_JSON_FILE.write_text(
        json.dumps(raw_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Always overwrite (never delete) this file -- some environments this
    # script runs in restrict unlinking files, but overwriting is fine.
    if review or removed:
        lines = ["# Recipe sync review\n"]
        if review:
            lines.append("## New recipes with a guessed category\n")
            lines.append("Fix in `categories.json` if wrong, then re-run.\n")
            for title, cat in review:
                lines.append(f"- **{title}** -> guessed `{cat}`")
            lines.append("")
        if removed:
            lines.append("## Recipes removed (no longer in vault)\n")
            for title in removed:
                lines.append(f"- {title}")
        REVIEW_FILE.write_text("\n".join(lines), encoding="utf-8")
    else:
        REVIEW_FILE.write_text(
            "# Recipe sync review\n\nNothing needs review as of the last sync.\n",
            encoding="utf-8",
        )

    print("CHANGED" if changed else "UNCHANGED")
    if review:
        print(f"NEEDS_REVIEW: {len(review)} new recipe(s) auto-categorized, see {REVIEW_FILE.name}")
    if removed:
        print(f"REMOVED: {len(removed)} recipe(s) no longer in vault")


if __name__ == "__main__":
    main()
