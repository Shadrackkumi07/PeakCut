"""Posting only one topic (GTA 6), the clean-up of off-topic clips, and a mix of creators through the day.
No network: the AI's answers are made up here."""
import os, sys, tempfile, time
os.environ.update(PEAKCUT_DATA=tempfile.mkdtemp(prefix="pc-topic-"), DATABASE_URL="", PEAKCUT_ACCOUNTS="0")
sys.path.insert(0, ".")
from unittest.mock import patch
from studio import accounts, brain, creators, ctx, db, jobs, posting, topic
db.init()
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


ACC = {"id": "yt_t", "name": "Chan", "platform": "youtube"}
now = time.time()
with ctx.as_user("u_local"), patch.object(posting.accounts, "get", return_value=ACC), patch.object(jobs._q, "put"):
    cs = {n: creators.create({"name": n, "autopost": True, "accounts": ["yt_t"], "post_min_score": 70, "priority": n in ("Speed", "Kai")})
          for n in ("Speed", "Kai", "CaseOh", "Jynxzi", "Clix")}

    def video(name, items):
        j = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/1", "name": "v"}, {}, enqueue=False, creator=cs[name]["id"])
        j.update(status="done", title=f"{name} plays GTA 6")
        for i, (score, title, game) in enumerate(items):
            j["clips"].append({"id": f"clip{i + 1:02d}", "status": "done", "made_at": now, "score": score, "title": title,
                               "fixed_words": [{"w": __import__("secrets").token_hex(4)} for n in range(12)],
                               "start": i * 100, "end": i * 100 + 30, **({"game": game} if game else {})})
        jobs.save(j)
        return j

    js = {"Speed": video("Speed", [(90, "Speed robs the bank", "GTA 6"), (88, "Speed plays Fortnite", "Fortnite"), (86, "Speed's old clip", None)]),
          "Kai": video("Kai", [(85, "Kai escapes the cops", "GTA VI"), (84, "Kai's heist", "gta6")]),
          "CaseOh": video("CaseOh", [(80, "CaseOh drives off a cliff", "Grand Theft Auto 6")]),
          "Jynxzi": video("Jynxzi", [(78, "Jynxzi rages in Siege", "Rainbow Six Siege"), (77, "Jynxzi meets Jason", "GTA 6")]),
          "Clix": video("Clix", [(76, "Clix lands in Tilted", "Fortnite")])}

    # the topic filter
    check("with no topic, every clip can be posted", len(posting.pool(now=now)), 9)
    accounts.save_settings({"only_topic": "GTA 6", "schedule_on": True, "auto_post": True})
    titles = [k["title"] for _, k, _ in posting.pool(now=now)]
    check("with GTA 6 set, only GTA 6 clips can be posted", sorted(titles),
          sorted(["Speed robs the bank", "Kai escapes the cops", "Kai's heist", "CaseOh drives off a cliff", "Jynxzi meets Jason"]))
    check("a clip with no game label yet is not posted", "Speed's old clip" not in titles)

    # a mix of creators through the day
    order = []
    day = posting.slot_times(now + 86400)
    accounts.save_settings({"post_times": "00:00,03:00,06:00,09:00,12:00,15:00,18:00,21:00"})
    for _key, t in posting.slot_times(now + 86400):
        for p in posting.run_slots(t + 60):
            order.append(next(n for n, c in cs.items() if c["id"] == p["creator"]))
    check("each creator with a GTA 6 clip gets a turn before anyone posts twice", order[:4], lambda o: sorted(o) == ["CaseOh", "Jynxzi", "Kai", "Speed"])
    check("priority creators go first when the counts are equal", order[:2], lambda o: set(o) == {"Speed", "Kai"})
    check("then the rest is used up, never the same creator twice in a row while others have clips", order, lambda o: len(o) == 5 and all(a != b for a, b in zip(o, o[1:])))

    # the clean-up: label the unlabelled, remove off-topic, cancel their waiting posts, keep posted ones
    js["Speed"]["clips"][1].pop("game")             # the Fortnite clip has no label yet
    with patch.object(posting.accounts, "list_accounts", return_value=[ACC]):
        posting.schedule_clip(js["Clix"]["id"], "clip01", now + 7200)       # set by hand, but off-topic
    def fake_label(items):
        return {key: ("Fortnite" if "Fortnite" in c["title"] else "none") for key, _vt, c in items}
    with patch.object(topic, "label", fake_label):
        r = topic.sweep()
    left = [k["title"] for j in jobs.list_jobs() for k in j["clips"]]
    check("off-topic clips are removed", sorted(x["title"] for x in r["removed"]),
          sorted(["Speed plays Fortnite", "Speed's old clip", "Jynxzi rages in Siege", "Clix lands in Tilted"]))
    check("GTA 6 clips stay", all(t in left for t in ["Speed robs the bank", "Kai's heist", "Jynxzi meets Jason"]))
    check("a post waiting for an off-topic clip is cancelled", r["cancelled"], 1)

    # new clips: the AI's game label is kept, and anything else is never made
    c = brain._tidy({"score": 80, "title": "t", "game": "GTA 6"}, 0, 30)
    check("the AI's game label is kept on the clip", c["game"], "GTA 6")
    check("no label from the AI counts as none", brain._tidy({"score": 80}, 0, 30)["game"], "none")

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
