#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Shambala -> Prom.ua YML feed

Default rules:
- source: https://shambala.com.ua/
- only products in stock
- minimum price: 2000 UAH
- maximum products: 3000
- keep Shambala images unchanged
- output: prom_feed.xml

The scraper tries:
1) sitemap.xml / sitemap_index.xml
2) site map and public category pages as a fallback

Please use a reasonable delay and respect the source website's access rules.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from lxml import etree

BASE_URL = os.getenv("BASE_URL", "https://shambala.com.ua/").rstrip("/") + "/"
MIN_PRICE = float(os.getenv("MIN_PRICE", "2000"))
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "3000"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
TIMEOUT = int(os.getenv("TIMEOUT", "30"))
OUTPUT = os.getenv("OUTPUT", "prom_feed.xml")

UA = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
)

session = requests.Session()
session.headers.update({
    "User-Agent": UA,
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
})


@dataclass
class Product:
    url: str
    name: str = ""
    sku: str = ""
    price: float = 0.0
    available: bool = False
    description: str = ""
    vendor: str = ""
    category: str = "Товари Shambala"
    images: List[str] = field(default_factory=list)
    params: Dict[str, str] = field(default_factory=dict)

    @property
    def product_id(self) -> str:
        source = self.sku.strip() or self.url
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        return digest


def absolute(url: str, base: str = BASE_URL) -> str:
    return urllib.parse.urljoin(base, url)


def same_domain(url: str) -> bool:
    return urllib.parse.urlparse(url).netloc.lower() == urllib.parse.urlparse(BASE_URL).netloc.lower()


def clean_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def get(url: str, *, allow_404: bool = False) -> Optional[requests.Response]:
    try:
        time.sleep(REQUEST_DELAY)
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if allow_404 and r.status_code == 404:
            return None
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        print(f"[WARN] GET failed: {url}: {e}", file=sys.stderr)
        return None


def parse_xml_urls(text: str) -> Tuple[List[str], List[str]]:
    """Return (page URLs, child sitemap URLs)."""
    pages, sitemaps = [], []
    try:
        root = etree.fromstring(text.encode("utf-8", errors="ignore"))
    except Exception:
        return pages, sitemaps

    tag = etree.QName(root).localname.lower()
    locs = root.xpath("//*[local-name()='loc']/text()")
    locs = [x.strip() for x in locs if x and x.strip()]
    if tag == "sitemapindex":
        sitemaps.extend(locs)
    else:
        pages.extend(locs)
    return pages, sitemaps


def discover_from_sitemaps() -> Set[str]:
    candidates = [
        absolute("sitemap.xml"),
        absolute("sitemap_index.xml"),
        absolute("sitemap-index.xml"),
    ]
    seen_maps: Set[str] = set()
    page_urls: Set[str] = set()

    queue = list(candidates)
    while queue and len(seen_maps) < 100:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        r = get(sm, allow_404=True)
        if not r:
            continue
        pages, children = parse_xml_urls(r.text)
        for u in pages:
            if same_domain(u):
                page_urls.add(clean_url(u))
        for child in children:
            if child not in seen_maps:
                queue.append(child)

    print(f"[INFO] Sitemap URLs discovered: {len(page_urls)}")
    return page_urls


def looks_like_product_page(soup: BeautifulSoup) -> bool:
    # Strong signals used by Shambala/Horoshop product pages
    text = soup.get_text(" ", strip=True)
    has_h1 = bool(soup.find("h1"))
    has_sku = bool(re.search(r"\bАртикул\s*:", text, re.I))
    has_price = bool(re.search(r"\d[\d\s\u00a0]*\s*грн", text, re.I))
    has_product_schema = False
    for tag in soup.find_all("script", type="application/ld+json"):
        if "Product" in (tag.string or ""):
            has_product_schema = True
            break
    return has_product_schema or (has_h1 and has_sku and has_price)


def extract_links_from_page(url: str) -> Set[str]:
    r = get(url)
    if not r:
        return set()
    soup = BeautifulSoup(r.text, "html.parser")
    links = set()
    for a in soup.find_all("a", href=True):
        u = clean_url(absolute(a["href"], r.url))
        if same_domain(u):
            links.add(u)
    return links


