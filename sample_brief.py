"""
Sample Brief Builder: the moment an app lead says "yes", this prepares everything to make their free 15-second sample.
  python sample_brief.py "Empirical Health"        (app name, or part of it, or the row Key)

It reads the lead's row in the "SaaS Client" tab, then writes  Yorelore - Outreach system/Samples/<App>/ :
  brief.md        3 hook versions -> 15-second script (hook / demo / CTA) + on-screen plan + labels + compliance
  prompts.md      Nano Banana avatar prompt + Flow (Veo 3.1 Fast) prompts per clip, in the SOP's 3-part format
  screens/        the app's real App Store / Google Play screenshots (what the creator "shows")
Follows UGC Production SOP + App Sample Kit: demo style only, AI creator labelled, no personal-result claims.
"""
import json, re, sys
from pathlib import Path
from urllib.parse import quote_plus

import requests
import lead_finder as lf
from copy_templates import first_name

try:
    from google_play_scraper import app as gp_app
except ImportError:
    gp_app = None

TAB = "SaaS Client"
OUT = lf.HERE.parent / "Samples"

PROMPT = """You are the creative director at Yorelore, making a FREE 15-second UGC-style sample ad for an app.
A realistic AI creator (clearly labelled "AI-generated") talks to camera on a phone (9:16) and the app's REAL screens
are shown. Demo / POV / "here's how" style ONLY: never a personal result or promise (no weight lost, money made,
partner found, cured, guaranteed returns), no medical or financial claims, nothing that implies a real customer.
Never mention a year. Only say "free" if the store description below says the app is free or has a free tier;
otherwise say "on the App Store" / "on Google Play". Use only features named in the description.

App: {app}
What it does: {one_liner}
Niche: {niche}
What we noticed: {opener}
Hooks we offered in the email (keep these exact lines if present): {hooks}
Feature to show: {feature}
Compliance note: {flag}
Store description (facts you may use): {desc}

Return JSON:
"avatar": one sentence describing the ideal creator for this app's buyer (age range, vibe, setting) - someone the
   buyer trusts or aspires to; ordinary, not a model; NEVER a doctor/nurse/financial adviser or any real person,
"setting": where they film (e.g. kitchen counter, car parked, bedroom desk, gym bench), natural light,
"versions": list of exactly 3 objects, one per hook, each with
   "hook" (0-3s, spoken, max 10 words),
   "demo" (3-10s, spoken, max 22 words, what the feature does, in plain words, present tense),
   "cta" (10-15s, spoken, max 10 words, e.g. "It's free on the App Store, link below"),
   "screen_plan" (which real screen shows when, e.g. "0-3s face only; 3-10s phone screen: dashboard; 10-15s face + app icon"),
   "caption" (on-screen text for the first 2 seconds, max 6 words),
"broll": 2 short shots to cut in (e.g. "close-up thumb scrolling the dashboard"),
"risk_lines": list of any words to avoid for this niche."""


def row_for(env, q):
    v = lf.sheet_call(env, {"action": "read", "tab": TAB}).get("values") or []
    rows = [dict(zip(v[0], r)) for r in v[1:]]
    ql = q.lower()
    hit = [r for r in rows if r.get("Key", "").lower() == ql] or [r for r in rows if ql in (r.get("App") or "").lower()]
    if not hit:
        sys.exit(f'No SaaS Client row matches "{q}".')
    return hit[0]


