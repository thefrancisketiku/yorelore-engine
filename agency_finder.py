"""
Yorelore Agency Finder: small, owner-run agencies (US + AU) on Instagram and TikTok, for partnership outreach.

Target: the "new wave" agencies run by one person or a small team (SMMA, social media, web design, Meta/TikTok ads,
content/UGC/video editing, email, lead gen, AI automation, branding), probably doing $7k+/month but not the
$30k+/month gurus, corporations or course sellers. The output is mainly Instagram handles to DM.

How (all through Google via Serper; we never log in to or scrape Instagram/TikTok):
  1. discovery searches find agency posts/profiles by type and place, and pull handles from the results
  2. Gemini drops obvious non-agencies before we pay to check anyone
  3. one search per handle reads the profile snippet (followers, bio) plus recent post captions
  4. Gemini keeps owner-run agencies in the size window, labels country, type, proof, and writes a DM
  5. rows are appended to the sheet's "Agencies" tab (never overwrites) and saved to output/

Usage:
  python agency_finder.py --count 100
  python agency_finder.py --count 30 --country AU --max-searches 300
"""
import argparse, csv, datetime as dt, html, json, random, re, sys
from concurrent.futures import ThreadPoolExecutor

import lead_finder as lf

TAB = "Agencies"
SEEN = lf.HERE / "seen_agencies.txt"
COLUMNS = ["Date added", "Agency", "Owner / founder", "IG handle", "IG URL", "IG followers", "TikTok handle",
           "TikTok followers", "Agency type", "Services", "Country", "City / state", "Account type", "Size signal",
           "Proof seen", "Website", "Email", "Fit note", "Opener line", "DM (ready to send)", "Date touch 1",
           "Follow-up 1", "Follow-up 2", "Replied?", "Status", "Notes", "Key"]

IG_TPL = ['"agency owner" {p}', '"my agency" "clients" {p}', '"social media agency" "founder" {p}',
          '"marketing agency" "DM" "results" {p}', '"web design" "agency" "founder" {p}', '"smma" {p}',
          '"meta ads" "agency" {p}', '"content agency" OR "video editing agency" {p}', '"ugc agency" {p}',
          '"ai automation agency" {p}', '"lead generation agency" {p}', '"branding studio" "founder" {p}',
          '"client results" "agency" {p}', '"testimonials" "agency" "book a call" {p}',
          '"email marketing agency" {p}', '"ads agency" "ecom" {p}', '"social media manager" "agency" "clients" {p}',
          '"growth agency" "founder" {p}']
TT_TPL = ['"agency owner" {p}', '"smma" "clients" {p}', '"marketing agency" "founder" {p}',
          '"web design agency" {p}', '"social media agency" {p}']
PLACES = {"US": ["USA", "Texas", "California", "Florida", "New York", "Atlanta", "Chicago", "Los Angeles", "Miami",
                 "Dallas", "Houston", "Arizona", "North Carolina", "Nashville", "Denver"],
          "AU": ["Australia", "Sydney", "Melbourne", "Brisbane", "Perth", "Gold Coast", "Adelaide"]}
NEG = " -insurance -farmers -allstate -realty -realtor -travel -staffing -modeling -talent"
IG_MIN, IG_MAX, TT_MIN, TT_MAX = 500, 80_000, 500, 150_000
JUNK = set(lf.SOCIAL_JUNK) | {"stories", "accounts", "about", "directory", "tag", "discover", "music", "explore"}
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"


def handles_from(o):
    """Pull (platform, handle) pairs out of one Google result: profile URLs, '(@x)' titles, '- x on <date>' captions."""
    link, blob = o.get("link", ""), f"{o.get('title', '')} {o.get('snippet', '')}"
    out = []
    m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?.*)?$", link)
    if m:
        out.append(("ig", m.group(1)))
    elif "instagram.com" in link:
        pats = [rf"(?:^|[\s\"(])([A-Za-z0-9_.]{{3,30}}) on (?:{MONTHS}) \d", r"\(@([A-Za-z0-9_.]{2,30})\)",
                r"([A-Za-z0-9_.]{3,30})'s profile picture", r"([A-Za-z0-9_.]{3,30})\. ?•\. ?Follow",
                r"(?:founder|owner|ceo|run by|by)\s+@([A-Za-z0-9_.]{2,30})"]
        for pat in pats:
            out += [("ig", h) for h in re.findall(pat, blob, re.I)]
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]{2,30})", link)
    if m:
        out.append(("tt", m.group(1)))
    return [(p, h.strip(".").lower()) for p, h in out if len(h.strip(".")) >= 3 and h.lower().strip(".") not in JUNK]


CACHE_FILE = lf.OUT_DIR / "agency_serper_cache.json"
try:
    CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    CACHE = {}


