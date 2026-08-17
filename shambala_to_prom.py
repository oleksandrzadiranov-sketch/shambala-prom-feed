#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib, html, json, os, random, re, sys, time, urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple
import requests
from bs4 import BeautifulSoup
from lxml import etree

BASE_URL=os.getenv('BASE_URL','https://shambala.com.ua/').rstrip('/')+'/'
MIN_PRICE=float(os.getenv('MIN_PRICE','2000'))
MAX_PRODUCTS=int(os.getenv('MAX_PRODUCTS','3000'))
REQUEST_DELAY=float(os.getenv('REQUEST_DELAY','3.0'))
JITTER=float(os.getenv('JITTER','2.0'))
TIMEOUT=int(os.getenv('TIMEOUT','30'))
MAX_RETRIES=int(os.getenv('MAX_RETRIES','5'))
BACKOFF_BASE=float(os.getenv('BACKOFF_BASE','20'))
OUTPUT=os.getenv('OUTPUT','prom_feed.xml')
FILE_EXTENSIONS=('.jpg','.jpeg','.png','.webp','.gif','.svg','.ico','.pdf','.zip','.rar','.7z','.css','.js','.xml','.txt','.mp4','.webm','.avi','.mov','.woff','.woff2','.ttf','.eot')

session=requests.Session()
session.headers.update({'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36','Accept-Language':'uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7','Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8','Cache-Control':'no-cache'})

@dataclass
class Product:
    url:str
    name:str=''
    sku:str=''
    price:float=0.0
    available:bool=False
    description:str=''
    vendor:str=''
    category:str='Товари Shambala'
    images:List[str]=field(default_factory=list)
    params:Dict[str,str]=field(default_factory=dict)
    @property
    def product_id(self):
        src=self.sku.strip() or self.url
        return hashlib.sha1(src.encode('utf-8')).hexdigest()[:12]

def absolute(url,base=BASE_URL): return urllib.parse.urljoin(base,url)
def same_domain(url): return urllib.parse.urlparse(url).netloc.lower()==urllib.parse.urlparse(BASE_URL).netloc.lower()
def clean_url(url):
    p=urllib.parse.urlsplit(url); return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,'',''))
def is_static_file_url(url):
    path=urllib.parse.urlparse(url).path.lower()
    return path.endswith(FILE_EXTENSIONS) or '/content/images/' in path or '/images/' in path

def get(url):
    for attempt in range(MAX_RETRIES+1):
        time.sleep(REQUEST_DELAY+random.uniform(0,max(JITTER,0)))
        try:
            r=session.get(url,timeout=TIMEOUT,allow_redirects=True)
            if r.status_code in (404,410): return None
            if 400<=r.status_code<500 and r.status_code not in (408,429):
                print(f'[SKIP] HTTP {r.status_code}: {url}',file=sys.stderr); return None
            if r.status_code==429:
                ra=r.headers.get('Retry-After')
                wait=max(float(ra),BACKOFF_BASE) if ra and ra.isdigit() else BACKOFF_BASE*(2**attempt)
                wait=min(wait+random.uniform(1,8),600)
                if attempt<MAX_RETRIES:
                    print(f'[429] waiting {wait:.0f}s | retry {attempt+1}/{MAX_RETRIES}: {url}',file=sys.stderr); time.sleep(wait); continue
                print(f'[WARN] repeated 429, skipping: {url}',file=sys.stderr); return None
            if r.status_code in (408,500,502,503,504):
                wait=min(BACKOFF_BASE*(2**attempt)+random.uniform(1,5),300)
                if attempt<MAX_RETRIES:
                    print(f'[WARN] HTTP {r.status_code}, retry in {wait:.0f}s: {url}',file=sys.stderr); time.sleep(wait); continue
            r.raise_for_status(); return r
        except (requests.Timeout,requests.ConnectionError) as e:
            if attempt<MAX_RETRIES:
                wait=min(BACKOFF_BASE*(2**attempt)+random.uniform(1,5),300)
                print(f'[WARN] temporary connection error, retry in {wait:.0f}s: {e}',file=sys.stderr); time.sleep(wait); continue
            return None
        except requests.RequestException as e:
            print(f'[SKIP] request failed: {url}: {e}',file=sys.stderr); return None
    return None

