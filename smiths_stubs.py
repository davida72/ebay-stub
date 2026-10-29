"""Daily eBay sweeper for The Smiths ticket stubs.

Uses the eBay Browse API (OAuth client-credentials). Discovers listings via
`item_summary/search`, pulls details via `item/{itemId}`, downloads every
gallery image at the highest resolution eBay serves, and writes a Markdown
record per listing with a best-effort gig-date extraction (1982–1986).

Env vars (required):
  EBAY_CLIENT_ID
  EBAY_CLIENT_SECRET

Dedup state lives in stubs/index.json so re-runs are idempotent.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import dateparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ----- config ---------------------------------------------------------------

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BROWSE_ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
MARKETPLACE_ID = "EBAY_GB"

# Keyword query equivalent to _nkw="smiths"+ticket on the web search.
SEARCH_QUERY = '"smiths" ticket'

# EU + UK countries — approximates the website's LH_PrefLoc=2 ("European Union")
# filter. The Browse API has no single "EU" value, so we enumerate.
EU_COUNTRIES = (
    "GB", "IE", "FR", "DE", "IT", "ES", "NL", "BE", "PT",
    "AT", "DK", "SE", "FI", "PL", "CZ", "GR", "HU", "RO",
)
LOCATION_FILTER = "itemLocationCountry:{" + "|".join(EU_COUNTRIES) + "}"

PAGE_SIZE = 200   # Browse API max per request
MAX_PAGES = 10
REQUEST_DELAY_S = 0.5   # API calls — generous but not excessive

IMAGE_SIZES = ("s-l2400", "s-l1600", "s-l1200", "s-l800")

# Smiths played no gigs after 1986; 1982 is their first live year.
SMITHS_YEAR_MIN = 1982
SMITHS_YEAR_MAX = 1986

MORRISSEY_SOLO_KEYWORDS = (
    "morrissey",
    "your arsenal",
    "vauxhall",
    "viva hate",
    "kill uncle",
    "ringleader",
    "quarry",
    "world peace",
)

# Marr post-Smiths projects — unambiguous: these never appear in a
# genuine 1982–1986 Smiths listing.
MARR_SOLO_KEYWORDS = (
    "the healers",
    "modest mouse",
    "the cribs",
    "the messenger",
    "playland",
    "call the comet",
    "fever dreams",
    "spirit power",
    "electronic",  # Marr + Bernard Sumner, 1988+
)

# Years that place a gig outside The Smiths' 1982–1986 active window.
_NON_SMITHS_YEAR_RE = re.compile(r"\b(?:19(?:8[7-9]|9\d)|20\d{2})\b")

STUBS_DIR = Path("stubs")
INDEX_PATH = STUBS_DIR / "index.json"
TOKEN_CACHE_PATH = Path(".ebay-token.json")

USER_AGENT = "ebay-stub-sweeper/1.0 (+github.com/davidamor/ebay-stub)"

# kiss-my-shades R2 "Incoming" bucket. Images uploaded here show up in the
# admin's Incoming review view and get AI-suggested dates by the admin service.
R2_BUCKET = os.environ.get("R2_BUCKET", "smiths-live")
R2_INCOMING_PREFIX = "_incoming/assets"

log = logging.getLogger("smiths_stubs")


# ----- data types -----------------------------------------------------------


@dataclass
class Listing:
    listing_id: str          # legacyItemId — the human-visible numeric id
    item_id: str             # API composite id e.g. v1|123|0
    url: str
    title: str = ""
    price: str = ""
    seller: str = ""
    condition: str = ""
    specifics: dict[str, str] = field(default_factory=dict)
    description_text: str = ""
    image_urls: list[str] = field(default_factory=list)


# ----- OAuth ----------------------------------------------------------------


def _load_cached_token() -> str | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text())
        if data.get("expires_at", 0) > time.time() + 60:
            return data["access_token"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _store_token(token: str, expires_in: int) -> None:
    TOKEN_CACHE_PATH.write_text(json.dumps({
        "access_token": token,
        "expires_at": int(time.time()) + int(expires_in),
    }))


def fetch_oauth_token() -> str:
    cached = _load_cached_token()
    if cached:
        return cached
    client_id = os.environ.get("EBAY_CLIENT_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET must be set. "
            "Create a production app at developer.ebay.com and export both."
        )
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        OAUTH_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
        timeout=30,
    )
    if not resp.ok:
        raise SystemExit(
            f"eBay OAuth {resp.status_code}: {resp.text}\n"
            f"client_id length={len(client_id)}, client_secret length={len(client_secret)}\n"
            "Common causes: using sandbox keys (should be production), "
            "swapped App ID / Cert ID, or whitespace in the exported values."
        )
    body = resp.json()
    _store_token(body["access_token"], body["expires_in"])
    log.info("obtained new eBay OAuth token (expires in %ss)", body["expires_in"])
    return body["access_token"]


def api_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    return s


def polite_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    time.sleep(REQUEST_DELAY_S)
    resp = session.get(url, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


# ----- image URL helpers ----------------------------------------------------


def _max_res_url(src: str) -> str:
    """Rewrite an eBay image URL to its highest-resolution variant."""
    return re.sub(r"s-l\d+(?=\.(?:jpg|jpeg|png|webp))", IMAGE_SIZES[0], src, count=1)


# ----- search (Browse API) --------------------------------------------------


def search(session: requests.Session, max_pages: int = MAX_PAGES) -> list[tuple[str, str, str]]:
    """Yield (legacy_id, item_id, web_url) for every listing in the query.

    Dedups by legacy_id across pages.
    """
    seen: dict[str, tuple[str, str]] = {}
    for page in range(max_pages):
        offset = page * PAGE_SIZE
        params = {
            "q": SEARCH_QUERY,
            "limit": PAGE_SIZE,
            "offset": offset,
            "filter": LOCATION_FILTER,
        }
        log.info("search page %d (offset=%d)", page + 1, offset)
        resp = polite_get(session, BROWSE_SEARCH_URL, params=params)
        body = resp.json()
        summaries = body.get("itemSummaries") or []
        page_hits = 0
        for s in summaries:
            legacy = s.get("legacyItemId") or _legacy_from_item_id(s.get("itemId", ""))
            item_id = s.get("itemId", "")
            url = s.get("itemWebUrl", "")
            if not legacy or legacy in seen:
                continue
            seen[legacy] = (item_id, url)
            page_hits += 1
        total = body.get("total", 0)
        log.info("  returned %d, new %d (total matching=%s, dedup set=%d)",
                 len(summaries), page_hits, total, len(seen))
        if len(summaries) < PAGE_SIZE:
            break
    if not seen:
        log.warning("search returned zero listings")
    return [(lid, item_id, url) for lid, (item_id, url) in seen.items()]


def _legacy_from_item_id(item_id: str) -> str | None:
    # API composite is "v1|<legacy>|<variant>"
    parts = item_id.split("|")
    return parts[1] if len(parts) >= 2 and parts[1].isdigit() else None


# ----- listing detail -------------------------------------------------------


def _strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def fetch_listing(session: requests.Session, legacy_id: str, item_id: str, web_url: str) -> Listing:
    url = BROWSE_ITEM_URL.format(item_id=item_id)
    resp = polite_get(session, url)
    body = resp.json()

    title = body.get("title", "")
    price_obj = body.get("price") or {}
    price = f"{price_obj.get('value', '')} {price_obj.get('currency', '')}".strip()
    seller = (body.get("seller") or {}).get("username", "")
    condition = body.get("condition", "")

    specifics: dict[str, str] = {}
    for aspect in body.get("localizedAspects") or []:
        name = aspect.get("name")
        value = aspect.get("value")
        if name and value:
            specifics[name] = value

    description_text = _strip_html(body.get("description", "")) or body.get("shortDescription", "")

    image_urls: list[str] = []
    seen_hashes: set[str] = set()
    primary = ((body.get("image") or {}).get("imageUrl")) or ""
    extras = [img.get("imageUrl") for img in (body.get("additionalImages") or []) if img.get("imageUrl")]
    for src in [primary, *extras]:
        if not src or "ebayimg.com" not in src:
            continue
        m = re.search(r"/g/([^/]+)/", src)
        key = m.group(1) if m else src
        if key in seen_hashes:
            continue
        seen_hashes.add(key)
        image_urls.append(_max_res_url(src))

    return Listing(
        listing_id=legacy_id,
        item_id=item_id,
        url=web_url or f"https://www.ebay.co.uk/itm/{legacy_id}",
        title=title,
        price=price,
        seller=seller,
        condition=condition,
        specifics=specifics,
        description_text=description_text,
        image_urls=image_urls,
    )


# ----- filters & date extraction -------------------------------------------


def is_non_smiths(listing: "Listing") -> str | None:
    """Return a short reason string if the listing is a Morrissey or Marr solo
    item, otherwise None. Scans title, item specifics, and description."""
    title_lower = (listing.title or "").lower()
    specifics_blob = " ".join(f"{k} {v}" for k, v in listing.specifics.items()).lower()
    body_blob = (listing.description_text or "").lower()

    for kw in MARR_SOLO_KEYWORDS:
        if kw in title_lower or kw in specifics_blob:
            return f"marr-solo:{kw}"

    # Johnny Marr by name is a signal only when he's clearly the headliner —
    # in a Smiths listing he often appears in passing descriptions.
    if "johnny marr" in title_lower and (
        "smiths" not in title_lower or _NON_SMITHS_YEAR_RE.search(title_lower)
    ):
        return "marr-solo:marr-headlines-title"

    # Morrissey keyword in the title with no "smiths" claim: original behaviour.
    if "smiths" not in title_lower:
        for kw in MORRISSEY_SOLO_KEYWORDS:
            if kw in title_lower:
                return f"morrissey-solo:{kw}"
        # Fall through to specifics/description when title doesn't claim smiths.
        for kw in MORRISSEY_SOLO_KEYWORDS:
            if kw in specifics_blob or kw in body_blob:
                return f"morrissey-solo-body:{kw}"
        m = _NON_SMITHS_YEAR_RE.search(title_lower)
        if m:
            return f"non-smiths-year-in-title:{m.group(0)}"

    return None


_MONTH_RE = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE_WORDS_RE = re.compile(
    rf"(?i)(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MONTH_RE}\s+(?:19)?8[2-6]"
    rf"|{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?:19)?8[2-6])"
)
_DATE_NUMERIC_RE = re.compile(
    r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.]((?:19)?8[2-6])\b"
)


def extract_date(text_blob: str) -> str | None:
    if not text_blob:
        return None
    candidates: list[str] = []
    for m in _DATE_WORDS_RE.finditer(text_blob):
        candidates.append(m.group(0))
    for m in _DATE_NUMERIC_RE.finditer(text_blob):
        candidates.append(m.group(0))

    for cand in candidates:
        parsed = dateparser.parse(
            cand,
            settings={"DATE_ORDER": "DMY", "REQUIRE_PARTS": ["day", "month", "year"]},
        )
        if not parsed:
            continue
        year = parsed.year
        if year < 100:
            year += 1900
            parsed = parsed.replace(year=year)
        if SMITHS_YEAR_MIN <= year <= SMITHS_YEAR_MAX:
            return parsed.date().isoformat()
    return None


# ----- persistence ----------------------------------------------------------


def load_index() -> dict:
    if not INDEX_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_PATH.read_text())
    except json.JSONDecodeError:
        log.warning("index.json unreadable; starting fresh")
        return {}


def save_index(idx: dict) -> None:
    STUBS_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, indent=2, sort_keys=True) + "\n")


def download_image(url: str, dest: Path) -> bool:
    """Download an image. Not authenticated — direct GET to i.ebayimg.com."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in (1, 2):
        try:
            time.sleep(REQUEST_DELAY_S)
            r = requests.get(url, timeout=60, stream=True, headers=headers)
            if r.status_code == 404 and attempt == 1:
                for size in IMAGE_SIZES[1:]:
                    fallback = re.sub(r"s-l\d+", size, url, count=1)
                    r2 = requests.get(fallback, timeout=60, stream=True, headers=headers)
                    if r2.ok:
                        with dest.open("wb") as fh:
                            for chunk in r2.iter_content(32768):
                                fh.write(chunk)
                        return True
                return False
            r.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in r.iter_content(32768):
                    fh.write(chunk)
            return True
        except requests.RequestException as e:
            log.warning("image download failed (attempt %d) %s: %s", attempt, url, e)
    return False


