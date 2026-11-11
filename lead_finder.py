"""
Yorelore Lead Finder v3: any small seller of a physical product, from any platform.

Sources (all found through Google via Serper; we never log in to or scrape Instagram/TikTok/Etsy/eBay):
  web     independent stores on any platform (Shopify, WooCommerce, Wix, Squarespace, Big Cartel, Square...)
  ig      Instagram seller profiles (followers come from Google's result snippet)
  tiktok  TikTok seller accounts
  etsy    Etsy shops (total sales from the snippet = size signal)
  ebay    eBay stores (items sold from the snippet)
For social/marketplace sellers one extra search finds their own website, Facebook and contact details;
the website is then read for published email/phone (free).

Fit target: small, independent sellers big enough to fund ads (~$7k-10k+/month): IG 2k-150k followers,
Etsy 2k-80k sales, eBay 1k+ sold, web stores with an ad pixel or 5-800 products, no big-retail stockists.

Usage:
  python lead_finder.py --country US --count 60
  python lead_finder.py --country AU --count 20 --niche "pet accessories" --sources web,ig
  python lead_finder.py --sync          # push the whole tracker CSV to the Google Sheet
"""
import argparse, csv, datetime as dt, html, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests
import phonenumbers
import dns.resolver

HERE = Path(__file__).resolve().parent
TRACKER = HERE.parent / "Yorelore lead tracker.csv"
SEEN_FILE = HERE / "seen_domains.txt"          # every domain/handle ever checked (so we never pay twice)
DNC_FILE = HERE / "do_not_contact.txt"         # one domain, handle or email per line
OUT_DIR = HERE / "output"
# 2.5 models return 404 for new keys (checked 11 Oct 2026); 3.5-flash answers in ~3 s
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}

DEFAULT_NICHES = ["boutique", "handmade jewelry", "candles", "streetwear", "kitchen gadgets", "home decor",
                  "pet accessories", "crochet", "phone cases", "baby clothes", "activewear", "hair accessories",
                  "bags", "home gadgets"]
PLACES = {"US": ["USA", "Texas", "California", "Florida", "New York", "Georgia", "Ohio", "North Carolina", "Arizona"],
          "AU": ["Australia", "Sydney", "Melbourne", "Brisbane", "Perth", "Gold Coast"]}
NON_SELLER = re.compile(r"web ?design|hosting|seo|marketing|agency|magazine|news|blog|recipe|review|wiki|"
                        r"catalog|directory|software|app|theme|template|plugin|course|coupon|forum|podcast|"
                        r"esquire|wired|mapquest|scribd|dashlane|landerlab|decorilla", re.I)

COLUMNS = [
    "Date added", "Source", "Brand / seller", "Product to feature", "Niche", "Country", "Timezone", "Platform",
    "Website", "Profile / shop URL", "Business phone", "Email", "IG handle", "IG followers", "TikTok handle",
    "Facebook", "Size signal", "Independent?", "Running ads? (pixel)", "Founder / owner", "Qualify score (0-6)",
    "Best channel", "Angle", "Opener line", "DM (ready to send)", "Call angle", "Email subject", "Email first line",
    "Call attempts", "Last call outcome", "Do not call (Y/N)", "Variant", "Date touch 1", "Replied? (Y/N)",
    "Reply date", "Sample requested? (Y/N)", "Sample sent date", "Loom sent? (Y/N)", "FU day 2", "FU day 5",
    "FU day 9", "Call booked (date + their time)", "Call outcome", "Package offered", "Price (USD/AUD)", "Status",
    "Proof (where email/phone was published)", "Notes", "Key",
]

SKIP_DOMAINS = re.compile(
    r"(amazon|etsy|ebay|walmart|target\.com|reddit|youtube|facebook|instagram|pinterest|tiktok|"
    r"twitter|x\.com|linkedin|wikipedia|shopify\.com|myshopify\.com$|medium\.com|quora|bestbuy|"
    r"homedepot|lowes|wayfair|costco|chewy|petco|petsmart|kohls|macys|nordstrom|nytimes|forbes|"
    r"buzzfeed|cnn|google|apple\.com|yelp|trustpilot|alibaba|aliexpress|temu|shein|wish\.com|"
    r"bigcommerce\.com|wix\.com|squarespace\.com|goodhousekeeping|wirecutter|rei\.com|dickssportinggoods|"
    r"linktr\.ee|beacons\.ai|stan\.store|hubspot|bluehost|zenbusiness|lendio|merchantmaverick|"
    r"blog|news|magazine|\.gov|\.edu|faire\.com|poshmark|mercari|depop|zazzle|redbubble|teepublic|"
    r"spreadshirt|threadless|ulta|sephora|target|ikea|overstock|groupon|bbb\.org|crunchbase|printful|printify|"
    r"gelato|spocket|zendrop|cjdropshipping|dsers|alibaba|made-in-china|dhgate|canva|wordpress\.com|godaddy)", re.I)
BIG_RETAIL = re.compile(r"(available|sold|find us|now) (at|in) (select )?(target|walmart|petco|petsmart|ulta|"
                        r"sephora|costco|best buy|nordstrom|whole foods|macy'?s|kohl'?s|dick'?s|bed bath|tj ?maxx)", re.I)
VENDOR_NOISE = re.compile(r"(wholesale|vendor|supplier|academy|dropship(ping)? course|mentor|coach|agency|"
                          r"how to start|we help brands|ugc creator)", re.I)

