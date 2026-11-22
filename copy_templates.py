"""Email 1 templates for app leads. A/B test chosen by Francis on 11 Nov 2026: Option A vs Option C (50/50 by lead key).
A = "Which of these 3 hooks" (Schwab 'which of these' + reader participation + Voss no-oriented ask)
C = accusation audit + free gift + Columbo P.S. (Voss + Hopkins + Schwab 'admit a weakness')
Rules: plain text, no links, permission-first, AI creator always labelled, hooks demo-style (never personal results)."""
import hashlib

NICHE_WORD = {"health": "health", "relationships": "dating and relationship", "money": "finance", "saas": "software"}


def variant_for(key):
    # 22 Nov 2026: A/B closed. C won (7/42 replies vs A 1/44), so every new Email 1 uses C (Francis).
    return "C"


def first_name(founder):
    n = (founder or "").strip().split()
    return n[0] if n and n[0].isalpha() and len(n[0]) > 1 else "there"


def email_one(variant, app, founder, opener, feature, hooks, niche):
    """Returns (subject, body) without signature. Falls back to C when A has no 3 hooks."""
    fn = first_name(founder)
    app_l = app.strip().lower()
    feature = (feature or "your app's key feature").strip()
    opener = (opener or "").strip()
    hooks = [h.strip().strip('"').strip("“”") for h in (hooks or []) if str(h).strip()][:3]
    if variant == "A" and len(hooks) == 3:
        subject = f"which of these 3 hooks for {app_l}?"
        body = (f"Hi {fn},\n\n" + (f"{opener}\n\n" if opener else "") +
                f"I wrote 3 opening lines for {'an' if app.strip()[:1].lower() in 'aeiou' else 'a'} {app} ad:\n"
                f'1. "{hooks[0]}"\n2. "{hooks[1]}"\n3. "{hooks[2]}"\n\n'
                f"Pick one and I'll turn it into a free 15-second video. A realistic AI creator (labelled as AI) walks "
                f"through {feature}, filmed on your real app. No creator fees, no shoot, and it's yours to run.\n\n"
                f"Would it be a bad idea if I made the one you like best?")
        return subject, body, "A"
    word = NICHE_WORD.get((niche or "").lower(), "")
    subject = f"a free ad for {app_l}"
    body = (f"Hi {fn},\n\nYou probably get a dozen \"we'll grow your app\" emails a week, so I'll skip the pitch and offer "
            f"something instead.\n\n" + (f"{opener}\n\n" if opener else "") +
            f"I'd like to make you one free 15-second UGC-style ad: a realistic AI creator (labelled as AI) showing "
            f"{feature}, filmed on your real app. If it's good, run it. If not, delete it. Nothing to sign, no call needed.\n\n"
            f"Want it? A one-word reply is fine.\n\n"
            f"P.S. I'm only doing these free for a handful of {word + ' ' if word else ''}apps this month while I build my portfolio.")
    return subject, body, "C"


HOOKS_PROMPT_RULES = ('"feature" (the ONE screen/feature to show, as a short phrase starting with "your", e.g. "your biomarker dashboard"), '
                      '"hooks" (a list of exactly 3 opening lines for the ad, each max 10 words, spoken by the creator or on-screen text. '
                      'Demo, POV, curiosity or "here\'s how" style ONLY. NEVER a personal result or promise: no weight lost, money made, '
                      'partner found, cured, guaranteed. No medical or financial claims.)')
