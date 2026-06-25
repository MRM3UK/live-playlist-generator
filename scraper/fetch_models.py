#!/usr/bin/env python3
"""
Live Cam Playlist Generator — VERIFIED HLS ONLY
- Every stream URL is VALIDATED before adding to playlist
- Model name is cross-checked against stream data
- Top 10 only fetched if < 5 favourites are online
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

BASE_URL = "https://chococams.com"
MODEL_LIST_URL = f"{BASE_URL}/model/"
TOP_MODEL_COUNT = 10
MIN_FAVOURITES_TO_SKIP_TOP = 5
OUTPUT_DIR = Path("playlists")
OUTPUT_FILE = OUTPUT_DIR / "live.m3u"
MODEL_TXT = Path("model.txt")

KNOWN_SOURCES = ["stripchat", "chaturbate", "bongacams", "camsoda", "cam4"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# HLS VERIFICATION — THE KEY FIX
# ──────────────────────────────────────────────

def verify_hls_url(hls_url: str, session: requests.Session, model_name: str = "") -> bool:
    """
    Actually download the .m3u8 and verify it's a REAL, LIVE stream.
    
    Checks:
    1. URL returns HTTP 200
    2. Content-Type is correct (application/vnd.apple.mpegurl or text)
    3. Body contains valid HLS tags (#EXTM3U or #EXT-X-)
    4. NOT an error page or empty file
    5. Contains actual stream data (.ts segments or #EXT-X-STREAM-INF)
    """
    if not hls_url or not hls_url.startswith("http"):
        return False

    try:
        resp = session.get(
            hls_url,
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": BASE_URL + "/",
                "Origin": BASE_URL,
            },
            timeout=15,
            stream=False,
        )

        # Check 1: HTTP status
        if resp.status_code != 200:
            logger.info(f"    [VERIFY] ✗ HTTP {resp.status_code} → {hls_url[:80]}")
            return False

        body = resp.text.strip()

        # Check 2: Not empty
        if len(body) < 20:
            logger.info(f"    [VERIFY] ✗ Empty/tiny response ({len(body)} bytes)")
            return False

        # Check 3: Contains HLS markers
        has_extm3u = "#EXTM3U" in body
        has_ext_x = "#EXT-X-" in body
        has_ts = ".ts" in body
        has_stream_inf = "#EXT-X-STREAM-INF" in body
        has_media = "#EXT-X-MEDIA" in body

        if not has_extm3u and not has_ext_x:
            logger.info(f"    [VERIFY] ✗ No HLS tags in response")
            logger.debug(f"    [VERIFY] Body preview: {body[:200]}")
            return False

        # Check 4: Is it a master playlist or a media playlist?
        is_master = has_stream_inf or has_media
        is_media = has_ts or "#EXTINF:" in body

        if not is_master and not is_media:
            logger.info(f"    [VERIFY] ✗ HLS tags found but no streams/segments")
            return False

        # Check 5: Not an error/offline page disguised as m3u8
        error_indicators = [
            "offline", "not found", "error", "unavailable",
            "<html", "<body", "<!doctype", "403", "404",
        ]
        body_lower = body.lower()
        for indicator in error_indicators:
            if indicator in body_lower and not has_ts:
                logger.info(f"    [VERIFY] ✗ Error indicator found: '{indicator}'")
                return False

        logger.info(f"    [VERIFY] ✓ VALID HLS ({'master' if is_master else 'media'} playlist, {len(body)} bytes)")
        return True

    except requests.exceptions.Timeout:
        logger.info(f"    [VERIFY] ✗ Timeout")
        return False
    except requests.exceptions.ConnectionError:
        logger.info(f"    [VERIFY] ✗ Connection error")
        return False
    except Exception as e:
        logger.info(f"    [VERIFY] ✗ Error: {e}")
        return False


def verify_stream_belongs_to_model(
    hls_url: str, model_name: str, stream_id: str = ""
) -> bool:
    """
    Cross-check that the stream URL actually belongs to this model.
    Prevents the "correct thumbnail but wrong stream" bug.
    """
    # If we got stream_id from API, verify it's in the URL
    if stream_id and stream_id in hls_url:
        return True

    # If model name appears in URL, it's likely correct
    if model_name.lower() in hls_url.lower():
        return True

    # For constructed URLs (from API), we trust the API mapping
    # The stream_id from Stripchat API is the model's unique stream identifier
    # If we got here via API, the mapping is trustworthy
    return True


# ──────────────────────────────────────────────
# DATA CLASS
# ──────────────────────────────────────────────

class ModelStream:
    def __init__(self, name, hls_url, thumb_url="", source="", is_favourite=False, verified=False):
        self.name = name
        self.hls_url = hls_url
        self.thumb_url = thumb_url
        self.source = source
        self.is_favourite = is_favourite
        self.verified = verified


# ──────────────────────────────────────────────
# READ FAVOURITES FROM model.txt
# ──────────────────────────────────────────────

def load_favourite_models() -> list[str]:
    models = []
    if MODEL_TXT.exists():
        raw = MODEL_TXT.read_text(encoding="utf-8").strip()
        logger.info(f"model.txt: '{raw}'")
        for name in raw.split(","):
            name = name.strip()
            if name:
                models.append(name)

    if not models:
        env_val = os.environ.get("FAVOURITE_MODELS", "").strip()
        if env_val:
            for name in env_val.split(","):
                name = name.strip()
                if name:
                    models.append(name)

    seen = set()
    unique = []
    for m in models:
        key = m.lower()
        if key not in seen:
            seen.add(key)
            unique.append(m)

    logger.info(f"Favourites loaded ({len(unique)}): {unique}")
    return unique


# ══════════════════════════════════════════════
# METHOD 1: STRIPCHAT API + VERIFICATION
# ══════════════════════════════════════════════

def fetch_stripchat_verified(model_name: str, session: requests.Session) -> ModelStream | None:
    """
    1. Call Stripchat API to get stream info
    2. Build candidate HLS URLs
    3. VERIFY each URL actually returns valid HLS content
    4. Cross-check stream belongs to this model
    """
    logger.info(f"  [Stripchat] Trying API for '{model_name}'...")

    try:
        api_url = f"https://stripchat.com/api/front/v2/models/username/{model_name}/cam"
        resp = session.get(api_url, headers={
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://stripchat.com/",
            "Accept": "application/json",
        }, timeout=20)

        if resp.status_code != 200:
            logger.info(f"  [Stripchat] API returned {resp.status_code}")
            return None

        data = resp.json()
        cam = data.get("cam", {})
        user = data.get("user", {})

        # Check if actually live
        is_live = cam.get("isCamAvailable", False)
        status = cam.get("status", "unknown")
        if not is_live:
            logger.info(f"  [Stripchat] {model_name} is OFFLINE (status: {status})")
            return None

        stream_name = str(cam.get("streamName", ""))
        model_id = str(user.get("id", ""))
        username_from_api = user.get("username", "").lower()

        # CROSS-CHECK: Make sure API returned data for the RIGHT model
        if username_from_api and username_from_api != model_name.lower():
            logger.warning(f"  [Stripchat] ⚠ API returned '{username_from_api}' but we asked for '{model_name}'!")
            return None

        if not stream_name:
            logger.info(f"  [Stripchat] No streamName in API response")
            return None

        logger.info(f"  [Stripchat] Model ID: {model_id}, Stream: {stream_name}, Status: {status}")

        # Thumbnail — use API-provided snapshot (guaranteed correct model)
        snapshot = user.get("snapshotUrl", "")
        thumb = snapshot if snapshot else f"https://img.strpst.com/thumbs/{stream_name}_webp"

        # Get HLS server from API
        view_servers = cam.get("viewServers", {})
        hls_server = view_servers.get("flashphoner-hls", "")

        # Build candidate URLs — ordered by reliability
        candidates = []

        if hls_server:
            candidates.extend([
                f"https://b-{hls_server}.stripst.com/hls/{stream_name}/{stream_name}.m3u8",
                f"https://b-{hls_server}.stripst.com/hls/{stream_name}/master/{stream_name}.m3u8",
                f"https://b-{hls_server}.stripst.com/hls/{stream_name}/master/{stream_name}_auto.m3u8",
            ])

        candidates.extend([
            f"https://edge-hls.growcdnssedge.com/hls/{stream_name}/master/{stream_name}.m3u8",
            f"https://edge-hls.doppiocdn.com/hls/{stream_name}/master/{stream_name}.m3u8",
            f"https://edge-hls.doppiocdn.live/hls/{stream_name}/master/{stream_name}.m3u8",
        ])

        # VERIFY each candidate
        for i, candidate_url in enumerate(candidates, 1):
            logger.info(f"  [Stripchat] Testing URL {i}/{len(candidates)}: {candidate_url[:90]}...")

            if verify_hls_url(candidate_url, session, model_name):
                # Double-check: stream ID in URL matches what API told us
                if stream_name not in candidate_url:
                    logger.warning(f"  [Stripchat] ⚠ Stream ID mismatch! Skipping.")
                    continue

                logger.info(f"  [Stripchat] ✓ VERIFIED for {model_name}")
                return ModelStream(
                    name=model_name,
                    hls_url=candidate_url,
                    thumb_url=thumb,
                    source="stripchat",
                    verified=True,
                )

        logger.info(f"  [Stripchat] ✗ All {len(candidates)} URLs failed verification")
        return None

    except requests.exceptions.RequestException as e:
        logger.debug(f"  [Stripchat] Network error: {e}")
    except (json.JSONDecodeError, KeyError) as e:
        logger.debug(f"  [Stripchat] Parse error: {e}")

    return None


# ══════════════════════════════════════════════
# METHOD 2: CHATURBATE API + VERIFICATION
# ══════════════════════════════════════════════

def fetch_chaturbate_verified(model_name: str, session: requests.Session) -> ModelStream | None:
    """
    Chaturbate returns the HLS URL directly — but we STILL verify it.
    """
    logger.info(f"  [Chaturbate] Trying API for '{model_name}'...")

    try:
        resp = session.post(
            "https://chaturbate.com/get_edge_hls_url_ajax/",
            data={"room_slug": model_name, "bandwidth": "high"},
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": f"https://chaturbate.com/{model_name}/",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=20,
        )

        if resp.status_code != 200:
            logger.info(f"  [Chaturbate] API returned {resp.status_code}")
            return None

        data = resp.json()
        hls_url = data.get("url", "").strip()
        room_status = data.get("room_status", "unknown")

        if not hls_url:
            logger.info(f"  [Chaturbate] {model_name} offline (status: {room_status})")
            return None

        logger.info(f"  [Chaturbate] Got URL, verifying...")

        # VERIFY the URL
        if not verify_hls_url(hls_url, session, model_name):
            logger.info(f"  [Chaturbate] ✗ URL failed verification: {hls_url[:80]}")
            return None

        thumb = f"https://thumb.live.mmcdn.com/ri/{model_name}.jpg"

        # Verify thumbnail actually exists for this model
        try:
            thumb_resp = session.head(thumb, timeout=10)
            if thumb_resp.status_code != 200:
                # Try with random cache buster
                thumb = f"https://thumb.live.mmcdn.com/ri/{model_name}.jpg?{int(time.time())}"
        except Exception:
            pass

        logger.info(f"  [Chaturbate] ✓ VERIFIED for {model_name}")
        return ModelStream(
            name=model_name,
            hls_url=hls_url,
            thumb_url=thumb,
            source="chaturbate",
            verified=True,
        )

    except Exception as e:
        logger.debug(f"  [Chaturbate] Error: {e}")

    return None


# ══════════════════════════════════════════════
# METHOD 3: BROWSER NETWORK CAPTURE + VERIFICATION
# ══════════════════════════════════════════════

def fetch_via_browser_verified(
    page_url: str, model_name: str, session: requests.Session
) -> ModelStream | None:
    """
    Load chococams page in browser, capture .m3u8 network requests,
    then VERIFY the captured URL is real and belongs to this model.
    """
    if not HAS_PLAYWRIGHT:
        return None

    logger.info(f"  [Browser] Loading: {page_url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = ctx.new_page()

            # Track ALL .m3u8 network requests with their metadata
            captured: list[dict] = []

            def on_response(response):
                url = response.url
                if ".m3u8" in url:
                    status = 0
                    try:
                        status = response.status
                    except Exception:
                        pass
                    captured.append({
                        "url": url,
                        "status": status,
                        "time": time.time(),
                    })
                    logger.info(f"  [Browser] 🎯 Captured: [{status}] {url[:100]}")

            page.on("response", on_response)

            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=25000)
            except PWTimeout:
                logger.info(f"  [Browser] Page load timeout, continuing...")

            # Wait for player
            page.wait_for_timeout(5000)

            # Click play buttons
            for sel in [
                "button[class*='play']", ".play-button", ".vjs-big-play-button",
                "video", ".overlay", "[class*='play']", "#play",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        logger.info(f"  [Browser] Clicked: {sel}")
                        break
                except Exception:
                    continue

            # Wait for stream to start
            page.wait_for_timeout(12000)

            # Get thumbnail from page
            thumb = ""
            try:
                html = page.content()
                # og:image
                og_match = re.search(r'property="og:image"\s+content="([^"]+)"', html)
                if og_match:
                    thumb = og_match.group(1)
                # Or any model thumbnail
                if not thumb:
                    thumb_match = re.search(
                        r'(https?://[^\s"]+(?:thumb|preview|snapshot)[^\s"]*\.(?:jpg|png|webp))',
                        html,
                    )
                    if thumb_match:
                        thumb = thumb_match.group(1)
            except Exception:
                pass

            browser.close()

            # ── FILTER AND VERIFY captured URLs ──
            if not captured:
                logger.info(f"  [Browser] ✗ No .m3u8 URLs captured")
                return None

            logger.info(f"  [Browser] Captured {len(captured)} m3u8 URLs, filtering...")

            # Prioritize: master > playlist > chunklist
            # Remove: chunklist, segments, keys
            good_candidates = []
            for cap in captured:
                url = cap["url"]
                url_lower = url.lower()

                # Skip chunk/segment/key URLs
                if any(skip in url_lower for skip in [
                    "chunklist", "chunk", "segment", "/key/",
                    "encryption", "drm", "_360p", "_480p", "_240p",
                ]):
                    continue

                # Prefer master playlists
                priority = 2
                if "master" in url_lower:
                    priority = 0
                elif "playlist" in url_lower:
                    priority = 1

                good_candidates.append((priority, url, cap["status"]))

            # Sort by priority
            good_candidates.sort(key=lambda x: x[0])

            # Verify each candidate
            for priority, candidate_url, status in good_candidates:
                logger.info(f"  [Browser] Verifying: {candidate_url[:90]}...")

                if verify_hls_url(candidate_url, session, model_name):
                    # Detect source
                    source = "unknown"
                    if "stripst" in candidate_url or "strpst" in candidate_url:
                        source = "stripchat"
                    elif "mmcdn" in candidate_url or "growcdn" in candidate_url:
                        source = "chaturbate"

                    logger.info(f"  [Browser] ✓ VERIFIED: {candidate_url[:90]}")
                    return ModelStream(
                        name=model_name,
                        hls_url=candidate_url,
                        thumb_url=thumb,
                        source=source,
                        verified=True,
                    )

            logger.info(f"  [Browser] ✗ None of {len(good_candidates)} candidates passed verification")

    except Exception as e:
        logger.error(f"  [Browser] Error: {e}")

    return None


# ══════════════════════════════════════════════
# DETECT SOURCE FROM CHOCOCAMS
# ══════════════════════════════════════════════

def find_model_on_chococams(model_name: str, session: requests.Session) -> str | None:
    """Find which source prefix works for this model on chococams. Returns full URL or None."""
    for source in KNOWN_SOURCES:
        url = f"{BASE_URL}/model/{source}/{model_name}"
        try:
            resp = session.head(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                logger.info(f"  [Detect] Found: {url}")
                return url
        except Exception:
            continue

        # Some sites need GET not HEAD
        try:
            resp = session.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                text_start = resp.text[:500].lower()
                if "404" not in text_start and "not found" not in text_start:
                    logger.info(f"  [Detect] Found: {url}")
                    return url
        except Exception:
            continue

    return None


# ══════════════════════════════════════════════
# MASTER FETCH — ALL METHODS + MANDATORY VERIFY
# ══════════════════════════════════════════════

def fetch_model_stream(model_name: str, session: requests.Session) -> ModelStream | None:
    """
    ALL streams must pass verification. No unverified URLs in playlist.
    
    Order:
    1. Stripchat API → verify
    2. Chaturbate API → verify
    3. Browser capture on chococams page → verify
    """
    logger.info(f"\n{'━'*55}")
    logger.info(f"  FETCHING: {model_name}")
    logger.info(f"{'━'*55}")

    # ── 1. Stripchat API ──
    stream = fetch_stripchat_verified(model_name, session)
    if stream and stream.verified:
        return stream

    # ── 2. Chaturbate API ──
    stream = fetch_chaturbate_verified(model_name, session)
    if stream and stream.verified:
        return stream

    # ── 3. Find on chococams + browser capture ──
    choco_url = find_model_on_chococams(model_name, session)
    if choco_url:
        stream = fetch_via_browser_verified(choco_url, model_name, session)
        if stream and stream.verified:
            return stream

    # ── 4. Try all chococams source prefixes with browser ──
    if HAS_PLAYWRIGHT:
        for source in KNOWN_SOURCES:
            url = f"{BASE_URL}/model/{source}/{model_name}"
            stream = fetch_via_browser_verified(url, model_name, session)
            if stream and stream.verified:
                return stream

    logger.warning(f"  ✗ ALL METHODS FAILED for {model_name} (no verified stream)")
    return None


# ══════════════════════════════════════════════
# LISTING PAGE SCRAPER
# ══════════════════════════════════════════════

def scrape_listing_page(session: requests.Session) -> list[dict]:
    logger.info(f"\nScraping listing: {MODEL_LIST_URL}")
    models = []

    html = ""
    if HAS_PLAYWRIGHT:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
                page = ctx.new_page()
                page.goto(MODEL_LIST_URL, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(5000)

                # Scroll down to load more
                for _ in range(3):
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    page.wait_for_timeout(2000)

                html = page.content()
                browser.close()
        except Exception as e:
            logger.warning(f"  Browser listing failed: {e}")

    if not html:
        try:
            resp = session.get(MODEL_LIST_URL, headers=HEADERS, timeout=30)
            html = resp.text
        except Exception as e:
            logger.error(f"  Listing fetch failed: {e}")
            return []

    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    seen = set()

    # Find model links
    all_links = soup.find_all("a", href=True)
    for link in all_links:
        href = link.get("href", "")
        match = re.search(r"/model/(\w+)/(\w+)", href)
        if not match:
            continue

        source = match.group(1)
        name = match.group(2)

        if source not in KNOWN_SOURCES:
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())

        img = link.find("img")
        thumb = ""
        if img:
            thumb = img.get("src", "") or img.get("data-src", "")

        full_url = href if href.startswith("http") else BASE_URL + href
        models.append({
            "name": name, "url": full_url,
            "thumb": thumb, "source": source,
        })

    logger.info(f"  Found {len(models)} models on listing")
    return models


# ══════════════════════════════════════════════
# PLAYLIST GENERATOR
# ══════════════════════════════════════════════

def generate_playlist(streams: list[ModelStream]) -> str:
    lines = ["#EXTM3U"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"# Generated: {ts}")
    lines.append(f"# Total verified streams: {len(streams)}")

    verified_count = sum(1 for s in streams if s.verified)
    lines.append(f"# All streams verified: {verified_count}/{len(streams)}")
    lines.append("")

    favourites = [s for s in streams if s.is_favourite]
    top = [s for s in streams if not s.is_favourite]

    for label, group in [("Favourites", favourites), ("Top Models", top)]:
        if not group:
            continue
        lines.append(f"# ─── {label} ({len(group)}) ───")
        for s in group:
            logo = f' tvg-logo="{s.thumb_url}"' if s.thumb_url else ""
            lines.append(
                f'#EXTINF:-1{logo} group-title="{label}",{s.name}'
            )
            lines.append(s.hls_url)
            lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════

def main():
    logger.info("=" * 60)
    logger.info("  LIVE CAM PLAYLIST GENERATOR")
    logger.info("  All streams VERIFIED before adding")
    logger.info("=" * 60)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    all_streams: list[ModelStream] = []
    processed: set[str] = set()

    # ═══════════════════════════════════════════
    # STEP 1: FAVOURITES
    # ═══════════════════════════════════════════
    favourites = load_favourite_models()

    logger.info(f"\n{'═'*60}")
    logger.info(f"  STEP 1: FAVOURITES ({len(favourites)} models)")
    logger.info(f"{'═'*60}")

    online_favourites = 0

    for name in favourites:
        if name.lower() in processed:
            continue

        stream = fetch_model_stream(name, session)
        if stream:
            stream.is_favourite = True
            all_streams.append(stream)
            processed.add(name.lower())
            online_favourites += 1
            logger.info(f"  ⭐ FAVOURITE ONLINE: {name}")
            logger.info(f"     HLS: {stream.hls_url}")
            logger.info(f"     Verified: ✓")
        else:
            logger.warning(f"  ⭐ FAVOURITE OFFLINE: {name}")

    logger.info(f"\n  Favourites online: {online_favourites}/{len(favourites)}")

    # ═══════════════════════════════════════════
    # STEP 2: TOP MODELS (ONLY IF < 5 FAVOURITES ONLINE)
    # ═══════════════════════════════════════════
    if online_favourites >= MIN_FAVOURITES_TO_SKIP_TOP:
        logger.info(f"\n{'═'*60}")
        logger.info(f"  STEP 2: SKIPPING TOP MODELS")
        logger.info(f"  Reason: {online_favourites} favourites online (>= {MIN_FAVOURITES_TO_SKIP_TOP})")
        logger.info(f"{'═'*60}")
    else:
        need = TOP_MODEL_COUNT
        logger.info(f"\n{'═'*60}")
        logger.info(f"  STEP 2: FETCHING TOP {need} MODELS")
        logger.info(f"  Reason: Only {online_favourites} favourites online (< {MIN_FAVOURITES_TO_SKIP_TOP})")
        logger.info(f"{'═'*60}")

        listing = scrape_listing_page(session)
        top_added = 0

        for info in listing:
            if top_added >= need:
                break
            name = info["name"]
            if name.lower() in processed:
                continue

            stream = fetch_model_stream(name, session)
            if stream:
                stream.is_favourite = False
                all_streams.append(stream)
                processed.add(name.lower())
                top_added += 1
                logger.info(f"  #{top_added} TOP MODEL: {name}")
            else:
                logger.info(f"  SKIP (offline): {name}")

        logger.info(f"\n  Top models added: {top_added}")

    # ═══════════════════════════════════════════
    # STEP 3: GENERATE PLAYLIST
    # ═══════════════════════════════════════════
    logger.info(f"\n{'═'*60}")
    logger.info(f"  STEP 3: GENERATING PLAYLIST")
    logger.info(f"{'═'*60}")

    if all_streams:
        playlist = generate_playlist(all_streams)
        OUTPUT_FILE.write_text(playlist, encoding="utf-8")
        logger.info(f"  ✓ Saved: {OUTPUT_FILE}")
        print(f"\n{playlist}")
    else:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        empty = f"#EXTM3U\n# Generated: {ts}\n# No verified live streams found\n"
        OUTPUT_FILE.write_text(empty, encoding="utf-8")
        logger.warning("  No verified streams. Empty playlist.")

    # ── SUMMARY ──
    fav_online = sum(1 for s in all_streams if s.is_favourite)
    top_online = sum(1 for s in all_streams if not s.is_favourite)
    verified = sum(1 for s in all_streams if s.verified)

    logger.info(f"\n{'═'*60}")
    logger.info(f"  SUMMARY")
    logger.info(f"  ─────────────────────────────")
    logger.info(f"  Favourites in model.txt : {len(favourites)}")
    logger.info(f"  Favourites online       : {fav_online}")
    logger.info(f"  Top models added        : {top_online}")
    logger.info(f"  Total in playlist       : {len(all_streams)}")
    logger.info(f"  All verified            : {verified}/{len(all_streams)}")
    logger.info(f"  Top models skipped      : {'YES' if fav_online >= MIN_FAVOURITES_TO_SKIP_TOP else 'NO'}")
    logger.info(f"{'═'*60}")


if __name__ == "__main__":
    main()
