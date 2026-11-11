"""
Tidy the sheet's Agencies tab after a run (edits only the cells named; never clears or rewrites the tab):
  - finds the Instagram handle + followers for TikTok-only agencies (1-2 Google searches each)
  - fills IG followers from the size signal when Google showed it there
  - deletes rows given with --drop (keys like ig:handle / tt:handle)

  python agency_cleanup.py --dry-run
  python agency_cleanup.py --drop ig:branding tt:jenayroseofficial
"""
import argparse, re

import lead_finder as lf
import agency_finder as af


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_ig(row, env, budget):
    """Look for the agency's own Instagram profile; accept it only if the handle or name clearly matches."""
    tt = row["TikTok handle"].lstrip("@")
    names = [n for n in {norm(row["Agency"]), norm(row["Owner / founder"]), norm(tt)} if len(n) >= 4]
    for q in (f'site:instagram.com "{tt}"', f'site:instagram.com "{row["Agency"]}"' if row["Agency"] else None):
        if not q:
            continue
        for o in af.search(q, env, budget, "us"):
            m = re.match(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?.*)?$", o.get("link", ""))
            if not m or m.group(1).lower() in af.JUNK:
                continue
            h, title = m.group(1), o.get("title", "")
            hn, tn = norm(h), norm(re.sub(r"\(@.*", "", title))
            if any(n == hn or n in hn or hn in n or (len(n) >= 6 and n in tn) for n in names):
                f = re.search(r"([\d.,]+\s*[KkMm]?)\+?\s+followers", f"{title} {o.get('snippet', '')}", re.I)
                return h, lf.num(f.group(1)) if f else None
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", nargs="*", default=[])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-searches", type=int, default=120)
    a = ap.parse_args()
    env, budget = lf.load_env(), lf.Budget(a.max_searches)
    vals = lf.sheet_call(env, {"action": "read", "tab": af.TAB})["values"]
    head, rows = vals[0], [dict(zip(vals[0], r)) for r in vals[1:]]
    updates = []
    for r in rows:
        if not r.get("Key") or r["Key"] in a.drop:
            continue
        fields = {}
        if not r["IG handle"] and r["TikTok handle"]:
            h, f = find_ig(r, env, budget)
            if h:
                fields.update({"IG handle": "@" + h, "IG URL": f"https://instagram.com/{h}", "IG followers": f or ""})
                print(f'{r["Key"]:<30} -> @{h} ({f or "?"} followers)')
            else:
                print(f'{r["Key"]:<30} -> no matching Instagram found')
        if not r["IG followers"] and not fields.get("IG followers") and r["IG handle"]:
            m = re.search(r"([\d.,]+\s*[KkMm]?)\+?\s+followers", r["Size signal"], re.I)
            if m and r["Key"].startswith("ig:"):
                fields["IG followers"] = lf.num(m.group(1))
        if fields.get("IG followers") and "Follower count not visible" in r["Notes"]:
            fields["Notes"] = re.sub(r";?\s*Follower count not visible on Google; check profile", "", r["Notes"]).strip("; ")
        if fields:
            updates.append({"key": r["Key"], "fields": {k: str(v) for k, v in fields.items()}})
    af.save_cache()
    print(f"\n{len(updates)} rows to update, {len(a.drop)} to delete, {budget.used} searches")
    if a.dry_run:
        return
    if updates:
        print("update:", lf.sheet_call(env, {"action": "update", "tab": af.TAB, "header": head, "updates": updates}))
    if a.drop:
        print("delete:", lf.sheet_call(env, {"action": "delete", "tab": af.TAB, "header": head, "keys": a.drop}))


if __name__ == "__main__":
    main()
