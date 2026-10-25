"""Cloud poster: runs in GitHub Actions every 30 min, publishes queue items whose time has come.

queue.json: [{"id": "...", "type": "reel"|"carousel", "files": ["media/x.mp4"], "caption": "...",
              "post_at": "2026-10-26T22:00:00Z", "thumb_ms": 3000, "status": "queued"}]
Media is served by GitHub Pages at PAGES_BASE/<file>. Posted items' media is deleted to keep the repo small.
Env: IG_ACCESS_TOKEN, IG_USER_ID, PAGES_BASE (e.g. https://<user>.github.io/<repo>)
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://graph.instagram.com/v23.0"
TOKEN, USER, BASE = os.environ["IG_ACCESS_TOKEN"], os.environ["IG_USER_ID"], os.environ["PAGES_BASE"].rstrip("/")
HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"


def call(method, path, **params):
    params["access_token"] = TOKEN
    url, body = f"{API}/{path}", urllib.parse.urlencode(params).encode()
    if method == "GET":
        url, body = url + "?" + body.decode(), None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, method=method), timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"IG {e.code}: {e.read().decode(errors='replace')}")


def wait_ready(cid):
    for _ in range(90):
        st = call("GET", cid, fields="status_code")
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"container failed: {st}")
        time.sleep(10)
    raise RuntimeError("processing timeout")


def url_of(f):
    u = f"{BASE}/{f}"
    # Pages can take a minute to deploy new files; make sure Instagram will be able to fetch it
    for _ in range(18):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=30) as r:
                if r.status == 200:
                    return u
        except urllib.error.URLError:
            pass
        time.sleep(10)
    raise RuntimeError(f"media not reachable yet: {u}")


def post(item):
    if item["type"] == "reel":
        c = call("POST", f"{USER}/media", media_type="REELS", video_url=url_of(item["files"][0]), caption=item["caption"],
                 share_to_feed="true", thumb_offset=str(item.get("thumb_ms", 0)))
        wait_ready(c["id"])
    else:
        kids = []
        for f in item["files"]:
            vid = f.lower().endswith((".mp4", ".mov"))
            k = call("POST", f"{USER}/media", is_carousel_item="true", **({"media_type": "VIDEO", "video_url": url_of(f)} if vid else {"image_url": url_of(f)}))
            if vid:
                wait_ready(k["id"])
            kids.append(k["id"])
        c = call("POST", f"{USER}/media", media_type="CAROUSEL", children=",".join(kids), caption=item["caption"])
        wait_ready(c["id"])
    res = call("POST", f"{USER}/media_publish", creation_id=c["id"])
    return call("GET", res["id"], fields="permalink")["permalink"]


queue = json.loads(QUEUE.read_text(encoding="utf-8"))
now = datetime.now(timezone.utc)
changed = False
for item in queue:
    if item.get("status") != "queued" or datetime.fromisoformat(item["post_at"].replace("Z", "+00:00")) > now:
        continue
    try:
        item["permalink"] = post(item)
        item["status"], item["posted_at"] = "posted", now.isoformat(timespec="seconds")
        for f in item["files"]:
            (HERE / f).unlink(missing_ok=True)
        print("posted", item["id"], item["permalink"])
    except Exception as e:  # keep going; retry next run up to 3 times
        item["errors"] = item.get("errors", 0) + 1
        item["last_error"] = str(e)[:500]
        if item["errors"] >= 3:
            item["status"] = "failed"
        print("FAILED", item["id"], e, file=sys.stderr)
    changed = True
    break  # one post per run, so posts never stack up at the same minute
if changed:
    QUEUE.write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")