# Each source: list of query templates. {niche}, {place} = "USA"/"Australia"
QUERIES = {
    "web": ['inurl:/products/ "{niche}" {place} -amazon -etsy -ebay',
            'inurl:/product-page/ "{niche}"',                       # Wix stores
            'inurl:/product/ "{niche}" "SKU" "add to cart"',        # WooCommerce
            'inurl:/shop/p/ "{niche}"',                             # Squarespace stores
            '"powered by shopify" "{niche}" small business'],
    "ig": ['site:instagram.com {niche} shop {place}',
           'site:instagram.com {niche} small business {place}',
           'site:instagram.com {niche} owner {place} -reel'],
    "tiktok": ['site:tiktok.com/@ {niche} small business {place} -vendor -wholesale',
               'site:tiktok.com {niche} "tiktok shop" "link in bio" {place} -vendor -wholesale',
               'site:tiktok.com {niche} "tiktok shop" small business {place} -vendor -wholesale'],
    "etsy": ['site:etsy.com/shop "{niche}" "Sales" "On Etsy since"'],
    "ebay": ['site:ebay.com/str "{niche}" "items sold"'],
}

US_STATES = {"AL": "CT", "AK": "AKT", "AZ": "MT", "AR": "CT", "CA": "PT", "CO": "MT", "CT": "ET", "DE": "ET",
             "FL": "ET", "GA": "ET", "HI": "HST", "ID": "MT", "IL": "CT", "IN": "ET", "IA": "CT", "KS": "CT",
             "KY": "ET", "LA": "CT", "ME": "ET", "MD": "ET", "MA": "ET", "MI": "ET", "MN": "CT", "MS": "CT",
             "MO": "CT", "MT": "MT", "NE": "CT", "NV": "PT", "NH": "ET", "NJ": "ET", "NM": "MT", "NY": "ET",
             "NC": "ET", "ND": "CT", "OH": "ET", "OK": "CT", "OR": "PT", "PA": "ET", "RI": "ET", "SC": "ET",
             "SD": "CT", "TN": "CT", "TX": "CT", "UT": "MT", "VT": "ET", "VA": "ET", "WA": "PT", "WV": "ET",
             "WI": "CT", "WY": "MT", "DC": "ET"}
US_STATE_NAMES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA",
                  "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA",
                  "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
                  "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
                  "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
                  "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
                  "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
                  "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
                  "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
                  "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY"}
AU_TZ = {"WA": "AWST", "QLD": "AEST", "SA": "ACDT", "NT": "ACST", "NSW": "AEDT", "VIC": "AEDT", "ACT": "AEDT",
         "TAS": "AEDT", "Western Australia": "AWST", "Queensland": "AEST", "South Australia": "ACDT",
         "Northern Territory": "ACST"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK_EMAIL = re.compile(r"(sentry|wixpress|example\.|domain\.com|email\.com|yourname|\.png|\.jpg|\.webp|"
                        r"\.gif|\.svg|godaddy|shopify|u003e|noreply|no-reply|@2x|privacy@|abuse@|dmca@|"
                        r"yourstore|yourdomain|sample@|test@|wordpress|woocommerce|squarespace)", re.I)
SOCIAL_RE = {
    "instagram": re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})/?[\"'?\s]", re.I),
    "tiktok": re.compile(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I),
    "facebook": re.compile(r"facebook\.com/([A-Za-z0-9_.\-]{3,60})/?[\"'?\s]", re.I),
}
SOCIAL_JUNK = {"p", "reel", "reels", "explore", "sharer", "share", "sharer.php", "tr", "plugins", "dialog",
               "accounts", "about", "help", "policies", "legal", "watch", "stories", "privacy", "shopify",
               "instagram", "facebook", "tiktok", "wix", "squarespace", "groups", "pages", "events", "profile.php"}
META_PIXEL = re.compile(r'pixel_type\\*"\s*:\s*\\*"facebook_pixel|fbq\(\s*[\'"]init|connect\.facebook\.net/[^"\']*fbevents|'
                        r'"facebookCapiEnabled"\s*:\s*true', re.I)
TIKTOK_PIXEL = re.compile(r"analytics\.tiktok\.com|ttq\.load|pixelCode", re.I)
PLATFORMS = [("Shopify", r"cdn\.shopify\.com|Shopify\.theme"), ("WooCommerce", r"woocommerce"),
             ("Wix", r"wixstatic\.com|wix-code|_wixCIDX"), ("Squarespace", r"squarespace"),
             ("BigCommerce", r"bigcommerce"), ("Big Cartel", r"bigcartel"), ("Square Online", r"square\.site|squareup|weebly"),
             ("Ecwid", r"ecwid"), ("Magento", r"Magento|mage/cookies")]
CART = re.compile(r"add[ -_]to[ -_]cart|/cart|checkout|shop now|buy now|add to bag", re.I)

_lock = threading.Lock()


# ---------- helpers ----------
def load_env():
    env = {}
    for name in (".env", ".env.txt"):
        p = HERE / name
        if p.exists():
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
    env.update({k: v for k, v in os.environ.items() if k in ("SERPER_API_KEY", "GEMINI_API_KEY", "SHEETS_WEBHOOK_URL", "SHEETS_TOKEN")})
    return env


def root_domain(url):
    host = urlparse(url if "//" in url else "https://" + url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def get(url, timeout=15):
    for attempt in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
            return r if r.status_code == 200 else None
        except requests.RequestException:
            time.sleep(2)
    return None


def html_to_text(html, limit=3000):
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def num(s):
    """'65.2K' -> 65200, '1,368' -> 1368"""
    m = re.match(r"([\d.,]+)\s*([KkMm])?", (s or "").strip())
    try:
        v = float(m.group(1).replace(",", "").rstrip("."))
    except (AttributeError, ValueError):
        return None
    return int(v * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1))


def read_lines(path):
    return {l.strip().lower() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()} if path.exists() else set()


class Budget:
    def __init__(self, n): self.left, self.used = n, 0
    def take(self):
        with _lock:
            if self.left <= 0:
                return False
            self.left -= 1; self.used += 1
            return True
    def refund(self):
        with _lock:
            self.left += 1; self.used -= 1


def serper(q, key, budget, gl, page=1):
    if not budget.take():
        return None
    for attempt in range(3):  # flaky connections: retry; a failed call is refunded
        try:
            r = requests.post("https://google.serper.dev/search", timeout=30,
                              headers={"X-API-KEY": key, "Content-Type": "application/json"},
                              json={"q": q, "gl": gl, "num": 10, "page": page})
            if r.status_code == 200:
                return r.json().get("organic", [])
        except Exception:
            pass
        time.sleep(4 * (attempt + 1))
    budget.refund()
    return None