def discover_fallback() -> Set[str]:
    """
    Fallback crawler:
    - starts from homepage and common catalog/site-map URLs
    - explores internal pages
    - keeps likely product URLs
    This is intentionally bounded.
    """
    starts = [
        BASE_URL,
        absolute("site-map/"),
        absolute("sitemap/"),
        absolute("catalog/"),
        absolute("sale/"),
    ]
    queue = list(dict.fromkeys(starts))
    seen: Set[str] = set()
    products: Set[str] = set()

    # We don't need every page. Usually product/category links are reachable
    # from catalogue navigation and pagination.
    max_pages = int(os.getenv("MAX_DISCOVERY_PAGES", "1200"))

    while queue and len(seen) < max_pages and len(products) < MAX_PRODUCTS * 3:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        r = get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")

        if looks_like_product_page(soup):
            products.add(clean_url(r.url))
            if len(products) % 100 == 0:
                print(f"[INFO] Product URLs found: {len(products)}")
            continue

        # Add internal links, prioritizing likely catalogue/pagination links.
        all_links = []
        for a in soup.find_all("a", href=True):
            u = clean_url(absolute(a["href"], r.url))
            if not same_domain(u) or u in seen:
                continue
            path = urllib.parse.urlparse(u).path.lower()
            # avoid account, cart, blog and service pages
            if any(x in path for x in (
                "/cart", "/checkout", "/login", "/registration", "/compare",
                "/wishlist", "/blog/", "/news/", "/contacts", "/delivery",
                "/payment", "/garanti", "/dogovor", "/privacy",
            )):
                continue
            score = 0
            if "page=" in a["href"] or "/page-" in path:
                score += 3
            if a.find("img"):
                score += 2
            txt = a.get_text(" ", strip=True).lower()
            if "купити" in txt or "грн" in txt:
                score += 2
            all_links.append((score, u))

        all_links.sort(key=lambda x: x[0], reverse=True)
        for _, u in all_links[:300]:
            if u not in queue:
                queue.append(u)

        if len(seen) % 50 == 0:
            print(f"[INFO] Discovery pages: {len(seen)}; products: {len(products)}")

    print(f"[INFO] Fallback product URLs discovered: {len(products)}")
    return products


def jsonld_objects(soup: BeautifulSoup) -> Iterable[dict]:
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        def walk(obj):
            if isinstance(obj, dict):
                yield obj
                for v in obj.values():
                    yield from walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from walk(v)

        yield from walk(data)


def normalize_price(value) -> float:
    if value is None:
        return 0.0
    s = str(value).replace("\xa0", " ").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0


def text_after_label(soup: BeautifulSoup, label: str) -> str:
    text = soup.get_text("\n", strip=True)
    m = re.search(re.escape(label) + r"\s*:?\s*([^\n]+)", text, re.I)
    return (m.group(1).strip() if m else "")


