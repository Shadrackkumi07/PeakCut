"""Keeps a server that runs by itself from filling its disk.

With PEAKCUT_KEEP_DAYS set (for example 10), once a day every finished video or live recording older than that is
removed, with its clips: by then its clips have been posted or passed over. A video with a clip still waiting to be
posted is kept until that post is done. Off (0) by default, so nothing is ever removed on a normal install."""
import threading
import time

from . import auth, config, ctx, jobs


def run_once(now=None) -> int:
    from . import posting
    days = config.KEEP_DAYS
    if days <= 0:
        return 0
    now = now or time.time()
    removed = 0
    for uid in (auth.user_ids() if config.ACCOUNTS else [auth.local_user().id]):
        with ctx.as_user(uid):
            waiting = {p["job"] for p in posting._mine() if p["status"] in posting.OPEN}
            for j in jobs.list_jobs():
                if j["status"] not in ("done", "error") or j["id"] in waiting or now - j["created"] < days * 86400:
                    continue
                if jobs.delete(j["id"]):
                    removed += 1
    return removed


def loop():
    time.sleep(300)
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(86400)


def start():
    if config.KEEP_DAYS > 0:
        threading.Thread(target=loop, daemon=True, name="tidy").start()
