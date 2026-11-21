"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views.

Two ways clips go out:
- One by one: each good clip is queued as soon as it is made, with a gap between posts and a daily cap.
- At set times (Settings: posting times, the usual way once it is set up): finished clips wait in a pool, and at
  each posting time the best one is picked. The pick skips anything too like what was already posted, spreads the
  day across creators (at most 2 a day each, never the same creator twice in a row), and during the approval
  period only takes clips you pressed Approve on."""
import difflib
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, auth, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def enqueue(job, clip, account_ids, now=False, slot=None, at=None) -> list:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            due = float(at) if at else time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None,
                 "text": _words(clip)[:800], "start": clip.get("start"), "end": clip.get("end"), "slot": slot}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if accounts.settings()["schedule_on"]:
        return  # posting at set times: the clip waits in the pool and is picked at a posting time
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


# ---------------------------------------------------------------- posting at set times
PER_CREATOR_DAY = 2          # most posts from one creator on one account in a day
PRIORITY_DAY = 3             # ... for a creator marked as a priority
PRIORITY_BONUS = 5           # a priority creator's clip counts this many points higher when choosing what to post
FRESH_HOURS = 48             # older clips are not posted by the schedule (the moment has passed)
SAME_WORDS = 0.6             # this alike in what was said: the same moment, skip it
SAME_TITLE = 0.8             # this alike in title: the same story, skip it
REPEAT_DAYS = 30
SLOT_WINDOW = 50 * 60        # a posting time still counts this long after it, if nothing could go out on time


def _words(clip) -> str:
    return " ".join(w.get("w", "") for w in clip.get("fixed_words") or []) or (clip.get("description") or "")


def _alike(a, b) -> float:
    a, b = (a or "").lower(), (b or "").lower()
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _tz():
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(accounts.settings()["post_tz"])
    except Exception:  # noqa: BLE001
        return ZoneInfo("America/New_York")


def slot_times(day_start: float = None) -> list:
    """Today's posting times as (key, unix time), in the chosen time zone."""
    from datetime import datetime
    tz = _tz()
    d = datetime.fromtimestamp(day_start or time.time(), tz)
    out = []
    for hm in accounts.settings()["post_times"].split(","):
        h, m = map(int, hm.split(":"))
        t = d.replace(hour=h, minute=m, second=0, microsecond=0)
        out.append((t.strftime("%Y-%m-%d ") + hm, t.timestamp()))
    return out


def _local_day(t) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(t, _tz()).strftime("%Y-%m-%d")


def is_repeat(clip, job_id, posted) -> str:
    """Why this clip would repeat something already posted (empty if it would not)."""
    words, title = _words(clip), clip.get("title") or ""
    for p in posted:
        if p["job"] == job_id and p.get("start") is not None and clip.get("start") is not None \
                and clip["start"] < p["end"] and p["start"] < clip["end"]:
            return "overlaps a posted clip from the same video"
        if _alike(words, p.get("text")) >= SAME_WORDS:
            return "says the same as a posted clip"
        if _alike(title, p.get("title")) >= SAME_TITLE:
            return "has nearly the same title as a posted clip"
    return ""


