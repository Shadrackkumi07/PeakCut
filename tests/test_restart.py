"""A video interrupted because the server is stopping (an update) carries on after the restart instead of being
marked as failed. A real failure is still marked as failed. No network."""
import json, os, sys, tempfile, threading, time
os.environ.update(PEAKCUT_DATA=tempfile.mkdtemp(prefix="pc-restart-"), DATABASE_URL="", PEAKCUT_ACCOUNTS="0")
sys.path.insert(0, ".")
from pathlib import Path
from unittest.mock import patch
from studio import auth, creators, ctx, db, jobs
db.init()
fails = []
def check(n, ok):
    print(("PASS " if ok else "FAIL ") + n); ok or fails.append(n)


uid = auth.local_user().id
with ctx.as_user(uid), patch.object(jobs._q, "put"):
    c = creators.create({"name": "Speed"})
    a = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/1", "name": "a"}, {}, enqueue=False, creator=c["id"])
    b = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/2", "name": "b"}, {}, enqueue=False, creator=c["id"])
    for j in (a, b):
        j["status"] = "running"
        j["clips"].append({"id": "clip01", "status": "done", "score": 80})
        jobs.save(j)

errors = {a["id"]: RuntimeError("cannot schedule new futures after shutdown"), b["id"]: ValueError("the download broke")}
def boom(job):
    raise errors[job["id"]]

with patch.object(jobs, "_run_job", boom):
    threading.Thread(target=jobs._worker, daemon=True).start()
    jobs._q.put(("job", a["id"], None), user=uid)
    jobs._q.put(("job", b["id"], None), user=uid)
    for _ in range(50):
        if jobs._jobs[b["id"]]["status"] == "error":
            break
        time.sleep(0.1)
    time.sleep(0.3)

check("stopped by a server shutdown: not marked as failed", jobs._jobs[a["id"]]["status"] == "running")
check("a real failure is still marked as failed", jobs._jobs[b["id"]]["status"] == "error")
saved = json.loads((Path(a["dir"]) / "job.json").read_text(encoding="utf-8"))
check("on disk it is still running", saved["status"] == "running")
jobs._load_file(Path(a["dir"]) / "job.json", uid)
again = jobs._jobs[a["id"]]
check("after the restart it is picked up again from where it got to", again["status"] == "queued" and again.get("resume") is True)
check("its clips are kept", [k["id"] for k in again["clips"]] == ["clip01"])

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
