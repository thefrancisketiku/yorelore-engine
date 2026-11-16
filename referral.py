"""
Referral track (runs inside outreach_engine.py, same veto rules).

Why: about half the app leads only publish a shared inbox (hello@, support@...). Those land in a ticket queue.
So for each of those apps the engine:
  1. finds ONE real person on the team (public LinkedIn results via Google) and works out their work email,
  2. asks them: who handles growth / video ads, and what is that person focused on right now?
  3. when they answer: thanks them (same thread) and writes to the person they named
     ("<name> at <app> suggested I reach out"), tailored with what they said.
Same thing happens when a shared inbox itself replies "contact X instead" (wrong_person).

Everything is written to the "Referrals" tab. Every email waits VETO_MIN minutes after Francis is alerted
(set Status = Hold in the tab to stop it), then sends inside the lead's normal send window.
Work emails only (never personal addresses). One ask per company, no chasing the insider.
"""
import datetime as dt
import hashlib
import re
import unicodedata

import lead_finder as lf
import outreach_engine as oe

TAB = "Referrals"
COLS = ["Key", "Kind", "App", "Lead key", "Country", "Domain", "Name", "Title", "LinkedIn", "Email", "Email basis",
        "Status", "Queued on", "Subject", "Body", "Sent on", "Step", "Next touch", "Referred by", "Reply date",
        "Reply type", "Reply summary", "Insights", "Notes"]
ASKS_PER_DAY = 5          # new insider asks a day (on top of the main 20)
MAX_PER_RUN = 4
VETO_MIN = 90             # minutes between the alert and the send
GENERIC = re.compile(r"^(hello|hi|hey|info|support|help|team|contact|admin|office|mail|general|enquir|inquir|care|customer|"
                     r"success|android|ios|app|feedback|partner|press|media|sales|marketing|growth|connect|community|"
                     r"social|subjectrights|privacy|questions|ask|founders?)", re.I)
HUMAN_REPLY = {"interested", "question", "not_interested", "unsubscribe", "wrong_person"}
PATTERNS = ["first", "first.last", "firstlast", "flast", "f.last"]


# ---------- sheet ----------
def rows_(env):
    v = lf.sheet_call(env, {"action": "read", "tab": TAB}).get("values") or []
    return [dict(zip(v[0], r)) for r in v[1:] if any(str(x).strip() for x in r)] if v else []


def add(env, recs, preview):
    if not recs:
        return
    try:
        have = {r.get("Key") for r in rows_(env)}
    except Exception:
        have = set()
    recs = [r for r in recs if r.get("Key") not in have]      # the webhook updates the FIRST row with a key: duplicates never get marked Sent
    if not recs:
        return
    if preview:
        for r in recs:
            print(f"   (preview) Referrals += {r['Kind']} -> {r.get('Name')} <{r.get('Email')}> : {r.get('Subject')}")
        return
    lf.sheet_call(env, {"action": "append", "tab": TAB, "header": COLS, "rows": [[r.get(c, "") for c in COLS] for r in recs]})


def upd(env, key, fields, preview):
    if preview:
        print(f"   (preview) Referrals {key}: {fields}"); return
    lf.sheet_call(env, {"action": "update", "tab": TAB, "updates": [{"key": key, "fields": fields}]})


def ai_obj(prompt, env):
    """Gemini JSON, always a dict (it sometimes wraps the object in a list)."""
    x = lf.gemini(prompt, env["GEMINI_API_KEY"])
    if isinstance(x, list):
        x = next((i for i in x if isinstance(i, dict)), {}) if x and isinstance(x[0], dict) else {"people": x}
    return x if isinstance(x, dict) else {}


def h12(key):
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def mid(key, step):
    return f"<yl-r-{h12(key)}-{step}@{oe.DOMAIN}>"


def stamp(now=None):
    return (now or oe.wat_now()).strftime("%Y-%m-%d %H:%M")