def search(q, env, budget, gl):
    k = f"{gl}|{q}"
    if k not in CACHE:
        res = lf.serper(q, env["SERPER_API_KEY"], budget, gl)
        if res is None:
            return []
        CACHE[k] = res
    return CACHE[k]


def save_cache():
    lf.OUT_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(CACHE), encoding="utf-8")


def discover(queries, env, budget):
    """Run discovery searches; return {key: {platform, handle, texts, hint}}."""
    found = {}
    def run(q):
        site, tpl, place, gl = q
        return q, search(f"site:{site} " + tpl.format(p=place) + NEG, env, budget, gl)
    with ThreadPoolExecutor(6) as ex:
        for (site, tpl, place, gl), res in ex.map(run, queries):
            for o in res:
                for p, h in handles_from(o):
                    c = found.setdefault(f"{p}:{h}", {"platform": p, "handle": h, "texts": [], "hint": gl.upper(), "place": place})
                    c["texts"].append(f"{o.get('title', '')} | {o.get('snippet', '')}"[:400])
    return found


PREFILTER = """Below are Instagram/TikTok accounts seen in Google results, with the text Google showed.
Return a JSON list of the ids that MIGHT be a service agency or freelancer serving clients (social media, marketing,
ads, web design, content, video editing, UGC, branding, email, lead gen, automation, SMMA...), run by a person or a small team.
Leave out: product brands/shops, news/media, events, job boards, big famous companies, software tools,
and pure influencers. When unsure, include it.

{items}"""

FINAL = """You qualify partnership leads for Francis, who runs Yorelore, a creative studio that makes UGC-style video ads
from a brand's product photos (no creators, no shoots, no shipping product). He wants to partner with small,
owner-run agencies in the US and Australia so they can resell or add these ads for their clients.

TARGET: a "new wave" agency run by one founder or a small team (SMMA, social media management, Meta/TikTok/Google ads,
web design, content/video editing, UGC, branding, email, lead gen, AI automation, growth), that shows signs of real
clients/results (likely doing $7k+/month) but is NOT big (not $30k+/month gurus, not 7/8-figure claims,
not a corporation with large staff or many offices, not a decades-old established firm).

Below is REAL data from Google (profile snippet + post captions). Use ONLY this data. Never invent facts, numbers,
clients or results. You have NOT watched their videos.

{items}

Return a JSON list, one object per account in the same order:
"id",
"keep" (true only if the account is the agency itself or its owner/founder (not an employee or intern), the agency serves clients, it is run by an individual or small team, and fits the size window; false for coaches/gurus who mainly sell courses or masterminds about starting agencies, big or corporate agencies, product brands, communities, events, job posts, software, and accounts with no sign of being an agency),
"too_big" (true if the data shows a big/established operation or $30k+/month / 7-figure claims),
"too_small" (true only if the data clearly shows a beginner with no clients yet, e.g. "day 1 of starting my agency"),
"agency_name", "owner_name" (only if written in the data, else ""),
"account_type" ("personal brand" if it is the founder's own account, "agency page" if it is the business account),
"agency_type" (2-4 words, e.g. "social media agency", "web design studio", "Meta ads agency", "SMMA"),
"services" (short comma list from the data),
"country" ("US", "AU", "other" or "unknown", from the data only), "city" (city/state if written, else ""),
"size_signal" (one short line from the data: followers, client count, results claimed, team, years),
"proof" (results/testimonials/case studies/client names visible in the data, else ""),
"website" (if written in the data, else ""), "email" (if written in the data, else ""),
"ig_handle" (for TikTok accounts: their Instagram handle if their bio names it, else ""),
"fit_note" (one line: why they are a good partner for Yorelore, e.g. they run ads for e-com brands who always need new creatives),
"opener_line" (one specific, true observation about them from the data, max 20 words, no flattery, nothing generic),
"dm" (under 65 words. Start "Hey [first name]," if the owner name is known, else "Hey there,". Then the opener line. Then: "I run Yorelore, a creative studio. We make UGC-style video ads from product photos, white-label, no creators or shoots." Then a soft ask that fits their services, e.g. "Happy to make a free sample for one of your clients so you can see the quality. Open to it?").
"""


def gem(prompt, key):
    for _ in range(2):
        r = lf.gemini(prompt, key)
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            for v in r.values():
                if isinstance(v, list):
                    return v
    return []