def _image_extension(url: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(ext):
            return ext
    return ".jpg"


def _yaml_escape(v: str) -> str:
    v = (v or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()
    return f'"{v}"'


def write_markdown(folder: Path, listing: Listing, gig_date: str | None, first_seen: str,
                   image_filenames: list[str]) -> None:
    lines = ["---",
             f"title: {_yaml_escape(listing.title)}",
             f"listing_id: {listing.listing_id}",
             f"url: {_yaml_escape(listing.url)}",
             f"price: {_yaml_escape(listing.price)}",
             f"seller: {_yaml_escape(listing.seller)}",
             f"condition: {_yaml_escape(listing.condition)}",
             f"gig_date: {gig_date or 'null'}",
             f"first_seen: {first_seen}",
             "images:"]
    for name in image_filenames:
        lines.append(f"  - {name}")
    if listing.specifics:
        lines.append("specifics:")
        for k, v in listing.specifics.items():
            lines.append(f"  {_yaml_escape(k)}: {_yaml_escape(v)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {listing.title}".rstrip())
    lines.append("")
    lines.append(listing.description_text or "_(no description provided by seller)_")
    lines.append("")
    (folder / "listing.md").write_text("\n".join(lines))


# ----- kiss-my-shades Incoming push ----------------------------------------


def _r2_client():
    """Lazy S3 client for Cloudflare R2. Only called when push mode is on."""
    import boto3
    from botocore.config import Config

    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def _r2_credentials_present() -> bool:
    return all(os.environ.get(k) for k in
               ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))


def fetch_incoming_manifest(s3) -> list[dict]:
    key = f"{R2_INCOMING_PREFIX}/assets.json"
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=key)
        data = json.loads(obj["Body"].read())
        return data if isinstance(data, list) else []
    except s3.exceptions.NoSuchKey:
        return []
    except Exception as e:
        # Fall back to empty rather than clobbering with a half-known state.
        # ClientError.NoSuchKey can also surface this way depending on botocore.
        if "NoSuchKey" in str(e):
            return []
        log.warning("could not fetch incoming manifest, assuming empty: %s", e)
        return []


