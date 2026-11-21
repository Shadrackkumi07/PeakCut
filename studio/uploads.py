"""Clip each creator's new videos by themselves.

For every creator with "Clip new uploads" switched on, PeakCut looks at their channel every few hours (only the
titles and lengths, nothing is downloaded). A video it has not seen before, long enough to hold a good moment,
becomes a normal video job, made with that creator's own settings.

Where a creator streams on Twitch or Kick, their past broadcasts there are used rather than YouTube: they are the
full streams, and YouTube often refuses downloads to servers in data centres. A past broadcast is only taken once
its length has stopped growing (the stream is over), and not when PeakCut already recorded that stream live.

The first look at a channel only remembers what is already there, so the whole back catalogue is never clipped.
At most a few videos per creator per day are taken, and nothing new is started while a lot is still waiting, so
the server never falls behind. What has been seen is kept in the creator's folder (uploads_seen.json)."""
import json
import threading
import time

from . import auth, config, creators, ctx, jobs, media

EVERY = 3600                 # how often each channel is looked at (a finished stream waits one more look, to be sure it is over)
MIN_SECONDS = 6 * 60         # shorter than this is a Short or a trailer: nothing worth clipping
PER_DAY = 2                  # new videos per creator per day
PRIORITY_PER_DAY = 3         # ... for a creator marked as a priority
MAX_WAITING = 3              # videos already waiting or being made: don't add more until they are done
TABS = ("videos", "streams")
_lock = threading.Lock()


def _file(cid):
    return creators.root(cid) / "uploads_seen.json"


def _load(cid) -> dict:
    try:
        d = json.loads(_file(cid).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cid, d):
    f = _file(cid)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(f)


def source(c) -> str:
    """Where this creator's new videos are read from: Twitch, then Kick, then YouTube."""
    ch = c.get("channels") or {}
    return ch.get("twitch") or ch.get("kick") or ch.get("youtube") or ""


def _past_broadcasts(channel_url: str, n: int) -> list:
    """The newest past broadcasts on a Twitch or Kick channel, newest first."""
    name = channel_url.rstrip("/").split("?")[0].split("/")[3] if channel_url.count("/") >= 3 else ""
    if not name:
        return []
    twitch = "twitch.tv" in channel_url
    url = f"https://www.twitch.tv/{name}/videos?filter=archives&sort=time" if twitch else f"https://kick.com/{name}/videos"
    with media._ydl({"extract_flat": True, "playlistend": n, "noplaylist": False}) as y:
        info = y.extract_info(url, download=False) or {}
    out = []
    for e in (info.get("entries") or [])[:n]:
        vid, link = (e or {}).get("id"), ((e or {}).get("url") or "").split("#")[0]
        if vid and link:
            out.append({"id": vid, "url": link, "title": e.get("title") or "", "duration": float(e.get("duration") or 0),
                        "live": False})
    return out


def latest(channel_url: str, n: int = 8) -> list:
    """The newest videos on a channel: [{id, url, title, duration}], newest first. On YouTube, across its Videos and
    Live tabs; on Twitch and Kick, the past broadcasts. Only the list is read."""
    if "twitch.tv" in channel_url or "kick.com" in channel_url:
        return _past_broadcasts(channel_url, n)
    base = channel_url.rstrip("/")
    for tab in TABS:
        if base.endswith("/" + tab):
            base = base[: -len(tab) - 1]
    out, seen = [], set()
    for tab in TABS:
        media.check_youtube(base)
        try:
            with media._ydl({"extract_flat": True, "playlistend": n, "noplaylist": False}) as y:
                info = y.extract_info(f"{base}/{tab}", download=False) or {}
        except Exception as e:  # noqa: BLE001 - a channel without a Live tab, or YouTube refusing for a while
            media.note_refusal(base, e)
            continue
        for e in (info.get("entries") or [])[:n]:
            vid = (e or {}).get("id")
            if vid and vid not in seen:
                seen.add(vid)
                out.append({"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": e.get("title") or "",
                            "duration": float(e.get("duration") or 0), "live": e.get("live_status") in ("is_live", "is_upcoming")})
    return out


