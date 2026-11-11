"""Rewrite Email 1 (subject + body + Variant) for SaaS Client rows not yet emailed, using the A/C templates.
Only rows with Sequence step 0 and Status New/Queued/blank are touched. Nothing is deleted.
  python rewrite_emails.py [--dry-run]"""
import sys
import lead_finder as lf
from copy_templates import email_one, variant_for, HOOKS_PROMPT_RULES

TAB = "SaaS Client"
PROMPT = """For each app below, write the material for a 15-second UGC-style ad made with a realistic AI creator who is
labelled as AI. Use only the data given; never invent features.

{items}

Return a JSON list, one object per app: {{"id": .., """ + HOOKS_PROMPT_RULES.replace("{", "{{").replace("}", "}}") + """}}"""


def main():
    dry = "--dry-run" in sys.argv
    env = lf.load_env()
    lf.sheet_call(env, {"action": "append", "tab": TAB, "header": ["Variant"], "rows": []})
    v = lf.sheet_call(env, {"action": "read", "tab": TAB}).get("values") or []
    rows = [dict(zip(v[0], r)) for r in v[1:]]
    todo = [r for r in rows if r.get("Key") and r.get("Email") and str(r.get("Sequence step") or "0") in ("0", "")
            and (r.get("Status") or "New").strip().lower() in ("new", "queued", "")]
    print(f"{len(todo)} unsent rows to rewrite")
    updates, counts = [], {"A": 0, "C": 0}
    for s in range(0, len(todo), 8):
        chunk = todo[s:s + 8]
        items = "\n\n".join(f"id {i}: {r.get('App','')}\none-liner: {r.get('One-liner','')}\nniche: {r.get('Niche','')}\n"
                            f"what we noticed: {r.get('Opener line','')}\nad idea: {r.get('Hook idea (for the sample)','')}\n"
                            f"compliance: {r.get('Compliance flag','')}" for i, r in enumerate(chunk))
        out = lf.gemini(PROMPT.format(items=items), env["GEMINI_API_KEY"]) or []
        if isinstance(out, dict):
            out = next((x for x in out.values() if isinstance(x, list)), [])
        got = {}
        for o in out:
            try:
                got[int(o["id"])] = o
            except (KeyError, ValueError, TypeError):
                pass
        for i, r in enumerate(chunk):
            o = got.get(i, {})
            subj, body, var = email_one(variant_for(r["Key"]), r.get("App", ""), r.get("Founder / owner", ""),
                                        r.get("Opener line", ""), o.get("feature", ""),
                                        o.get("hooks") if isinstance(o.get("hooks"), list) else [], r.get("Niche", ""))
            counts[var] += 1
            updates.append({"key": r["Key"], "fields": {"Email subject": subj, "Email body (ready to send)": body, "Variant": var}})
        print(f"  {min(s + 8, len(todo))}/{len(todo)}")
    print(f"Variant A: {counts['A']}, Variant C: {counts['C']}")
    for u in updates[:2]:
        print("\n--- " + u["fields"]["Variant"] + " | " + u["fields"]["Email subject"] + "\n" + u["fields"]["Email body (ready to send)"])
    if updates and not dry:
        print(lf.sheet_call(env, {"action": "update", "tab": TAB, "updates": updates}))


if __name__ == "__main__":
    main()