def parse_xml_urls(text):
    pages,maps=[],[]
    try: root=etree.fromstring(text.encode('utf-8',errors='ignore'))
    except Exception: return pages,maps
    tag=etree.QName(root).localname.lower()
    locs=[x.strip() for x in root.xpath("//*[local-name()='loc']/text()") if x and x.strip()]
    (maps if tag=='sitemapindex' else pages).extend(locs)
    return pages,maps

def discover_from_sitemaps():
    queue=[absolute('sitemap.xml'),absolute('sitemap_index.xml'),absolute('sitemap-index.xml')]
    seen=set(); pages=set()
    while queue and len(seen)<100:
        sm=queue.pop(0)
        if sm in seen: continue
        seen.add(sm)
        r=get(sm)
        if not r: continue
        found,children=parse_xml_urls(r.text)
        for u in found:
            u=clean_url(u)
            if same_domain(u) and not is_static_file_url(u): pages.add(u)
        for u in children:
            if u not in seen: queue.append(u)
    print(f'[INFO] sitemap page URLs after file filtering: {len(pages)}')
    return pages

def jsonld_objects(soup):
    for tag in soup.find_all('script',type='application/ld+json'):
        raw=tag.string or tag.get_text()
        if not raw: continue
        try: data=json.loads(raw)
        except Exception: continue
        def walk(obj):
            if isinstance(obj,dict):
                yield obj
                for v in obj.values(): yield from walk(v)
            elif isinstance(obj,list):
                for v in obj: yield from walk(v)
        yield from walk(data)

def normalize_price(v):
    if v is None: return 0.0
    s=str(v).replace('\xa0','').replace(' ','').replace(',','.')
    s=re.sub(r'[^\d.]','',s)
    try: return float(s)
    except: return 0.0

def looks_like_product_page(soup):
    for obj in jsonld_objects(soup):
        typ=obj.get('@type')
        if typ=='Product' or (isinstance(typ,list) and 'Product' in typ): return True
    text=soup.get_text(' ',strip=True)
    return bool(soup.find('h1')) and bool(re.search(r'\bАртикул\s*:',text,re.I)) and bool(re.search(r'\d[\d\s\u00a0]*\s*грн',text,re.I))

def extract_product(url):
    if is_static_file_url(url): return None
    r=get(url)
    if not r: return None
    ctype=(r.headers.get('Content-Type') or '').lower()
    if 'html' not in ctype and 'xhtml' not in ctype: return None
    soup=BeautifulSoup(r.text,'html.parser')
    if not looks_like_product_page(soup): return None
    p=Product(url=clean_url(r.url))
    for obj in jsonld_objects(soup):
        typ=obj.get('@type'); is_product=typ=='Product' or (isinstance(typ,list) and 'Product' in typ)
        if not is_product: continue
        p.name=str(obj.get('name') or p.name).strip(); p.sku=str(obj.get('sku') or obj.get('mpn') or p.sku).strip(); p.description=str(obj.get('description') or p.description).strip()
        brand=obj.get('brand')
        if isinstance(brand,dict): p.vendor=str(brand.get('name') or '').strip()
        elif brand: p.vendor=str(brand).strip()
        imgs=obj.get('image'); imgs=[imgs] if isinstance(imgs,str) else imgs
        if isinstance(imgs,list): p.images.extend(absolute(str(x),r.url) for x in imgs if x)
        offers=obj.get('offers'); offers=offers if isinstance(offers,list) else [offers] if isinstance(offers,dict) else []
        for off in offers:
            pr=normalize_price(off.get('price') or off.get('lowPrice'))
            if pr: p.price=pr
            av=str(off.get('availability') or '').lower()
            if 'instock' in av or 'in_stock' in av: p.available=True
    if not p.name:
        h1=soup.find('h1'); p.name=h1.get_text(' ',strip=True) if h1 else ''
    full=soup.get_text('\n',strip=True)
    if not p.sku:
        m=re.search(r'Артикул\s*:\s*([^\n]+)',full,re.I)
        if m: p.sku=m.group(1).strip()
    if not p.price:
        m=re.search(r'(\d[\d\s\u00a0]*)\s*грн',full,re.I)
        if m: p.price=normalize_price(m.group(1))
    low=full.lower()
    if 'немає в наявності' in low: p.available=False
    elif 'в наявності' in low or 'є в наявності' in low: p.available=True
    for sel in ('.breadcrumbs a','.breadcrumb a',"[class*='breadcrumb'] a"):
        els=soup.select(sel)
        if els:
            crumbs=[x.get_text(' ',strip=True) for x in els if x.get_text(' ',strip=True)]
            useful=[x for x in crumbs if x.lower() not in ('головна','shambala')]
            if useful: p.category=useful[-1]
            break
    for meta in soup.select("meta[property='og:image'], meta[name='twitter:image']"):
        c=meta.get('content')
        if c: p.images.append(absolute(c,r.url))
    p.images=list(dict.fromkeys(x for x in p.images if x.startswith(('http://','https://'))))[:10]
    params={}
    for row in soup.select('table tr'):
        cells=row.find_all(['th','td'])
        if len(cells)>=2:
            k=cells[0].get_text(' ',strip=True); v=cells[1].get_text(' ',strip=True)
            if k and v and len(k)<120: params[k]=v
    p.params=dict(list(params.items())[:80])
    if p.description:
        txt=BeautifulSoup(p.description,'html.parser').get_text('\n',strip=True)
        p.description='<p>'+html.escape(txt).replace('\n','<br>')+'</p>' if txt else ''
    return p