# ---------- 1. discover ----------
def parse_result(source, o):
    """Turn one Google result into a candidate, or None."""
    link, title, snip = o.get("link", ""), o.get("title", ""), o.get("snippet", "")
    blob = f"{title} {snip}"
    if VENDOR_NOISE.search(blob):
        return None
    c = {"source": source, "snippet": blob[:600], "title": title}
    if source == "web":
        d = root_domain(link)
        if not d or SKIP_DOMAINS.search(d) or NON_SELLER.search(title + " " + d):
            return None
        return {**c, "key": d, "domain": d, "name": ""}
    if source == "ig":
        m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:reels/?)?(?:\?.*)?$", link)
        if not m or m.group(1).lower() in SOCIAL_JUNK:
            return None
        f = re.search(r"([\d.,]+\s*[KkMm]?)\s+followers", blob, re.I)
        name = re.sub(r"\s*[\(•|].*$", "", title).strip()
        return {**c, "key": "ig:" + m.group(1).lower(), "ig": m.group(1), "name": name,
                "followers": num(f.group(1)) if f else None, "url": f"https://instagram.com/{m.group(1)}"}
    if source == "tiktok":
        m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", link)
        if not m:
            return None
        f = re.search(r"([\d.,]+\s*[KkMm]?)\s+Followers", blob, re.I)
        # video pages have the video caption as title, so only trust the title on profile pages
        is_profile = re.match(r"https?://(?:www\.)?tiktok\.com/@[^/?]+/?(?:\?.*)?$", link)
        name = re.sub(r"\s*[\(|-].*$", "", title).strip() if is_profile else ""
        return {**c, "key": "tt:" + m.group(1).lower(), "tiktok": m.group(1), "name": name or "@" + m.group(1),
                "followers": num(f.group(1)) if f else None, "url": f"https://www.tiktok.com/@{m.group(1)}"}
    if source == "etsy":
        m = re.search(r"etsy\.com/(?:[a-z-]+/)?shop/([A-Za-z0-9]+)", link)
        if not m:
            return None
        s = re.search(r"Sales\s*([\d,]+)|([\d,]+)\s+sales", blob, re.I)
        since = re.search(r"On Etsy since (\d{4})", blob)
        return {**c, "key": "etsy:" + m.group(1).lower(), "name": m.group(1), "url": f"https://www.etsy.com/shop/{m.group(1)}",
                "sales": num(s.group(1) or s.group(2)) if s else None, "since": since.group(1) if since else ""}
    if source == "ebay":
        m = re.search(r"ebay\.com/str/([A-Za-z0-9_.-]+)", link)
        if not m:
            return None
        s = re.search(r"([\d.,]+\s*[KkMm]?)\s+items sold", blob, re.I)
        fol = re.search(r"([\d.,]+\s*[KkMm]?)\s+followers", blob, re.I)
        name = re.sub(r"\s*\|.*$", "", title).strip()
        return {**c, "key": "ebay:" + m.group(1).lower(), "name": name or m.group(1), "url": f"https://www.ebay.com/str/{m.group(1)}",
                "sales": num(s.group(1)) if s else None, "followers": num(fol.group(1)) if fol else None}
    return None


def discover(niches, sources, country, key, budget, exclude):
    gl = country.lower()
    plan = [(src, tpl) for src in sources for tpl in QUERIES[src]]
    for page in (1, 2):
        for ni, niche in enumerate(niches):  # rotate niches, then sources, so a run spreads wide
            place = PLACES[country][(ni + page) % len(PLACES[country])]
            for src, tpl in plan:
                q = tpl.format(niche=niche, place=place)
                results = serper(q, key, budget, gl, page)
                if results is None:
                    if budget.left <= 0:
                        return
                    continue
                for o in results:
                    c = parse_result(src, o)
                    if c and c["key"] not in exclude:
                        exclude.add(c["key"])
                        if c.get("domain"):
                            exclude.add(c["domain"])
                        c["seed_niche"] = niche
                        yield c


