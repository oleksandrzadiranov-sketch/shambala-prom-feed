#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import html
import json
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup
from lxml import etree

BASE_URL = os.getenv("BASE_URL", "https://shambala.com.ua/").rstrip("/") + "/"
MIN_PRICE = float(os.getenv("MIN_PRICE", "2000"))
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "3000"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "4.0"))
JITTER = float(os.getenv("JITTER", "2.5"))
TIMEOUT = int(os.getenv("TIMEOUT", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))
BACKOFF_BASE = float(os.getenv("BACKOFF_BASE", "20"))
OUTPUT = os.getenv("OUTPUT", "prom_feed.xml")

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
    ),
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
        src = self.sku.strip() or self.url
        return hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]

def absolute(url: str, base: str = BASE_URL) -> str:
    return urllib.parse.urljoin(base, url)

def same_domain(url: str) -> bool:
    return urllib.parse.urlparse(url).netloc.lower() == urllib.parse.urlparse(BASE_URL).netloc.lower()

def clean_url(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))

def get(url: str, allow_404: bool = False) -> Optional[requests.Response]:
    for attempt in range(MAX_RETRIES + 1):
        # polite delay before every request
        time.sleep(REQUEST_DELAY + random.uniform(0, max(JITTER, 0)))

        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True)

            if allow_404 and r.status_code == 404:
                return None

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(float(retry_after), BACKOFF_BASE)
                else:
                    wait = BACKOFF_BASE * (2 ** attempt)

                wait = min(wait + random.uniform(1, 8), 600)

                if attempt < MAX_RETRIES:
                    print(
                        f"[429] {url} | waiting {wait:.0f}s | retry {attempt+1}/{MAX_RETRIES}",
                        file=sys.stderr
                    )
                    time.sleep(wait)
                    continue

                print(f"[WARN] repeated 429, skipping: {url}", file=sys.stderr)
                return None

            if r.status_code in (500, 502, 503, 504):
                wait = min(BACKOFF_BASE * (2 ** attempt) + random.uniform(1, 5), 300)
                if attempt < MAX_RETRIES:
                    print(f"[WARN] HTTP {r.status_code}, retry in {wait:.0f}s: {url}", file=sys.stderr)
                    time.sleep(wait)
                    continue

            r.raise_for_status()
            return r

        except requests.RequestException as e:
            if attempt < MAX_RETRIES:
                wait = min(BACKOFF_BASE * (2 ** attempt) + random.uniform(1, 5), 300)
                print(f"[WARN] {e} | retry in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue

            print(f"[WARN] giving up: {url}: {e}", file=sys.stderr)
            return None

    return None

def parse_xml_urls(text: str) -> Tuple[List[str], List[str]]:
    pages, maps = [], []
    try:
        root = etree.fromstring(text.encode("utf-8", errors="ignore"))
    except Exception:
        return pages, maps

    root_tag = etree.QName(root).localname.lower()
    locs = [x.strip() for x in root.xpath("//*[local-name()='loc']/text()") if x and x.strip()]

    if root_tag == "sitemapindex":
        maps.extend(locs)
    else:
        pages.extend(locs)
    return pages, maps

def discover_from_sitemaps() -> Set[str]:
    queue = [
        absolute("sitemap.xml"),
        absolute("sitemap_index.xml"),
        absolute("sitemap-index.xml"),
    ]
    seen_maps = set()
    pages = set()

    while queue and len(seen_maps) < 100:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)

        r = get(sm, allow_404=True)
        if not r:
            continue

        found_pages, child_maps = parse_xml_urls(r.text)
        for u in found_pages:
            if same_domain(u):
                pages.add(clean_url(u))
        for u in child_maps:
            if u not in seen_maps:
                queue.append(u)

    print(f"[INFO] sitemap URLs: {len(pages)}")
    return pages

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
    s = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0

def looks_like_product_page(soup: BeautifulSoup) -> bool:
    text = soup.get_text(" ", strip=True)
    has_h1 = bool(soup.find("h1"))
    has_sku = bool(re.search(r"\bАртикул\s*:", text, re.I))
    has_price = bool(re.search(r"\d[\d\s\u00a0]*\s*грн", text, re.I))
    has_schema = any("Product" in (x.string or "") for x in soup.find_all("script", type="application/ld+json"))
    return has_schema or (has_h1 and has_sku and has_price)