def extract_product(url: str) -> Optional[Product]:
    r = get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    if not looks_like_product_page(soup):
        return None

    p = Product(url=clean_url(r.url))

    # JSON-LD first
    for obj in jsonld_objects(soup):
        typ = obj.get("@type")
        if isinstance(typ, list):
            is_product = "Product" in typ
        else:
            is_product = typ == "Product"
        if not is_product:
            continue

        p.name = str(obj.get("name") or p.name).strip()
        p.sku = str(obj.get("sku") or obj.get("mpn") or p.sku).strip()
        p.description = str(obj.get("description") or p.description).strip()

        brand = obj.get("brand")
        if isinstance(brand, dict):
            p.vendor = str(brand.get("name") or "").strip()
        elif brand:
            p.vendor = str(brand).strip()

        imgs = obj.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list):
            p.images.extend([absolute(str(x), r.url) for x in imgs if x])

        offers = obj.get("offers")
        offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
        for offer in offer_list:
            price = normalize_price(offer.get("price") or offer.get("lowPrice"))
            if price:
                p.price = price
            av = str(offer.get("availability") or "").lower()
            if "instock" in av or "in_stock" in av:
                p.available = True

    # HTML fallbacks
    h1 = soup.find("h1")
    if h1 and not p.name:
        p.name = h1.get_text(" ", strip=True)

    full_text = soup.get_text("\n", strip=True)

    if not p.sku:
        m = re.search(r"Артикул\s*:\s*([^\n]+)", full_text, re.I)
        if m:
            p.sku = m.group(1).strip()

    if not p.price:
        # Prefer price-ish elements, then text
        price_selectors = [
            "[itemprop='price']", ".product-price", ".price",
            "[class*='price']"
        ]
        for sel in price_selectors:
            for el in soup.select(sel):
                pr = normalize_price(el.get("content") or el.get_text(" ", strip=True))
                if pr:
                    p.price = pr
                    break
            if p.price:
                break
        if not p.price:
            m = re.search(r"(\d[\d\s\u00a0]*)\s*грн", full_text, re.I)
            if m:
                p.price = normalize_price(m.group(1))

    # Stock signal
    low = full_text.lower()
    if any(x in low for x in ("в наявності", "є в наявності")) and "немає в наявності" not in low:
        p.available = True
    if "немає в наявності" in low:
        p.available = False

    # Breadcrumbs/category
    crumbs = []
    for sel in (".breadcrumbs a", ".breadcrumb a", "[class*='breadcrumb'] a"):
        els = soup.select(sel)
        if els:
            crumbs = [x.get_text(" ", strip=True) for x in els if x.get_text(" ", strip=True)]
            break
    if crumbs:
        # last breadcrumb often current product, so use previous meaningful item
        useful = [x for x in crumbs if x.lower() not in ("головна", "shambala")]
        if useful:
            p.category = useful[-1]

    # Description: use itemprop or known content blocks
    if not p.description:
        desc = soup.select_one("[itemprop='description'], .product-description, [class*='description']")
        if desc:
            p.description = str(desc)

    # Characteristics
    params: Dict[str, str] = {}
    # Tables / definition-like rows
    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            k = cells[0].get_text(" ", strip=True)
            v = cells[1].get_text(" ", strip=True)
            if k and v and len(k) < 120:
                params[k] = v

    # Common Horoshop characteristic markup
    for row in soup.select("[class*='characteristic'] li, [class*='characteristic'] .row, [class*='feature'] li"):
        parts = [x.get_text(" ", strip=True) for x in row.find_all(recursive=False) if x.get_text(" ", strip=True)]
        if len(parts) >= 2:
            params[parts[0].rstrip(":")] = " ".join(parts[1:])

    # Generic text pairs in characteristic section
    char_header = soup.find(lambda tag: tag.name in ("h2", "h3", "a") and
                            "характерист" in tag.get_text(" ", strip=True).lower())
    if char_header:
        container = char_header.find_parent()
        if container:
            txt = container.get_text("\n", strip=True).splitlines()
            txt = [x.strip() for x in txt if x.strip()]
            for i in range(0, len(txt) - 1):
                k, v = txt[i], txt[i+1]
                if 1 < len(k) < 70 and 0 < len(v) < 250 and ":" not in k:
                    # avoid obvious interface labels
                    if k.lower() not in ("опис", "характеристики", "відгуки", "купити"):
                        params.setdefault(k, v)

    p.params = dict(list(params.items())[:80])

    # Images from product area / og:image
    for meta in soup.select("meta[property='og:image'], meta[name='twitter:image']"):
        content = meta.get("content")
        if content:
            p.images.append(absolute(content, r.url))

    for img in soup.select("img"):
        src = img.get("data-src") or img.get("data-lazy") or img.get("src")
        if not src:
            continue
        alt = (img.get("alt") or "").strip()
        # Keep likely product images. JSON-LD/OG normally provides main image.
        if p.name and alt and (p.name[:25].lower() in alt.lower() or alt.lower() in p.name.lower()):
            p.images.append(absolute(src, r.url))

    # Unique HTTP(S) images
    uniq = []
    for img in p.images:
        img = absolute(img, r.url)
        if img.startswith(("http://", "https://")) and img not in uniq:
            uniq.append(img)
    p.images = uniq[:10]

    # Clean description and remove store contacts/promo boilerplate
    if p.description:
        ds = BeautifulSoup(p.description, "html.parser")
        txt = ds.get_text("\n", strip=True)
        # Remove known final promotional paragraph if present
        txt = re.sub(
            r"Ви можете купити.*?(?=(?:\n|$))",
            "",
            txt,
            flags=re.I | re.S
        ).strip()
        p.description = "<p>" + html.escape(txt).replace("\n", "<br>") + "</p>" if txt else ""

    return p


def is_candidate_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.strip("/")
    if not path:
        return False
    # Exclude known non-product areas
    deny = (
        "blog/", "news/", "contacts", "delivery", "payment", "garanti",
        "dogovor", "privacy", "shops", "brands", "comparison", "wishlist",
        "cart", "checkout", "login", "registration", "site-map", "sitemap"
    )
    return not any(path.lower().startswith(x) for x in deny)