# ---------- 2. read a website (free) ----------
def read_site(domain):
    base = f"https://{domain}"
    home = get(base)
    if not home:
        return None
    paths = ("/pages/contact", "/pages/contact-us", "/pages/about", "/pages/about-us", "/contact", "/contact-us",
             "/about", "/about-us", "/policies/privacy-policy", "/policies/refund-policy", "/privacy-policy",
             "/policies/terms-of-service", "/pages/faq")
    pages = {"home": home}
    with ThreadPoolExecutor(max_workers=7) as pool:
        for path, r in zip(paths, pool.map(lambda p: get(base + p, timeout=10), paths)):
            if r and r.url.rstrip("/") != home.url.rstrip("/"):
                pages[path] = r
    html_all = {k: r.text for k, r in pages.items()}
    h = html_all["home"]

    emails, phones, socials = {}, {}, {}
    for src, html in html_all.items():
        url = base if src == "home" else base + src
        for e in set(EMAIL_RE.findall(html)):
            e = e.lower().strip(".")
            if not JUNK_EMAIL.search(e) and e not in emails:
                emails[e] = url
        for t in re.findall(r'href=["\']tel:([^"\']+)', html, re.I):
            t = re.sub(r"[^\d+]", "", t)
            if len(t) >= 9 and t not in phones:
                phones[t] = url
        if src != "home":
            for t in re.findall(r"(?:\+?1[\s.-]?)?\(?\b[2-9]\d{2}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\+61[\s\d]{9,12}|\b1[38]00[\s\d]{6,8}|\b0[2-478][\s\d]{8,10}", html_to_text(html, 20000)):
                t2 = re.sub(r"[^\d+]", "", t)
                if t2 not in phones:
                    phones[t2] = url
        for net, rx in SOCIAL_RE.items():
            for mt in rx.finditer(html):
                handle = mt.group(1)
                if handle.lower() not in SOCIAL_JUNK and net not in socials:
                    socials[net] = handle

    text_all = " ".join(html_to_text(x, 20000) for x in html_all.values())
    about_text = " ".join(html_to_text(x, 2000) for k, x in html_all.items() if "about" in k)
    platform = next((n for n, rx in PLATFORMS if re.search(rx, h, re.I)), "Custom")

    meta, prods = None, []
    if platform == "Shopify":
        r = get(f"{base}/meta.json")
        if r:
            try:
                meta = r.json()
            except ValueError:
                pass
        r = get(f"{base}/products.json?limit=100")
        if r:
            try:
                prods = r.json().get("products", [])
            except ValueError:
                pass
    newest_days, prices = None, []
    for p in prods:
        for v in p.get("variants", [])[:1]:
            try:
                prices.append(float(v.get("price") or 0))
            except (ValueError, TypeError):
                pass
        ts = p.get("updated_at") or p.get("published_at")
        if ts:
            try:
                age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
                newest_days = age if newest_days is None else min(newest_days, age)
            except ValueError:
                pass
    titles = [p.get("title", "") for p in prods[:40]]
    if not titles:  # other platforms: product names from product links on the homepage
        titles = list(dict.fromkeys(html_to_text(a, 80) for a in re.findall(
            r'<a[^>]+href=["\'][^"\']*/(?:product|products|product-page|shop/p)/[^"\']+["\'][^>]*>(.*?)</a>', h, re.I | re.S)))
        titles = [t for t in titles if 3 < len(t) < 80][:30]

    return {
        "domain": domain, "platform": platform, "emails": emails, "phones": phones, "socials": socials,
        "meta_pixel": bool(META_PIXEL.search(h)), "tiktok_pixel": bool(TIKTOK_PIXEL.search(h)),
        "is_store": bool(CART.search(h)) or platform != "Custom",
        "big_retail": bool(BIG_RETAIL.search(text_all)),
        "home_text": html_to_text(h, 1500), "about_text": about_text[:1500], "text_all": text_all[:60000],
        "titles": titles, "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
        "newest_days": newest_days, "meta": meta,
        "name": (meta or {}).get("name") or html_to_text(re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S).group(1), 60)
        if re.search(r"<title[^>]*>(.*?)</title>", h, re.I | re.S) else domain,
    }


def locate(site, country):
    """Country + region + timezone from Shopify meta.json, else from published phone/address text."""
    m = site.get("meta") or {}
    if m.get("country"):
        prov = m.get("province") or ""
        if m["country"] == "US":
            st = US_STATE_NAMES.get(prov, prov)
            return "US", f"{m.get('city','')}, {prov}".strip(", "), US_STATES.get(st, "")
        if m["country"] == "AU":
            return "AU", f"{m.get('city','')}, {prov}".strip(", "), AU_TZ.get(prov, "AEDT")
        return m["country"], "", ""
    t = site["text_all"]
    for c in (country, "AU" if country == "US" else "US"):   # both targets: run's country first
        hit = _locate_text(site, t, c)
        if hit:
            return hit
    return "", "", ""


def _locate_text(site, t, country):
    if country == "US":
        z = re.search(r"\b(" + "|".join(US_STATES) + r")\s+\d{5}(?:-\d{4})?\b", t)
        if z:
            return "US", z.group(1), US_STATES[z.group(1)]
        for full, ab in US_STATE_NAMES.items():
            if re.search(rf"{full},?\s+\d{{5}}\b", t):
                return "US", ab, US_STATES[ab]
        if any(p.startswith("+1") or (len(p) == 10 and p[0] in "23456789") for p in site["phones"]):
            return "US", "", ""
    else:
        z = re.search(r"\b(NSW|VIC|QLD|WA|SA|TAS|NT|ACT)\s+\d{4}\b", t)
        if z:
            return "AU", z.group(1), AU_TZ[z.group(1)]
        if site["domain"].endswith(".au") or any(p.startswith("+61") or p.startswith("0") for p in site["phones"]):
            return "AU", "", "AEDT"
    return None


# ---------- 3. verify ----------
_mx_cache = {}
def has_mx(email):
    d = email.split("@")[1]
    if d not in _mx_cache:
        try:
            _mx_cache[d] = bool(dns.resolver.resolve(d, "MX", lifetime=8))
        except Exception:
            _mx_cache[d] = False
    return _mx_cache[d]


FREE_MAIL = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "aol.com", "proton.me", "live.com"}

def own_email(e, domain):
    """Only the seller's own inbox: same domain (or subdomain) or a personal free mailbox. Drops PR agencies,
    helpdesks on other domains and parent companies, which aren't the seller's published contact."""
    ed = e.split("@")[1]
    if ed in FREE_MAIL:
        return True
    if not domain:
        return False
    stem = domain.split(".")[0]
    return ed == domain or ed.endswith("." + domain) or ed.split(".")[0] == stem


def best_email(emails, domain):
    good = [e for e in emails if own_email(e, domain) and has_mx(e)]
    good.sort(key=lambda e: (bool(domain) and not e.endswith(domain),
                             e.split("@")[0] in ("support", "orders", "returns", "help", "service"), len(e)))
    return (good[0], emails[good[0]]) if good else ("", "")


def best_phone(phones, country):
    for raw, src in phones.items():
        try:
            n = phonenumbers.parse(raw, country)
            if phonenumbers.is_valid_number(n) and phonenumbers.region_code_for_number(n) == country:
                return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL), src
        except phonenumbers.NumberParseException:
            pass
    return "", ""