def pool(account_id=None, now=None) -> list:
    """Finished clips that could be posted at a posting time, best first: [(job, clip, why_not)]. why_not is ""
    for a clip that can go, else the reason it is held back (needs approval, a repeat, ...)."""
    now = now or time.time()
    s = accounts.settings()
    need_ok = bool(s.get("auto_post")) and now < float(s.get("approve_until") or 0)   # only automatic posting asks for Approve
    mine = _mine()
    recent = [p for p in mine if p["status"] in OPEN + ("posted",) and (p.get("posted_at") or p["due_at"]) > now - REPEAT_DAYS * 86400]
    out = []
    for j in jobs.list_jobs():
        c = creators.get(j.get("creator"))
        if not c or not c["autopost"] or not c["accounts"] or (account_id and account_id not in c["accounts"]):
            continue
        for k in j["clips"]:
            if k.get("status") != "done" or k.get("pool") == "skipped":
                continue
            if now - (k.get("made_at") or j["created"]) > FRESH_HOURS * 3600 or k.get("score", 0) < c["post_min_score"]:
                continue
            if any(p["job"] == j["id"] and p["clip"] == k["id"] for p in mine if p["status"] in OPEN + ("posted",)):
                continue
            why = is_repeat(k, j["id"], recent)
            if not why and need_ok and k.get("pool") != "approved":
                why = "waiting for your Approve"
            out.append((j, k, why))
    bonus = {c["id"]: PRIORITY_BONUS for c in creators.all_creators() if c.get("priority")}
    out.sort(key=lambda x: (-((x[1].get("score") or 0) + bonus.get(x[0].get("creator"), 0)), -(x[1].get("made_at") or 0)))
    return out


def pick(account_id, now=None):
    """The clip to post on this account now, or None. Spreads the day across creators."""
    now = now or time.time()
    today = _local_day(now)
    done = sorted((p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)),
                  key=lambda p: p.get("posted_at") or p["due_at"])
    per = {}
    for p in done:
        if _local_day(p.get("posted_at") or p["due_at"]) == today:
            per[p["creator"]] = per.get(p["creator"], 0) + 1
    last = done[-1]["creator"] if done else None
    cap = {c["id"]: PRIORITY_DAY for c in creators.all_creators() if c.get("priority")}
    ok = [(j, k) for j, k, why in pool(account_id, now) if not why and per.get(j["creator"], 0) < cap.get(j["creator"], PER_CREATOR_DAY)]
    fresh = [(j, k) for j, k in ok if j["creator"] != last]
    return (fresh or ok or [None])[0]


def run_slots(now=None) -> list:
    """Post at any posting time that has come. Called every few seconds; each time is used once per account."""
    now = now or time.time()
    s = accounts.settings()
    if not s["schedule_on"] or not s.get("auto_post") or s["paused"]:
        return []                             # manual posting: you choose when each clip goes out
    made = []
    targets = {a for c in creators.all_creators() if c["autopost"] for a in c["accounts"]}
    for key, t in slot_times(now):
        if not t <= now < t + SLOT_WINDOW:
            continue
        for aid in targets:
            if any(p.get("slot") == key and p["account"] == aid for p in _mine()):
                continue                      # this posting time has been used on this account
            got = pick(aid, now)
            if not got:
                continue                      # nothing good enough yet: tried again until the window closes
            job, clip = got
            made += enqueue(job, clip, [aid], now=True, slot=key)
            creators.log(job["creator"], f"Posting at {key[-5:]}: {clip.get('title') or clip.get('hook')}")
    return made


def set_pool(job_id, clip_id, action):
    """Approve a clip for the posting times, Skip it (never posted by the schedule), or put it back."""
    job = jobs.get(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None) if job else None
    if not clip:
        raise KeyError(clip_id)
    if action not in ("approve", "skip", "reset"):
        raise ValueError("Unknown action.")
    if action == "reset":
        clip.pop("pool", None)
    else:
        clip["pool"] = "approved" if action == "approve" else "skipped"
    jobs.save(job)
    ctx.bump()
    return clip


def next_times(n=6, now=None) -> list:
    """The next few posting times, for the buttons beside each clip: [{at, label, taken}]. taken: a post is
    already set for about that time."""
    now = now or time.time()
    from datetime import datetime
    tz = _tz()
    today = datetime.fromtimestamp(now, tz).date()
    out = []
    for d in range(3):
        for _key, t in slot_times(now + d * 86400):
            if t > now + 60 and len(out) < n:
                day = datetime.fromtimestamp(t, tz).date()
                word = "Today" if day == today else "Tomorrow" if (day - today).days == 1 else datetime.fromtimestamp(t, tz).strftime("%a")
                taken = any(p["status"] in OPEN and abs(p["due_at"] - t) < 600 for p in _mine())
                out.append({"at": t, "label": f"{word} {datetime.fromtimestamp(t, tz).strftime('%H:%M')}", "taken": taken})
    return out


