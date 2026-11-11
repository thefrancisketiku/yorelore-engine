"""
Yorelore App Finder: apps, startups and software (health -> relationships -> money first), for AI UGC outreach.

Sources (all free or Serper-cheap; nothing logs in to any platform):
  play     Google Play search (developer email is public on every listing)          free
  ios      Apple App Store: iTunes Search API + category top charts                 free
  ph       Product Hunt category feeds (new launches + the hunter/maker name)        free
  hn       Hacker News "Show HN" launches (official Algolia HN API)                 free
  yc       Y Combinator company pages (website, founders, their LinkedIn/X)        1 Serper search per keyword
  launch   BetaList, Uneed, Microlaunch, There's An AI For That listings           1 Serper search per keyword
  funding  Google News: "raises seed" etc. in the last month                       1 Serper search per keyword
  x        X #buildinpublic founders talking about their app                       1 Serper search per keyword
Then every app's website is read for public email, socials and an ad pixel; Gemini writes the angle,
a sample-ad hook, a DM and a ready email (permission-first: we never claim a sample already exists).

Separate mode, --intent: finds fresh public posts where someone is LOOKING for UGC for an app
(LinkedIn, X, Threads, Reddit, Upwork) and drafts a reply -> sheet tab "UGC Requests".

Usage:
  python app_finder.py --count 40
  python app_finder.py --count 30 --niche health --sources play,ios,ph
  python app_finder.py --intent
Rows go to "Yorelore SaaS client leads.csv" + the sheet's "SaaS Client" tab (append only; the sheet is the master).
"""
import argparse, csv, datetime as dt, html, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle
from urllib.parse import quote_plus, urlparse

import requests
import lead_finder as lf
from copy_templates import email_one, variant_for, HOOKS_PROMPT_RULES

try:
    from google_play_scraper import search as gp_search, app as gp_app
except ImportError:
    gp_search = gp_app = None

sys.stdout.reconfigure(errors="replace", line_buffering=True)

TAB, INTENT_TAB = "SaaS Client", "UGC Requests"
TRACKER = lf.HERE.parent / "Yorelore SaaS client leads.csv"
INTENT_CSV = lf.HERE.parent / "Yorelore UGC requests.csv"
SEEN = lf.HERE / "seen_apps.txt"
TARGET = {"US", "GB", "CA", "AU", "IE", "NZ"}
GL = {"US": "us", "GB": "gb", "UK": "gb", "CA": "ca", "AU": "au"}
UA = lf.UA

# store-search keywords (Play / App Store)
STORE_KW = {
    "health": ["calorie counter", "workout planner", "meditation", "sleep tracker", "habit tracker", "intermittent fasting",
               "period tracker", "mental health", "step counter", "yoga", "weight loss", "therapy", "nutrition coach"],
    "relationships": ["dating", "couples app", "relationship coach", "date ideas", "love language",
                      "long distance relationship", "friendship app", "dating coach", "marriage", "breakup recovery"],
    "money": ["budget", "savings goals", "expense tracker", "investing for beginners", "credit score", "debt payoff",
              "side hustle", "invoice maker", "bill reminder", "personal finance", "net worth tracker"],
    "saas": ["AI video editor", "social media scheduler", "AI writing assistant", "small business CRM", "creator tools"],
}
# web/news/social keywords (YC, launch sites, funding, X)
WEB_KW = {
    "health": ["health app", "fitness app", "mental health app", "nutrition app", "sleep app", "women's health app"],
    "relationships": ["dating app", "couples app", "relationship app", "social app"],
    "money": ["fintech app", "budgeting app", "personal finance app", "investing app", "savings app"],
    "saas": ["AI app", "SaaS", "creator tool"],
}
PH_FEEDS = {"health": ["health-fitness"], "relationships": ["dating"], "money": ["fintech", "personal-finance"],
            "saas": ["artificial-intelligence", "productivity"]}
IOS_GENRES = {"health": [6013, 6020], "relationships": [6005, 6012], "money": [6015], "saas": [6007, 6000]}
LAUNCH_SITES = ["betalist.com/startups", "uneed.best/tool", "microlaunch.net/p", "theresanaiforthat.com/ai"]

PLAY_MIN, PLAY_MAX = 1_000, 5_000_000          # installs window: alive but not a giant
IOS_MIN, IOS_MAX = 10, 100_000                 # US rating-count window
STALE_DAYS = 150

COLUMNS = ["Date added", "Source", "App", "Company", "Niche", "One-liner", "Country", "Website", "Play Store",
           "App Store", "Size signal", "Last updated", "Founder / owner", "Founder X", "Founder LinkedIn", "Email",
           "IG handle", "TikTok handle", "X handle", "LinkedIn page", "Running ads? (pixel)", "Audience", "Ad Library check",
           "Qualify score (0-6)", "Best channel", "Angle", "Hook idea (for the sample)", "Opener line",
           "DM (ready to send)", "Email subject", "Email body (ready to send)", "Variant", "Compliance flag", "Sequence step",
           "Next touch", "Date touch 1", "Last touch", "Replied? (Y/N)", "Status", "Proof", "Notes", "Key"]
INTENT_COLUMNS = ["Date found", "Platform", "Post URL", "Posted", "Author", "What they want", "App / company",
                  "App or software?", "Suggested reply", "Status", "Key"]