# ---------- 4. find a social/marketplace seller's own site (1 search) ----------
def find_links(c, key, budget, gl):
    q = f'"{c.get("ig") or c.get("tiktok") or c["name"]}"' + ("" if c["source"] in ("ig", "tiktok") else " shop")
    res = serper(q, key, budget, gl) or []
    out = {"domain": "", "ig": "", "tiktok": "", "facebook": "", "emails": {}, "snips": ""}
    for o in res:
        link, blob = o.get("link", ""), f'{o.get("title","")} {o.get("snippet","")}'
        out["snips"] += " " + blob
        for e in EMAIL_RE.findall(blob):
            if not JUNK_EMAIL.search(e):
                out["emails"].setdefault(e.lower().strip("."), link)
        d = root_domain(link)
        if "instagram.com" in d and not out["ig"]:
            m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?$", link.split("?")[0])
            if m and m.group(1).lower() not in SOCIAL_JUNK:
                out["ig"] = m.group(1)
        elif "tiktok.com" in d and not out["tiktok"]:
            m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", link)
            if m:
                out["tiktok"] = m.group(1)
        elif "facebook.com" in d and not out["facebook"]:
            out["facebook"] = link.split("?")[0]
        elif not out["domain"] and not SKIP_DOMAINS.search(d):
            # only accept a site whose domain resembles the seller's name/handle
            stem = re.sub(r"[^a-z0-9]", "", (c.get("ig") or c.get("tiktok") or c["name"]).lower())[:8]
            if stem and stem[:5] in d.replace("-", ""):
                out["domain"] = d
    return out


