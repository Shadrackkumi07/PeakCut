"""Posting at set times: the best clip goes out at each time, creators take turns (at most 2 a day each), nothing that
repeats a posted clip goes out, the approval period holds clips until Approve, and each time is used once. Also the
new-uploads watcher, the post text, and the subscribe button in the captions. No network, no AI.
Runs against the code directly, with its own empty data folder."""
import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-sched-"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from unittest.mock import patch
from studio import accounts, creators, ctx, db, jobs, posting, render, uploads
uploads.EVERY = 3 * 3600   # the checks below are spaced for a 3-hour look; the rules are the same at any spacing
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

ACC = {"id": "yt_test", "name": "Test channel", "platform": "youtube"}
db.init()
with ctx.as_user("u_local"), patch.object(posting.accounts, "get", return_value=ACC), patch.object(jobs._q, "put"):
    A = creators.create({"name": "Speedy", "autopost": True, "accounts": ["yt_test"], "post_min_score": 80,
                         "channels": {"youtube": "https://www.youtube.com/@Speedy"}})
    B = creators.create({"name": "Kai", "autopost": True, "accounts": ["yt_test"], "post_min_score": 80})
    now = time.time()

    def clips(cid, items):
        j = jobs.create({"type": "url", "url": "https://www.youtube.com/watch?v=x", "name": "v"}, {}, enqueue=False, creator=cid)
        for i, (score, words, title, s) in enumerate(items):
            j["clips"].append({"id": f"clip{i + 1:02d}", "status": "done", "made_at": now, "score": score, "title": title,
                               "fixed_words": [{"w": w} for w in words.split()], "start": s, "end": s + 30,
                               "hashtags": ["ishowspeed", "gaming", "#bad tag!"], "description": "He did not expect that."})
        jobs.save(j)
        return j

    ja = clips(A["id"], [(95, "the car flipped over and he screamed at the top of his lungs", "Speed flips the car", 0),
                         (90, "chat could not believe the price of that pair of shoes in the store", "Speed sees the price", 100),
                         (85, "he lost the bet and had to eat the hottest chip in the world live", "Speed eats the chip", 200),
                         (60, "nothing much happens here at all really just walking", "Speed walks", 300)])
    jb = clips(B["id"], [(88, "the whole room went silent when the surprise guest walked through the door", "Kai's surprise guest", 0)])

    s = accounts.settings()
    check("the schedule is off at first: nothing posts", posting.run_slots(now), [])
    accounts.save_settings({"schedule_on": True, "post_times": "08:00,11:00,13:00", "post_tz": "America/New_York",
                            "approve_until": now + 7 * 86400})
    check("manual posting is the default: nothing goes out by itself", posting.run_slots(slots_first := posting.slot_times(now)[0][1] + 60), [])
    accounts.save_settings({"auto_post": True})
    slots = posting.slot_times(now)
    check("today's posting times are in New York time", [k[-5:] for k, _ in slots], ["08:00", "11:00", "13:00"])
    t8 = slots[0][1] + 60
    view = posting.pool(now=t8)
    check("during the approval week, clips wait for Approve", all(why == "waiting for your Approve" for _, _, why in view) and len(view) == 4)
    check("a low score never enters the pool", all(k["score"] >= 80 for _, k, _ in view))
    check("so nothing goes out yet", posting.run_slots(t8), [])

    for j, k in ((ja, "clip01"), (ja, "clip02"), (ja, "clip03"), (jb, "clip01")):
        posting.set_pool(j["id"], k, "approve")
    made = posting.run_slots(t8)
    check("at the posting time, the best approved clip goes out", [(p["creator"], p["clip"]) for p in made], [(A["id"], "clip01")])
    check("the post remembers its posting time", made[0]["slot"][-5:], "08:00")
    check("the same posting time is never used twice", posting.run_slots(t8 + 120), [])

    t11 = slots[1][1] + 60
    made = posting.run_slots(t11)
    check("the next time, another creator gets a turn", [(p["creator"], p["clip"]) for p in made], [(B["id"], "clip01")])
    made = posting.run_slots(slots[2][1] + 60)
    check("then the best one left", [p["clip"] for p in made], ["clip02"])
    for p in posting._mine():
        p["status"], p["posted_at"] = "posted", p["due_at"]
    left = [k["id"] for j, k, why in posting.pool(now=slots[2][1] + 120) if not why]
    check("two a day from one creator at most", posting.pick("yt_test", slots[2][1] + 120), None)
    check("but the clip is still there for tomorrow", left, ["clip03"])

    # a priority creator: a close call goes their way, and they can post a third time in a day
    creators.update(B["id"], {"priority": True})
    jp = clips(B["id"], [(82, "he tripped over the cable and the whole setup fell down", "Kai trips over the cable", 0)])
    check("a priority creator's clip wins a close call", posting.pool(now=now)[0][1]["title"], "Kai trips over the cable")
    posting.set_pool(jp["id"], "clip01", "skip")
    creators.update(B["id"], {"priority": False})

    # clips per video: up to 30
    check("up to 30 clips per video can be set", creators.update(A["id"], {"max_clips": 30})["max_clips"], 30)
    check("more than 30 is brought down to 30", creators.update(A["id"], {"max_clips": 99})["max_clips"], 30)
    check("a single video job allows 30 too", jobs.clean_options({"max_clips": 30})["max_clips"], 30)

    # repeats
    posted = [p for p in posting._mine()]
    same = {"fixed_words": [{"w": w} for w in "the car flipped over and he screamed at the top of his lungs again".split()],
            "title": "A new title", "start": 500, "end": 530}
    check("the same words as a posted clip is a repeat", posting.is_repeat(same, "other-video", posted), "says the same as a posted clip")
    overlap = {"fixed_words": [{"w": "completely different words here"}], "title": "Different", "start": 10, "end": 40}
    check("overlapping a posted clip of the same video is a repeat", posting.is_repeat(overlap, ja["id"], posted), "overlaps a posted clip from the same video")
    fresh = {"fixed_words": [{"w": w} for w in "a brand new moment nobody has seen before at all".split()], "title": "Speed meets a lion", "start": 900, "end": 930}
    check("something new is not a repeat", posting.is_repeat(fresh, ja["id"], posted), "")

    # stale clips and Skip
    ja["clips"][2]["made_at"] = now - 3 * 86400
    check("clips older than two days are not posted", "clip03" not in [k["id"] for _, k, _ in posting.pool(now=now)])
    ja["clips"][2]["made_at"] = now
    posting.set_pool(ja["id"], "clip03", "skip")
    check("a skipped clip leaves the pool", "clip03" not in [k["id"] for _, k, _ in posting.pool(now=now)])

    # after the approval week, no Approve is needed
    accounts.save_settings({"approve_until": 0})
    jc = clips(B["id"], [(91, "he finally hit the shot after two hundred tries and lost his mind", "Kai finally hits it", 0)])
    check("after the approval week, good clips go by themselves", [why for j, k, why in posting.pool(now=now) if j["id"] == jc["id"]], [""])

    # post text
    check("hashtags: #Shorts first, cleaned, at most 5", posting.hashtags(ja["clips"][0]), ["Shorts", "ishowspeed", "gaming", "badtag"])
    cap = posting._caption(ja["clips"][0], creators.get(A["id"]))
    check("the description credits the creator's channel", "🎥 Credit: @Speedy" in cap and cap.endswith("#Shorts #ishowspeed #gaming #badtag"))

    # new uploads
    vids = [{"id": "old1", "url": "u", "title": "Old video", "duration": 1200, "live": False}]
    creators.update(A["id"], {"watch_uploads": True})
    with patch.object(uploads, "latest", return_value=list(vids)):
        check("the first look only remembers what is there", uploads.check(creators.get(A["id"]), now), [])
    vids = [{"id": "new1", "url": "https://www.youtube.com/watch?v=new1", "title": "Brand new stream", "duration": 7200, "live": False},
            {"id": "short1", "url": "u", "title": "A short", "duration": 40, "live": False},
            {"id": "live1", "url": "u", "title": "Live now", "duration": 0, "live": True}] + vids
    with patch.object(uploads, "latest", return_value=list(vids)), patch.object(uploads, "_waiting", return_value=0):
        made = uploads.check(creators.get(A["id"]), now + uploads.EVERY)
        check("a new long upload is clipped, shorts and live streams are not", [j["title"] for j in made], ["Brand new stream"])
        check("with the creator's own settings", made[0]["creator"], A["id"])
        check("it is not looked at again before 3 hours", uploads.check(creators.get(A["id"]), now + uploads.EVERY + 60), [])
        more = [{"id": f"n{i}", "url": f"https://www.youtube.com/watch?v=n{i}", "title": f"Video {i}", "duration": 3600, "live": False} for i in range(4)]
    with patch.object(uploads, "latest", return_value=more + vids), patch.object(uploads, "_waiting", return_value=0):
        made = uploads.check(creators.get(A["id"]), now + 2 * uploads.EVERY)
        check("at most 2 new videos a day per creator", len(made), 1)

    # past broadcasts on Twitch: used instead of YouTube, only once the stream is over, never one recorded live
    creators.update(B["id"], {"watch_uploads": True, "channels": {"twitch": "https://www.twitch.tv/kai", "youtube": "https://www.youtube.com/@Kai"}})
    check("a Twitch streamer's videos come from Twitch", uploads.source(creators.get(B["id"])), "https://www.twitch.tv/kai")
    tw = [{"id": "v1", "url": "https://www.twitch.tv/videos/1", "title": "Old stream", "duration": 9000, "live": False}]
    with patch.object(uploads, "latest", return_value=list(tw)), patch.object(uploads, "_waiting", return_value=0):
        uploads.check(creators.get(B["id"]), now)
        tw = [{"id": "v2", "url": "https://www.twitch.tv/videos/2", "title": "New stream", "duration": 3000, "live": False}] + tw
    with patch.object(uploads, "latest", return_value=list(tw)), patch.object(uploads, "_waiting", return_value=0):
        check("a broadcast is not taken while it may still be going on", uploads.check(creators.get(B["id"]), now + uploads.EVERY), [])
        tw[0]["duration"] = 5000
        check("still growing: still waiting", uploads.check(creators.get(B["id"]), now + 2 * uploads.EVERY), [])
        made = uploads.check(creators.get(B["id"]), now + 3 * uploads.EVERY)
        check("once its length settles, it is clipped", [j["source"]["url"] for j in made], ["https://www.twitch.tv/videos/2"])
    lj = jobs.create({"type": "url", "url": "https://www.twitch.tv/kai", "name": "live"}, {}, enqueue=False, creator=B["id"], kind="live")
    lj["created"], lj["live"] = now + 4 * uploads.EVERY - 3600, {"elapsed": 3600}
    lj["clips"] = [{"id": f"clip0{i}", "status": "done", "score": 85} for i in (1, 2)]; jobs.save(lj)
    tw = [{"id": "v3", "url": "https://www.twitch.tv/videos/3", "title": "Recorded stream", "duration": 4000, "live": False}] + tw
    with patch.object(uploads, "latest", return_value=list(tw)), patch.object(uploads, "_waiting", return_value=0):
        uploads.check(creators.get(B["id"]), now + 4 * uploads.EVERY)
        check("a stream already recorded live is not clipped again", uploads.check(creators.get(B["id"]), now + 5 * uploads.EVERY), [])
    lj["live"], lj["clips"] = {"elapsed": 3600}, []; jobs.save(lj)
    tw = [{"id": "v35", "url": "https://www.twitch.tv/videos/35", "title": "Recorded, no clips", "duration": 4000, "live": False}] + tw
    with patch.object(uploads, "latest", return_value=list(tw)), patch.object(uploads, "_waiting", return_value=0):
        uploads.check(creators.get(B["id"]), now + 6 * uploads.EVERY)
        made = uploads.check(creators.get(B["id"]), now + 7 * uploads.EVERY)
        check("a stream recorded live that gave no clips is clipped from its broadcast", [j["source"]["url"] for j in made], ["https://www.twitch.tv/videos/35"])
    lj["live"], lj["clips"] = {"elapsed": 600}, [{"id": f"clip0{i}", "status": "done", "score": 85} for i in (1, 2)]; jobs.save(lj)
    tw = [{"id": "v4", "url": "https://www.twitch.tv/videos/4", "title": "Partly recorded", "duration": 4000, "live": False}] + tw
    with patch.object(uploads, "latest", return_value=list(tw)), patch.object(uploads, "_waiting", return_value=0):
        uploads.check(creators.get(B["id"]), now + 12 * uploads.EVERY)
        made = uploads.check(creators.get(B["id"]), now + 13 * uploads.EVERY)
        check("a stream only partly recorded live is still clipped", [j["source"]["url"] for j in made], ["https://www.twitch.tv/videos/4"])

    creators.update(B["id"], {"channels": {"kick": "https://kick.com/kai"}})
    kk = [{"id": "k1", "url": "https://kick.com/kai/videos/1", "title": "Old Kick stream", "duration": 5000, "live": False}]
    with patch.object(uploads, "latest", return_value=list(kk)), patch.object(uploads, "_waiting", return_value=0):
        uploads.check(creators.get(B["id"]), now + 16 * uploads.EVERY)
        check("moving to another channel never clips its old videos", uploads.check(creators.get(B["id"]), now + 17 * uploads.EVERY), [])

    # manual posting: you pick the time for each clip
    accounts.save_settings({"auto_post": False})
    nt = posting.next_times(now=now)
    check("six preset times are offered, all in the future", len(nt) == 6 and all(x["at"] > now for x in nt))
    check("labelled Today / Tomorrow with the time", all(x["label"].split()[0] in ("Today", "Tomorrow") or len(x["label"].split()[0]) == 3 for x in nt))
    jm = clips(A["id"], [(90, "a totally new moment for the manual test only here", "Speed manual clip", 0)])
    with patch.object(posting.accounts, "list_accounts", return_value=[ACC]):
        made = posting.schedule_clip(jm["id"], "clip01", nt[0]["at"])
    check("a clip is set for the chosen posting time", made[0]["due_at"] == nt[0]["at"] and made[0]["status"] == "scheduled")
    check("that time now shows as taken", posting.next_times(now=now)[0]["taken"])
    try:
        posting.schedule_clip(jm["id"], "clip01", nt[1]["at"]); twice = False
    except ValueError:
        twice = True
    check("the same clip cannot be set twice", twice)
    jn = clips(A["id"], [(90, "another fresh manual moment for posting right now", "Speed post now", 0)])
    try:
        posting.schedule_clip(jn["id"], "clip01", time.time() - 3600); past = False
    except ValueError as e:
        past = "from now" in str(e)
    check("a time in the past is refused", past)
    made = posting.schedule_clip(jn["id"], "clip01", "now")
    check("Post now sets it for right away", abs(made[0]["due_at"] - time.time()) < 5)
    check("in manual mode, clips never wait for Approve", all(why != "waiting for your Approve" for _, _, why in posting.pool(now=now)))

    # clean-up of old videos
    from studio import config, tidy
    from types import SimpleNamespace
    for j in jobs.list_jobs():
        j["status"] = "done"
    old = jobs.create({"type": "url", "url": "u", "name": "old"}, {}, enqueue=False, creator=A["id"])
    old.update(status="done", created=now - 20 * 86400)
    held = jobs.create({"type": "url", "url": "u", "name": "held"}, {}, enqueue=False, creator=A["id"])
    held.update(status="done", created=now - 20 * 86400)
    held["clips"].append({"id": "clip01", "status": "done", "made_at": now, "score": 99, "title": "Held back clip",
                          "fixed_words": [{"w": "held"}], "start": 0, "end": 30})
    posting.enqueue(held, held["clips"][0], ["yt_test"])
    with patch.object(tidy.auth, "local_user", return_value=SimpleNamespace(id="u_local")), patch.object(config, "ACCOUNTS", False):
        with patch.object(config, "KEEP_DAYS", 0):
            check("clean-up is off unless switched on", tidy.run_once(now), 0)
        with patch.object(config, "KEEP_DAYS", 10):
            tidy.run_once(now)
    ids = {j["id"] for j in jobs.list_jobs()}
    check("old finished videos are removed", old["id"] not in ids)
    check("but not one with a clip still waiting to post", held["id"] in ids)
    check("and nothing recent", ja["id"] in ids and jb["id"] in ids)