def prefilter(cands, env):
    keep = set()
    items = list(cands.values())
    for s in range(0, len(items), 30):
        chunk = items[s:s + 30]
        txt = "\n".join(f'id {i}: {c["platform"]} @{c["handle"]} :: ' + " || ".join(c["texts"][:2]) for i, c in enumerate(chunk))
        res = gem(PREFILTER.format(items=txt), env["GEMINI_API_KEY"])
        keys = [f'{c["platform"]}:{c["handle"]}' for c in chunk]
        got = set()
        for x in res:  # the lite model sometimes answers with "id 3", {"id": 3} or the handle itself
            v = str(x.get("id", "") if isinstance(x, dict) else x).lower()
            m = re.fullmatch(r"\s*(?:id\s*)?(\d+)\s*", v)
            if m and int(m.group(1)) < len(chunk):
                got.add(keys[int(m.group(1))])
            else:
                got |= {k for c, k in zip(chunk, keys) if c["handle"] in v}
        keep |= got or set(keys)  # no usable answer: check them all rather than lose them
    return keep


def verify(c, env, budget):
    """One search reads the profile snippet (followers, bio) and recent captions."""
    h = c["handle"]
    site = "instagram.com" if c["platform"] == "ig" else "tiktok.com"
    res = search(f'site:{site} "{h}"', env, budget, c["hint"].lower())
    c["profile"] = ""
    for o in res:
        link, blob = o.get("link", ""), f"{o.get('title', '')} | {o.get('snippet', '')}"
        if c["platform"] == "ig":
            m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)/?(?:\?.*)?$", link)
        else:
            m = re.match(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)/?(?:\?.*)?$", link)
        if m and m.group(1).lower() == h and not c["profile"]:
            c["profile"] = blob[:500]
        elif h in blob.lower() and len(c["texts"]) < 6:
            c["texts"].append(blob[:400])
    f = re.search(r"([\d.,]+\s*[KkMm]?)\+?\s+followers", c["profile"] or " ".join(c["texts"][:1]), re.I)
    c["followers"] = lf.num(f.group(1)) if f else None
    return c


def size_ok(c):
    f = c.get("followers")
    if f is None:
        return True
    lo, hi = (IG_MIN, IG_MAX) if c["platform"] == "ig" else (TT_MIN, TT_MAX)
    return lo <= f <= hi


def qualify(cands, env):
    out, failed = [], set()
    for s in range(0, len(cands), 8):
        chunk = cands[s:s + 8]
        txt = "\n\n".join(f'id {i}: {c["platform"].upper()} @{c["handle"]} (followers: {c.get("followers") or "unknown"}; '
                          f'found via a search about {c["place"]})\nPROFILE: {c.get("profile") or "-"}\nPOSTS: ' + " || ".join(c["texts"][:5])
                          for i, c in enumerate(chunk))
        res = gem(FINAL.format(items=txt), env["GEMINI_API_KEY"])
        if not res:
            failed |= {f'{c["platform"]}:{c["handle"]}' for c in chunk}
        for i, g in enumerate(res):
            if not isinstance(g, dict):
                continue
            try:
                c = chunk[int(g.get("id", i))]
            except (ValueError, TypeError, IndexError):
                continue
            c["g"] = g
            out.append(c)
    return out, failed