def choose_urls(all_urls: Set[str]) -> List[str]:
    urls = [u for u in all_urls if same_domain(u) and is_candidate_url(u)]
    # Product URLs on this site are usually leaf-like slugs.
    urls.sort()
    return urls


def build_feed(products: List[Product], output: str) -> None:
    root = etree.Element("yml_catalog", date=time.strftime("%Y-%m-%d %H:%M"))
    shop = etree.SubElement(root, "shop")
    etree.SubElement(shop, "name").text = "Shambala feed"
    etree.SubElement(shop, "company").text = "Shambala products"
    etree.SubElement(shop, "url").text = BASE_URL

    currencies = etree.SubElement(shop, "currencies")
    etree.SubElement(currencies, "currency", id="UAH", rate="1")

    categories = etree.SubElement(shop, "categories")
    cat_names = sorted({p.category or "Товари Shambala" for p in products})
    cat_ids = {name: str(i + 1) for i, name in enumerate(cat_names)}
    for name in cat_names:
        etree.SubElement(categories, "category", id=cat_ids[name]).text = name

    offers = etree.SubElement(shop, "offers")
    for p in products:
        offer = etree.SubElement(
            offers, "offer",
            id=p.product_id,
            available="true" if p.available else "false"
        )
        etree.SubElement(offer, "url").text = p.url
        etree.SubElement(offer, "price").text = f"{p.price:.2f}"
        etree.SubElement(offer, "currencyId").text = "UAH"
        etree.SubElement(offer, "categoryId").text = cat_ids[p.category or "Товари Shambala"]

        for img in p.images:
            etree.SubElement(offer, "picture").text = img

        etree.SubElement(offer, "name").text = p.name
        if p.vendor:
            etree.SubElement(offer, "vendor").text = p.vendor
        if p.sku:
            etree.SubElement(offer, "vendorCode").text = p.sku

        desc = etree.SubElement(offer, "description")
        desc.text = etree.CDATA(p.description or p.name)

        for k, v in p.params.items():
            if not k or not v:
                continue
            el = etree.SubElement(offer, "param", name=k[:250])
            el.text = v[:1000]

    tree = etree.ElementTree(root)
    tree.write(output, pretty_print=True, xml_declaration=True, encoding="UTF-8")
    print(f"[OK] Feed written: {output}; products: {len(products)}")


def main():
    print(f"[INFO] Source: {BASE_URL}")
    print(f"[INFO] Rules: stock only, price >= {MIN_PRICE:.0f} UAH, max {MAX_PRODUCTS}")

    urls = discover_from_sitemaps()
    candidates = choose_urls(urls)

    # If sitemap doesn't give enough page URLs, use bounded fallback crawl.
    if len(candidates) < 100:
        print("[INFO] Sitemap insufficient; starting catalogue discovery fallback...")
        urls |= discover_fallback()
        candidates = choose_urls(urls)

    print(f"[INFO] Candidate URLs: {len(candidates)}")

    products: List[Product] = []
    checked = 0

    for url in candidates:
        if len(products) >= MAX_PRODUCTS:
            break
        checked += 1
        p = extract_product(url)
        if not p:
            continue
        if not p.available:
            continue
        if p.price < MIN_PRICE:
            continue
        if not p.name or p.price <= 0:
            continue

        products.append(p)
        print(f"[{len(products)}/{MAX_PRODUCTS}] {p.price:.0f} грн | {p.name[:80]}")

    # If sitemap contained many non-product pages and yielded too few,
    # do one fallback pass and merge new product URLs.
    if len(products) < min(MAX_PRODUCTS, 300) and len(urls) > 0:
        more = discover_fallback()
        more_candidates = [u for u in choose_urls(more) if u not in set(candidates)]
        for url in more_candidates:
            if len(products) >= MAX_PRODUCTS:
                break
            p = extract_product(url)
            if not p or not p.available or p.price < MIN_PRICE or not p.name:
                continue
            products.append(p)
            print(f"[{len(products)}/{MAX_PRODUCTS}] {p.price:.0f} грн | {p.name[:80]}")

    if not products:
        raise SystemExit(
            "No products collected. The site may have changed its HTML or blocked automated requests. "
            "Check GitHub Actions logs."
        )

    build_feed(products[:MAX_PRODUCTS], OUTPUT)


if __name__ == "__main__":
    main()