# ---------- 5. qualify one candidate ----------
def qualify(c, a, env, budget):
    src, gl = c["source"], a.country.lower()
    L = {"source": src, "key": c["key"], "seed_niche": c["seed_niche"], "name": c.get("name", ""),
         "profile_url": c.get("url", ""), "ig": c.get("ig", ""), "tiktok": c.get("tiktok", ""), "facebook": "",
         "followers": c.get("followers"), "sales": c.get("sales"), "since": c.get("since", ""),
         "snippet": c.get("snippet", ""), "site": None, "email": "", "email_src": "", "phone": "", "phone_src": ""}

    # fit gates on the snippet numbers before spending anything
    if src == "ig" and L["followers"] is not None and not 2000 <= L["followers"] <= 150000:
        return None, f"IG followers {L['followers']}"
    if src == "etsy" and (L["sales"] is None or not 1000 <= L["sales"] <= 80000):
        return None, "Etsy sales outside 1k-80k"
    if src == "ebay" and (L["sales"] is None or L["sales"] < 1000):
        return None, "eBay sold <1k"

    domain = c.get("domain", "")
    if src != "web":
        links = find_links(c, env["SERPER_API_KEY"], budget, gl)
        domain = links["domain"]
        L["ig"] = L["ig"] or links["ig"]
        L["tiktok"] = L["tiktok"] or links["tiktok"]
        L["facebook"] = links["facebook"]
        L["snippet"] += " " + links["snips"][:600]
        e, s = best_email(links["emails"], domain)
        if e:
            L["email"], L["email_src"] = e, s
        if src == "ig" and L["followers"] is None:
            f = re.search(r"([\d.,]+\s*[KkMm]?)\s+followers", links["snips"], re.I)
            L["followers"] = num(f.group(1)) if f else None
            if L["followers"] is not None and not 2000 <= L["followers"] <= 150000:
                return None, "IG followers outside 2k-150k"

    country, region, tz = "", "", ""
    if domain:
        site = read_site(domain)
        if site:
            if src == "web" and (not site["is_store"] or (site["platform"] == "Custom" and not site["titles"])
                                 or NON_SELLER.search(site["name"])):
                return None, "not a store"
            if site["big_retail"]:
                return None, "big-retail stockist"
            m = site.get("meta") or {}
            n = m.get("published_products_count")
            if n is not None and not 3 <= n <= 800:
                return None, f"{n} products"
            L["site"] = site
            L["name"] = L["name"] if src != "web" else site["name"]
            country, region, tz = locate(site, a.country)
            if country and country not in ("US", "AU"):   # we target both US and AU
                return None, f"country {country}"
            e, s = best_email(site["emails"], domain)
            if e and not L["email"]:
                L["email"], L["email_src"] = e, s
            L["phone"], L["phone_src"] = best_phone(site["phones"], country or a.country)
            L["ig"] = L["ig"] or site["socials"].get("instagram", "")
            L["tiktok"] = L["tiktok"] or site["socials"].get("tiktok", "")
            fb = site["socials"].get("facebook")
            L["facebook"] = L["facebook"] or (f"https://facebook.com/{fb}" if fb else "")
        elif src == "web":
            return None, "site unreachable"
    if not country:
        place = "USA" if a.country == "US" else "Australia"
        bl = L["snippet"]
        if src == "web":
            return None, "country not confirmed"
        if re.search(place + r"|United States|\bUS\b" if a.country == "US" else place + r"|Aussie|\bAU\b", bl, re.I) \
                or any(re.search(rf"\b{s}\b", bl) for s in (US_STATE_NAMES if a.country == "US" else ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide"])):
            country = a.country
    L.update(country=country or f"{a.country}?", region=region, tz=tz, domain=domain)

    # size / spend signals
    site = L["site"] or {}
    pixel = bool(site.get("meta_pixel") or site.get("tiktok_pixel"))
    nprod = (site.get("meta") or {}).get("published_products_count")
    signals = []
    if L["followers"]: signals.append(f"{L['followers']:,} followers")
    if L["sales"]: signals.append(f"{L['sales']:,} {'Etsy sales' if src == 'etsy' else 'eBay items sold'}" + (f" since {L['since']}" if L["since"] else ""))
    if nprod: signals.append(f"{nprod} products")
    if pixel: signals.append("ad pixel installed")
    if site.get("newest_days") is not None and site["newest_days"] <= 30: signals.append("store updated in last 30 days")
    size_ok = ((L["followers"] and 2000 <= L["followers"] <= 150000) or (L["sales"] and L["sales"] >= 1000)
               or (nprod and 5 <= nprod <= 800) or (src == "web" and pixel))
    spend = pixel or (L["followers"] or 0) >= 10000 or (L["sales"] or 0) >= 5000
    checks = {"country confirmed": not L["country"].endswith("?"), "size fit": bool(size_ok),
              "ad-spend signal": bool(spend), "email": bool(L["email"]), "phone": bool(L["phone"]),
              "IG/TikTok": bool(L["ig"] or L["tiktok"])}
    L["score"] = sum(checks.values())
    L["missing"] = [k for k, v in checks.items() if not v]
    L["signals"] = "; ".join(signals)
    L["pixel"] = pixel
    L["platform"] = {"etsy": "Etsy", "ebay": "eBay"}.get(src, site.get("platform", "Instagram" if src == "ig" else "TikTok" if src == "tiktok" else ""))
    L["independent"] = "Likely" if (src in ("etsy", "ebay", "ig", "tiktok") or L["email"].split("@")[-1] in FREE_MAIL
                                     or (L["followers"] or 0) < 50000 and src != "web") else "Check"
    if not (L["email"] or L["phone"] or L["ig"] or L["tiktok"]):
        return None, "no contact route"
    if L["score"] < a.min_score:
        return None, f"score {L['score']}"
    return L, "ok"


# ---------- 6. personalise (Gemini, 5 leads per call) ----------
PROMPT = """You prepare cold outreach for Francis, who runs Yorelore, a creative studio. Yorelore turns a seller's
product photos into UGC-style video ads (no shipping product to creators, no shoots) and can run the ads.
Target: small independent sellers who can afford ads, in the US or Australia (see each seller's location).
Tone per seller: direct, specific, confident for US sellers; casual, understated for Australian sellers.

Below is REAL scraped data for {n} sellers. Use ONLY this data. Never invent facts, numbers, reviews, names, ads,
reels or posts. You have NOT watched their videos or ads, so never claim to have.

{leads}

Return a JSON list with one object per seller, in the same order, each with:
"id", "brand_name" (the clean shop/brand name as customers know it, not a page title or tagline), "summary" (2 plain sentences: what they sell, to whom), "niche" (1-3 words),
"product_to_feature" (one real product named in the data, best for a 15-second demo ad; if no product is named, a short product type),
"founder_name" (only if written in the data, else ""),
"best_channel" (one of: "IG DM", "TikTok DM", "Email", "Call"; pick the most direct route present in the data),
"angle" (one line: the specific reason ads would help THIS seller, from the data, e.g. Etsy seller with big sales but no own-site ads; boutique with 20k followers but no ad pixel),
"opener_line" (one specific, true observation, max 20 words, not flattery, nothing generic),
"dm" (under 60 words, first name if known else "Hey there". Structure: opener line, then "I run Yorelore, a creative studio. We turn product photos into UGC-style ads, no shipping product to creators.", then "I'd like to make [Brand] a free sample ad for [product]. Cool if I do?"),
"call_angle" (one sentence for a cold call, about their product),
"email_subject" (max 6 words, lowercase, names the brand or product),
"email_first_line" (max 25 words, built on the opener line),
"established_brand" (true if the data shows a large or national company: a platform or supplier rather than a seller, an MLM, a franchise, stocked in major retailers, a household name; false for small or independent sellers),
"account_type" ("brand" for a business account; "personal" for a personal/creator account that sells products through its posts, TikTok Shop or a link),
"not_a_seller" (true ONLY if the account sells no products at all: a community/"shop local" page, directory, blog, news, agency, service business; a personal account that sells through posts or TikTok Shop is a seller, so false).
"""


def lead_brief(i, L):
    s = L["site"] or {}
    parts = [f"id: {i}", f"source: {L['source']}", f"name: {L['name']}", f"location: {L.get('region','')} {L['country']}"]
    if L["profile_url"]: parts.append(f"profile: {L['profile_url']}")
    if L["domain"]: parts.append(f"website: {L['domain']} ({s.get('platform','')})")
    if L["signals"]: parts.append(f"signals: {L['signals']}")
    parts.append(f"search snippets: {L['snippet'][:700]}")
    if s.get("titles"): parts.append("products: " + "; ".join(s["titles"][:25]))
    if s.get("price_min") is not None: parts.append(f"price range: ${s['price_min']:.0f}-${s['price_max']:.0f}")
    if s.get("home_text"): parts.append(f"homepage text: {s['home_text'][:1200]}")
    if s.get("about_text"): parts.append(f"about text: {s['about_text'][:900]}")
    return "\n".join(parts)


DEAD_MODELS = set()


def gemini(prompt, key):
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}}
    for model in GEMINI_MODELS:
        if model in DEAD_MODELS:
            continue
        for attempt in range(3):
            try:
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  params={"key": key}, json=body, timeout=75)
            except requests.RequestException:
                time.sleep(5); continue
            if r.status_code == 200:
                try:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(re.sub(r"^```(json)?|```$", "", txt.strip()).strip())
                except (KeyError, IndexError, ValueError):
                    break
            if r.status_code == 429 and "PerDay" in r.text:
                DEAD_MODELS.add(model); break  # daily free quota used up: skip this model for the rest of the run
            if r.status_code in (429, 500, 503):
                time.sleep(15 * (attempt + 1)); continue
            break  # other 4xx: try the next model
    return None


def personalise(leads, country, key):
    tone = "Tone: casual, understated Australian." if country == "AU" else "Tone: direct, specific, confident American."
    for start in range(0, len(leads), 5):
        chunk = leads[start:start + 5]
        p = PROMPT.format(country=country, tone=tone, n=len(chunk),
                          leads="\n\n---\n".join(lead_brief(i, L) for i, L in enumerate(chunk)))
        out = gemini(p, key) or []
        if isinstance(out, dict):
            out = out.get("sellers") or out.get("leads") or list(out.values())[0] if out else []
        by_id = {str(o.get("id")): o for o in out if isinstance(o, dict)}
        for i, L in enumerate(chunk):
            L["ai"] = by_id.get(str(i), {})
        print(f"  openers {min(start + 5, len(leads))}/{len(leads)}: {sum(bool(L['ai']) for L in chunk)}/{len(chunk)} ok")
        time.sleep(3)