def choose_urls(urls):
    deny=('/blog/','/news/','/contacts','/delivery','/payment','/garanti','/privacy','/cart','/checkout','/login','/registration','/wishlist','/compare','/site-map','/sitemap','/content/','/static/','/assets/')
    out=[]
    for u in urls:
        u=clean_url(u)
        if not same_domain(u) or is_static_file_url(u): continue
        path=urllib.parse.urlparse(u).path.lower()
        if not path.strip('/') or any(x in path for x in deny): continue
        out.append(u)
    random.shuffle(out)
    print(f'[INFO] candidate HTML URLs: {len(out)}')
    return out

def build_feed(products):
    root=etree.Element('yml_catalog',date=time.strftime('%Y-%m-%d %H:%M'))
    shop=etree.SubElement(root,'shop')
    etree.SubElement(shop,'name').text='Shambala feed'; etree.SubElement(shop,'company').text='Shambala products'; etree.SubElement(shop,'url').text=BASE_URL
    currencies=etree.SubElement(shop,'currencies'); etree.SubElement(currencies,'currency',id='UAH',rate='1')
    cats=etree.SubElement(shop,'categories'); names=sorted({p.category or 'Товари Shambala' for p in products}); ids={n:str(i+1) for i,n in enumerate(names)}
    for n in names: etree.SubElement(cats,'category',id=ids[n]).text=n
    offers=etree.SubElement(shop,'offers')
    for p in products:
        o=etree.SubElement(offers,'offer',id=p.product_id,available='true')
        etree.SubElement(o,'url').text=p.url; etree.SubElement(o,'price').text=f'{p.price:.2f}'; etree.SubElement(o,'currencyId').text='UAH'; etree.SubElement(o,'categoryId').text=ids[p.category or 'Товари Shambala']
        for img in p.images: etree.SubElement(o,'picture').text=img
        etree.SubElement(o,'name').text=p.name
        if p.vendor: etree.SubElement(o,'vendor').text=p.vendor
        if p.sku: etree.SubElement(o,'vendorCode').text=p.sku
        d=etree.SubElement(o,'description'); d.text=etree.CDATA(p.description or p.name)
        for k,v in p.params.items(): etree.SubElement(o,'param',name=k[:250]).text=v[:1000]
    etree.ElementTree(root).write(OUTPUT,pretty_print=True,xml_declaration=True,encoding='UTF-8')
    print(f'[OK] feed written: {OUTPUT} | products: {len(products)}')

def main():
    print(f'[INFO] source: {BASE_URL}')
    print(f'[INFO] stock only | price >= {MIN_PRICE:.0f} | max {MAX_PRODUCTS}')
    urls=discover_from_sitemaps(); candidates=choose_urls(urls)
    if not candidates: raise SystemExit('No candidate HTML URLs found in sitemap.')
    products=[]; checked=0
    for url in candidates:
        if len(products)>=MAX_PRODUCTS: break
        checked+=1; p=extract_product(url)
        if not p or not p.available or p.price<MIN_PRICE or not p.name: continue
        products.append(p); print(f'[{len(products)}/{MAX_PRODUCTS}] checked={checked} | {p.price:.0f} грн | {p.name[:90]}')
    if not products: raise SystemExit('No matching products collected.')
    build_feed(products)

if __name__=='__main__': main()
