#!/usr/bin/env python3
"""
Fetches live model streams from chococams.com and generates an M3U playlist.
- Scrapes favourite models' individual pages for HLS links.
- Scrapes top 10 models from the main model listing page.
- Outputs: playlists/live.m3u
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_URL = "https://chococams.com"
MODEL_LIST_URL = f"{BASE_URL}/model/"

# Favourite models (lowercase for matching, original case preserved below)
FAVOURITE_MODELS = [
    "redpointx_",
    "tommy_and_sophie",
    
    "denobluora",
    "shannelpink_",
    "stupid_little_kitten",
    "lilkimchii",
    "_tenderpassion_",
    "tommy_and_sophie",
    "gimbobar",
    "beawolf1887",
    "dolls_wallen",
    "miamax88",
    "twogirls2boys",
    "hakxram",
    "evaandtommi",
    "calehot98",
    "bonieandclyde1",
    "lovers_clover_x",
    "drake_and_zara",
    "lailaasher",
    "sc_andre",
    "beibi_sin",
    "loyliksi",
    "cumplaycouple",
    "bellapazzia13",
    "laurenxcros",
    "dexandlily",
    "cutefacebigass",
    "selinabentzzz",
    "playwithmil",
    "julesxdann",
    "alissa_and_alann",
    "dreamspussy",
    "notfallenangel",
    "lola_linss",
    "_sweetandsinner_",
    "butterfly_on_dick",
    "ashleyandzamir",
    "sam_y_sen",
    "gamebelka",
    "juliyajam",
    "amateur2friendswithbenefits",
    "jessdant_luv",
    "limiyan",
    "marilyn_mike",
    "lost_wanderers",
    "keutypie",
    "luis7777hui",
    "sweet_sugar87",
    "ebangelion",
    "sophywhisper",
    "elisabethwillian",
    "jackandjill",
    "alpugh",
    "homeofsex_",
    "ali_and_louie1",
    "jasson_n_emma",
    "amandatalk",
    "sandra_and_charly",
    "assayo444",
    "ethan_chloee",
    "luna_horny00",
    "kinga_da_vinci",
    "sashahoneyvice",
    "danyandannarearden",
    "kjbennet",
    "the_isa_bella",
    "jonnalinaproduction",
    "Litzy1_",
    "MaxMia",
    "Threesome-no-mercy",
    "crazycats_",
]

# Individual model page URL pattern
# e.g. https://chococams.com/model/stripchat/redpointx_
# The site may use different source prefixes (stripchat, chaturbate, etc.)
# We'll try common prefixes or discover them from the listing page.
KNOWN_SOURCES = ["stripchat", "chaturbate", "bongacams", "camsoda", "cam4"]

# How many top models to grab from the listing page
TOP_MODEL_COUNT = 10

OUTPUT_DIR = Path("playlists")
OUTPUT_FILE = OUTPUT_DIR / "live.m3u"

# Common HLS / stream URL patterns to search for in page source
HLS_PATTERNS = [
    re.compile(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)'),
]

THUMB_PATTERNS = [
    re.compile(r'(https?://thumb[^\s"\'<>]+\.jpg[^\s"\'<>]*)'),
    re.compile(r'(https?://[^\s"\'<>]*mmcdn\.com[^\s"\'<>]+\.jpg[^\s"\'<>]*)'),
    re.compile(r'(https?://[^\s"\'<>]*thumbnail[^\s"\'<>]+\.jpg[^\s"\'<>]*)'),
    re.compile(r'(https?://[^\s"\'<>]*preview[^\s"\'<>]+\.jpg[^\s"\'<>]*)'),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DATA CLASS
# ---------------------------------------------------------------------------

class ModelStream:
    def __init__(self, name: str, hls_url: str, thumb_url: str = "", source: str = "", is_favourite: bool = False):
        self.name = name
        self.hls_url = hls_url
        self.thumb_url = thumb_url
        self.source = source
        self.is_favourite = is_favourite

    def __repr__(self):
        return f"<ModelStream {self.name} fav={self.is_favourite}>"


# ---------------------------------------------------------------------------
# PAGE FETCHING HELPERS
# ---------------------------------------------------------------------------

def fetch_page_requests(url: str, session: requests.Session) -> str:
    """Fetch page HTML using requests (fast, but won't execute JS)."""
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.warning(f"requests failed for {url}: {e}")
        return ""


def fetch_page_playwright(url: str, wait_ms: int = 8000) -> str:
    """Fetch page HTML using Playwright (headless Chromium) to render JS."""
    if not HAS_PLAYWRIGHT:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ])
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()
            
            # Capture network requests for HLS URLs
            captured_urls = []
            
            def handle_response(response):
                url_str = response.url
                if ".m3u8" in url_str:
                    captured_urls.append(url_str)

            page.on("response", handle_response)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(wait_ms)

            html = page.content()

            # Append captured network URLs as a hidden comment so our
            # regex extraction picks them up later.
            if captured_urls:
                extra = "\n".join(f"<!-- NETWORK_HLS: {u} -->" for u in captured_urls)
                html += extra

            browser.close()
            return html
    except Exception as e:
        logger.warning(f"Playwright failed for {url}: {e}")
        return ""