# ---------- emails from names ----------
def name_parts(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|,.*$|\b(dr|mr|mrs|ms|jr|sr|phd|md|mba)\b\.?", " ", s)
    p = [re.sub(r"[^a-z]", "", x) for x in s.split()]
    p = [x for x in p if x]
    return (p[0], p[-1] if len(p) > 1 else "") if p else ("", "")


def make_email(name, domain, pat):
    f, l = name_parts(name)
    if not f:
        return ""
    loc = {"first": f, "first.last": f"{f}.{l}", "firstlast": f + l, "flast": f[0] + l, "f.last": f"{f[0]}.{l}"}[pat]
    if not l and pat != "first":
        return ""
    return f"{loc}@{domain}"


def pattern_of(name, addr):
    loc, _, dom = (addr or "").lower().partition("@")
    for p in PATTERNS:
        if make_email(name, dom, p) == addr.lower():
            return p
    return ""


def has_mx(domain):
    try:
        import dns.resolver
        return bool(dns.resolver.resolve(domain, "MX", lifetime=10))
    except Exception:
        return False


def known_pattern(domain, env, budget):
    """Best guess at the company's email pattern from addresses published on the web."""
    seen = []
    res = lf.serper(f'"@{domain}"', env["SERPER_API_KEY"], budget, "us") or []
    for o in res:
        for e in re.findall(r"[a-z0-9._%+-]+@" + re.escape(domain), (o.get("snippet", "") + " " + o.get("title", "")).lower()):
            if not GENERIC.match(e) and not oe.BAD_LOCAL.match(e):
                seen.append(e.split("@")[0])
    if any("." in x for x in seen):
        return "first.last"
    if any(len(x) > 9 for x in seen):
        return "firstlast"
    return "first"


# ---------- 1. find an insider + write the ask ----------
FIND = """We want ONE real person who works at the company below right now, to ask them politely who handles growth / marketing.
Company: {app} (website {domain}). What it does: {one}
Google results (public LinkedIn profiles):
{res}
Return JSON: "people": a list (max 4) of objects {{"name", "title", "linkedin", "is_decision_maker"}} ONLY for people who clearly
work at THIS company now (same product / same domain, not a different company with a similar name, not "former"/"ex-").
is_decision_maker = true for founder, co-founder, CEO, CMO, head/VP/director of growth or marketing. Empty list if unsure."""

ASK = """Write a short cold email from Francis Ketiku (founder of Yorelore: realistic AI-creator UGC video ads for apps, always
labelled as AI, demo-style) to {name}, {title} at {app}.
Context: Francis already emailed {app}'s shared inbox ({inbox}) offering a free 15-second sample ad; it went into a support queue.
Ask {first} two easy things: (1) who at {app} looks after growth / paid social / video ads (a name and email is perfect),
(2) in a line, what that person is focused on right now, so Francis's note is useful and not generic.
{dm_note}
Rules: under 75 words, plain text, warm and human, no flattery, no hype, no links, easy to answer in one line,
thank them either way. Start with "Hi {first},". No signature. Facts about the app you may use: {one}
Return JSON: "subject" (lowercase, max 6 words, no "quick question"), "body"."""


GROWTH = re.compile(r"growth|marketing|cmo|ugc|paid|acquisition|performance|brand|social|creative|content", re.I)
JUNIOR = re.compile(r"intern|assistant|associate|junior|recruit|talent|hr", re.I)


def rank(p):
    t = p.get("title") or ""
    if GROWTH.search(t) and not JUNIOR.search(t):
        return 0
    if p.get("is_decision_maker"):
        return 1
    return 3 if JUNIOR.search(t) else 2


PITCH = """Write a short cold email from Francis Ketiku (founder of Yorelore: realistic AI-creator UGC video ads for apps, always
labelled as AI, demo-style) to {name}, {title} at {app}.
Context: Francis already emailed {app}'s shared inbox ({inbox}) offering a free 15-second sample ad; it went into a support queue,
so he is writing to {first} directly as the person who looks after growth / ads.
Offer to make a free 15-second sample ad on {app}'s real app screens with a realistic AI creator (labelled as AI), name ONE
concrete thing the sample would show (a real feature from the facts), and end with one easy yes/no question. Add one line:
if someone else handles video ads, a name is just as helpful.
Rules: under 80 words, plain text, warm and human, no flattery, no hype, no links. Start with "Hi {first},". No signature.
Facts about the app you may use: {one}
Return JSON: "subject" (lowercase, max 6 words, no "quick question"), "body"."""


def find_insider(env, lead, budget):
    dom = (lead.get("Email") or "").split("@")[-1].lower() or lf.root_domain(lead.get("Website") or "")
    app = lead.get("App") or dom
    if not dom or not has_mx(dom):
        return None
    res = []
    for q in (f'site:linkedin.com/in "{app}" {dom}', f'site:linkedin.com/in "{app}"'):
        res += lf.serper(q, env["SERPER_API_KEY"], budget, "us") or []
        if len(res) >= 6:
            break
    if not res:
        return None
    txt = "\n".join(f"- {o.get('title','')} | {o.get('link','')} | {o.get('snippet','')[:200]}" for o in res[:12])
    ppl = ai_obj(FIND.format(app=app, domain=dom, one=lead.get("One-liner", ""), res=txt), env).get("people") or []
    ppl = [p for p in ppl if isinstance(p, dict)]
    ppl = [p for p in ppl if name_parts(p.get("name"))[1]]          # need a first AND last name
    if not ppl:
        return None
    ppl.sort(key=rank)          # growth/marketing/UGC lead first, then founder/CEO, then anyone else
    who = ppl[0]
    pat = known_pattern(dom, env, budget)
    return {"name": who["name"], "title": who.get("title", ""), "linkedin": who.get("linkedin", ""), "domain": dom,
            "email": make_email(who["name"], dom, pat), "basis": f"guessed: {pat}",
            "dm": [p for p in ppl if p.get("is_decision_maker")], "direct": rank(who) <= 1}


def eligible(lead, today, have):
    e = (lead.get("Email") or "").lower()
    if not e or lead.get("Key") in have:
        return False
    st = (lead.get("Status") or "").strip().lower()
    if st in ("do not contact", "bounced", "won", "call booked", "sample sent", "hold", "lost"):
        return False
    if st == "auto-reply":
        return True
    if not GENERIC.match(e) or (lead.get("Reply type") or "") in HUMAN_REPLY:
        return False
    t1 = oe.as_date(lead.get("Date touch 1"))
    return bool(t1 and (today - t1).days >= 2)


def make_asks(env, leads, preview):
    """Once a weekday morning: pick up to ASKS_PER_DAY shared-inbox leads, find an insider, queue the ask."""
    now = oe.wat_now()
    refs = rows_(env)
    if any(r.get("Kind") == "ask" and (r.get("Queued on") or "").startswith(now.date().isoformat()) for r in refs):
        return
    have = {r.get("Lead key") for r in refs}
    dnc = oe.dnc_set(env)
    cand = [l for l in leads if eligible(l, now.date(), have) and not oe.blocked(l, dnc)]
    cand.sort(key=lambda l: ((l.get("Status") or "").lower() != "auto-reply", (l.get("Audience") or "").lower() != "consumer",
                             l.get("Date touch 1") or "9"))
    budget = lf.Budget(4 * ASKS_PER_DAY + 4)
    made = []
    for lead in cand:
        if len(made) >= ASKS_PER_DAY:
            break
        ins = find_insider(env, lead, budget)
        if not ins or not ins["email"] or ins["email"] in dnc:
            print(f"   referral: no insider found for {lead.get('App')}")
            continue
        first = name_parts(ins["name"])[0].title()
        dm = ins["dm"]
        dm_note = (f"If it helps, Francis has seen {dm[0]['name']} ({dm[0].get('title','')}) - {first} can just confirm if that's right."
                   if dm and dm[0]["name"] != ins["name"] else "")
        ai = ai_obj((PITCH if ins.get("direct") else ASK).format(name=ins["name"], first=first, title=ins["title"] or "on the team", app=lead.get("App", ""),
                               inbox=lead.get("Email", ""), one=lead.get("One-liner", ""), dm_note=dm_note), env)
        body = (ai.get("body") or "").strip()
        if not body:
            continue
        made.append({"Key": f"ask:{lead['Key']}", "Kind": "ask", "App": lead.get("App", ""), "Lead key": lead["Key"],
                     "Country": lead.get("Country", ""), "Domain": ins["domain"], "Name": ins["name"], "Title": ins["title"],
                     "LinkedIn": ins["linkedin"], "Email": ins["email"], "Email basis": ins["basis"], "Status": "Queued",
                     "Queued on": stamp(now), "Subject": (ai.get("subject") or f"who handles growth at {lead.get('App','')}?").lower(),
                     "Body": body, "Step": "0", "Notes": ("decision maker seen: " + ", ".join(f"{p['name']} ({p.get('title','')})" for p in dm[:2])) if dm else ""})
    print(f"Referral asks: {len(made)} queued ({len(cand)} shared-inbox leads eligible)")
    add(env, made, preview)
    if made:
        oe.alert(env, f"Yorelore: {len(made)} referral asks queued (veto within {VETO_MIN} min)",
                 "Each goes to one person on the app's team, asking who handles growth/ads. To stop one, set its Status to "
                 f"Hold in the Referrals tab.\n{oe.SHEET_URL}\n\n" + "\n\n".join(
                     f"{i+1}. {m['App']} -> {m['Name']} ({m['Title']}) <{m['Email']}> [{m['Email basis']}]\n"
                     f"Subject: {m['Subject']}\n{m['Body']}" for i, m in enumerate(made)), preview)


# ---------- 2. send whatever is queued and past its veto time ----------
def send_queued(env, preview, force=False):
    now = oe.wat_now()
    if now.weekday() >= 5 and not force:
        return
    live = env.get("ENGINE_LIVE") == "1" and not preview
    refs = rows_(env)
    dnc = oe.dnc_set(env)
    today = now.date().isoformat()

    def ready(r):
        try:
            q = dt.datetime.strptime(r.get("Queued on", ""), "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        if not force and (now.replace(tzinfo=None) - q).total_seconds() < VETO_MIN * 60:
            return False
        lo, hi = oe.WINDOWS.get((r.get("Country") or "").upper().rstrip("?"), (13, 18))
        return force or r.get("Kind") == "thanks" or lo <= now.hour < hi

    done_keys = {r.get("Key") for r in refs if (r.get("Status") or "").strip().lower() != "queued"}
    mailed_today = {(r.get("Email") or "").lower() for r in refs if r.get("Sent on") == today}
    due = [r for r in refs if (r.get("Status") or "").strip().lower() == "queued" and r.get("Email")
           and r.get("Key") not in done_keys and r["Email"].lower() not in mailed_today
           and r["Email"].lower() not in dnc and r["Email"].split("@")[-1].lower() not in dnc and ready(r)]
    for r in refs:   # intro bump (day 4)
        nt = oe.as_date(r.get("Next touch"))
        if r.get("Kind") == "intro" and (r.get("Status") or "") == "Sent" and nt and nt <= now.date() and r.get("Step") == "1":
            r["_bump"] = True
            due.append(r)
    uniq, seen_k = [], set()
    for r in due:
        if r.get("Key") not in seen_k:
            seen_k.add(r.get("Key")); uniq.append(r)
    due = uniq
    print(f"Referral send: {len(due)} due")
    for r in due[:MAX_PER_RUN]:
        bump = r.get("_bump")
        step = 2 if bump else 1
        subj = ("Re: " + r["Subject"]) if bump or r.get("Kind") == "thanks" else r["Subject"]
        if bump:
            first = name_parts(r.get("Name"))[0].title() or "there"
            body = (f"Hi {first},\n\nJust floating this up in case it got buried. Happy to make the {r.get('App','')} sample this week "
                    f"so you can judge it for yourself. A yes or no is totally fine.")
        else:
            body = re.sub(r"^((?:Hi|Hey|Hello) [^,\n]{1,40},)[ \t]+", r"\1\n\n", (r.get("Body") or "").strip())
        parent = r.get("Notes", "") if r.get("Kind") == "thanks" else ""
        reply_to = re.search(r"<[^>]+>", parent).group(0) if re.search(r"<[^>]+>", parent) else (mid(r["Key"], 1) if bump else None)
        print(f" -> referral {r['Kind']}{' bump' if bump else ''} to {r['Email']} ({r.get('App')}): {subj}")
        if live:
            try:
                oe.smtp_send(env, r["Email"], subj, body, mid=mid(r["Key"], step), reply_to_mid=reply_to)
            except oe.smtplib.SMTPRecipientsRefused:
                upd(env, r["Key"], {"Status": "Bounced"}, preview); continue
            except Exception as e:
                print(f"    send failed: {e}"); break
        else:
            print("    " + body.replace("\n", "\n    ")[:600])
        f = {"Sent on": today, "Step": str(step)}
        if r.get("Kind") == "intro":
            f.update({"Status": "Sent" if step == 1 else "Sequence done",
                      "Next touch": (now.date() + dt.timedelta(days=4)).isoformat() if step == 1 else ""})
        else:
            f["Status"] = "Sent"
        upd(env, r["Key"], f, not live)


# ---------- 3. replies ----------
REPLY = """Francis (Yorelore: realistic AI-creator UGC video ads for apps, labelled as AI, demo-style, free 15-second sample offer)
wrote to {who} at {app}. {context}
What {app} does (facts you may use): {one}
Their reply:
{text}

Return JSON:
"type": "referral" (they named someone or gave an address to contact), "self" (they are the right person and are open to it),
"no" (not interested / stop), "auto" (auto-reply, ticket bot, out of office), or "other";
"summary" (max 20 words);
"referred_name", "referred_role", "referred_email" ("" when not given; copy exactly);
"insights" (anything said about what the company or that person is focused on, problems, or how to approach them; "" if none);
"thanks" (Francis's reply to {first}: max 45 words, warm, specific, says he'll mention them; "" unless type is referral);
"intro_subject" (lowercase, max 6 words; "" unless referral);
"intro_body" (Francis's first email to the referred person, "" unless referral: start "Hi <their first name>,", say {first}
   {intro_from} suggested reaching out, use the insights for ONE specific angle (never invent facts), offer to make a free
   15-second sample ad on their real app screens with a realistic AI creator labelled as AI, and name ONE concrete thing the
   sample would show (a real feature from the facts above). End with one easy yes/no question. Under 100 words, no signature,
   no links, no corporate words like "collaboration opportunities" or "synergy");
"self_draft" (Francis's answer if type is self: under 70 words, offers the sample and one question; else "")."""


def guess_from(name, ref_email, insider_name, insider_email, domain):
    if ref_email:
        return ref_email.lower(), "given"
    pat = pattern_of(insider_name, insider_email) if insider_name else ""
    return make_email(name, domain, pat or "first"), f"guessed: {pat or 'first'}"


def queue_referral(env, ai, app, lead_key, country, domain, by_name, by_email, parent_mid, subj, preview, now):
    """Queue the thank-you (threaded to them) + the intro to the person they named."""
    first = name_parts(by_name)[0].title() if by_name else ""
    name = ai.get("referred_name") or ""
    addr, basis = guess_from(name, ai.get("referred_email"), by_name, by_email, domain)
    recs = []
    if ai.get("thanks") and first:
        recs.append({"Key": f"thanks:{lead_key}:{by_email}", "Kind": "thanks", "App": app, "Lead key": lead_key, "Country": country,
                     "Domain": domain, "Name": by_name, "Email": by_email, "Status": "Queued", "Queued on": stamp(now),
                     "Subject": re.sub(r"^(re:\s*)+", "", subj, flags=re.I), "Body": ai["thanks"], "Notes": parent_mid})
    if addr and ai.get("intro_body"):
        recs.append({"Key": f"intro:{lead_key}:{addr}", "Kind": "intro", "App": app, "Lead key": lead_key, "Country": country,
                     "Domain": addr.split("@")[-1], "Name": name, "Title": ai.get("referred_role", ""), "Email": addr,
                     "Email basis": basis, "Status": "Queued", "Queued on": stamp(now),
                     "Subject": (ai.get("intro_subject") or f"{app.lower()} video ads").lower(), "Body": ai["intro_body"],
                     "Step": "0", "Referred by": f"{by_name or 'their team inbox'} <{by_email}>", "Insights": ai.get("insights", "")})
    add(env, recs, preview)
    if recs:
        oe.alert(env, f"🤝 {app}: referral to {name or addr} - intro queued (veto within {VETO_MIN} min)",
                 f"Referred by: {by_name or by_email}\nWhat they told us: {ai.get('insights') or '-'}\n\n"
                 + "\n\n".join(f"[{r['Kind'].upper()}] to {r['Name'] or ''} <{r['Email']}>"
                               f"{' (' + r['Email basis'] + ')' if r.get('Email basis') else ''}\nSubject: {r['Subject']}\n{r['Body']}" for r in recs)
                 + f"\n\nTo stop either, set Status = Hold in the Referrals tab: {oe.SHEET_URL}", preview)
    return recs


def lead_row(env, key):
    try:
        return next((r for r in oe.read_rows(env) if r.get("Key") == key), {}) if key else {}
    except Exception:
        return {}


def handle_reply(env, r, frm, subj, text, msg_id_hdr, when_s, preview):
    """A reply to one of OUR referral emails (ask or intro)."""
    now = oe.wat_now()
    if (r.get("Reply date") or "") >= when_s:
        return False
    if r.get("Kind") == "intro":     # the referred person answered: this is a hot lead -> Francis
        ai = ai_obj(oe.CLASSIFY.format(app=r.get("App", ""), name=r.get("Name", ""), subject=subj, text=text), env)
        kind = ai.get("type", "other")
        upd(env, r["Key"], {"Status": "Do not contact" if kind in ("not_interested", "unsubscribe") else "Replied",
                            "Reply date": when_s, "Reply type": kind, "Reply summary": ai.get("summary", "")}, preview)
        if kind in ("not_interested", "unsubscribe"):
            oe.add_dnc(env, r["Email"], r.get("App", ""), f"referral intro replied: {kind}", preview)
        elif kind != "out_of_office":
            oe.alert(env, f"🔥 {r.get('App')}: {r.get('Name') or frm} replied to the referral intro ({kind})",
                     f"THEIR REPLY:\n{text}\n\nDRAFTED ANSWER (edit, then send from your inbox):\n{ai.get('draft','')}\n\n"
                     f"Referred by: {r.get('Referred by')}\nSheet: {oe.SHEET_URL}", preview)
        print(f" referral intro reply from {frm} ({r.get('App')}): {kind}")
        return True
    first = name_parts(r.get("Name"))[0].title()
    lead = lead_row(env, r.get("Lead key"))
    ai = ai_obj(REPLY.format(who=r.get("Name") or frm, app=r.get("App", ""), first=first or "them", intro_from=f"at {r.get('App','')}",
                             one=lead.get("One-liner", ""), context="Francis asked who handles growth / video ads and what they're focused on.",
                             text=text), env)
    kind = ai.get("type", "other")
    upd(env, r["Key"], {"Status": {"referral": "Referred", "self": "Replied", "no": "Do not contact", "auto": "Sent"}.get(kind, "Replied"),
                        "Reply date": when_s, "Reply type": kind, "Reply summary": ai.get("summary", ""),
                        "Insights": ai.get("insights", "")}, preview)
    print(f" referral ask reply from {frm} ({r.get('App')}): {kind}")
    if kind == "referral":
        queue_referral(env, ai, r.get("App", ""), r.get("Lead key", ""), r.get("Country", ""), r.get("Domain", ""),
                       r.get("Name", ""), r.get("Email", ""), msg_id_hdr or mid(r["Key"], 1), r.get("Subject", ""), preview, now)
    elif kind == "self":
        oe.alert(env, f"🔥 {r.get('App')}: {r.get('Name')} says they're the right person",
                 f"THEIR REPLY:\n{text}\n\nDRAFTED ANSWER (edit, then send from your inbox):\n{ai.get('self_draft','')}\n\nSheet: {oe.SHEET_URL}", preview)
    elif kind == "no":
        oe.add_dnc(env, r["Email"], r.get("App", ""), "referral ask: no", preview)
    return True


def from_lead_reply(env, lead, frm, subj, text, msg_id_hdr, preview):
    """The app's shared inbox said 'contact X instead': thank them + write to X. Returns True if a referral was queued."""
    ai = ai_obj(REPLY.format(who=f"the shared inbox {frm}", app=lead.get("App", ""), first="the team",
                             intro_from=f"from the {lead.get('App','')} team", one=lead.get("One-liner", ""),
                             context="Francis offered a free sample ad; the inbox may have pointed him to someone else.", text=text), env)
    if ai.get("type") != "referral":
        return False
    ai["thanks"] = ai.get("thanks") or ""
    if ai["thanks"]:
        ai["thanks"] = re.sub(r"^Hi [^,\n]+,", "Hi there,", ai["thanks"].strip())
    recs = queue_referral(env, ai, lead.get("App", ""), lead["Key"], lead.get("Country", ""), frm.split("@")[-1],
                          "the team", frm, msg_id_hdr or oe.msg_id(lead, 1), lead.get("Email subject", ""), preview, oe.wat_now())
    return bool(recs)


def handle_bounce(env, r, preview):
    """A guessed insider email bounced: try the next pattern (max 3 tries), else give up."""
    tried = [x for x in re.findall(r"guessed: ([\w.]+)", r.get("Email basis", ""))]
    if r.get("Kind") not in ("ask", "intro") or "given" in (r.get("Email basis") or "") or len(tried) >= 3:
        upd(env, r["Key"], {"Status": "Bounced"}, preview); return
    nxt = next((p for p in PATTERNS if p not in tried and make_email(r.get("Name"), r["Domain"], p)), None)
    if not nxt:
        upd(env, r["Key"], {"Status": "Bounced"}, preview); return
    upd(env, r["Key"], {"Email": make_email(r["Name"], r["Domain"], nxt), "Email basis": r["Email basis"] + f", guessed: {nxt}",
                        "Status": "Queued", "Queued on": stamp(oe.wat_now() - dt.timedelta(minutes=VETO_MIN))}, preview)
    print(f" referral bounce {r['Email']} -> retry as {nxt}")


def stats(env, day):
    try:
        refs = rows_(env)
    except Exception:
        return ""
    sent = [r for r in refs if r.get("Sent on") == day]
    return (f"Referral track today: {sum(r.get('Kind') == 'ask' for r in sent)} asks, {sum(r.get('Kind') == 'intro' for r in sent)} intros, "
            f"{sum(r.get('Kind') == 'thanks' for r in sent)} thank-yous sent | {sum((r.get('Reply date') or '').startswith(day) for r in refs)} referral replies")