def write_incoming_manifest(s3, entries: list[dict]) -> None:
    body = (json.dumps(entries, indent=2) + "\n").encode("utf-8")
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=f"{R2_INCOMING_PREFIX}/assets.json",
        Body=body,
        ContentType="application/json",
        CacheControl="no-cache",
    )


def merge_manifest(existing: list[dict], additions: list[dict]) -> list[dict]:
    """Merge-by-filename. New entries win on conflict (idempotent re-uploads)."""
    by_name = {e.get("filename"): e for e in existing if e.get("filename")}
    for a in additions:
        by_name[a["filename"]] = a
    return list(by_name.values())


_CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "png": "image/png", "webp": "image/webp"}


def push_listing_to_incoming(s3, listing: Listing, image_sources: list[tuple[str, Path]],
                             gig_date: str | None) -> list[dict]:
    """Upload each image to R2 _incoming/assets/ and merge the manifest.

    image_sources: list of (remote_filename, local_path) tuples.
    """
    additions = []
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for fname, local in image_sources:
        ext = fname.rsplit(".", 1)[-1].lower()
        ctype = _CONTENT_TYPES.get(ext, "image/jpeg")
        s3.upload_file(
            str(local), R2_BUCKET, f"{R2_INCOMING_PREFIX}/{fname}",
            ExtraArgs={"ContentType": ctype},
        )
        additions.append({
            "filename": fname,
            "uploadedAt": now,
            "type": "stub",
            "contentType": ctype,
            "source": "ebay-sweep",
            "ebayListingId": listing.listing_id,
            "ebayUrl": listing.url,
            "gigDateHint": gig_date or None,
        })
    existing = fetch_incoming_manifest(s3)
    merged = merge_manifest(existing, additions)
    write_incoming_manifest(s3, merged)
    return additions


