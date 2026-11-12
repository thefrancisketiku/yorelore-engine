"""
Yorelore Outreach Engine: sends the cold-email sequence to the sheet's "SaaS Client" tab, watches replies, alerts Francis.
Runs on GitHub Actions every 30 min on weekdays (cloud: works with the laptop off). One command does everything due:

  python outreach_engine.py            # = replies + (queue at 08:00-11:00 WAT) + (send inside each lead's window)
  python outreach_engine.py --queue    # force just one step   (--send, --replies)
  python outreach_engine.py --preview  # print what would be queued/sent; changes nothing

Rules (Francis, 11 Nov 2026):
  * "Send unless I veto": 08:00 WAT the day's batch is marked Status=Queued and emailed to Francis.
    Anything he sets to Status=Hold by 13:00 WAT is skipped. Nothing sends before 13:00 WAT.
  * Sequence (emails only): day 0 Email 1 (from the sheet) -> day 3 bump -> day 7 cost stack + Loom -> day 14 breakup.
  * Any reply stops the sequence. "No / unsubscribe" -> Do Not Contact. Bounces -> Bounced. Out-of-office -> +7 days.
  * Interested replies are NEVER auto-answered: Francis gets an alert with the reply and a drafted answer.
  * ENGINE_LIVE=1 must be set or nothing is actually sent (safe by default).
"""
import argparse, datetime as dt, email, email.utils, hashlib, imaplib, json, os, random, re, smtplib, ssl, sys, time
from email.mime.text import MIMEText
from email.header import decode_header, make_header

import lead_finder as lf

sys.stdout.reconfigure(errors="replace", line_buffering=True)

TAB = "SaaS Client"
DNC_TAB = "Do Not Contact"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1S1duNcYyiz4waiPyhufH7s8PN-xvKZ052lsPC32SSX4/edit"
LOOM = "https://www.loom.com/share/6eb32e0bdb3843f8aff6581df35078bd"
DOMAIN = "francisketiku.com"
SIGNATURE = ("Francis Ketiku\nFounder, Yorelore · francisketiku.com\n17 Half Moon Street, Huddersfield HD1 2JN, UK\n\n"
             'Not for you? Reply "no thanks" and I won\'t email again.')
DAILY_CAP = int(os.environ.get("DAILY_CAP", "20"))     # new + follow-ups per day, per inbox (raise slowly)
MAX_PER_RUN = 6                                        # a run every 30 min -> spreads sends out naturally
MIN_SCORE = 3
GAP = (35, 110)                                        # seconds between sends inside one run
STEP_DAYS = {1: 3, 2: 4, 3: 7}                         # after step N, wait this many days (day 0 -> 3 -> 7 -> 14)
EXTRA_COLS = ["Reply date", "Reply type", "Reply summary", "Drafted answer", "Queued on"]
BAD_LOCAL = re.compile(r"^(cancel|unsubscribe|billing|invoices?|accounts?payable|legal|privacy|dpo|gdpr|compliance|security|abuse|careers?|jobs|hr|recruiting|noreply|no-reply|donotreply|returns|refunds|orders|carlsagan|example|test)\b", re.I)
STOP = {"replied", "hold", "lost", "do not contact", "bounced", "sequence done", "won", "call booked", "sample sent"}

# send windows in WAT (UTC+1), by lead country: inside each lead's working morning, never before the 13:00 veto
WINDOWS = {"US": (13, 18), "CA": (13, 18), "?": (13, 18), "": (13, 18), "GB": (13, 16), "IE": (13, 16),
           "AU": (22, 24), "NZ": (22, 24)}


def wat_now():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)