def fetch_page(url: str, session: requests.Session, use_browser: bool = False) -> str:
    """Fetch page: try requests first, fall back to Playwright if needed."""
    html = fetch_page_requests(url, session)
    if use_browser or not html:
        browser_html = fetch_page_playwright(url)
        if browser_html:
            html = browser_html
    return html


# ---------------------------------------------------------------------------
# EXTRACTION HELPERS
# ---------------------------------------------------------------------------

def extract_hls_url(html: str) -> str:
    """Extract the first HLS .m3u8 URL from HTML source."""
    for pattern in HLS_PATTERNS:
        matches = pattern.findall(html)
        if matches:
            # Prefer 'master' playlists
            for m in matches:
                if "master" in m.lower():
                    return clean_url(m)
            return clean_url(matches[0])
    return ""


def extract_thumb_url(html: str, model_name: str = "") -> str:
    """Extract a thumbnail/preview image URL from HTML source."""
    for pattern in THUMB_PATTERNS:
        matches = pattern.findall(html)
        if matches:
            # Prefer URLs that contain the model name
            if model_name:
                for m in matches:
                    if model_name.lower() in m.lower():
                        return clean_url(m)
            return clean_url(matches[0])

    # Fallback: look for og:image or meta thumbnail
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"]

    img_tags = soup.find_all("img")
    for img in img_tags:
        src = img.get("src", "")
        if model_name.lower() in src.lower() and ("thumb" in src.lower() or "preview" in src.lower()):
            return src

    return ""


def clean_url(url: str) -> str:
    """Remove trailing quotes, spaces, etc."""
    url = url.strip().rstrip("\\").rstrip("'").rstrip('"')
    # Remove anything after a space or closing bracket
    url = url.split(" ")[0].split(">")[0].split("<")[0].split("'")[0].split('"')[0]
    return url


def extract_json_data(html: str) -> dict:
    """Try to extract JSON data embedded in script tags (common pattern)."""
    data = {}
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        text = script.string or ""
        # Look for JSON-like structures containing HLS URLs
        if ".m3u8" in text:
            # Try to find JSON objects
            json_pattern = re.compile(r'\{[^{}]*m3u8[^{}]*\}')
            for match in json_pattern.findall(text):
                try:
                    parsed = json.loads(match)
                    data.update(parsed)
                except (json.JSONDecodeError, ValueError):
                    pass

            # Also try larger JSON blocks
            json_block = re.compile(r'(?:var\s+\w+\s*=\s*|JSON\.parse\([\'"])(\{.+?\})')
            for match in json_block.findall(text):
                try:
                    parsed = json.loads(match)
                    data.update(parsed)
                except (json.JSONDecodeError, ValueError):
                    pass
    return data


# ---------------------------------------------------------------------------
# MODEL PAGE SCRAPING
# ---------------------------------------------------------------------------

def scrape_model_page(model_name: str, session: requests.Session) -> ModelStream | None:
    """
    Try to scrape a single model's page to get HLS stream and thumbnail.
    Tries multiple source prefixes.
    """
    logger.info(f"Scraping model page: {model_name}")

    urls_to_try = []
    for source in KNOWN_SOURCES:
        urls_to_try.append((f"{BASE_URL}/model/{source}/{model_name}", source))
    # Also try without source prefix
    urls_to_try.append((f"{BASE_URL}/model/{model_name}", "unknown"))

    for url, source in urls_to_try:
        logger.info(f"  Trying: {url}")

        # First try with requests (fast)
        html = fetch_page_requests(url, session)
        if not html or "404" in html[:500].lower() or "not found" in html[:500].lower():
            continue

        hls_url = extract_hls_url(html)

        # If no HLS found with requests, try with browser
        if not hls_url and HAS_PLAYWRIGHT:
            logger.info(f"  Trying with browser: {url}")
            html = fetch_page_playwright(url, wait_ms=10000)
            if html:
                hls_url = extract_hls_url(html)

        if hls_url:
            thumb_url = extract_thumb_url(html, model_name)
            logger.info(f"  ✓ Found HLS for {model_name}: {hls_url}")
            return ModelStream(
                name=model_name,
                hls_url=hls_url,
                thumb_url=thumb_url,
                source=source,
                is_favourite=model_name.lower() in [f.lower() for f in FAVOURITE_MODELS],
            )

    logger.warning(f"  ✗ No HLS stream found for {model_name}")
    return None