def _incoming_filename(listing_id: str, gig_date: str | None, idx: int, ext: str) -> str:
    date_hint = gig_date or "unknown"
    ext = ext.lstrip(".").lower() or "jpg"
    return f"ebay-{listing_id}-{date_hint}-stub-{idx:02d}.{ext}"


# ----- backfill -------------------------------------------------------------


def backfill_to_incoming(s3, index: dict) -> dict:
    """Upload already-saved local stub images to R2 incoming.

    Walks every index entry with status=saved and no pushed_to flag, finds the
    stub-NN.* images in stubs/<folder>/, uploads each under the standard
    _incoming filename, merges the manifest once at the end, and marks the
    entry as pushed in-place. Idempotent: entries missing images on disk are
    logged and skipped without blocking the rest.
    """
    counters = {"candidates": 0, "pushed": 0, "no-images": 0, "errors": 0}
    all_additions: list[dict] = []
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    for listing_id, entry in list(index.items()):
        if entry.get("status") != "saved" or entry.get("pushed_to"):
            continue
        counters["candidates"] += 1
        folder_name = entry.get("folder")
        if not folder_name:
            counters["no-images"] += 1
            continue
        folder = STUBS_DIR / folder_name
        images = sorted(folder.glob("stub-*.*"))
        if not images:
            log.warning("backfill: %s has no local images in %s", listing_id, folder)
            counters["no-images"] += 1
            continue

        gig_date = entry.get("gig_date")
        r2_filenames: list[str] = []
        try:
            for i, local in enumerate(images, start=1):
                ext = local.suffix
                remote_name = _incoming_filename(listing_id, gig_date, i, ext)
                ctype = _CONTENT_TYPES.get(ext.lstrip(".").lower(), "image/jpeg")
                s3.upload_file(
                    str(local), R2_BUCKET, f"{R2_INCOMING_PREFIX}/{remote_name}",
                    ExtraArgs={"ContentType": ctype},
                )
                r2_filenames.append(remote_name)
                all_additions.append({
                    "filename": remote_name,
                    "uploadedAt": now,
                    "type": "stub",
                    "contentType": ctype,
                    "source": "ebay-sweep-backfill",
                    "ebayListingId": listing_id,
                    "ebayUrl": f"https://www.ebay.co.uk/itm/{listing_id}",
                    "gigDateHint": gig_date or None,
                })
        except Exception as e:
            counters["errors"] += 1
            log.exception("backfill failed for %s: %s", listing_id, e)
            continue

        entry["pushed_to"] = "kms-incoming"
        entry["r2_filenames"] = r2_filenames
        counters["pushed"] += 1
        log.info("backfilled %s (%d images)", listing_id, len(r2_filenames))

    if all_additions:
        existing = fetch_incoming_manifest(s3)
        merged = merge_manifest(existing, all_additions)
        write_incoming_manifest(s3, merged)
        log.info("manifest merged: +%d entries, total=%d", len(all_additions), len(merged))

    return counters