def schedule_clip(job_id, clip_id, at) -> list:
    """You chose when this clip goes out: at a posting time, a time you picked, or now ("now")."""
    job = jobs.get(job_id)
    clip = next((c for c in job["clips"] if c["id"] == clip_id), None) if job else None
    if not clip or clip.get("status") != "done":
        raise KeyError(clip_id)
    now = time.time()
    when = now if at == "now" else float(at)
    if when < now - 120 or when > now + 30 * 86400:
        raise ValueError("Choose a time from now up to 30 days ahead.")
    c = creators.get(job.get("creator")) or {}
    targets = c.get("accounts") or [a["id"] for a in accounts.list_accounts() if a["platform"] == "youtube"]
    if not targets:
        raise ValueError("Connect your YouTube channel first (Settings, Connected apps).")
    made = enqueue(job, clip, targets, at=max(when, now))
    if not made:
        raise ValueError("This clip is already set to post, or was posted.")
    when_txt = "now" if at == "now" else time.strftime("%d %b %H:%M", time.localtime(when))
    creators.log(job["creator"], f"Set to post {when_txt}: {clip.get('title') or clip.get('hook')}")
    ctx.bump()
    return made


def pool_view(limit=30) -> list:
    out = []
    for j, k, why in pool()[:limit]:
        out.append({"job": j["id"], "clip": k["id"], "creator": j["creator"], "title": k.get("title") or k.get("hook") or "Clip",
                    "hook": k.get("hook") or "", "score": k.get("score", 0), "file": k.get("file"), "cover": k.get("cover"),
                    "why": why, "approved": k.get("pool") == "approved"})
    return out


def _handle(creator) -> str:
    """The creator's YouTube @handle (for the credit line), or their name."""
    import re
    m = re.search(r"youtube\.com/(@[\w.\-]+)", (creator.get("channels") or {}).get("youtube", ""))
    return m.group(1) if m else creator.get("name", "")


def hashtags(clip) -> list:
    """#Shorts first, then the clip's own tags: at most 5, no repeats."""
    out = ["Shorts"]
    for h in clip.get("hashtags", []):
        h = "".join(ch for ch in str(h) if ch.isalnum() or ch == "_")
        if h and h.lower() not in {x.lower() for x in out}:
            out.append(h)
    return out[:5]


def _caption(clip, creator):
    tags = " ".join("#" + h for h in hashtags(clip))
    credit = f"🎥 Credit: {_handle(creator)}" if creator.get("name") else ""
    return "\n\n".join(x for x in (clip.get("description", ""), credit, creator.get("post_footer", ""), tags) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            cover = path.with_suffix(".jpg")
            rid, url = accounts.youtube_upload(acc, path, title, cap, hashtags(clip)[1:] + [creator.get("name", "")],
                                               creator["privacy"], creator.get("category", "24"), cover)
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        for uid in auth.user_ids() if config.ACCOUNTS else [None]:
            try:
                with ctx.as_user(uid or auth.local_user().id):
                    run_slots()
            except Exception:  # noqa: BLE001 - one account's schedule failing never stops the others
                pass
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    s = accounts.settings()
    return {"paused": s["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine()),
            "schedule": {"on": s["schedule_on"], "auto": bool(s.get("auto_post")), "times": s["post_times"].split(","), "tz": s["post_tz"],
                         "approve_until": s["approve_until"], "next": next((k for k, t in slot_times() if t > time.time()), ""),
                         "presets": next_times() if s["schedule_on"] else []},
            "pool": [dict(x, creator_name=names.get(x["creator"], "")) for x in pool_view()] if s["schedule_on"] else []}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