def to_row(c, today):
    g, ig = c["g"], c["platform"] == "ig"
    country = g.get("country") or "unknown"
    if country in ("unknown", ""):
        country = c["hint"] + "?"
    ig_handle = c["handle"] if ig else (g.get("ig_handle") or "").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]{2,30}", ig_handle or "-"):
        ig_handle = ""
    notes = []
    if c.get("followers") is None:
        notes.append("Follower count not visible on Google; check profile")
    return {"Date added": today, "Agency": html.unescape(g.get("agency_name") or ""),
            "Owner / founder": g.get("owner_name") or "", "IG handle": "@" + ig_handle if ig_handle else "",
            "IG URL": f"https://instagram.com/{ig_handle}" if ig_handle else "",
            "IG followers": c.get("followers") if ig else "", "TikTok handle": "" if ig else "@" + c["handle"],
            "TikTok followers": "" if ig else c.get("followers") or "", "Agency type": g.get("agency_type") or "",
            "Services": g.get("services") or "", "Country": country, "City / state": g.get("city") or "",
            "Account type": g.get("account_type") or "", "Size signal": g.get("size_signal") or "",
            "Proof seen": g.get("proof") or "", "Website": g.get("website") or "", "Email": g.get("email") or "",
            "Fit note": g.get("fit_note") or "", "Opener line": g.get("opener_line") or "", "DM (ready to send)": g.get("dm") or "",
            "Status": "New", "Notes": "; ".join(notes), "Key": f'{c["platform"]}:{c["handle"]}'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--country", choices=["US", "AU", "both"], default="both")
    ap.add_argument("--max-searches", type=int, default=700)
    ap.add_argument("--dry-run", action="store_true", help="don't write to the sheet or the seen file")
    a = ap.parse_args()
    env = lf.load_env()
    budget = lf.Budget(a.max_searches)
    today = dt.date.today().strftime("%d/%m/%Y")

    seen = lf.read_lines(SEEN)
    try:
        vals = lf.sheet_call(env, {"action": "read", "tab": TAB}).get("values") or []
        if vals:
            k = vals[0].index("Key") if "Key" in vals[0] else -1
            seen |= {r[k].lower() for r in vals[1:] if k >= 0 and r[k]}
            seen |= {"ig:" + r[vals[0].index("IG handle")].lstrip("@").lower() for r in vals[1:] if "IG handle" in vals[0] and r[vals[0].index("IG handle")]}
        print(f"{len(vals) - 1 if vals else 0} agencies already on the sheet tab")
    except Exception as e:
        print(f"could not read the sheet tab ({e}); continuing with the local seen list")
    seen |= {l for l in lf.read_lines(lf.DNC_FILE)}

    countries = ["US", "AU"] if a.country == "both" else [a.country]
    queries = []
    for ctry in countries:
        for p in PLACES[ctry]:
            queries += [("instagram.com", t, p, ctry.lower()) for t in IG_TPL]
            queries += [("tiktok.com", t, p, ctry.lower()) for t in TT_TPL]
    random.seed(16); random.shuffle(queries)
    # roughly 2 US queries for each AU one when running both
    if a.country == "both":
        us = [q for q in queries if q[3] == "us"]; au = [q for q in queries if q[3] == "au"]
        queries = [x for pair in zip(us[0::2], us[1::2], au) for x in pair] + us[len(au) * 2:]

    lf.OUT_DIR.mkdir(exist_ok=True)
    out = lf.OUT_DIR / f"agencies_{dt.date.today():%Y-%m-%d}_{dt.datetime.now():%H%M}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        csv.DictWriter(f, COLUMNS).writeheader()
    saved, names, qi = [], set(), 0
    while len(saved) < a.count and qi < len(queries) and budget.left > 20:
        batch, qi = queries[qi:qi + 12], qi + 12
        found = discover(batch, env, budget)
        fresh = {k: v for k, v in found.items() if k not in seen}
        if not fresh:
            continue
        maybe = prefilter(fresh, env)
        todo = [fresh[k] for k in fresh if k in maybe][: max(0, budget.left - 10)]
        with ThreadPoolExecutor(6) as ex:
            todo = [c for c in ex.map(lambda c: verify(c, env, budget), todo) if size_ok(c)]
        judged, failed = qualify(todo, env)
        done = set(fresh) - failed          # Gemini didn't answer for "failed": leave them for a later run
        seen |= done
        rows = []
        for c in judged:
            g = c["g"]
            ok = g.get("keep") and not g.get("too_big") and not g.get("too_small") and g.get("country") != "other"
            print(f'{"KEEP" if ok else "drop"}  {c["platform"]}:@{c["handle"]:<28} {str(c.get("followers") or "?"):>7}  '
                  f'{g.get("country", "?"):<7} {(g.get("agency_type") or "")[:30]}', flush=True)
            if not ok:
                continue
            r = to_row(c, today)
            n = re.sub(r"[^a-z0-9]", "", r["Agency"].lower())
            ig = r["IG handle"].lower()
            if (n and n in names) or (ig and "ig:" + ig.lstrip("@") in seen and not r["Key"].startswith("ig:")):
                # same agency already saved (agency page + founder account, or the same person on IG and TikTok)
                prev = next((x for x in saved + rows if re.sub(r"[^a-z0-9]", "", x["Agency"].lower()) == n), None)
                if prev is not None and prev in rows:
                    prev["Notes"] = "; ".join(x for x in [prev["Notes"], f"Also on {r['IG handle'] or r['TikTok handle']}"] if x)
                continue
            names.add(n)
            rows.append(r)
        rows = rows[: a.count - len(saved)]
        saved += rows
        save_cache()
        with out.open("a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, COLUMNS).writerows(rows)
        if not a.dry_run:
            with SEEN.open("a", encoding="utf-8") as f:
                f.write("".join(k + "\n" for k in done))
            if rows:
                payload = {"action": "append", "tab": TAB, "header": COLUMNS,
                           "rows": [[str(r.get(c, "") if r.get(c) is not None else "") for c in COLUMNS] for r in rows]}
                try:
                    res = lf.sheet_call(env, payload)
                    print(f"sheet append ({TAB}): {'ok' if res.get('ok') else 'FAILED ' + str(res)[:200]}")
                except Exception as e:
                    print(f"sheet append problem: {e} (rows are in {out.name})")
        print(f"-- {len(saved)}/{a.count} saved, {budget.used} searches used, {len(seen)} accounts seen"
              f"{', Gemini skipped ' + str(len(failed)) if failed else ''}", flush=True)

    print(f"\n{len(saved)} agencies -> {out.name} ({budget.used} searches)")


if __name__ == "__main__":
    main()
