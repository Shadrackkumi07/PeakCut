"""A private server (PEAKCUT_PRIVATE=1): nobody can sign up, the home page goes to sign-in, search engines are kept
out, and the one owner account made on the server can sign in. Runs the app in this process with its own data."""
import os, sys, tempfile
os.environ.update(PEAKCUT_DATA=tempfile.mkdtemp(prefix="pc-private-"), DATABASE_URL="", PEAKCUT_ACCOUNTS="1",
                  PEAKCUT_PRIVATE="1", PEAKCUT_PUBLIC_URL="http://testserver", PEAKCUT_FETCH_MODEL="0", PEAKCUT_UPDATE_CHECK="0")
sys.path.insert(0, ".")
import warnings; warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from studio import admin, auth, config, db, server
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

db.init()
check("private mode is on", config.PRIVATE)
web = TestClient(server.app, base_url="http://testserver", follow_redirects=False)
H = {"Origin": "http://testserver"}

r = web.get("/")
check("the home page goes to sign-in", (r.status_code in (302, 303, 307), r.headers.get("location")), (True, "/login"))
check("there is no sign-up page", web.get("/signup").status_code, 404)
check("there is no sitemap", web.get("/sitemap.xml").status_code, 404)
check("search engines are told to stay away", web.get("/robots.txt").text.strip(), "User-agent: *\nDisallow: /")
check("the sign-in page hides sign-up and Google", web.get("/api/public").json(), lambda d: d["signup"] is False and d["google"] is False)
r = web.post("/api/auth/signup", json={"email": "someone@example.com", "password": "a-long-password"}, headers=H)
check("signing up is refused", (r.status_code, "private" in r.text), (400, True))
check("so no account was made", auth.user_ids(), [])
check("the app needs sign-in", web.get("/app").status_code in (302, 303, 307))
check("the API needs sign-in", web.get("/api/sync").status_code, 401)

import io, contextlib
out = io.StringIO()
with contextlib.redirect_stdout(out):
    code = admin._cli(["create-owner", "owner@example.com"])
password = out.getvalue().rsplit(": ", 1)[-1].strip()
check("the owner account is made on the server", (code, len(password) >= 20), (0, True))
check("the owner is an admin", auth.get_user(auth.user_ids()[0]).is_admin)
with contextlib.redirect_stdout(io.StringIO()):
    check("a second owner cannot be made", admin._cli(["create-owner", "owner@example.com"]), 1)
r = web.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-password"}, headers=H)
check("a wrong password is refused", r.status_code, 400)
r = web.post("/api/auth/login", json={"email": "owner@example.com", "password": password}, headers=H)
check("the owner can sign in", r.status_code, 200)
check("signed in, the home page goes to the app", web.get("/").headers.get("location"), "/app")
check("signed in, the API answers", web.get("/api/sync").status_code, 200)

# posting a clip at a chosen time, through the page's own route
import time
from unittest.mock import patch
from studio import creators, ctx, jobs, posting
uid = auth.user_ids()[0]
with ctx.as_user(uid), patch.object(jobs._q, "put"):
    cr = creators.create({"name": "Timed"})
    jb = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/9", "name": "v"}, {}, enqueue=False, creator=cr["id"])
    jb["status"] = "done"
    jb["clips"].append({"id": "clip01", "status": "done", "made_at": time.time(), "score": 90, "title": "Timed clip", "start": 0, "end": 30, "fixed_words": [{"w": "x"}]})
    jobs.save(jb)
check("the next posting times are offered", len(web.get("/api/post-times").json()), 6)
at = web.get("/api/post-times").json()[2]["at"]
acc = {"id": "yt_t", "name": "Chan", "platform": "youtube"}
with patch.object(posting.accounts, "get", return_value=acc):
    r = web.post("/api/posts", json={"job": jb["id"], "clip": "clip01", "accounts": ["yt_t"], "at": at}, headers=H)
    check("a clip can be posted at a chosen posting time", r.status_code, 200)
    with ctx.as_user(uid):
        check("it is set for exactly that time", [p["due_at"] for p in posting._mine() if p["clip"] == "clip01"], [at])
    r = web.post("/api/posts", json={"job": jb["id"], "clip": "clip01", "accounts": ["yt_t"], "at": time.time() - 7200}, headers=H)
    check("a time in the past is refused", r.status_code, 400)

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