# ----- main loop ------------------------------------------------------------


def process_listing(session: requests.Session, legacy_id: str, item_id: str, web_url: str,
                    index: dict, debug_dates: bool, s3=None) -> str:
    push_incoming = s3 is not None
    listing = fetch_listing(session, legacy_id, item_id, web_url)

    reason = is_non_smiths(listing)
    if reason:
        log.info("skip non-smiths (%s): %s (%s)", reason, legacy_id, listing.title[:80])
        index[legacy_id] = {"status": "skipped-non-smiths", "reason": reason,
                            "title": listing.title,
                            "first_seen": dt.date.today().isoformat()}
        return "skipped-non-smiths"

    date_text = "\n".join([listing.title,
                           *(f"{k}: {v}" for k, v in listing.specifics.items()),
                           listing.description_text])
    gig_date = extract_date(date_text)
    if debug_dates:
        log.info("DATE-DEBUG %s -> %s | text: %s", legacy_id, gig_date,
                 date_text[:300].replace("\n", " ⏎ "))

    folder_name = f"{gig_date}__{legacy_id}" if gig_date else f"unknown__{legacy_id}"
    folder = STUBS_DIR / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    image_filenames: list[str] = []
    r2_filenames: list[str] = []

    if push_incoming:
        # Download to a temp dir, upload to R2, then discard the bytes locally.
        # Only listing.md stays in the repo so stubs/ never bloats.
        with tempfile.TemporaryDirectory(prefix=f"ebay-{legacy_id}-") as tmp:
            tmpdir = Path(tmp)
            sources: list[tuple[str, Path]] = []
            for i, img_url in enumerate(listing.image_urls, start=1):
                ext = _image_extension(img_url)
                local = tmpdir / f"stub-{i:02d}{ext}"
                if not download_image(img_url, local):
                    continue
                remote_name = _incoming_filename(legacy_id, gig_date, i, ext)
                sources.append((remote_name, local))
            if sources:
                push_listing_to_incoming(s3, listing, sources, gig_date)
                r2_filenames = [name for name, _ in sources]
                image_filenames = r2_filenames  # markdown points at R2 names
    else:
        for i, img_url in enumerate(listing.image_urls, start=1):
            ext = _image_extension(img_url)
            fname = f"stub-{i:02d}{ext}"
            if download_image(img_url, folder / fname):
                image_filenames.append(fname)

    first_seen = dt.date.today().isoformat()
    write_markdown(folder, listing, gig_date, first_seen, image_filenames)

    index_entry = {
        "status": "saved",
        "folder": folder_name,
        "title": listing.title,
        "gig_date": gig_date,
        "image_count": len(image_filenames),
        "first_seen": first_seen,
    }
    if push_incoming:
        index_entry["pushed_to"] = "kms-incoming"
        index_entry["r2_filenames"] = r2_filenames
    index[legacy_id] = index_entry
    where = "→ kms-incoming" if push_incoming else folder_name
    log.info("saved %s %s (%d images, date=%s)",
             legacy_id, where, len(image_filenames), gig_date)
    return "saved" if gig_date else "saved-undated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug-dates", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after processing N new listings (0 = no limit)")
    parser.add_argument("--push-incoming", action="store_true",
                        default=os.environ.get("KMS_PUSH_INCOMING", "").lower() in ("1", "true", "yes"),
                        help="upload each new listing's images to the kiss-my-shades R2 "
                             "Incoming bucket instead of saving them locally. Default "
                             "honours KMS_PUSH_INCOMING env var. Requires R2_ACCOUNT_ID, "
                             "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.")
    parser.add_argument("--backfill", action="store_true",
                        help="upload already-saved local stub images to the kiss-my-shades "
                             "R2 Incoming bucket for every index entry missing pushed_to. "
                             "Skips the eBay search entirely. Requires R2 credentials.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    STUBS_DIR.mkdir(parents=True, exist_ok=True)

    if args.backfill:
        if not _r2_credentials_present():
            log.error("--backfill requires R2 credentials "
                      "(R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).")
            return 2
        s3 = _r2_client()
        log.info("backfill mode: uploading existing local stubs to s3://%s/%s/",
                 R2_BUCKET, R2_INCOMING_PREFIX)
        index = load_index()
        counters = backfill_to_incoming(s3, index)
        save_index(index)
        log.info("backfill done: %s", counters)
        return 0

    s3 = None
    if args.push_incoming:
        if not _r2_credentials_present():
            log.error("--push-incoming requested but R2 credentials are missing. "
                      "Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY.")
            return 2
        s3 = _r2_client()
        log.info("push-incoming mode: uploading to s3://%s/%s/", R2_BUCKET, R2_INCOMING_PREFIX)

    token = fetch_oauth_token()
    session = api_session(token)
    index = load_index()

    hits = search(session, max_pages=args.max_pages)
    counters = {"new": 0, "saved": 0, "saved-undated": 0,
                "skipped-non-smiths": 0, "errors": 0}

    for legacy_id, item_id, web_url in hits:
        if legacy_id in index:
            continue
        counters["new"] += 1
        try:
            tag = process_listing(session, legacy_id, item_id, web_url, index,
                                  args.debug_dates, s3=s3)
            if tag in counters:
                counters[tag] += 1
            else:
                counters[tag] = 1
        except requests.HTTPError as e:
            counters["errors"] += 1
            log.warning("HTTP error on %s: %s", legacy_id, e)
        except Exception as e:
            counters["errors"] += 1
            log.exception("failed to process %s: %s", legacy_id, e)
        save_index(index)
        if args.limit and counters["new"] >= args.limit:
            break

    log.info("done: %s", counters)
    return 0


if __name__ == "__main__":
    sys.exit(main())
