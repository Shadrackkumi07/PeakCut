"""Priority creators and live recording: when a priority creator goes live and every recording slot is taken by
others, the newest other recording is stopped and the priority creator is recorded instead. No network."""
import os, sys, tempfile
os.environ.update(PEAKCUT_DATA=tempfile.mkdtemp(prefix="pc-prio-"), DATABASE_URL="", PEAKCUT_ACCOUNTS="0",
                  PEAKCUT_MAX_LIVE="2", PEAKCUT_MAX_LIVE_PER_USER="2")
sys.path.insert(0, ".")
from unittest.mock import patch
from studio import creators, ctx, db, live
db.init()
fails = []
def check(n, ok):
    print(("PASS " if ok else "FAIL ") + n); ok or fails.append(n)


class Fake:
    """Stands in for a recording."""
    def __init__(self, cid, t):
        self.job = {"creator": cid, "user": "u_local", "created": t, "id": cid}
        self.finished = self.user_stopped = False

    def stop(self, note):
        self.user_stopped = True


with ctx.as_user("u_local"):
    a = creators.create({"name": "CaseOh", "channel": "https://www.twitch.tv/caseoh_", "auto_watch": True})
    b = creators.create({"name": "Clix", "channel": "https://www.twitch.tv/clix", "auto_watch": True})
    sp = creators.create({"name": "Speed", "channel": "https://www.twitch.tv/ishowspeed", "auto_watch": True, "priority": True})
    started = []

    def watch_one(c):
        if not live.active_for(c["id"]) and len(live.active_mine()) < 2 and live._is_live(c):
            started.append(c["name"])
            live.SESSIONS[c["id"]] = Fake(c["id"], 3)

    def run(is_live):
        with patch.object(live, "_watch_one", watch_one), patch("studio.profiles.live_shared", lambda p, u: {"live": is_live(u)}):
            live._watch_user("u_local")

    live.SESSIONS.clear(); live.SESSIONS.update({"a": Fake(a["id"], 1), "b": Fake(b["id"], 2)})
    run(lambda u: True)
    check("a live priority creator gets recorded when both slots were taken", started == ["Speed"])
    check("the newest other recording makes room, the older one keeps going", live.SESSIONS["b"].user_stopped and not live.SESSIONS["a"].user_stopped)
    run(lambda u: True)
    check("nothing more is stopped once they are recording", not live.SESSIONS["a"].user_stopped and started == ["Speed"])

    live.SESSIONS.clear(); live.SESSIONS.update({"a": Fake(a["id"], 1), "b": Fake(b["id"], 2)}); started.clear()
    run(lambda u: "ishowspeed" not in u)
    check("when the priority creator is not live, nobody is stopped", not any(s.user_stopped for s in live.SESSIONS.values()))

    live.SESSIONS.clear(); live.SESSIONS.update({"a": Fake(a["id"], 1), "b": Fake(b["id"], 2)})
    with patch.object(live, "_watch_one", lambda c: None), patch("studio.profiles.live_shared", lambda p, u: {"live": True}):
        live._watch_user("u_local")
        check("if their stream will not record, one recording was stopped", sum(s.user_stopped for s in live.SESSIONS.values()) == 1)
        live._watch_user("u_local")
    check("... and no more are stopped for the next half hour", sum(s.user_stopped for s in live.SESSIONS.values()) == 1)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