# ---------- 7. output ----------
def greet(dm):
    d = (dm or "").strip()
    if d and not d.lower().startswith(("hey", "hi ", "hi,", "hello")):
        d = "Hey there, " + d[0].lower() + d[1:]
    return d


def to_row(L, today):
    ai = L.get("ai") or {}
    proof = "; ".join(x for x in (f"email: {L['email_src']}" if L["email"] else "",
                                  f"phone: {L['phone_src']}" if L["phone"] else "") if x)
    return {
        "Date added": today, "Source": {"web": "Website", "ig": "Instagram", "tiktok": "TikTok", "etsy": "Etsy", "ebay": "eBay"}[L["source"]],
        "Brand / seller": ai.get("brand_name") or L["name"], "Product to feature": ai.get("product_to_feature", ""),
        "Niche": ai.get("niche", L["seed_niche"]), "Country": L["country"], "Timezone": L.get("tz", ""),
        "Platform": L["platform"], "Website": f"https://{L['domain']}" if L["domain"] else "",
        "Profile / shop URL": L["profile_url"], "Business phone": L["phone"], "Email": L["email"],
        "IG handle": f"@{L['ig']}" if L["ig"] else "", "IG followers": L["followers"] if L["source"] == "ig" and L["followers"] else "",
        "TikTok handle": f"@{L['tiktok']}" if L["tiktok"] else "", "Facebook": L["facebook"],
        "Size signal": L["signals"], "Independent?": L["independent"],
        "Running ads? (pixel)": "Y" if L["pixel"] else ("?" if L["site"] else "no site"),
        "Founder / owner": ai.get("founder_name", ""), "Qualify score (0-6)": L["score"],
        "Best channel": ai.get("best_channel", ""), "Angle": ai.get("angle", ""), "Opener line": ai.get("opener_line", ""),
        "DM (ready to send)": greet(ai.get("dm", "")), "Call angle": ai.get("call_angle", ""),
        "Email subject": ai.get("email_subject", ""), "Email first line": ai.get("email_first_line", ""),
        "Call attempts": 0, "Do not call (Y/N)": "N", "Variant": "B", "Status": "New",
        "Proof (where email/phone was published)": proof,
        "Notes": ("Personal account (sells via posts/TikTok Shop) | " if ai.get("account_type") == "personal" else "")
                 + f"{ai.get('summary','')} | Missing: {', '.join(L['missing']) or 'none'}",
        "Key": L["key"],
    }


def read_tracker():
    if not TRACKER.exists():
        return []
    with open(TRACKER, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_tracker(rows):
    with open(TRACKER, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: html.unescape(str(r.get(c, ""))) for c in COLUMNS})


def sheet_call(env, payload, tries=5):
    # read-only actions are safe to re-send, so retry the whole call; writes are sent once
    if payload.get("action") in ("read", "version", "dnc"):
        for attempt in range(3):
            try:
                return _sheet_call(env, payload, tries)
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(10)
    return _sheet_call(env, payload, tries)


def _sheet_call(env, payload, tries=5):
    """POST once to the Apps Script web app, then fetch its reply with retries. Google's reply page is flaky
    (sometimes an HTML 404), so we retry the reply fetch rather than re-sending the write."""
    url = env.get("SHEETS_WEBHOOK_URL")
    payload = {"token": env.get("SHEETS_TOKEN"), **payload}
    r = requests.post(url, json=payload, timeout=180, allow_redirects=False)
    loc = r.headers.get("Location")
    if not loc:
        return r.json()
    for attempt in range(tries):
        try:
            g = requests.get(loc, timeout=120)
            if g.text.lstrip().startswith("{"):
                return g.json()
        except requests.RequestException:
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("no reply from the sheet (the write may still have worked; check the sheet)")


def push_sheet(env, rows, mode="append", tab="Leads"):
    """Send rows to the Google Sheet through the Apps Script web app (see SHEETS SETUP.md)."""
    url, token = env.get("SHEETS_WEBHOOK_URL"), env.get("SHEETS_TOKEN")
    if not url or not token:
        print("Google Sheet not connected yet (SHEETS_WEBHOOK_URL missing): rows are in the CSV only.")
        return False
    payload = {"action": mode, "tab": tab, "header": COLUMNS,
               "rows": [[html.unescape(str(r.get(c, ""))) for c in COLUMNS] for r in rows]}
    try:
        res = sheet_call(env, payload)
        print(f"Google Sheet {mode} ({tab}): {'ok' if res.get('ok') else 'FAILED ' + str(res)[:200]}")
        return bool(res.get("ok"))
    except Exception as e:
        print(f"Google Sheet push problem: {e}")
        return False


FIRST_RUN_TAB = "First run (11 Oct)"


def sheet_rows(env, tab):
    v = sheet_call(env, {"action": "read", "tab": tab}).get("values") or []
    if not v or not any(v[0]):
        return []
    hdr = v[0]
    return [dict(zip(hdr, r)) for r in v[1:] if any(str(x).strip() for x in r)]