def _waiting() -> int:
    return sum(1 for j in jobs.list_jobs() if j.get("kind") != "live" and j["status"] in ("queued", "running"))


LIVE_CLIPS_ENOUGH = 2         # live clips from a stream that make its past broadcast unnecessary


def _recorded_live(cid, v, now) -> bool:
    """True when PeakCut recorded most of this stream live already and got clips from it, so its past broadcast
    would only repeat those clips. A recording stopped early (for a priority creator, say), or one that found
    nothing, leaves the whole stream to be clipped again: reading the full stream at once finds the best moments."""
    since = now - v["duration"] - EVERY - 3600
    mine = [j for j in jobs.list_jobs() if j.get("kind") == "live" and j.get("creator") == cid and j.get("created", 0) >= since]
    got = sum(float((j.get("live") or {}).get("elapsed") or j.get("duration") or 0) for j in mine)
    clips = sum(1 for j in mine for k in j.get("clips", []) if k.get("status") == "done")
    return got >= 0.6 * v["duration"] and clips >= LIVE_CLIPS_ENOUGH


def check(c, now=None) -> list:
    """Look at one creator's channel. Returns the jobs started."""
    now = now or time.time()
    url = source(c)
    if not url or not c.get("watch_uploads"):
        return []
    d = _load(c["id"])
    if now - d.get("checked", 0) < EVERY - 60:
        return []
    vids = latest(url)
    if d.get("source") not in (None, url):
        d = {"started": d.get("started", [])}     # a different channel now: its back catalogue is left alone too
    d["source"] = url
    seen = set(d.get("seen", []))
    first = not d.get("checked")
    d["checked"] = now
    today = [t for t in d.get("started", []) if t > now - 86400]
    broadcasts = "youtube.com" not in url
    growing, last = {}, d.get("lengths", {})
    made = []
    for v in vids:
        if v["id"] in seen:
            continue
        if first or v["live"]:
            if not v["live"]:
                seen.add(v["id"])          # already there the first time we looked: never clipped
            continue                       # still live: the live recorder handles it, the upload comes later
        if broadcasts and last.get(v["id"]) != v["duration"]:
            growing[v["id"]] = v["duration"]   # a stream still going on (or just seen): wait until its length settles
            continue
        seen.add(v["id"])
        if not MIN_SECONDS <= v["duration"] <= config.MAX_VIDEO_HOURS * 3600:
            continue
        if broadcasts and _recorded_live(c["id"], v, now):
            creators.log(c["id"], f"Skipped a past broadcast already recorded live: {v['title']}")
            continue
        if len(today) >= (PRIORITY_PER_DAY if c.get("priority") else PER_DAY) or _waiting() >= MAX_WAITING:
            seen.discard(v["id"])          # not now: looked at again next time
            growing[v["id"]] = v["duration"]
            continue
        job = jobs.create({"type": "url", "url": v["url"], "name": v["title"] or v["url"]}, {}, creator=c["id"])
        job.update(title=v["title"], duration=v["duration"])
        jobs.save(job)
        today.append(now)
        made.append(job)
        creators.log(c["id"], f"New upload found, clipping it: {v['title']}")
    d["seen"] = list(seen)[-500:]
    d["lengths"] = growing
    d["started"] = today
    _save(c["id"], d)
    return made


def _round():
    for uid in auth.user_ids():
        with ctx.as_user(uid):
            for c in creators.all_creators():
                if not c.get("watch_uploads"):
                    continue
                try:
                    with _lock:
                        check(c)
                except Exception as e:  # noqa: BLE001 - one channel failing never stops the others
                    creators.log(c["id"], f"Checking for new uploads failed: {str(e)[-150:]}")


def loop():
    time.sleep(60)
    while True:
        try:
            _round()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(600)       # each channel is only really looked at every EVERY seconds; this just checks who is due


def start():
    threading.Thread(target=loop, daemon=True, name="uploads").start()