# the subscribe button
ass = render.build_ass([{"w": "hello", "s": 0.5, "e": 0.9}], 0, 30, "WOW", "bounce", [], 300, 1380, sub_at=12.0)
lines = [l for l in ass.splitlines() if l.startswith("Dialogue: 5") or l.startswith("Dialogue: 6") or l.startswith("Dialogue: 7")]
check("the subscribe button is in the captions file", any("SUBSCRIBE" in l for l in lines) and any("SUBSCRIBED" in l for l in lines))
check("it starts at the chosen moment", any(",0:00:12.00," in l for l in lines))
check("it sits below the captions, above YouTube's own text", render.subscribe_y(1380), 1545)
check("never lower than 1610", render.subscribe_y(1640), 1610)
no = render.build_ass([{"w": "hello", "s": 0.5, "e": 0.9}], 0, 30, "WOW", "bounce", [], 300, 1380)
check("no button unless asked for", "SUBSCRIBE" not in no)
levels = [1.0] * 60
levels[40] = 9.0                       # loud at 20 s
check("it shows on the loudest moment in the middle", render.subscribe_at(levels, 30), 20.0)
check("without loudness, at 60%", render.subscribe_at([], 30), 18.0)
check("it never runs past the end", render.subscribe_at([1.0] * 20 + [9.0], 10) <= 10 * 0.85)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
