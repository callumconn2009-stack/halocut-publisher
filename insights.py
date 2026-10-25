"""Cloud analytics: runs in GitHub Actions every 3 hours.

For every posted item: pull Instagram insights, store a time series, score trial batches, pick winners after the
test window, and write data/dashboard.json for the dashboard (index.html).
Env: IG_ACCESS_TOKEN. Optional settings in config.json.
"""
import json, os, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://graph.instagram.com/v23.0"
TOKEN = os.environ["IG_ACCESS_TOKEN"]
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
CFG = {"test_hours": 72, "auto_post_winner": False, "winner_slot_hour_utc": 7, "ad_retention": 0.45, "ad_share_rate": 0.01}
if (HERE / "config.json").exists():
    CFG.update(json.loads((HERE / "config.json").read_text(encoding="utf-8")))
METRICS = ["views", "reach", "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "likes", "comments", "shares", "saved", "total_interactions"]
now = datetime.now(timezone.utc)


def get(path, **params):
    params["access_token"] = TOKEN
    try:
        with urllib.request.urlopen(f"{API}/{path}?{urllib.parse.urlencode(params)}", timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode(errors="replace")[:300]}


def insights(media_id):
    out = {}
    for m in METRICS:  # one call per metric so an unsupported one doesn't sink the rest
        d = get(f"{media_id}/insights", metric=m)
        if d.get("data"):
            out[m] = d["data"][0]["values"][0]["value"]
    return out


def score(v):
    """0-100. Weighted to what the algorithm rewards: holding attention, then shares/saves, then reach."""
    views = max(1, v.get("views", 0))
    ret = v.get("retention", 0)
    share_rate = (v.get("shares", 0) + v.get("saved", 0)) / views
    return round(min(100, 60 * min(1, ret / 0.6) + 25 * min(1, share_rate / 0.03) + 15 * min(1, views / 1000)), 1)


queue = json.loads((HERE / "queue.json").read_text(encoding="utf-8"))
series_path = DATA / "series.json"
series = json.loads(series_path.read_text(encoding="utf-8")) if series_path.exists() else {}
posts = []
for item in queue:
    if item.get("status") != "posted" or not item.get("media_id"):
        continue
    m = insights(item["media_id"])
    if not m:
        continue
    series.setdefault(item["media_id"], []).append({"t": now.isoformat(timespec="minutes"), **m})
    dur = item.get("duration") or 0
    avg_s = m.get("ig_reels_avg_watch_time", 0) / 1000
    v = {"id": item["id"], "media_id": item["media_id"], "permalink": item.get("permalink"), "type": item["type"],
         "batch": item.get("batch"), "variant": item.get("variant"), "label": item.get("label") or item["caption"].splitlines()[0][:70],
         "trial": bool(item.get("trial")), "posted_at": item.get("posted_at"), "duration": dur, "avg_watch_s": round(avg_s, 1),
         "retention": round(avg_s / dur, 3) if dur else 0, **m}
    v["score"] = score(v)
    posts.append(v)
series_path.write_text(json.dumps(series), encoding="utf-8")

# ---- trial batches: leaderboard, winner after the test window ----
batches = {}
for p in posts:
    if p["batch"]:
        batches.setdefault(p["batch"], []).append(p)
bstate_path = DATA / "batches.json"
bstate = json.loads(bstate_path.read_text(encoding="utf-8")) if bstate_path.exists() else {}
changed_queue = False
out_batches = []
for bid, vs in batches.items():
    vs.sort(key=lambda x: -x["score"])
    last = max(datetime.fromisoformat(v["posted_at"]) for v in vs if v["posted_at"])
    ends = last + timedelta(hours=CFG["test_hours"])
    st = bstate.setdefault(bid, {})
    best = vs[0]
    notes = []
    if best["retention"] >= CFG["ad_retention"] and (best.get("shares", 0) + best.get("saved", 0)) / max(1, best.get("views", 0)) >= CFG["ad_share_rate"]:
        notes.append("Green across the board: worth testing as a paid ad.")
    worst = vs[-1]
    if len(vs) > 1 and best["retention"] - worst["retention"] > 0.1:
        notes.append(f"Hook matters here: variant {best['variant']} holds {round(best['retention']*100)}% vs {round(worst['retention']*100)}% for {worst['variant']}.")
    if now >= ends and not st.get("winner"):
        st["winner"] = best["variant"]; st["decided_at"] = now.isoformat(timespec="minutes")
        if CFG["auto_post_winner"]:  # re-post the winning video as a normal Reel for followers
            src = next(q for q in queue if q["id"] == best["id"])
            slot = (now + timedelta(days=1)).replace(hour=CFG["winner_slot_hour_utc"], minute=0, second=0, microsecond=0)
            queue.append({**{k: src[k] for k in ("type", "caption", "duration", "files") if k in src},
                          "id": f"winner-{bid}", "post_at": slot.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "queued", "cleanup_batch": bid})
            changed_queue = True
    out_batches.append({"id": bid, "variants": vs, "test_ends": ends.isoformat(timespec="minutes"), "winner": st.get("winner"),
                        "status": "decided" if st.get("winner") else "testing", "notes": notes})
bstate_path.write_text(json.dumps(bstate, indent=1), encoding="utf-8")
if changed_queue:
    (HERE / "queue.json").write_text(json.dumps(queue, indent=1, ensure_ascii=False), encoding="utf-8")

acct = get("me", fields="username,followers_count,media_count")
# eligibility: proven by a successful trial post, disproven by Instagram's follower error, otherwise a follower estimate
if any(q.get("trial") and q.get("status") == "posted" for q in queue):
    trial_eligible = True
elif any("2207081" in q.get("last_error", "") for q in queue) or CFG.get("trial_eligible") is False:
    trial_eligible = False
else:
    trial_eligible = (acct.get("followers_count") or 0) >= CFG.get("trial_followers_estimate", 1000)
dash = {"updated": now.isoformat(timespec="minutes"), "account": {k: acct.get(k) for k in ("username", "followers_count", "media_count")},
        "trial_eligible": trial_eligible,
        "batches": sorted(out_batches, key=lambda b: b["id"], reverse=True),
        "posts": sorted([p for p in posts if not p["batch"]], key=lambda p: p["posted_at"] or "", reverse=True),
        "queue": [{"id": q["id"], "post_at": q["post_at"], "status": q["status"], "trial": bool(q.get("trial"))} for q in queue if q["status"] != "posted"]}
(DATA / "dashboard.json").write_text(json.dumps(dash, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"insights: {len(posts)} posts, {len(out_batches)} batches")