def env_all():
    env = lf.load_env()
    for k in ("SMTP_USER", "SMTP_PASS", "SMTP_HOST", "IMAP_HOST", "ALERT_TO", "ENGINE_LIVE", "FROM_NAME",
              "SERPER_API_KEY", "GEMINI_API_KEY", "SHEETS_WEBHOOK_URL", "SHEETS_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.setdefault("SMTP_HOST", "smtppro.zoho.com")   # paid Zoho org accounts; free/personal = smtp.zoho.com
    env.setdefault("IMAP_HOST", "imappro.zoho.com")
    env.setdefault("FROM_NAME", "Francis Ketiku")
    return env


# ---------- sheet ----------
def read_rows(env):
    v = lf.sheet_call(env, {"action": "read", "tab": TAB}).get("values") or []
    if not v:
        return []
    hdr = v[0]
    return [dict(zip(hdr, r)) for r in v[1:] if any(str(x).strip() for x in r)]


def ensure_columns(env):
    lf.sheet_call(env, {"action": "append", "tab": TAB, "header": EXTRA_COLS, "rows": []})


def update(env, updates, preview):
    if not updates:
        return
    if preview:
        for u in updates:
            print(f"   (preview) {u['key']}: {u['fields']}")
        return
    res = lf.sheet_call(env, {"action": "update", "tab": TAB, "updates": updates})
    print(f"   sheet updated: {res.get('updated')} rows")


def dnc_set(env):
    try:
        return {str(x).strip().lower().lstrip("@") for x in lf.sheet_call(env, {"action": "dnc"}).get("values", []) if str(x).strip()}
    except Exception:
        return set()


def add_dnc(env, email_addr, brand, reason, preview):
    if preview:
        print(f"   (preview) Do Not Contact += {email_addr} ({reason})"); return
    lf.sheet_call(env, {"action": "append", "tab": DNC_TAB, "header": ["Domain / handle / email", "Brand", "Date", "Reason"],
                        "rows": [[email_addr, brand, dt.date.today().isoformat(), reason]]})


def blocked(r, dnc):
    e = (r.get("Email") or "").lower()
    dom = e.split("@")[-1]
    site = lf.root_domain(r.get("Website") or "") if r.get("Website") else ""
    return bool(e) and (e in dnc or dom in dnc or (site and site in dnc))


def as_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def step_of(r):
    try:
        return int(str(r.get("Sequence step") or 0).split(".")[0])
    except ValueError:
        return 0


def first_name(r):
    n = (r.get("Founder / owner") or "").strip().split()
    return n[0] if n and n[0].isalpha() and len(n[0]) > 1 else "there"


# ---------- email copy ----------
def msg_id(r, step):
    h = hashlib.sha1(r["Key"].encode()).hexdigest()[:12]
    return f"<yl-{h}-{step}@{DOMAIN}>"


def compose(r, step):
    """step = the step being sent (1..4). Returns (subject, body)."""
    app, fn = r.get("App") or "your app", first_name(r)
    subj = (r.get("Email subject") or f"{app.lower()} ad idea").strip()
    if step == 1:
        body = (r.get("Email body (ready to send)") or "").strip()
        if not body:
            return None, None
        body = re.sub(r"^((?:Hi|Hey|Hello) [^,\n]{1,40},)[ \t]+", r"\1\n\n", body)   # greeting on its own line
        return subj, body
    re_subj = "Re: " + subj
    if step == 2:
        return re_subj, (f"Hi {fn},\n\nJust floating this up in case it got buried. Happy to make the {app} sample this week. "
                         f"A yes or no is totally fine.")
    if step == 3:
        return re_subj, (f"Hi {fn},\n\nOne number that might be useful: creator UGC for apps usually runs $150-400 a video, and most "
                         f"apps need 10+ new hooks a month to find one that brings down cost per install.\n\n"
                         f"We do that with realistic AI creators on your real app screens, labelled as AI, first batch in 48 hours. "
                         f"The free {app} sample is still on the table. Want it?\n\n"
                         f"P.S. If you'd rather see how it works first, here's a 3-minute walkthrough: {LOOM}")
    month = (dt.date.today() + dt.timedelta(days=60)).strftime("%B")
    return re_subj, (f"Hi {fn},\n\nI'll stop here, I don't want to be that guy. If ads aren't a priority right now, no stress. "
                     f"Should I check back in {month}? And if someone else handles growth at {app}, I'd love a name.")


def smtp_send(env, to, subject, body, mid=None, reply_to_mid=None, sign=True):
    m = MIMEText(body + ("\n\n" + SIGNATURE if sign else ""), "plain", "utf-8")
    m["From"] = email.utils.formataddr((env["FROM_NAME"], env["SMTP_USER"]))
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = email.utils.formatdate(localtime=False)
    m["Message-ID"] = mid or email.utils.make_msgid(domain=DOMAIN)
    if reply_to_mid:
        m["In-Reply-To"] = reply_to_mid
        m["References"] = reply_to_mid
    with smtplib.SMTP_SSL(env["SMTP_HOST"], 465, context=ssl.create_default_context(), timeout=60) as s:
        s.login(env["SMTP_USER"], env["SMTP_PASS"])
        s.sendmail(env["SMTP_USER"], [to], m.as_string())


def alert(env, subject, body, preview):
    """Email Francis (his own inbox). Falls back to printing."""
    print(f"   ALERT: {subject}")
    if preview or not env.get("ALERT_TO") or not env.get("SMTP_PASS") or env.get("ENGINE_LIVE") != "1":
        print("   " + body.replace("\n", "\n   ")[:1500]); return
    smtp_send(env, env["ALERT_TO"], subject, body, sign=False)


# ---------- 1. queue (08:00-11:00 WAT) ----------
def due_rows(rows, dnc, today):
    new, follow = [], []
    for r in rows:
        st = (r.get("Status") or "New").strip().lower()
        if st in STOP or not r.get("Email") or BAD_LOCAL.match(r["Email"]) or blocked(r, dnc) or (r.get("Replied? (Y/N)") or "").upper().startswith("Y"):
            continue
        step = step_of(r)
        if step == 0 and st in ("new", "queued"):
            try:
                score = int(float(r.get("Qualify score (0-6)") or 0))
            except ValueError:
                score = 0
            if score >= MIN_SCORE and (r.get("Email body (ready to send)") or "").strip():
                new.append(r)
        elif 1 <= step <= 3:
            nt = as_date(r.get("Next touch"))
            if nt and nt <= today:
                follow.append(r)
    pri = lambda r: (0 if (r.get("Audience") or "").lower() == "consumer" else 1 if not r.get("Audience") else 2,
                     0 if (r.get("Niche") or "") in ("health", "relationships", "money") else 1)
    new.sort(key=lambda r: (*pri(r), -int(float(r.get("Qualify score (0-6)") or 0)), r.get("Date added", "")))
    return new, follow


def sent_today(rows, today):
    return sum(1 for r in rows if as_date(r.get("Last touch")) == today)


def do_queue(env, rows, preview):
    today = wat_now().date()
    dnc = dnc_set(env)
    new, follow = due_rows(rows, dnc, today)
    already = [r for r in rows if (r.get("Status") or "").strip().lower() == "queued"]
    room = max(0, DAILY_CAP - sent_today(rows, today) - len(already))
    pick = [r for r in follow if r not in already][:room]          # follow-ups first: they're warmer
    pick += [r for r in new if r not in already and (r.get("Status") or "").strip().lower() != "queued"][:max(0, room - len(pick))]
    print(f"Queue: {len(pick)} new to queue (+{len(already)} already queued) | due follow-ups {len(follow)}, new leads {len(new)}, cap {DAILY_CAP}")
    stamp = wat_now().strftime("%Y-%m-%d %H:%M")
    update(env, [{"key": r["Key"], "fields": {"Status": "Queued", "Queued on": stamp}} for r in pick], preview)
    allq = already + pick
    if allq:
        lines = [f"{i+1}. {r.get('App','')} ({r.get('Country','?')}) -> {r.get('Email','')} | step {step_of(r)+1}"
                 f"{' | FLAG: ' + r['Compliance flag'] if r.get('Compliance flag') else ''}" for i, r in enumerate(allq)]
        score = []
        for v in ("A", "C"):
            sent = [r for r in rows if r.get("Variant") == v and r.get("Date touch 1")]
            rep = [r for r in sent if (r.get("Replied? (Y/N)") or "").upper().startswith("Y")]
            pos = [r for r in rep if (r.get("Reply type") or "") in ("interested", "question")]
            score.append(f"{v}: {len(sent)} sent, {len(rep)} replies ({len(rep) / len(sent):.0%}), {len(pos)} interested"
                         if sent else f"{v}: none sent yet")
        late = wat_now().hour >= 13
        alert(env, f"Yorelore: {len(allq)} emails queued for {'TOMORROW' if late else 'today'} (veto by 13:00 WAT{' tomorrow' if late else ''})",
              "These send from 13:00 WAT (US/UK) and 22:00 WAT (AU).\nTo stop one: set its Status to Hold in the "
              f"SaaS Client tab.\n{SHEET_URL}\n\nA/B test so far -> " + " | ".join(score) + "\n\n" + "\n".join(lines), preview)


# ---------- 2. send (inside each lead's window, never before 13:00 WAT) ----------
def do_send(env, rows, preview, force=False):
    now = wat_now()
    today = now.date()
    if now.weekday() >= 5 and not force:
        print("Send: weekend, nothing sends."); return
    live = env.get("ENGINE_LIVE") == "1" and not preview
    if live and not (env.get("SMTP_USER") and env.get("SMTP_PASS")):
        print("Send: SMTP_USER / SMTP_PASS missing, nothing sent."); return
    dnc = dnc_set(env)
    left = DAILY_CAP - sent_today(rows, today)
    def vetoable_passed(r):
        """Francis must have had until 13:00 WAT to veto: queued on an earlier day, or today before 13:00."""
        qo = (r.get("Queued on") or "").strip()
        if not qo:
            return True                       # queued before this rule existed
        d, _, t = qo.partition(" ")
        return d < today.isoformat() or (d == today.isoformat() and t < "13:00")
    q = [r for r in rows if (r.get("Status") or "").strip().lower() == "queued" and not blocked(r, dnc) and (force or vetoable_passed(r))]
    ready = [r for r in q if force or WINDOWS.get((r.get("Country") or "").upper().rstrip("?"), (13, 18))[0] <= now.hour
             < WINDOWS.get((r.get("Country") or "").upper().rstrip("?"), (13, 18))[1]]
    batch = ready[:max(0, min(MAX_PER_RUN, left))]
    print(f"Send: {len(q)} queued, {len(ready)} in window now, sending {len(batch)} (left today {left})"
          f"{'' if live else '  [TEST MODE: ENGINE_LIVE is not 1, nothing really sends]'}")
    for i, r in enumerate(batch):
        step = step_of(r) + 1
        subj, body = compose(r, step)
        if not subj:
            update(env, [{"key": r["Key"], "fields": {"Status": "New", "Notes": (r.get("Notes", "") + " | no email body").strip(" |")}}], preview)
            continue
        print(f" -> step {step} to {r['Email']} ({r.get('App')}) : {subj}")
        if live:
            try:
                smtp_send(env, r["Email"], subj, body, mid=msg_id(r, step), reply_to_mid=msg_id(r, 1) if step > 1 else None)
            except smtplib.SMTPRecipientsRefused:
                update(env, [{"key": r["Key"], "fields": {"Status": "Bounced"}}], preview); continue
            except Exception as e:
                print(f"    send failed: {e}"); break     # stop the run; the row stays Queued for the next run
        else:
            print("    " + (body + "\n\n" + SIGNATURE).replace("\n", "\n    ")[:900])
        fields = {"Sequence step": step, "Last touch": today.isoformat(),
                  "Status": "Sequence done" if step >= 4 else "Contacted"}
        if step == 1:
            fields["Date touch 1"] = today.isoformat()
        if step < 4:
            fields["Next touch"] = (today + dt.timedelta(days=STEP_DAYS[step])).isoformat()
        update(env, [{"key": r["Key"], "fields": fields}], not live)
        if i < len(batch) - 1 and live:
            time.sleep(random.randint(*GAP))


# ---------- 3. replies (every run) ----------
CLASSIFY = """Francis (Yorelore, AI UGC video ads for apps) cold-emailed this person offering a free 15-second sample ad.
Here is their reply. Classify it and draft Francis's answer.

App: {app}
Their name: {name}
Our last email subject: {subject}
Their reply:
{text}

Return JSON: "type" (one of "interested", "question", "not_interested", "unsubscribe", "out_of_office", "wrong_person", "other"),
"summary" (max 20 words), "draft" (Francis's reply, plain, under 90 words, warm and direct. If interested: say you'll make the sample,
ask ONE question (which feature or audience matters most), and offer a 15-min walkthrough at https://calendly.com/francisketiku/discovery-call
once it's ready. If question: answer only from these facts: realistic AI creators, labelled as AI, demo-style (never fake results),
first batch in 48 hours, pricing discussed on the call. If wrong person: thank them and ask who handles growth/marketing.
If not interested / unsubscribe / out of office: "" )."""


def body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return lf.html_to_text(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"), 4000)
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")


def strip_quoted(t):
    out = []
    for line in t.splitlines():
        if line.startswith(">") or re.match(r"^On .+wrote:$", line.strip()) or line.strip().startswith("-----Original"):
            break
        out.append(line)
    return "\n".join(out).strip()[:2500]


def do_replies(env, rows, preview):
    if not (env.get("SMTP_USER") and env.get("SMTP_PASS")):
        print("Replies: no inbox credentials yet, skipped."); return
    by_email = {r["Email"].lower(): r for r in rows if r.get("Email")}
    by_dom = {}
    for r in rows:
        if r.get("Email") and r["Email"].split("@")[-1] not in lf.FREE_MAIL:
            by_dom.setdefault(r["Email"].split("@")[-1].lower(), r)
    by_hash = {hashlib.sha1(r["Key"].encode()).hexdigest()[:12]: r for r in rows if r.get("Key")}
    since = (dt.date.today() - dt.timedelta(days=4)).strftime("%d-%b-%Y")
    im = imaplib.IMAP4_SSL(env["IMAP_HOST"], 993)
    im.login(env["SMTP_USER"], env["SMTP_PASS"])
    im.select("INBOX", readonly=True)
    _, ids = im.search(None, f'(SINCE "{since}")')
    seen_hits = 0
    for num in ids[0].split()[-200:]:
        _, data = im.fetch(num, "(BODY.PEEK[])")
        msg = email.message_from_bytes(data[0][1])
        frm = email.utils.parseaddr(msg.get("From", ""))[1].lower()
        subj = str(make_header(decode_header(msg.get("Subject", ""))))
        refs = f"{msg.get('In-Reply-To','')} {msg.get('References','')}"
        when = email.utils.parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
        when_s = when.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M") if when else dt.date.today().isoformat()
        text = body_text(msg)
        # bounces
        if re.match(r"(mailer-daemon|postmaster)@", frm) or "undeliverable" in subj.lower() or "delivery status" in subj.lower():
            h = re.search(r"yl-([0-9a-f]{12})-", text + refs)
            r = by_hash.get(h.group(1)) if h else next((by_email[e] for e in by_email if e in text.lower()), None)
            if r and (r.get("Status") or "").lower() != "bounced":
                update(env, [{"key": r["Key"], "fields": {"Status": "Bounced"}}], preview)
                print(f" bounce: {r['Email']}")
            continue
        h = re.search(r"yl-([0-9a-f]{12})-", refs)
        r = (by_hash.get(h.group(1)) if h else None) or by_email.get(frm) or by_dom.get(frm.split("@")[-1])
        if not r:
            continue
        if (r.get("Reply date") or "") >= when_s:
            seen_hits += 1
            continue                                   # already handled
        reply = strip_quoted(text)
        ai = lf.gemini(CLASSIFY.format(app=r.get("App", ""), name=r.get("Founder / owner", ""), subject=subj, text=reply),
                       env["GEMINI_API_KEY"]) or {}
        kind = ai.get("type", "other")
        fields = {"Replied? (Y/N)": "Y", "Reply date": when_s, "Reply type": kind, "Reply summary": ai.get("summary", ""),
                  "Drafted answer": ai.get("draft", "")}
        if kind in ("not_interested", "unsubscribe"):
            fields["Status"] = "Do not contact"
            add_dnc(env, r["Email"], r.get("App", ""), f"replied: {kind}", preview)
        elif kind == "out_of_office":
            fields.pop("Replied? (Y/N)")
            fields["Next touch"] = (dt.date.today() + dt.timedelta(days=7)).isoformat()
        else:
            fields["Status"] = "Replied"
        update(env, [{"key": r["Key"], "fields": fields}], preview)
        r.update(fields)
        print(f" reply from {frm} ({r.get('App')}): {kind}")
        if kind in ("interested", "question", "wrong_person", "other"):
            alert(env, f"🔥 {r.get('App')} replied ({kind}) - answer today",
                  f"From: {frm}\nApp: {r.get('App')}  |  {r.get('Website','')}\nSubject: {subj}\n\nTHEIR REPLY:\n{reply}\n\n"
                  f"DRAFTED ANSWER (edit, then send from your inbox):\n{ai.get('draft','')}\n\n"
                  + (f"IF THEY SAID YES: tell Claude \"make the sample for {r.get('App')}\". It runs sample_brief.py "
                     f"(script, prompts, real screenshots) so the 48-hour promise holds.\n\n" if kind == "interested" else "")
                  + f"Sheet: {SHEET_URL}", preview)
    im.logout()
    print(f"Replies: checked {len(ids[0].split())} messages since {since} ({seen_hits} already handled)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--replies", action="store_true")
    ap.add_argument("--preview", action="store_true", help="print only; change nothing")
    ap.add_argument("--force", action="store_true", help="ignore time windows (testing)")
    ap.add_argument("--test-mail", action="store_true", help="log in to SMTP + IMAP and send one test email to ALERT_TO")
    a = ap.parse_args()
    env = env_all()
    if a.test_mail:
        env.setdefault("ALERT_TO", "francisketiku@gmail.com")
        im = imaplib.IMAP4_SSL(env["IMAP_HOST"], 993)
        im.login(env["SMTP_USER"], env["SMTP_PASS"]); im.select("INBOX", readonly=True); im.logout()
        print("IMAP login ok")
        r = next((x for x in read_rows(env) if x.get("Email body (ready to send)")), None)
        subj, body = compose(r, 1) if r else ("Yorelore engine test", "Test.")
        smtp_send(env, env["ALERT_TO"], "[TEST - what a lead receives] " + subj, body)
        print(f"SMTP ok: test email sent to {env['ALERT_TO']}")
        return
    now = wat_now()
    print(f"Outreach engine | {now:%a %Y-%m-%d %H:%M} WAT | live={env.get('ENGINE_LIVE') == '1'} | cap {DAILY_CAP}/day")
    if not a.preview:
        ensure_columns(env)
    rows = read_rows(env)
    print(f"{len(rows)} rows in '{TAB}'")
    only = a.queue or a.send or a.replies
    if a.replies or not only:
        try:
            do_replies(env, rows, a.preview)
        except Exception as e:
            print(f"Replies check failed: {e}")
    weekday = now.weekday() < 5
    queued_today = any((r.get("Queued on") or "").startswith(now.date().isoformat()) for r in rows)
    if a.queue or (not only and weekday and now.hour >= 7 and not queued_today) or (a.preview and not only):
        do_queue(env, rows, a.preview)
        rows = read_rows(env) if not a.preview else rows
    if a.send or (not only and weekday and now.hour >= 13):
        do_send(env, rows, a.preview, force=a.force)
    if not only and not a.preview and weekday and now.hour >= 18:
        daily_report(env, now)


LOG_TAB = "Engine log"


def daily_report(env, now):
    """Once per weekday after 18:00 WAT: email Francis what actually happened today (proof the aim was met)."""
    day = now.date().isoformat()
    try:
        logged = lf.sheet_call(env, {"action": "read", "tab": LOG_TAB}).get("values") or []
    except Exception:
        logged = []
    if any(r and str(r[0]) == day for r in logged[1:]):
        return
    rows = read_rows(env)
    sent = [r for r in rows if r.get("Last touch") == day]
    new = [r for r in sent if str(r.get("Sequence step")) == "1"]
    reps = [r for r in rows if (r.get("Reply date") or "").startswith(day)]
    hot = [r for r in reps if r.get("Reply type") in ("interested", "question")]
    bounced = [r for r in rows if (r.get("Status") or "").lower() == "bounced" and r.get("Last touch") == day]
    queued = [r for r in rows if (r.get("Status") or "").lower() == "queued"]
    ab = []
    for v in ("A", "C"):
        s = [r for r in rows if r.get("Variant") == v and r.get("Date touch 1")]
        rp = [r for r in s if (r.get("Replied? (Y/N)") or "").upper().startswith("Y")]
        ab.append(f"{v}: {len(s)} sent / {len(rp)} replied")
    target = DAILY_CAP
    status = "TARGET MET" if len(sent) >= target else f"BELOW TARGET ({len(sent)}/{target})"
    body = (f"{status}\n\nSent today: {len(sent)} ({len(new)} first emails, {len(sent) - len(new)} follow-ups)\n"
            f"Replies today: {len(reps)} ({len(hot)} interested/questions)\nBounces today: {len(bounced)}\n"
            f"Still queued (AU tonight or tomorrow): {len(queued)}\nA/B so far: {' | '.join(ab)}\n\n"
            + ("Interested: " + ", ".join(r.get("App", "") for r in hot) + "\n\n" if hot else "")
            + f"Sheet: {SHEET_URL}")
    alert(env, f"Yorelore daily report {day}: {len(sent)} sent, {len(reps)} replies - {status}", body, False)
    lf.sheet_call(env, {"action": "append", "tab": LOG_TAB, "header": ["Date", "Sent", "First emails", "Replies", "Interested", "Bounces", "Status"],
                        "rows": [[day, len(sent), len(new), len(reps), len(hot), len(bounced), status]]})


if __name__ == "__main__":
    main()