def sync_sheet(env, dry=False):
    """The SHEET IS THE MASTER. Never replaces anything Francis typed:
    1. reads every tab, copies his edits into the local CSV (sheet wins);
    2. moves the 11 Oct test batch to its own tab with his edits (append there first, then delete from Leads);
    3. appends only rows the sheet doesn't have yet."""
    if sheet_call(env, {"action": "version"}).get("version") != 2:
        sys.exit("Sheet script is not v2; update it first (SHEETS SETUP.md). Nothing was changed.")
    rows = read_tracker()
    leads, first = sheet_rows(env, "Leads"), sheet_rows(env, FIRST_RUN_TAB)
    local = {r["Key"].lower(): r for r in rows if r.get("Key")}
    pulled = 0
    for sr in leads + first:
        k = str(sr.get("Key", "")).lower()
        if not k:
            continue                      # a row he added by hand: left alone in the sheet
        if k in local:
            changed = {c: sr[c] for c in COLUMNS if c in sr and str(sr[c]) != str(local[k].get(c, ""))}
            if changed:
                local[k].update(changed); pulled += 1
        else:
            r = {c: sr.get(c, "") for c in COLUMNS}; rows.append(r); local[k] = r; pulled += 1
    is_first = lambda r: "Test batch" in r.get("Notes", "")
    have_first = {str(r.get("Key", "")).lower() for r in first}
    have_leads = {str(r.get("Key", "")).lower() for r in leads}
    to_first = [r for r in rows if is_first(r) and r["Key"].lower() not in have_first]
    leave_leads = [r["Key"] for r in rows if is_first(r) and r["Key"].lower() in have_leads]
    to_leads = [r for r in rows if not is_first(r) and r["Key"].lower() not in have_leads]
    print(f"Sheet edits pulled into CSV: {pulled} rows | to '{FIRST_RUN_TAB}': {len(to_first)} | "
          f"remove from Leads after copy: {len(leave_leads)} | new to Leads: {len(to_leads)}")
    if dry:
        return
    write_tracker(rows)
    if to_first and not push_sheet(env, to_first, "append", FIRST_RUN_TAB):
        sys.exit("Copy to the first-run tab failed; nothing deleted from Leads.")
    if leave_leads:
        print("Removed from Leads:", sheet_call(env, {"action": "delete", "tab": "Leads", "keys": leave_leads}))
    if to_leads:
        push_sheet(env, to_leads, "append", "Leads")


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="US", choices=["US", "AU"])
    ap.add_argument("--niche", action="append", help="repeatable; default = a broad product mix")
    ap.add_argument("--sources", default="ig,tiktok,etsy,ebay,web", help="social first: independent sellers need us most")
    ap.add_argument("--count", type=int, default=40, help="qualified leads to collect")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--max-searches", type=int, default=200, help="Serper credit cap for this run")
    ap.add_argument("--dry-run", action="store_true", help="don't write to the tracker or sheet")
    ap.add_argument("--sync", action="store_true", help="replace the Google Sheet's Leads tab with the tracker CSV")
    a = ap.parse_args()

    env = load_env()
    if a.sync:
        sync_sheet(env, dry=a.dry_run)
        return
    if not env.get("SERPER_API_KEY") or not env.get("GEMINI_API_KEY"):
        sys.exit("Missing SERPER_API_KEY / GEMINI_API_KEY in lead-finder\\.env")

    niches = a.niche or DEFAULT_NICHES
    sources = [s.strip() for s in a.sources.split(",") if s.strip() in QUERIES]
    tracker = read_tracker()
    exclude = read_lines(SEEN_FILE) | read_lines(DNC_FILE) | {r.get("Key", "").lower() for r in tracker} \
              | {root_domain(r["Website"]) for r in tracker if r.get("Website", "").startswith("http")}
    if env.get("SHEETS_WEBHOOK_URL"):   # the sheet's Do Not Contact tab is honoured too
        try:
            res = sheet_call(env, {"action": "dnc"})
            exclude |= {str(v).strip().lower().lstrip("@") for v in res.get("values", []) if str(v).strip()}
            exclude |= {"ig:" + x for x in list(exclude)} | {"tt:" + x for x in list(exclude)}
        except Exception as e:
            print(f"(couldn't read the sheet's Do Not Contact tab: {e})")
    today = dt.date.today().isoformat()
    budget = Budget(a.max_searches)
    leads, checked, rejected = [], 0, {}

    print(f"Lead Finder v3: {a.country} | sources {','.join(sources)} | {len(niches)} niches | target {a.count}")
    gen = discover(niches, sources, a.country, env["SERPER_API_KEY"], budget, exclude)
    with ThreadPoolExecutor(max_workers=8) as pool, \
            open(os.devnull if a.dry_run else SEEN_FILE, "a", encoding="utf-8") as seen_f:
        while len(leads) < a.count:
            batch = [x for _, x in zip(range(8), gen)]
            if not batch:
                break
            for c, (L, why) in zip(batch, pool.map(lambda c: qualify(c, a, env, budget), batch)):
                checked += 1
                seen_f.write(c["key"] + "\n")
                if L and len(leads) < a.count:
                    leads.append(L)
                    print(f"  + [{L['source']:<6}] {L['name'][:34]:<34} score {L['score']}  {L['signals'][:60]}")
                elif not L:
                    k = c["source"] + ": " + re.sub(r"[\d,]+", "N", why)
                    rejected[k] = rejected.get(k, 0) + 1
            seen_f.flush()

    print(f"\nQualified {len(leads)} of {checked} checked. Serper searches used: {budget.used}. Rejected: {rejected}")
    if not leads:
        return
    print("Writing angles + openers with Gemini (5 per call)...")
    personalise(leads, a.country, env["GEMINI_API_KEY"])
    big = [L["name"] for L in leads if (L.get("ai") or {}).get("established_brand") is True]
    if big:
        print(f"Dropped {len(big)} established brands (gatekeepers + existing agencies): {', '.join(big)}")
    notsell = [L["name"] for L in leads if (L.get("ai") or {}).get("not_a_seller") is True]
    if notsell:
        print(f"Dropped {len(notsell)} non-sellers: {', '.join(notsell)}")
    leads = [L for L in leads if (L.get("ai") or {}).get("established_brand") is not True
             and (L.get("ai") or {}).get("not_a_seller") is not True]

    rows = [to_row(L, today) for L in leads]
    OUT_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    with open(OUT_DIR / f"leads_{a.country}_{stamp}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    if a.dry_run:
        print(f"Dry run: tracker untouched. See output/leads_{a.country}_{stamp}.csv")
        return
    try:
        write_tracker(read_tracker() + rows)  # re-read: another run may have saved meanwhile
        print(f"Added {len(rows)} rows to {TRACKER.name}")
    except PermissionError:
        print("Tracker CSV is open in another program; rows saved in output/ only.")
    push_sheet(env, rows, "append")


if __name__ == "__main__":
    main()