# ---------------------------------------------------------------------------
# LISTING PAGE SCRAPING
# ---------------------------------------------------------------------------

def scrape_model_listing(session: requests.Session) -> list[dict]:
    """
    Scrape the main model listing page to find top online models.
    Returns list of dicts with 'name', 'url', 'thumb', 'source'.
    """
    logger.info(f"Scraping model listing: {MODEL_LIST_URL}")

    html = fetch_page(MODEL_LIST_URL, session, use_browser=True)
    if not html:
        logger.error("Failed to fetch model listing page")
        return []

    soup = BeautifulSoup(html, "lxml")
    models = []

    # Common CSS patterns for model cards on aggregator sites
    card_selectors = [
        "div.model-card",
        "div.model-item",
        "div.cam-card",
        "div.performer",
        "div.thumb",
        "a.model-link",
        "div[class*='model']",
        "div[class*='cam']",
        "div[class*='performer']",
        "article",
        "div.grid-item",
        "li.model",
    ]

    cards = []
    for selector in card_selectors:
        cards = soup.select(selector)
        if len(cards) >= 3:
            logger.info(f"  Found {len(cards)} cards with selector: {selector}")
            break

    if not cards:
        # Fallback: find all links that match model page pattern
        logger.info("  Trying link-based extraction...")
        links = soup.find_all("a", href=re.compile(r'/model/\w+/\w+'))
        seen = set()
        for link in links:
            href = link.get("href", "")
            if href in seen:
                continue
            seen.add(href)

            parts = href.rstrip("/").split("/")
            if len(parts) >= 2:
                model_name = parts[-1]
                source = parts[-2] if len(parts) >= 3 else "unknown"
            else:
                continue

            # Try to find thumbnail
            img = link.find("img")
            thumb = img.get("src", "") or img.get("data-src", "") if img else ""

            models.append({
                "name": model_name,
                "url": href if href.startswith("http") else BASE_URL + href,
                "thumb": thumb,
                "source": source,
            })

            if len(models) >= TOP_MODEL_COUNT * 2:
                break

    for card in cards[:TOP_MODEL_COUNT * 3]:
        # Extract model name and URL
        link = card.find("a", href=True) if card.name != "a" else card
        if not link or not link.get("href"):
            continue

        href = link.get("href", "")
        if not href:
            continue

        # Extract name from URL or text
        parts = href.rstrip("/").split("/")
        model_name = parts[-1] if parts else ""
        source = ""
        if len(parts) >= 2:
            for s in KNOWN_SOURCES:
                if s in parts[-2].lower():
                    source = s
                    break

        if not model_name or model_name in ("model", ""):
            # Try text content
            name_elem = card.find(class_=re.compile(r'name|title|username', re.I))
            if name_elem:
                model_name = name_elem.get_text(strip=True)

        if not model_name:
            continue

        # Thumbnail
        img = card.find("img")
        thumb = ""
        if img:
            thumb = img.get("src", "") or img.get("data-src", "") or img.get("data-lazy-src", "")

        full_url = href if href.startswith("http") else BASE_URL + href

        models.append({
            "name": model_name,
            "url": full_url,
            "thumb": thumb,
            "source": source,
        })

    # Deduplicate
    seen_names = set()
    unique = []
    for m in models:
        key = m["name"].lower()
        if key not in seen_names:
            seen_names.add(key)
            unique.append(m)

    logger.info(f"  Found {len(unique)} unique models from listing")
    return unique[:TOP_MODEL_COUNT * 2]


# ---------------------------------------------------------------------------
# PLAYLIST GENERATION
# ---------------------------------------------------------------------------