def extract_product(url: str) -> Optional[Product]:
    r = get(url)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    if not looks_like_product_page(soup):
        return None

    p = Product(url=clean_url(r.url))

    for obj in jsonld_objects(soup):
        typ = obj.get("@type")
        is_product = ("Product" in typ) if isinstance(typ, list) else (typ == "Product")
        if not is_product:
            continue

        p.name = str(obj.get("name") or p.name).strip()
        p.sku = str(obj.get("sku") or obj.get("mpn") or p.sku).strip()
        p.description = str(obj.get("description") or p.description).strip()

        brand = obj.get("brand")
        if isinstance(brand, dict):
            p.vendor = str(brand.get("name") or "").strip()

        images = obj.get("image")
        if isinstance(images, str):
            images = [images]
        if isinstance(images, list):
            p.images.extend(absolute(str(x), r.url) for x in images if x)

        offers = obj.get("offers")
        offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
        for offer in offers:
            pr = normalize_price(offer.get("price") or offer.get("lowPrice"))
            if pr:
                p.price = pr
            av = str(offer.get("availability") or "").lower()
            if "instock" in av:
                p.available = True

    if not p.name:
        h1 = soup.find("h1")
        if h1:
            p.name = h1.get_text(" ", strip=True)

    full_text = soup.get_text("\n", strip=True)

    if not p.sku:
        m = re.search(r"Артикул\s*:\s*([^\n]+)", full_text, re.I)
        if m:
            p.sku = m.group(1).strip()

    if not p.price:
        m = re.search(r"(\d[\d\s\u00a0]*)\s*грн", full_text, re.I)
        if m:
            p.price = normalize_price(m.group(1))

    low = full_text.lower()
    if "немає в наявності" in low:
        p.available = False
    elif "в наявності" in low or "є в наявності" in low:
        p.available = True

    # breadcrumbs
    for sel in (".breadcrumbs a", ".breadcrumb a", "[class*='breadcrumb'] a"):
        els = soup.select(sel)
        if els:
            crumbs = [x.get_text(" ", strip=True) for x in els if x.get_text(" ", strip=True)]
            useful = [x for x in crumbs if x.lower() not in ("головна", "shambala")]
            if useful:
                p.category = useful[-1]
            break

    # images
    for meta in soup.select("meta[property='og:image'], meta[name='twitter:image']"):
        content = meta.get("content")
        if content:
            p.images.append(absolute(content, r.url))

    p.images = list(dict.fromkeys(
        x for x in p.images if x.startswith(("http://", "https://"))
    ))[:10]

    # parameters from tables
    params = {}
    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            k = cells[0].get_text(" ", strip=True)
            v = cells[1].get_text(" ", strip=True)
            if k and v and len(k) < 120:
                params[k] = v
    p.params = dict(list(params.items())[:80])

    # clean description
    if p.description:
        ds = BeautifulSoup(p.description, "html.parser")
        txt = ds.get_text("\n", strip=True)
        p.description = "<p>" + html.escape(txt).replace("\n", "<br>") + "</p>" if txt else ""

    return p

def choose_urls(urls: Set[str]) -> List[str]:
    deny = (
        "/blog/", "/news/", "/contacts", "/delivery", "/payment", "/garanti",
        "/privacy", "/cart", "/checkout", "/login", "/registration", "/wishlist"
    )
    out = []
    for u in urls:
        if not same_domain(u):
            continue
        path = urllib.parse.urlparse(u).path.lower()
        if not path.strip("/"):
            continue
        if any(x in path for x in deny):
            continue
        out.append(u)

    random.shuffle(out)
    return out

def build_feed(products: List[Product]):
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
        offer = etree.SubElement(offers, "offer", id=p.product_id, available="true")
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
            el = etree.SubElement(offer, "param", name=k[:250])
            el.text = v[:1000]

    etree.ElementTree(root).write(
        OUTPUT, pretty_print=True, xml_declaration=True, encoding="UTF-8"
    )
    print(f"[OK] {OUTPUT}: {len(products)} products")

def main():
    print(f"[INFO] source: {BASE_URL}")
    print(f"[INFO] stock only | price >= {MIN_PRICE:.0f} | max {MAX_PRODUCTS}")
    print(f"[INFO] delay: {REQUEST_DELAY}+0..{JITTER}s | retries: {MAX_RETRIES}")

    urls = discover_from_sitemaps()
    candidates = choose_urls(urls)

    if not candidates:
        raise SystemExit("No URLs found in sitemap.")

    products = []
    for idx, url in enumerate(candidates, start=1):
        if len(products) >= MAX_PRODUCTS:
            break

        p = extract_product(url)
        if not p:
            continue
        if not p.available:
            continue
        if p.price < MIN_PRICE:
            continue
        if not p.name:
            continue

        products.append(p)
        print(f"[{len(products)}/{MAX_PRODUCTS}] {p.price:.0f} грн | {p.name[:90]}")

    if not products:
        raise SystemExit("No matching products collected.")

    build_feed(products)

if __name__ == "__main__":
    main()
