import argparse
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None


BASE_URL = "https://apkvision.org"
TELEGRAM_URL = "https://t.me/s/apkvision"
OUTPUT_PATH = Path("data/apkvision.json")
MAX_ITEMS = 500

POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?apkvision\.org/(games|apps|app)/.+?-\d+/?$",
    re.IGNORECASE,
)
VERSION_RE = re.compile(r"\b(v?\d+(?:\.\d+){1,3}(?:[a-zA-Z0-9._-]*)?)\b")
MOD_RE = re.compile(
    r"\b(MOD\s*APK|MOD|Full\s*APK|Paid|Pro|Premium|Unlocked|Menu)\b",
    re.IGNORECASE,
)
BG_URL_RE = re.compile(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fetch_text(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE_URL + "/",
    }

    if cffi_requests is not None:
        resp = cffi_requests.get(
            url,
            headers=headers,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
        resp.raise_for_status()
        return resp.text

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def normalize_url(raw_url: str) -> str:
    clean = urljoin(BASE_URL, raw_url).split("#")[0].split("?")[0]
    if not clean.endswith("/"):
        clean += "/"
    return clean


def extract_category_and_genre(url: str) -> tuple[str, str]:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if not parts:
        return "APK", ""
    root = parts[0].lower()
    category = "Games" if root == "games" else "Apps"
    genre = parts[1].replace("-", " ").title() if len(parts) >= 3 else ""
    return category, genre


def extract_version_and_mod(text: str) -> tuple[str, str]:
    ver_match = VERSION_RE.search(text or "")
    version = ver_match.group(1) if ver_match else ""
    if version and not version.lower().startswith("v"):
        version = f"v{version}"

    mod_matches = MOD_RE.findall(text or "")
    mod_type = ""
    if mod_matches:
        unique = []
        for m in mod_matches:
            norm = m.strip().upper()
            if norm not in unique:
                unique.append(norm)
        mod_type = " / ".join(unique[:2])
    return version, mod_type


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    stripped = BeautifulSoup(html.unescape(raw), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", stripped).strip()


def scrape_wp_api(per_page: int = 50) -> list[dict]:
    url = f"{BASE_URL}/wp-json/wp/v2/posts?per_page={per_page}&_embed"
    raw = fetch_text(url)
    posts = json.loads(raw)
    if not isinstance(posts, list):
        return []

    items = []
    for post in posts:
        link = normalize_url(post.get("link", ""))
        if not POST_URL_RE.match(link):
            continue
        title = clean_text(post.get("title", {}).get("rendered", ""))
        summary = clean_text(post.get("excerpt", {}).get("rendered", ""))
        category, genre = extract_category_and_genre(link)
        version, mod_type = extract_version_and_mod(f"{title} {summary}")

        thumb = ""
        embedded = post.get("_embedded", {})
        media_list = embedded.get("wp:featuredmedia", [])
        if media_list and isinstance(media_list, list):
            thumb = media_list[0].get("source_url", "") or ""

        published = post.get("modified_gmt") or post.get("date_gmt") or now_iso()
        if not published.endswith("Z") and "+" not in published:
            published += "Z"

        items.append({
            "title": title,
            "url": link,
            "version": version,
            "category": category,
            "genre": genre,
            "mod_type": mod_type,
            "thumbnail": thumb,
            "summary": summary[:220],
            "published_at": published,
        })
    return items


def scrape_rss() -> list[dict]:
    feed_urls = [
        f"{BASE_URL}/feed/",
        f"{BASE_URL}/games/feed/",
        f"{BASE_URL}/apps/feed/",
    ]
    collected = {}
    for feed_url in feed_urls:
        try:
            xml_text = fetch_text(feed_url)
            root = ET.fromstring(xml_text)
        except Exception:
            continue

        for item_el in root.findall("./channel/item"):
            link = normalize_url(item_el.findtext("link", ""))
            if not POST_URL_RE.match(link):
                continue
            title = clean_text(item_el.findtext("title", ""))
            desc = clean_text(item_el.findtext("description", ""))
            pub_raw = item_el.findtext("pubDate", "")
            category, genre = extract_category_and_genre(link)
            version, mod_type = extract_version_and_mod(f"{title} {desc}")

            thumb = ""
            raw_desc = item_el.findtext("description", "")
            if raw_desc:
                soup = BeautifulSoup(raw_desc, "html.parser")
                img = soup.find("img")
                if img:
                    thumb = img.get("data-src") or img.get("src") or ""

            pub_iso = now_iso()
            if pub_raw:
                try:
                    pub_iso = (
                        parsedate_to_datetime(pub_raw)
                        .astimezone(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                    )
                except Exception:
                    pass

            collected[link] = {
                "title": title,
                "url": link,
                "version": version,
                "category": category,
                "genre": genre,
                "mod_type": mod_type,
                "thumbnail": thumb,
                "summary": desc[:220],
                "published_at": pub_iso,
            }
    return list(collected.values())


def scrape_html_pages(pages: int = 2) -> list[dict]:
    urls = [BASE_URL + "/"]
    for page in range(1, pages + 1):
        suffix = "" if page == 1 else f"page/{page}/"
        urls.append(f"{BASE_URL}/games/{suffix}")
        urls.append(f"{BASE_URL}/apps/{suffix}")

    collected = {}
    for page_url in urls:
        try:
            html_text = fetch_text(page_url)
        except Exception:
            continue

        soup = BeautifulSoup(html_text, "html.parser")
        for anchor in soup.find_all("a", href=True):
            link = normalize_url(anchor["href"])
            if not POST_URL_RE.match(link):
                continue

            card = anchor.find_parent(["article", "div", "li"]) or anchor
            card_text = clean_text(card.get_text(" ", strip=True))
            title = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))

            if len(title) < 3 or title.lower() in {"download", "more", "read more"}:
                heading = card.find(["h1", "h2", "h3", "h4"])
                if heading:
                    title = clean_text(heading.get_text(" ", strip=True))
            if len(title) < 3:
                continue

            thumb = ""
            img = card.find("img")
            if img:
                thumb = (
                    img.get("data-src")
                    or img.get("data-lazy-src")
                    or img.get("src")
                    or ""
                )
                if thumb.startswith("//"):
                    thumb = "https:" + thumb
                elif thumb.startswith("/"):
                    thumb = BASE_URL + thumb

            category, genre = extract_category_and_genre(link)
            version, mod_type = extract_version_and_mod(f"{title} {card_text}")

            if link not in collected or (thumb and not collected[link]["thumbnail"]):
                collected[link] = {
                    "title": title,
                    "url": link,
                    "version": version,
                    "category": category,
                    "genre": genre,
                    "mod_type": mod_type,
                    "thumbnail": thumb,
                    "summary": card_text[:220] if card_text != title else "",
                    "published_at": now_iso(),
                }
    return list(collected.values())


def scrape_telegram_mirror() -> list[dict]:
    html_text = fetch_text(TELEGRAM_URL)
    soup = BeautifulSoup(html_text, "html.parser")
    collected = {}

    for msg in soup.select("div.tgme_widget_message"):
        post_link = ""
        for anchor in msg.find_all("a", href=True):
            candidate = normalize_url(anchor["href"])
            if POST_URL_RE.match(candidate):
                post_link = candidate
                break
        if not post_link:
            continue

        text_el = msg.select_one("div.tgme_widget_message_text")
        raw_lines = [
            ln.strip()
            for ln in (text_el.get_text("\n", strip=True).splitlines() if text_el else [])
            if ln.strip()
        ]
        title = raw_lines[0] if raw_lines else post_link.rstrip("/").split("/")[-1].replace("-", " ").title()
        summary = " ".join(raw_lines[1:]) if len(raw_lines) > 1 else ""

        time_el = msg.select_one(".tgme_widget_message_date time") or msg.find("time")
        pub_iso = (time_el.get("datetime") if time_el else "") or now_iso()

        thumb = ""
        photo_el = msg.select_one("a.tgme_widget_message_photo_wrap")
        if photo_el and photo_el.get("style"):
            match = BG_URL_RE.search(photo_el["style"])
            if match:
                thumb = match.group(1)

        category, genre = extract_category_and_genre(post_link)
        version, mod_type = extract_version_and_mod(f"{title} {summary}")

        collected[post_link] = {
            "title": title[:140],
            "url": post_link,
            "version": version,
            "category": category,
            "genre": genre,
            "mod_type": mod_type,
            "thumbnail": thumb,
            "summary": summary[:220],
            "published_at": pub_iso,
        }

    return list(collected.values())


def merge_items(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_url = {item["url"]: item for item in existing if item.get("url")}
    stamp = now_iso()

    for item in incoming:
        url = item["url"]
        prev = by_url.get(url)
        if prev:
            item["first_seen_at"] = prev.get("first_seen_at", stamp)
            item["last_seen_at"] = stamp
            if not item.get("thumbnail") and prev.get("thumbnail"):
                item["thumbnail"] = prev["thumbnail"]
            if not item.get("summary") and prev.get("summary"):
                item["summary"] = prev["summary"]
            if prev.get("version") == item.get("version") and prev.get("published_at"):
                item["published_at"] = prev["published_at"]
        else:
            item["first_seen_at"] = stamp
            item["last_seen_at"] = stamp
        by_url[url] = item

    merged = list(by_url.values())
    merged.sort(
        key=lambda x: x.get("published_at") or x.get("last_seen_at") or "",
        reverse=True,
    )
    return merged[:MAX_ITEMS]


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape latest uploads from apkvision.org")
    parser.add_argument("--pages", type=int, default=2, help="Number of category pages to scan")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="Output JSON path")
    args = parser.parse_args()

    existing_items = []
    if args.output.exists():
        try:
            existing_data = json.loads(args.output.read_text(encoding="utf-8"))
            existing_items = existing_data.get("items", [])
        except Exception:
            existing_items = []

    scraped = []
    strategy_used = ""

    for name, fn in [
        ("WordPress REST API", lambda: scrape_wp_api(50)),
        ("WordPress RSS", scrape_rss),
        ("HTML Listing", lambda: scrape_html_pages(args.pages)),
        ("Telegram Mirror", scrape_telegram_mirror),
    ]:
        try:
            result = fn()
            if result:
                scraped = result
                strategy_used = name
                break
        except Exception as exc:
            print(f"[{name}] failed: {exc}", file=sys.stderr)

    if not scraped:
        print("Error: All scraping strategies returned 0 items.", file=sys.stderr)
        return 1

    merged = merge_items(existing_items, scraped)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": BASE_URL,
        "strategy": strategy_used,
        "generated_at": now_iso(),
        "count": len(merged),
        "items": merged,
    }

    tmp_path = args.output.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, args.output)
    print(f"Updated {args.output} with {len(scraped)} scraped items ({len(merged)} total) via {strategy_used}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
