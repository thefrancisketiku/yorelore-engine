"""One-off / repeatable: fill the 'Audience' column (consumer / business) on SaaS Client rows that don't have it.
The outreach engine emails consumer apps first. Never deletes or overwrites anything else.
  python label_audience.py [--dry-run]"""
import sys
import lead_finder as lf

TAB = "SaaS Client"
PROMPT = """For each product below, answer who USES it. "consumer" = everyday people download or use it themselves
(fitness, dating, budgeting, wellness, social apps). "business" = sold to companies, clinics, professionals, insurers,
or it's biotech / drugs / devices / infrastructure. Use only the text.

{items}

Return a JSON list: [{{"id": .., "audience": "consumer" | "business"}}]"""


def main():
    dry = "--dry-run" in sys.argv
    env = lf.load_env()
    lf.sheet_call(env, {"action": "append", "tab": TAB, "header": ["Audience"], "rows": []})   # make sure the column exists
    v = lf.sheet_call(env, {"action": "read", "tab": TAB}).get("values") or []
    hdr, rows = v[0], [dict(zip(v[0], r)) for r in v[1:]]
    todo = [r for r in rows if r.get("Key") and not (r.get("Audience") or "").strip()]
    print(f"{len(todo)} rows need an audience label")
    updates = []
    for s in range(0, len(todo), 20):
        chunk = todo[s:s + 20]
        items = "\n".join(f"id {i}: {r.get('App','')} | {r.get('One-liner','')} | niche {r.get('Niche','')} | {r.get('Website','')}"
                          for i, r in enumerate(chunk))
        out = lf.gemini(PROMPT.format(items=items), env["GEMINI_API_KEY"]) or []
        if isinstance(out, dict):
            out = next((x for x in out.values() if isinstance(x, list)), [])
        for o in out:
            try:
                r = chunk[int(o["id"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if o.get("audience") in ("consumer", "business"):
                updates.append({"key": r["Key"], "fields": {"Audience": o["audience"]}})
                print(f"  {o['audience']:<9} {r.get('App','')[:40]}")
    print(f"consumer: {sum(u['fields']['Audience'] == 'consumer' for u in updates)}, business: {sum(u['fields']['Audience'] == 'business' for u in updates)}")
    if updates and not dry:
        print(lf.sheet_call(env, {"action": "update", "tab": TAB, "updates": updates}))


if __name__ == "__main__":
    main()