X_RE = re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,15})(?=[\"'/?\s])", re.I)
LI_RE = re.compile(r"linkedin\.com/(company|in)/([A-Za-z0-9_\-%.]{2,80})", re.I)
X_JUNK = {"share", "intent", "home", "search", "hashtag", "i", "privacy", "tos", "x", "twitter", "explore", "login"}
NOT_APP_SITE = re.compile(r"(producthunt|ycombinator|betalist|uneed|microlaunch|theresanaiforthat|crunchbase|"
                          r"techcrunch|apps\.apple|play\.google|linkedin|twitter|x\.com|facebook|instagram|youtube|"
                          r"medium|substack|github\.com|reddit|news\.ycombinator|wikipedia|bloomberg|forbes|"
                          r"businesswire|prnewswire|globenewswire|finsmes|eu-startups|tech\.eu|sifted|axios)", re.I)


def mix(table, niches):
    """Keywords round-robin across niches, so a run isn't all health."""
    lists = [[(kw, n) for kw in table[n]] for n in niches]
    return [x for row in __import__("itertools").zip_longest(*lists) for x in row if x]


BAD_LOCAL = re.compile(r"^(cancel|unsubscribe|billing|invoices?|accounts?payable|legal|privacy|dpo|gdpr|compliance|security|abuse|careers?|jobs|hr|recruiting|noreply|no-reply|donotreply|returns|refunds|orders|carlsagan|example|test)\b", re.I)


def now():
    return dt.datetime.now(dt.timezone.utc)


def jget(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=25, **kw)
        return r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def serper(path, q, key, budget, gl="us", tbs=None, num=10):
    """Serper web (/search) or news (/news). Failed calls are refunded."""
    if not budget.take():
        return []
    body = {"q": q, "gl": gl, "num": num}
    if tbs:
        body["tbs"] = tbs
    for attempt in range(3):
        try:
            r = requests.post(f"https://google.serper.dev/{path}", timeout=30,
                              headers={"X-API-KEY": key, "Content-Type": "application/json"}, json=body)
            if r.status_code == 200:
                d = r.json()
                return d.get("organic") or d.get("news") or []
        except Exception:
            pass
        time.sleep(4 * (attempt + 1))
    budget.refund()
    return []


def blank(source, key, name, **kw):
    c = {"source": source, "key": key, "name": name, "tagline": "", "desc": "", "domain": "", "play_url": "",
         "ios_url": "", "installs": None, "ratings": None, "updated_days": None, "launched_days": None,
         "email": "", "email_src": "", "founders": [], "x": "", "linkedin": "", "ig": "", "tiktok": "",
         "country": "", "address": "", "niche_hint": "", "signals": [], "funding": "", "snippet": ""}
    c.update(kw)
    return c


def days_since(ts):
    try:
        if isinstance(ts, (int, float)):
            t = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        else:
            t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        return (now() - t).days
    except (ValueError, TypeError, OSError):
        return None


# ---------- sources (each yields candidates lazily) ----------
def src_play(niches, a, env, budget):
    if not gp_search:
        print("  (google-play-scraper not installed: pip install google-play-scraper; Play skipped)")
        return
    gl = GL[a.country]
    for kw, niche in mix(STORE_KW, niches):
        if True:
            try:
                hits = gp_search(kw, lang="en", country=gl, n_hits=30)
            except Exception:
                continue
            for h in hits:
                if h.get("appId"):
                    yield blank("play", "play:" + h["appId"].lower(), h.get("title", ""), niche_hint=niche,
                                app_id=h["appId"])


def play_details(c, gl):
    try:
        d = gp_app(c["app_id"], lang="en", country=gl)
    except Exception:
        return "play details failed"
    inst = d.get("realInstalls") or lf.num(str(d.get("installs", "")).rstrip("+")) or 0
    if not PLAY_MIN <= inst <= PLAY_MAX:
        return f"installs {inst}"
    upd = days_since(d.get("updated"))
    if upd is not None and upd > STALE_DAYS:
        return "not updated in 150 days"
    c.update(name=d.get("title") or c["name"], company=d.get("developer", ""), tagline=d.get("summary") or "",
             desc=(d.get("description") or "")[:1200], play_url=d.get("url") or f"https://play.google.com/store/apps/details?id={c['app_id']}",
             installs=inst, updated_days=upd, address=d.get("developerAddress") or "",
             email=(d.get("developerEmail") or "").lower(), email_src="Google Play listing (developer contact)")
    if d.get("developerWebsite"):
        c["domain"] = lf.root_domain(d["developerWebsite"])
    c["signals"].append(f"{inst:,} Play installs, rating {d.get('score') or 0:.1f} ({d.get('ratings') or 0:,})")
    return None


def src_ios(niches, a, env, budget):
    gl = GL[a.country]
    for niche in niches:
        ids = []
        for g in IOS_GENRES[niche]:   # category top-200 charts, then size-filter
            d = jget(f"https://itunes.apple.com/{gl}/rss/topfreeapplications/limit=200/genre={g}/json") or {}
            ids += [e["id"]["attributes"]["im:id"] for e in d.get("feed", {}).get("entry", []) if "id" in e]
        apps = []
        for i in range(0, len(ids), 150):
            apps += (jget(f"https://itunes.apple.com/lookup?id={','.join(ids[i:i+150])}&country={gl}") or {}).get("results", [])
        for r in apps:
            if r.get("trackId"):
                yield ios_candidate(r, niche)
    for kw, niche in mix(STORE_KW, niches):
        if True:
            for r in (jget(f"https://itunes.apple.com/search?term={quote_plus(kw)}&entity=software&country={gl}&limit=50") or {}).get("results", []):
                if r.get("trackId"):
                    yield ios_candidate(r, niche)


def ios_candidate(r, niche):
    c = blank("ios", f"ios:{r['trackId']}", r.get("trackName", ""), niche_hint=niche, company=r.get("sellerName", ""),
              desc=(r.get("description") or "")[:1200], ios_url=(r.get("trackViewUrl") or "").split("?")[0],
              ratings=r.get("userRatingCount") or 0, updated_days=days_since(r.get("currentVersionReleaseDate")),
              launched_days=days_since(r.get("releaseDate")), genre=r.get("primaryGenreName", ""))
    if r.get("sellerUrl"):
        c["domain"] = lf.root_domain(r["sellerUrl"])
    c["signals"].append(f"{c['ratings']:,} App Store ratings ({r.get('averageUserRating') or 0:.1f})")
    return c


def src_ph(niches, a, env, budget):
    for feed, niche in mix(PH_FEEDS, niches):
        if True:
            try:
                x = requests.get(f"https://www.producthunt.com/feed?category={feed}", headers=UA, timeout=25).text
            except requests.RequestException:
                continue
            for e in x.split("<entry>")[1:]:
                g = lambda rx: html.unescape((re.search(rx, e, re.S) or [None, ""])[1]).strip()
                url, name = g(r'<link rel="alternate" type="text/html" href="([^"]+)"'), g(r"<title>(.*?)</title>")
                tag = lf.html_to_text(html.unescape(g(r"<content[^>]*>(.*?)</content>")), 300).split(" Discussion")[0]
                slug = url.rstrip("/").split("/")[-1]
                if not slug:
                    continue
                c = blank("ph", "ph:" + slug.lower(), name, niche_hint=niche, tagline=tag, ph_url=url,
                          launched_days=days_since(g(r"<published>(.*?)</published>")))
                maker = g(r"<author>\s*<name>(.*?)</name>")
                if maker:
                    c["founders"].append({"name": maker, "role": "Product Hunt hunter/maker"})
                c["signals"].append(f"launched on Product Hunt {c['launched_days']}d ago")
                yield c


def src_hn(niches, a, env, budget):
    since = int(time.time()) - 90 * 86400
    for kw, niche in mix(WEB_KW, niches):
        if True:
            d = jget("https://hn.algolia.com/api/v1/search_by_date", params={
                "query": kw.replace(" app", ""), "tags": "show_hn", "hitsPerPage": 40,
                "numericFilters": f"created_at_i>{since}"}) or {}
            for h in d.get("hits", []):
                url = h.get("url") or ""
                if not url or NOT_APP_SITE.search(url):
                    continue
                title = re.sub(r"^Show HN:\s*", "", h.get("title") or "")
                name, _, tag = re.split(r"\s+[-–—:]\s+", title, maxsplit=1)[0], "", title
                c = blank("hn", "hn:" + lf.root_domain(url), name, niche_hint=niche, tagline=tag,
                          domain=lf.root_domain(url), launched_days=days_since(h.get("created_at")))
                c["founders"].append({"name": h.get("author", ""), "role": "Show HN poster (HN username)"})
                c["signals"].append(f"Show HN {c['launched_days']}d ago, {h.get('points') or 0} points")
                yield c


def src_yc(niches, a, env, budget):
    for kw, niche in mix(WEB_KW, niches):
        if True:
            for o in serper("search", f'site:ycombinator.com/companies "{kw}"', env["SERPER_API_KEY"], budget):
                m = re.match(r"https://www\.ycombinator\.com/companies/([a-z0-9\-]+)/?$", o.get("link", ""))
                if m and m.group(1) not in ("industry", "location", "batch", "founders"):
                    yield blank("yc", "yc:" + m.group(1), o.get("title", "").split(":")[0].strip(), niche_hint=niche,
                                yc_slug=m.group(1), snippet=o.get("snippet", ""))


def yc_details(c):
    try:
        h = requests.get(f"https://www.ycombinator.com/companies/{c['yc_slug']}", headers=UA, timeout=25).text
        co = json.loads(html.unescape(re.search(r'data-page="([^"]+)"', h).group(1)))["props"]["company"]
    except Exception:
        return "YC page unreadable"
    if (co.get("team_size") or 0) > 60:
        return f"team {co.get('team_size')}"
    if (co.get("ycdc_status") or "Active") not in ("Active", None):
        return f"YC status {co.get('ycdc_status')}"
    c.update(name=co.get("name") or c["name"], tagline=co.get("one_liner") or "", desc=(co.get("long_description") or "")[:1000],
             domain=lf.root_domain(co["website"]) if co.get("website") else "", country=(co.get("country") or "").upper(),
             linkedin=co.get("linkedin_url") or "", x=(X_RE.search((co.get("twitter_url") or "") + "/") or [None, ""])[1])
    c["founders"] = [{"name": f.get("full_name", ""), "role": f.get("title") or "Founder",
                      "linkedin": f.get("linkedin_url") or "", "x": f.get("twitter_url") or ""} for f in co.get("founders") or []]
    c["signals"].append(f"YC {co.get('batch_name','')}, team {co.get('team_size') or '?'}, {co.get('location','')}")
    c["funding"] = f"YC {co.get('batch_name','')}"
    return None


def src_launch(niches, a, env, budget):
    for kw, niche in mix(WEB_KW, niches):
        if True:
            for site in LAUNCH_SITES:
                for o in serper("search", f'site:{site} "{kw}"', env["SERPER_API_KEY"], budget, tbs="qdr:y"):
                    link = o.get("link", "")
                    if site.split("/")[0] not in link or link.rstrip("/").endswith(site.split("/")[0]):
                        continue
                    name = re.split(r"\s+[-–—|:]\s+", o.get("title", ""))[0].strip()
                    yield blank("launch", "launch:" + link.rstrip("/").split("/")[-1].lower(), name, niche_hint=niche,
                                tagline=o.get("snippet", ""), listing=link, snippet=o.get("snippet", ""))


def src_funding(niches, a, env, budget):
    for kw, niche in mix(WEB_KW, niches):
        if True:
            for q in (f'"{kw}" raises seed', f'"{kw}" raises pre-seed OR "Series A"'):
                for o in serper("news", q, env["SERPER_API_KEY"], budget, tbs="qdr:m"):
                    m = re.match(r"(.{2,40}?)\s+(?:raises|secures|lands|closes|bags|nabs)\s", o.get("title", ""), re.I)
                    if not m:
                        continue
                    name = re.sub(r"^(?:[A-Z][\w-]+'s\s+|(?:[A-Z][\w-]+ )?(?:startup|app|platform|fintech|healthtech)\s+)", "",
                                  m.group(1)).strip(" '\"")
                    if len(name.split()) > 5:
                        continue
                    c = blank("funding", "fund:" + re.sub(r"\W+", "", name.lower()), name, niche_hint=niche,
                              tagline=o.get("snippet", ""), snippet=o.get("title", ""), funding=o.get("title", "")[:140],
                              news_domain=lf.root_domain(o.get("link", "")))
                    c["signals"].append(f"in the news: {o.get('title','')[:90]} ({o.get('date','')})")
                    yield c


def src_x(niches, a, env, budget):
    for kw, niche in mix(WEB_KW, niches):
        if True:
            q = f'site:x.com "{kw}" ("launched" OR "MRR" OR "downloads" OR "users") ("my app" OR "our app" OR "#buildinpublic")'
            for o in serper("search", q, env["SERPER_API_KEY"], budget, tbs="qdr:m"):
                m = re.match(r"https://(?:x|twitter)\.com/([A-Za-z0-9_]{2,15})/status/", o.get("link", ""))
                if not m or m.group(1).lower() in X_JUNK:
                    continue
                text = f"{o.get('title','')} {o.get('snippet','')}"
                d = next((d.lower() for d in re.findall(r"\b([a-z0-9-]{2,30}\.(?:app|com|io|ai|co|health|money|so))\b", text, re.I)
                          if not NOT_APP_SITE.search(d)), "")
                if not d:
                    continue
                c = blank("x", "x:" + m.group(1).lower(), d.split(".")[0].capitalize(), niche_hint=niche, x=m.group(1),
                          domain=d, snippet=text[:500])
                c["founders"].append({"name": "", "role": "posts on X", "x": f"https://x.com/{m.group(1)}"})
                yield c


# --yc-bulk (approved by Francis 11 Nov 2026 via /permissions): the YC directory's own public search index
YC_SUB = {"health": r"consumer health|wellness|fitness|mental|nutrition|women", "relationships": r"dating|social|family|relationship",
          "money": r"consumer finance|credit and lending|personal finance|asset management", "saas": r"productivity|marketing|creator|small business"}
YC_REGIONS = ["United States of America", "United Kingdom", "Canada", "Australia"]


def yc_index():
    h = requests.get("https://www.ycombinator.com/companies", headers=UA, timeout=25).text
    o = json.loads(re.search(r"window\.AlgoliaOpts\s*=\s*(\{[^}]*\})", h).group(1))
    url = f"https://{o['app'].lower()}-dsn.algolia.net/1/indexes/*/queries"
    hdr = {"x-algolia-application-id": o["app"], "x-algolia-api-key": o["key"]}

    def q(params):
        for attempt in range(5):          # survive short internet / DNS drops
            try:
                r = requests.post(url, headers=hdr, json={"requests": [{"indexName": "YCCompany_production", "params": params}]}, timeout=30)
                return r.json()["results"][0]
            except (requests.RequestException, ValueError, KeyError):
                time.sleep(20 * (attempt + 1))
        return {}
    return q


def src_ycbulk(niches, a, env, budget):
    try:
        q = yc_index()
        facets = q("query=&hitsPerPage=0&facets=[\"subindustry\",\"industries\"]&maxValuesPerFacet=300").get("facets", {})
    except Exception as e:
        print(f"  (YC bulk unavailable: {e})"); return
    subs = list((facets.get("subindustry") or {}).keys())
    regions = json.dumps([f"regions:{r}" for r in YC_REGIONS])
    for niche in niches:
        pick = [x for x in subs if re.search(YC_SUB[niche], x, re.I)]
        print(f"  YC {niche}: {len(pick)} subindustries -> {', '.join(pick[:8])}{' ...' if len(pick) > 8 else ''}")
        if not pick:
            continue
        ff = json.dumps([[f"subindustry:{x}" for x in pick], json.loads(regions)])
        page = 0
        while True:
            res = q(f"query=&hitsPerPage=200&page={page}&facetFilters={quote_plus(ff)}"
                    f"&numericFilters={quote_plus(json.dumps(['team_size<=60']))}&filters={quote_plus('status:Active')}")
            for h in res.get("hits", []):
                if h.get("slug"):
                    yield blank("yc", "yc:" + h["slug"], h.get("name", ""), niche_hint=niche, yc_slug=h["slug"],
                                snippet=h.get("one_liner", ""))
            page += 1
            if page >= res.get("nbPages", 0):
                break


SOURCES = {"play": src_play, "ios": src_ios, "ph": src_ph, "hn": src_hn, "yc": src_yc, "launch": src_launch,
           "funding": src_funding, "x": src_x, "ycbulk": src_ycbulk}


# ---------- resolve + enrich ----------
def resolve_site(c, env, budget, gl):
    """Find the app's own website: free App Store name match first, then one Serper search."""
    name = c["name"].strip()
    if not name and c["source"] == "x":
        return
    if name:
        for r in (jget(f"https://itunes.apple.com/search?term={quote_plus(name)}&entity=software&country={gl}&limit=5") or {}).get("results", []):
            if re.sub(r"\W", "", r.get("trackName", "").lower()).startswith(re.sub(r"\W", "", name.lower())[:12]) and r.get("sellerUrl"):
                c["domain"] = lf.root_domain(r["sellerUrl"])
                c["ios_url"] = c["ios_url"] or (r.get("trackViewUrl") or "").split("?")[0]
                c["ratings"] = c["ratings"] or r.get("userRatingCount")
                return
    q = f'"{name}" app' if name else c["snippet"][:80]
    for o in serper("search", q, env["SERPER_API_KEY"], budget, gl=gl):
        link = o.get("link", "")
        d = lf.root_domain(link) if link else ""
        stem = re.sub(r"[^a-z0-9]", "", name.lower())[:6]
        if d and not NOT_APP_SITE.search(link) and not lf.SKIP_DOMAINS.search(d) and d != c.get("news_domain")                 and (not stem or stem[:4] in re.sub(r"[^a-z0-9]", "", d)):
            c["domain"] = d
            return


def home_links(domain):
    r = lf.get(f"https://{domain}", timeout=12)
    if not r:
        return "", "", ""
    x = next((m for m in X_RE.findall(r.text) if m.lower() not in X_JUNK), "")
    li = next((f"https://www.linkedin.com/{t}/{s}" for t, s in LI_RE.findall(r.text) if t == "company"), "")
    if not li:
        li = next((f"https://www.linkedin.com/{t}/{s}" for t, s in LI_RE.findall(r.text)), "")
    return x, li, r.text


def qualify(c, a, env, budget, exclude):
    gl = GL[a.country]
    if c["source"] == "play":
        why = play_details(c, gl)
        if why:
            return None, why
    if c["source"] == "ios":
        if not IOS_MIN <= (c["ratings"] or 0) <= IOS_MAX:
            return None, "ratings outside window"
        if c["updated_days"] is not None and c["updated_days"] > STALE_DAYS:
            return None, "not updated in 150 days"
    if c["source"] == "yc":
        why = yc_details(c)
        if why:
            return None, why
    if not c["domain"]:
        resolve_site(c, env, budget, gl)
    if c["domain"]:
        if lf.SKIP_DOMAINS.search(c["domain"]) and not c["domain"].endswith(".app"):
            c["domain"] = ""
        elif "app:" + c["domain"] in exclude:
            return None, "already in tracker"
    site = lf.read_site(c["domain"]) if c["domain"] else None
    c["site"] = site or {}
    if site:
        if not c["email"]:
            e, s = lf.best_email(site["emails"], c["domain"])
            c["email"], c["email_src"] = e, s
        c["ig"] = site["socials"].get("instagram", "")
        c["tiktok"] = site["socials"].get("tiktok", "")
        x, li, _ = home_links(c["domain"])
        c["x"] = c["x"] or x
        c["linkedin"] = c["linkedin"] or li
        c["pixel"] = bool(site.get("meta_pixel") or site.get("tiktok_pixel"))
    else:
        c["pixel"] = False
    if c["email"] and (BAD_LOCAL.match(c["email"]) or not lf.has_mx(c["email"])):
        c["email"], c["email_src"] = "", ""
    if not (c["email"] or c["x"] or c["linkedin"] or c["ig"] or c["tiktok"] or any(f.get("linkedin") or f.get("x") for f in c["founders"])):
        return None, "no contact route"
    return c, "ok"


# ---------- personalise ----------
PROMPT = """You prepare cold outreach for Francis, founder of Yorelore, a creative studio that makes hyper-realistic
AI UGC video ads for apps: a realistic AI creator talks to camera while the app's real screens show the feature.
Benefits for the app: test 10+ new hooks a week, no creator fees, no shoots, first batch in 48 hours.
Every AI creator is labelled AI-generated. Targets: apps/startups in health, relationships, money (and SMB SaaS)
in the US, UK, Canada, Australia.

Below is REAL scraped data for {n} apps. Use ONLY this data. Never invent users, numbers, reviews, funding, ads or
posts, and never claim Francis already made a sample, used the app or saw their ads.

{leads}

Return a JSON list, one object per app, same order, each with:
"id",
"app_name" (clean name as users know it), "company" (legal/brand company if in data, else ""),
"one_liner" (max 15 words: what it does, for whom),
"niche" (one of "health", "relationships", "money", "saas", "other"),
"audience" ("consumer" if everyday people download/use it, "business" if it is sold to companies or professionals),
"not_software" (true if this is not an app/software product at all: an agency, article, directory, game studio portfolio, physical product),
"established_brand" (true for big companies: household names, public companies, big-tech owned, banks, >5M users in data; false for startups and indie apps),
"country" (ISO-2 of where the company is based, from address/phones/YC data/site text/TLD; "?" if not stated anywhere),
"founder_name" (a person named as founder/CEO/owner/maker in the data, else ""),
"best_channel" (one of "Email", "X DM", "LinkedIn", "IG DM", "TikTok DM": the most direct route that exists in the data),
"angle" (one line: why creator-style ads fit THIS app, from the data, e.g. "new launch, no video ads yet", "pixel installed = paid acquisition"),
"hook_idea" (one line, the sample ad concept: what the AI creator says in the first 2 seconds + which app screen/feature shows; demo or skit only, NEVER a personal result claim like weight lost, money made, partner found),
"opener_line" (one specific true observation about the app, max 20 words, no flattery),
"dm" (under 60 words, starts "Hey [first name]," if founder known else "Hey there,". Structure: opener line, then
  "I make UGC-style ads for apps with realistic AI creators, so you can test 10 new hooks a week without paying creators per video.",
  then "I'd like to make [App] a free 15-sec sample from your app screens. Cool if I do?"),
""" + HOOKS_PROMPT_RULES + """,
"compliance_flag" ("" or a short warning if the niche needs care: medical/weight claims, financial returns, sexual content)."""


def brief(i, c):
    s = c.get("site") or {}
    p = [f"id: {i}", f"source: {c['source']}", f"name: {c['name']}", f"keyword niche: {c['niche_hint']}"]
    for k, lab in (("company", "developer/company"), ("tagline", "tagline"), ("genre", "store genre"), ("address", "address"),
                   ("country", "country (from YC)"), ("funding", "funding/news"), ("snippet", "search snippet")):
        if c.get(k):
            p.append(f"{lab}: {str(c[k])[:400]}")
    if c["founders"]:
        p.append("people: " + "; ".join(f"{f.get('name','')} ({f.get('role','')})" for f in c["founders"][:4]))
    if c["domain"]: p.append(f"website: {c['domain']}")
    if c["signals"]: p.append("signals: " + "; ".join(c["signals"]))
    if c["desc"]: p.append(f"store/company description: {c['desc'][:900]}")
    if s.get("home_text"): p.append(f"homepage text: {s['home_text'][:900]}")
    if s.get("about_text"): p.append(f"about text: {s['about_text'][:600]}")
    return "\n".join(p)


def personalise(leads, key):
    for start in range(0, len(leads), 5):
        chunk = leads[start:start + 5]
        out = lf.gemini(PROMPT.format(n=len(chunk), leads="\n\n---\n".join(brief(i, c) for i, c in enumerate(chunk))), key) or []
        if isinstance(out, dict):
            out = next((v for v in out.values() if isinstance(v, list)), [])
        by_id = {str(o.get("id")): o for o in out if isinstance(o, dict)}
        for i, c in enumerate(chunk):
            c["ai"] = by_id.get(str(i), {})
        print(f"  writing {min(start + 5, len(leads))}/{len(leads)}: {sum(bool(c['ai']) for c in chunk)}/{len(chunk)} ok")
        time.sleep(2)


def score(c):
    ai = c.get("ai") or {}
    country = c["country"] or ai.get("country", "?")
    country = "GB" if country == "UK" else country
    c["country_final"] = country
    founder = ai.get("founder_name") or next((f["name"] for f in c["founders"] if f.get("name") and "HN" not in f.get("role", "")), "")
    c["founder"] = founder
    size = ((c["installs"] or 0) >= PLAY_MIN or IOS_MIN <= (c["ratings"] or 0) <= IOS_MAX
            or c["source"] in ("yc", "funding") or (c["source"] in ("ph", "hn") and (c["launched_days"] or 999) <= 120))
    active = (c["updated_days"] is not None and c["updated_days"] <= 60) or (c["launched_days"] is not None and c["launched_days"] <= 120) \
             or c["source"] in ("funding", "x")
    checks = {"target country": country in TARGET, "active": active, "size fit": size,
              "ad/funding signal": bool(c.get("pixel") or c["funding"]), "email": bool(c["email"]), "founder named": bool(founder)}
    c["score"] = sum(checks.values())
    c["missing"] = [k for k, v in checks.items() if not v]


def channel(c, ai):
    have = {"Email": bool(c["email"]), "X DM": bool(c["x"] or any(f.get("x") for f in c["founders"])),
            "LinkedIn": bool(c["linkedin"] or any(f.get("linkedin") for f in c["founders"])),
            "IG DM": bool(c["ig"]), "TikTok DM": bool(c["tiktok"])}
    pick = ai.get("best_channel", "")
    return pick if have.get(pick) else next((k for k, v in have.items() if v), "")


def to_row(c, today):
    ai = c.get("ai") or {}
    f0 = next((f for f in c["founders"] if f.get("linkedin") or f.get("x")), {})
    name = ai.get("app_name") or c["name"]
    email1 = email_one(variant_for(c["key"]), name, c["founder"], ai.get("opener_line", ""), ai.get("feature", ""),
                       ai.get("hooks") if isinstance(ai.get("hooks"), list) else [], ai.get("niche") or c["niche_hint"])
    proof = "; ".join(x for x in (f"email: {c['email_src']}" if c["email"] else "",
                                  f"listing: {c.get('ph_url') or c.get('listing') or ''}" if c.get("ph_url") or c.get("listing") else "") if x)
    return {
        "Date added": today, "Source": {"play": "Google Play", "ios": "App Store", "ph": "Product Hunt", "hn": "Show HN",
                                        "yc": "YC directory", "launch": "Launch site", "funding": "Funding news", "x": "X"}[c["source"]],
        "App": name, "Company": ai.get("company") or c.get("company", ""), "Niche": ai.get("niche") or c["niche_hint"],
        "One-liner": ai.get("one_liner") or c["tagline"][:120], "Country": c["country_final"],
        "Website": f"https://{c['domain']}" if c["domain"] else "", "Play Store": c["play_url"], "App Store": c["ios_url"],
        "Size signal": "; ".join(c["signals"]),
        "Last updated": f"{c['updated_days']}d ago" if c["updated_days"] is not None else "",
        "Founder / owner": c["founder"], "Founder X": f0.get("x", ""), "Founder LinkedIn": f0.get("linkedin", ""),
        "Email": c["email"], "IG handle": f"@{c['ig']}" if c["ig"] else "", "TikTok handle": f"@{c['tiktok']}" if c["tiktok"] else "",
        "X handle": f"@{c['x']}" if c["x"] else "", "LinkedIn page": c["linkedin"],
        "Running ads? (pixel)": "Y" if c.get("pixel") else ("?" if c.get("site") else "no site"),
        "Audience": ai.get("audience", ""), "Ad Library check": f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&q={quote_plus(name)}&search_type=keyword_unordered",
        "Qualify score (0-6)": c["score"], "Best channel": channel(c, ai), "Angle": ai.get("angle", ""),
        "Hook idea (for the sample)": ai.get("hook_idea", ""), "Opener line": ai.get("opener_line", ""),
        "DM (ready to send)": ai.get("dm", ""), "Email subject": email1[0] if c["email"] else "",
        "Email body (ready to send)": email1[1] if c["email"] else "", "Variant": email1[2] if c["email"] else "",
        "Compliance flag": ai.get("compliance_flag", ""), "Sequence step": 0, "Status": "New", "Proof": proof,
        "Notes": f"Missing: {', '.join(c['missing']) or 'none'}", "Key": c["key"],
    }


# ---------- tracker + sheet ----------
def read_csv(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_csv(path, cols, rows):
    old = read_csv(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in old + rows:
            w.writerow({c: html.unescape(str(r.get(c, ""))) for c in cols})


def push(env, rows, tab, cols):
    if not env.get("SHEETS_WEBHOOK_URL"):
        print("Sheet not connected: rows are in the CSV only."); return False
    try:
        res = lf.sheet_call(env, {"action": "append", "tab": tab, "header": cols,
                                  "rows": [[html.unescape(str(r.get(c, ""))) for c in cols] for r in rows]})
        print(f"Google Sheet append ({tab}): {'ok' if res.get('ok') else 'FAILED ' + str(res)[:200]}")
        return bool(res.get("ok"))
    except Exception as e:
        print(f"Google Sheet push problem: {e}"); return False


def sheet_keys(env, tab):
    try:
        v = lf.sheet_call(env, {"action": "read", "tab": tab}).get("values") or []
    except Exception:
        return set()
    if not v or "Key" not in v[0]:
        return set()
    k = v[0].index("Key")
    keys = {str(r[k]).lower() for r in v[1:] if len(r) > k and r[k]}
    if "Website" in v[0]:
        w = v[0].index("Website")
        keys |= {"app:" + lf.root_domain(r[w]) for r in v[1:] if len(r) > w and str(r[w]).startswith("http")}
    return keys


def load_exclude(env):
    ex = lf.read_lines(SEEN) | lf.read_lines(lf.DNC_FILE)
    for r in read_csv(TRACKER):
        ex.add(r.get("Key", "").lower())
        if r.get("Website", "").startswith("http"):
            ex.add("app:" + lf.root_domain(r["Website"]))
    if env.get("SHEETS_WEBHOOK_URL"):
        ex |= sheet_keys(env, TAB)
        try:
            dnc = lf.sheet_call(env, {"action": "dnc"}).get("values", [])
            for v in dnc:
                v = str(v).strip().lower().lstrip("@")
                if v:
                    ex |= {v, "app:" + lf.root_domain(v) if "." in v and "@" not in v else v}
        except Exception as e:
            print(f"(couldn't read the sheet's Do Not Contact tab: {e})")
    return ex


# ---------- intent mode ----------
INTENT_Q = [
    'site:linkedin.com/posts "looking for UGC creators" app', 'site:linkedin.com/posts "UGC" "for our app" hiring',
    'site:x.com "looking for UGC creators" app', 'site:x.com "need UGC" app', 'site:x.com "UGC creators" "our app"',
    'site:threads.net "UGC creators" app', 'site:reddit.com "UGC" "my app" (r/SaaS OR r/startups OR r/AppBusiness OR r/indiehackers)',
    'site:upwork.com/freelance-jobs UGC app video', 'site:linkedin.com/posts "AI UGC" app looking',
]
INTENT_PROMPT = """Francis runs Yorelore: hyper-realistic AI UGC video ads for apps (labelled as AI, demo-style, first batch in
48 hours). Below are public posts found on Google. For each, decide if the AUTHOR (or their company) is looking to BUY
UGC/video ads/creators right now. Use only the text given.

{posts}

Return a JSON list, one object per post, same order: "id", "is_request" (true only if they want to hire/buy UGC or
video ads; false for creators offering services, courses, news, opinions), "is_app" (true if the product is an app,
startup or software), "company" (if named, else ""), "what_they_want" (max 15 words), "reply" (under 60 words, plain,
from Francis: "I do exactly this for apps: realistic AI creators, clearly labelled, first batch in 48 hours, a fraction
of creator cost." + one line tailored to their post + "Happy to make one for [company/your app] free so you can judge it
on your own product." Never claim past clients; you may say "here's a sample: [SAMPLE LINK]")."""


def run_intent(env, a):
    budget = lf.Budget(a.max_searches)
    seen = {r.get("Key", "").lower() for r in read_csv(INTENT_CSV)} | (sheet_keys(env, INTENT_TAB) if env.get("SHEETS_WEBHOOK_URL") else set())
    posts = []
    for q in INTENT_Q:
        for o in serper("search", q, env["SERPER_API_KEY"], budget, tbs="qdr:w", num=10):
            link = o.get("link", "").split("?")[0]
            if link and link.lower() not in seen and link not in {p["link"] for p in posts}:
                posts.append({"link": link, "title": o.get("title", ""), "snippet": o.get("snippet", ""), "date": o.get("date", "")})
    print(f"Intent: {len(posts)} new posts from {budget.used} searches")
    keep = []
    for s in range(0, len(posts), 10):
        chunk = posts[s:s + 10]
        txt = "\n\n".join(f"id: {i}\nurl: {p['link']}\ntitle: {p['title']}\ntext: {p['snippet']}" for i, p in enumerate(chunk))
        out = lf.gemini(INTENT_PROMPT.format(posts=txt), env["GEMINI_API_KEY"]) or []
        if isinstance(out, dict):
            out = next((v for v in out.values() if isinstance(v, list)), [])
        for o in out:
            try:
                p = chunk[int(o.get("id"))]
            except (TypeError, ValueError, IndexError):
                continue
            if o.get("is_request"):
                host = urlparse(p["link"]).netloc.replace("www.", "")
                author = (re.search(r"(?:x\.com|threads\.net)/@?([A-Za-z0-9_.]+)", p["link"]) or
                          re.search(r"linkedin\.com/posts/([a-z0-9\-]+?)_", p["link"]) or [None, ""])[1]
                keep.append({"Date found": dt.date.today().isoformat(), "Platform": host, "Post URL": p["link"],
                             "Posted": p["date"], "Author": author, "What they want": o.get("what_they_want", ""),
                             "App / company": o.get("company", ""), "App or software?": "Y" if o.get("is_app") else "N",
                             "Suggested reply": o.get("reply", ""), "Status": "New", "Key": p["link"].lower()})
    keep.sort(key=lambda r: r["App or software?"] != "Y")
    print(f"Real UGC requests: {len(keep)} ({sum(r['App or software?'] == 'Y' for r in keep)} from apps/software)")
    for r in keep:
        print(f"  [{r['App or software?']}] {r['Platform']:<14} {r['What they want'][:70]}")
    if keep and not a.dry_run:
        append_csv(INTENT_CSV, INTENT_COLUMNS, keep)
        push(env, keep, INTENT_TAB, INTENT_COLUMNS)


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--niche", default="health,relationships,money", help="health,relationships,money,saas")
    ap.add_argument("--sources", default="play,ios,ph,yc,funding,launch,hn,x")
    ap.add_argument("--country", default="US", choices=["US", "GB", "UK", "CA", "AU"], help="store charts/search region")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--max-searches", type=int, default=120, help="Serper credit cap for this run")
    ap.add_argument("--yc-bulk", action="store_true", help="only the YC directory's full index (health/dating/fintech, US/UK/CA/AU)")
    ap.add_argument("--intent", action="store_true", help="find public 'looking for UGC' posts instead")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    env = lf.load_env()
    if not env.get("SERPER_API_KEY") or not env.get("GEMINI_API_KEY"):
        sys.exit("Missing SERPER_API_KEY / GEMINI_API_KEY in lead-finder\\.env.txt")
    if a.intent:
        return run_intent(env, a)

    niches = [n.strip() for n in a.niche.split(",") if n.strip() in STORE_KW]
    sources = ["ycbulk"] if a.yc_bulk else [s.strip() for s in a.sources.split(",") if s.strip() in SOURCES and s.strip() != "ycbulk"]
    budget = lf.Budget(a.max_searches)
    exclude = load_exclude(env)
    print(f"App Finder: {','.join(niches)} | sources {','.join(sources)} | target {a.count} | store region {a.country}")

    gens = cycle([(s, SOURCES[s](niches, a, env, budget)) for s in sources])
    live = set(sources)
    leads, checked, rejected, dupes = [], 0, {}, set()

    def next_batch(n=8):
        out = []
        while len(out) < n and live:
            s, g = next(gens)
            if s not in live:
                continue
            try:
                c = next(g)
            except StopIteration:
                live.discard(s); continue
            if c["key"] in exclude or c["key"] in dupes:
                continue
            dupes.add(c["key"]); out.append(c)
        return out

    drop = lambda c: (c.get("ai") or {}).get("established_brand") is True or (c.get("ai") or {}).get("not_software") is True \
                     or (c["country_final"] not in TARGET and c["country_final"] not in ("?", "")) \
                     or c["score"] < a.min_score or ((c.get("ai") or {}).get("niche") == "other" and "saas" not in niches)
    today = dt.date.today().isoformat()
    lf.OUT_DIR.mkdir(exist_ok=True)
    out = lf.OUT_DIR / f"apps_{dt.datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    saved, pending = [], []
    seen_f = open(lf.os.devnull if a.dry_run else SEEN, "a", encoding="utf-8")

    def flush():
        """Write + save every ~10 leads, so a run that is cut off never loses its work."""
        nonlocal pending
        if not pending:
            return
        batch, pending = pending, []
        print(f"Writing angles, hooks, DMs and emails for {len(batch)} leads (Gemini)...")
        personalise(batch, env["GEMINI_API_KEY"])
        for c in batch:
            score(c)
        gone = [c for c in batch if drop(c)]
        if gone:
            print(f"  dropped {len(gone)} (big brand / not software / outside US-UK-CA-AU / low score): "
                  + ", ".join(f"{(c.get('ai') or {}).get('app_name') or c['name']} [{c['country_final']},{c['score']}]" for c in gone))
        keep = sorted([c for c in batch if not drop(c)], key=lambda c: -c["score"])[:max(0, a.count - len(saved))]
        rows = [to_row(c, today) for c in keep]
        if rows:
            append_csv(out, COLUMNS, rows)
            if not a.dry_run:
                try:
                    append_csv(TRACKER, COLUMNS, rows)
                except PermissionError:
                    print("  SaaS client CSV is open in another program; rows saved in output/ only.")
                push(env, rows, TAB, COLUMNS)
        for c in batch:                      # kept candidates are marked seen only once saved
            seen_f.write(c["key"] + "\n" + (f"app:{c['domain']}\n" if c["domain"] else ""))
        seen_f.flush()
        saved.extend(rows)
        print(f"  saved {len(rows)} -> total {len(saved)}/{a.count}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        while len(saved) < a.count and live:
            batch = next_batch()
            if not batch:
                break
            futs = [pool.submit(qualify, c, a, env, budget, exclude) for c in batch]
            for c, f in zip(batch, futs):
                try:
                    L, why = f.result(timeout=150)
                except Exception as e:
                    L, why = None, "timeout" if "Timeout" in type(e).__name__ else f"error {type(e).__name__}"
                checked += 1
                if L and (not L["domain"] or "app:" + L["domain"] not in dupes):
                    if L["domain"]:
                        dupes.add("app:" + L["domain"])
                    pending.append(L)
                    print(f"  + [{L['source']:<7}] {L['name'][:32]:<32} {L['domain'][:28]:<28} {'email' if L['email'] else ''}")
                else:
                    seen_f.write(c["key"] + "\n")
                    k = c["source"] + ": " + re.sub(r"[\d,]+", "N", why if not L else "duplicate site")
                    rejected[k] = rejected.get(k, 0) + 1
            seen_f.flush()
            if len(pending) >= 10:
                flush()
        flush()
    seen_f.close()

    print(f"\nChecked {checked}. Serper searches used: {budget.used}. Saved {len(saved)} leads -> {out.name}")
    print("Rejected:", dict(sorted(rejected.items(), key=lambda x: -x[1])[:12]))
    by = {}
    for r in saved:
        by[r["Niche"]] = by.get(r["Niche"], 0) + 1
    print(f"By niche: {by}; with email: {sum(bool(r['Email']) for r in saved)}")
    if a.dry_run:
        print("Dry run: tracker and sheet untouched.")


if __name__ == "__main__":
    main()