def screenshots(r):
    shots, desc = [], ""
    m = re.search(r"/id(\d+)", r.get("App Store") or "")
    if m:
        d = requests.get(f"https://itunes.apple.com/lookup?id={m.group(1)}&country=us", timeout=20).json().get("results", [])
        if d:
            shots += d[0].get("screenshotUrls", [])[:6]
            desc = d[0].get("description", "")[:1500]
    m = re.search(r"id=([\w.]+)", r.get("Play Store") or "")
    if m and gp_app and len(shots) < 4:
        try:
            d = gp_app(m.group(1), lang="en", country="us")
            shots += (d.get("screenshots") or [])[:6]
            desc = desc or (d.get("description") or "")[:1500]
        except Exception:
            pass
    if not shots and r.get("App"):   # last try: App Store search by name
        for x in requests.get(f"https://itunes.apple.com/search?term={quote_plus(r['App'])}&entity=software&country=us&limit=3",
                              timeout=20).json().get("results", []):
            if r["App"].lower()[:6] in x.get("trackName", "").lower():
                shots, desc = x.get("screenshotUrls", [])[:6], x.get("description", "")[:1500]
                break
    return shots, desc


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    env = lf.load_env()
    r = row_for(env, " ".join(sys.argv[1:]))
    app = r.get("App", "app")
    folder = OUT / re.sub(r"[^\w\- ]", "", app).strip()
    (folder / "screens").mkdir(parents=True, exist_ok=True)
    shots, desc = screenshots(r)
    saved = []
    for i, u in enumerate(shots):
        try:
            img = requests.get(u, timeout=30)
            if img.status_code == 200 and img.headers.get("content-type", "").startswith("image/"):
                ext = ".png" if "png" in img.headers["content-type"] else ".jpg"
                p = folder / "screens" / f"screen_{i + 1}{ext}"
                p.write_bytes(img.content)
                saved.append(p.name)
        except requests.RequestException:
            pass
    hooks = re.findall(r'^\d\. "(.*)"$', r.get("Email body (ready to send)", ""), re.M)
    feat = re.search(r"(?:walks through|showing) (.+?), filmed on your real app", r.get("Email body (ready to send)", ""))
    ai = lf.gemini(PROMPT.format(app=app, one_liner=r.get("One-liner", ""), niche=r.get("Niche", ""),
                                 opener=r.get("Opener line", ""), hooks=json.dumps(hooks) if hooks else "(none - write 3)",
                                 feature=feat.group(1) if feat else r.get("Hook idea (for the sample)", ""),
                                 flag=r.get("Compliance flag", "") or "none", desc=desc or r.get("One-liner", "")),
                   env["GEMINI_API_KEY"]) or {}
    vs = ai.get("versions") or []
    who = first_name(r.get("Founder / owner", ""))

    brief = [f"# {app}: free sample brief",
             f"*Lead: {r.get('Founder / owner') or '?'} · {r.get('Email','')} · {r.get('Website','')} · Variant {r.get('Variant','')}*",
             f"*Promise made in the email: 15 seconds, 3 hook versions, AI creator labelled, free. Deadline: 48 hours from their yes.*", "",
             f"**Compliance:** {r.get('Compliance flag') or 'standard'}. Avoid: {', '.join(ai.get('risk_lines', [])) or '-'}",
             "Every version ends with the small tag **AI-generated** (bottom corner, whole ad) and the preview carries "
             "a light watermark **Yorelore · sample preview**.", "",
             f"**Creator:** {ai.get('avatar','')}  \n**Setting:** {ai.get('setting','')}", "",
             f"**Real screens:** {', '.join(saved) or 'none found: screen-record the app (free tier) or use the website'} (in `screens/`)", ""]
    for i, v in enumerate(vs, 1):
        brief += [f"## Version {i}: \"{v.get('hook','')}\"",
                  "| Time | Spoken | On screen |", "|---|---|---|",
                  f"| 0-3s | {v.get('hook','')} | {v.get('caption','')} (caption) · creator to camera |",
                  f"| 3-10s | {v.get('demo','')} | {v.get('screen_plan','')} |",
                  f"| 10-15s | {v.get('cta','')} | creator + app name/icon · AI-generated tag |", ""]
    brief += ["**B-roll:** " + " · ".join(ai.get("broll", [])), "",
              "## Delivery (on the walkthrough call or by reply)",
              f"1. Reply to {who} within the hour: \"On it, you'll have 3 versions by [day].\"",
              "2. Send the watermarked preview (6-8s teaser or 2 stills) + the Calendly link (Game Plan 1.4, message 2).",
              "3. Clean HD file after the call (or anyway if they refuse the call: goodwill = warm lead).",
              "4. Update the sheet row: Status = Sample sent, Sample sent date."]
    (folder / "brief.md").write_text("\n".join(brief), encoding="utf-8")

    rules = ("one continuous take, no jump cuts, mouth moves naturally with the words, steady eye contact, speaks at a natural "
             "conversational pace, does not touch the camera, realistic skin texture, natural window light, no text on screen")
    pr = [f"# {app}: prompts", "",
          "## 1. Avatar (Gemini / Nano Banana, one shot, from a REAL reference still - see SOP Step 2-3)",
          f"> Replace the person in this photo with a completely different person: {ai.get('avatar','')}. Change face shape, eyes, "
          f"nose, skin tone and hair so they could not be the same person. Setting: {ai.get('setting','')}. They hold a phone at chest "
          "height, screen facing away. Add one earbud and a simple watch. Keep the photo's natural phone-camera grain, real skin "
          "pores and imperfect lighting. Vertical 9:16.", "",
          "## 2. Phone-screen shot (Nano Banana, new chat: avatar image + one file from screens/)",
          "> Replace the phone screen in their hand with this app screenshot, facing the camera. Keep every pixel of the "
          "screenshot's text and layout exactly as it is; do not redraw the UI. Natural screen glare.", "",
          "## 3. Flow / Veo 3.1 Fast (Frames on, 9:16, start frame = avatar). One clip per line; lock clip 1, then only swap dialogue."]
    for i, v in enumerate(vs, 1):
        for part in ("hook", "demo", "cta"):
            pr += [f"**V{i} {part}:**",
                   f"> Handheld iPhone UGC selfie footage, {ai.get('setting','')}. The person talks to camera like a relaxed "
                   f"TikTok creator showing a friend an app. They say: \"{v.get(part,'')}\" Rules: {rules}; smiles slightly at the end.", ""]
    pr += ["## 4. CapCut (free)",
           "Stitch hook → demo (cut to the phone-screen shot or a screen recording at 3s) → CTA. Captions on. Tag `AI-generated` "
           "bottom-left for the whole ad. Export 9:16. Make the 3 versions by swapping only the hook clip.",
           "", f"**Credit estimate:** ~{3 + 2} Veo Fast clips (3 hooks + 1 demo + 1 CTA reused) ≈ 100 credits + retries."]
    (folder / "prompts.md").write_text("\n".join(pr), encoding="utf-8")
    print(f"Sample kit ready: {folder}\n  brief.md, prompts.md, screens/ ({len(saved)} real screenshots)")


if __name__ == "__main__":
    main()