def generate_playlist(streams: list[ModelStream]) -> str:
    """Generate an M3U playlist string."""
    lines = ["#EXTM3U"]
    lines.append(f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"# Total streams: {len(streams)}")
    lines.append("")

    # Favourites first
    favourites = [s for s in streams if s.is_favourite]
    others = [s for s in streams if not s.is_favourite]

    for section_name, section_streams in [("Favourites", favourites), ("Top Models", others)]:
        if not section_streams:
            continue

        lines.append(f"# --- {section_name} ---")
        for stream in section_streams:
            group = "Favourites" if stream.is_favourite else "Top Models"
            logo_part = f' tvg-logo="{stream.thumb_url}"' if stream.thumb_url else ''
            source_part = f' [{stream.source}]' if stream.source else ''

            lines.append(
                f'#EXTINF:-1{logo_part} group-title="{group}",{stream.name}{source_part}'
            )
            lines.append(stream.hls_url)
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ALTERNATIVE: DIRECT API APPROACH
# ---------------------------------------------------------------------------

def try_stripchat_direct(model_name: str, session: requests.Session) -> ModelStream | None:
    """
    Try to get HLS URL directly from Stripchat's API.
    Many chococams models come from Stripchat.
    """
    try:
        # Stripchat API endpoint
        api_url = f"https://stripchat.com/api/front/v2/models/username/{model_name}/cam"
        resp = session.get(api_url, headers={
            **HEADERS,
            "Referer": "https://stripchat.com/",
        }, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            if "cam" in data:
                cam = data["cam"]
                if cam.get("isCamAvailable"):
                    viewer_url = cam.get("viewServers", {}).get("flashphoner-hls", "")
                    model_id = cam.get("streamName", "")

                    if viewer_url and model_id:
                        hls_url = f"https://b-{viewer_url}.stripst.com/hls/{model_id}/{model_id}.m3u8"
                        thumb_url = f"https://img.strpst.com/thumbs/{model_id}_webp"

                        logger.info(f"  ✓ Stripchat API: {model_name} -> {hls_url}")
                        return ModelStream(
                            name=model_name,
                            hls_url=hls_url,
                            thumb_url=thumb_url,
                            source="stripchat",
                            is_favourite=model_name.lower() in [f.lower() for f in FAVOURITE_MODELS],
                        )
    except Exception as e:
        logger.debug(f"  Stripchat API failed for {model_name}: {e}")

    return None


def try_chaturbate_direct(model_name: str, session: requests.Session) -> ModelStream | None:
    """
    Try to get HLS URL from Chaturbate's public endpoint.
    """
    try:
        api_url = f"https://chaturbate.com/get_edge_hls_url_ajax/"
        resp = session.post(api_url, data={
            "room_slug": model_name,
            "bandwidth": "high",
        }, headers={
            **HEADERS,
            "Referer": f"https://chaturbate.com/{model_name}/",
            "X-Requested-With": "XMLHttpRequest",
        }, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            hls_url = data.get("url", "")
            if hls_url:
                thumb_url = f"https://thumb.live.mmcdn.com/ri/{model_name}.jpg"
                logger.info(f"  ✓ Chaturbate API: {model_name} -> {hls_url}")
                return ModelStream(
                    name=model_name,
                    hls_url=hls_url,
                    thumb_url=thumb_url,
                    source="chaturbate",
                    is_favourite=model_name.lower() in [f.lower() for f in FAVOURITE_MODELS],
                )
    except Exception as e:
        logger.debug(f"  Chaturbate API failed for {model_name}: {e}")

    return None


def try_direct_apis(model_name: str, session: requests.Session) -> ModelStream | None:
    """Try all direct API approaches."""
    result = try_stripchat_direct(model_name, session)
    if result:
        return result

    result = try_chaturbate_direct(model_name, session)
    if result:
        return result

    return None


# ---------------------------------------------------------------------------
# SCRAPE CHOCOCAMS MODEL PAGE WITH BROWSER
# ---------------------------------------------------------------------------

def scrape_chococams_model_browser(model_name: str) -> ModelStream | None:
    """
    Use Playwright to load a chococams model page, wait for the video
    player to initialise, and capture the HLS URL from network requests.
    """
    if not HAS_PLAYWRIGHT:
        return None

    logger.info(f"  Browser scraping chococams for: {model_name}")

    urls_to_try = [
        f"{BASE_URL}/model/{source}/{model_name}" for source in KNOWN_SOURCES
    ]

    for url in urls_to_try:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ])
                context = browser.new_context(user_agent=HEADERS["User-Agent"])
                page = context.new_page()

                hls_urls = []
                thumb_urls = []

                def on_response(response):
                    u = response.url
                    if ".m3u8" in u:
                        hls_urls.append(u)
                    if ("thumb" in u or "preview" in u) and u.endswith((".jpg", ".png", ".webp")):
                        thumb_urls.append(u)

                page.on("response", on_response)

                resp = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if resp and resp.status == 404:
                    browser.close()
                    continue

                # Wait for video player to load
                page.wait_for_timeout(12000)

                # Also check page source
                html = page.content()

                browser.close()

                # Check captured network HLS
                hls_url = ""
                if hls_urls:
                    # Prefer master playlist
                    for u in hls_urls:
                        if "master" in u.lower():
                            hls_url = u
                            break
                    if not hls_url:
                        hls_url = hls_urls[0]

                # Fallback to regex on HTML
                if not hls_url:
                    hls_url = extract_hls_url(html)

                if hls_url:
                    thumb_url = thumb_urls[0] if thumb_urls else extract_thumb_url(html, model_name)
                    source = ""
                    for s in KNOWN_SOURCES:
                        if s in url:
                            source = s
                            break

                    return ModelStream(
                        name=model_name,
                        hls_url=hls_url,
                        thumb_url=thumb_url,
                        source=source,
                        is_favourite=model_name.lower() in [f.lower() for f in FAVOURITE_MODELS],
                    )

        except Exception as e:
            logger.debug(f"  Browser scrape failed for {url}: {e}")
            continue

    return None


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 60)
    logger.info("Live Cam Playlist Generator")
    logger.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)

    all_streams: list[ModelStream] = []
    processed_names: set = set()

    # -----------------------------------------------------------------------
    # 1. SCRAPE FAVOURITE MODELS
    # -----------------------------------------------------------------------
    logger.info("\n--- Fetching Favourite Models ---")
    for model_name in FAVOURITE_MODELS:
        if model_name.lower() in processed_names:
            continue

        stream = None

        # Try direct APIs first (fastest)
        stream = try_direct_apis(model_name, session)

        # Try scraping chococams page
        if not stream:
            stream = scrape_model_page(model_name, session)

        # Try browser-based scraping
        if not stream:
            stream = scrape_chococams_model_browser(model_name)

        if stream:
            stream.is_favourite = True
            all_streams.append(stream)
            processed_names.add(model_name.lower())
        else:
            logger.warning(f"Could not find live stream for favourite: {model_name}")

    # -----------------------------------------------------------------------
    # 2. SCRAPE TOP MODELS FROM LISTING
    # -----------------------------------------------------------------------
    logger.info("\n--- Fetching Top Models from Listing ---")
    listing_models = scrape_model_listing(session)

    top_count = 0
    for model_info in listing_models:
        if top_count >= TOP_MODEL_COUNT:
            break

        model_name = model_info["name"]
        if model_name.lower() in processed_names:
            continue

        stream = None

        # Try direct APIs
        stream = try_direct_apis(model_name, session)

        # Try scraping model page on chococams
        if not stream:
            model_url = model_info.get("url", "")
            if model_url:
                logger.info(f"Scraping: {model_url}")
                html = fetch_page(model_url, session, use_browser=True)
                if html:
                    hls_url = extract_hls_url(html)
                    if hls_url:
                        thumb_url = model_info.get("thumb", "") or extract_thumb_url(html, model_name)
                        stream = ModelStream(
                            name=model_name,
                            hls_url=hls_url,
                            thumb_url=thumb_url,
                            source=model_info.get("source", ""),
                            is_favourite=False,
                        )

        # Browser fallback
        if not stream:
            stream = scrape_chococams_model_browser(model_name)

        if stream:
            all_streams.append(stream)
            processed_names.add(model_name.lower())
            top_count += 1
            logger.info(f"  Added top model #{top_count}: {model_name}")
        else:
            logger.warning(f"  Skipping {model_name} (no stream found)")

    # -----------------------------------------------------------------------
    # 3. GENERATE PLAYLIST
    # -----------------------------------------------------------------------
    logger.info(f"\n--- Generating Playlist ({len(all_streams)} streams) ---")

    if all_streams:
        playlist = generate_playlist(all_streams)
        OUTPUT_FILE.write_text(playlist, encoding="utf-8")
        logger.info(f"Playlist written to: {OUTPUT_FILE}")
        logger.info("\nPlaylist contents:")
        print(playlist)
    else:
        # Write empty playlist with timestamp
        empty = (
            "#EXTM3U\n"
            f"# Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            "# No live streams found\n"
        )
        OUTPUT_FILE.write_text(empty, encoding="utf-8")
        logger.warning("No streams found! Empty playlist generated.")

    logger.info("\nDone!")


if __name__ == "__main__":
    main()
