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
METRICS = ["views", "reach", "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate", "likes", "comments", "shares",
           "saved", "reposts", "total_interactions"]
THUMBS = DATA / "thumbs"; THUMBS.mkdir(exist_ok=True)
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


def rank_key(v):
    """Ranked like the hook test it is: % past 3 seconds, then shares per 1,000 views, then average watch."""
    return (v.get("past3s") or 0, v.get("shares_per_1k") or 0, v.get("avg_watch_s") or 0)


def score(v):
    """0-100 summary for one Reel: hook hold (past 3s) 50, retention 25, shares+saves per 1,000 views 25."""
    p3 = (v.get("past3s") or 0) / 100
    ss = ((v.get("shares", 0) + v.get("saved", 0)) / max(1, v.get("views", 0))) * 1000
    return round(50 * min(1, p3 / 0.7) + 25 * min(1, (v.get("retention") or 0) / 0.5) + 25 * min(1, ss / 20), 1)


def thumb(media_id):
    """Cache the Reel's cover locally (Instagram's CDN links expire)."""
    f = THUMBS / f"{media_id}.jpg"
    if not f.exists():
        url = get(media_id, fields="thumbnail_url,media_url").get("thumbnail_url")
        if url:
            try:
                f.write_bytes(urllib.request.urlopen(url, timeout=60).read())
            except Exception:
                return None
    return f"data/thumbs/{media_id}.jpg" if f.exists() else None


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
         "retention": round(avg_s / dur, 3) if dur else 0, "opens_on": item.get("opens_on"), **m}
    v["past3s"] = round(100 - m["reels_skip_rate"], 1) if "reels_skip_rate" in m else None
    v["shares_per_1k"] = round(m.get("shares", 0) / max(1, m.get("views", 0)) * 1000, 1)
    v["thumb"] = thumb(item["media_id"])
    v["series"] = [{"t": x["t"], "views": x.get("views", 0), "past3s": round(100 - x["reels_skip_rate"], 1) if "reels_skip_rate" in x else None}
                   for x in series.get(item["media_id"], [])][-40:]
    v["hook_type"] = item.get("hook_type")
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
    vs.sort(key=rank_key, reverse=True)
    for i, v in enumerate(vs):
        v["rank"] = i + 1
    last = max(datetime.fromisoformat(v["posted_at"]) for v in vs if v["posted_at"])
    ends = last + timedelta(hours=CFG["test_hours"])
    st = bstate.setdefault(bid, {})
    best = vs[0]
    notes = []
    if (best.get("past3s") or 0) >= 60 and best["retention"] >= CFG["ad_retention"] and (best.get("shares", 0) + best.get("saved", 0)) / max(1, best.get("views", 0)) >= CFG["ad_share_rate"]:
        notes.append("Green across the board: worth testing as a paid ad.")
    worst = vs[-1]
    if len(vs) > 1 and best["retention"] - worst["retention"] > 0.1:
        notes.append(f"Hook matters here: variant {best['variant']} holds {round(best['retention']*100)}% vs {round(worst['retention']*100)}% for {worst['variant']}.")
    if best.get("past3s") is not None and worst.get("past3s") is not None and len(vs) > 1:
        notes.append(f"{best['variant']} keeps {best['past3s']}% past 3 seconds vs {worst['past3s']}% for {worst['variant']}.")
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


def acct_total(metric, since, until):
    d = get(f"{acct.get('id', 'me')}/insights", metric=metric, period="day", metric_type="total_value",
            since=int(since.timestamp()), until=int(until.timestamp()))
    return (d.get("data") or [{}])[0].get("total_value", {}).get("value")


d28 = now - timedelta(days=28)
account_stats = {"views_28d": acct_total("views", d28, now), "views_prev_28d": acct_total("views", d28 - timedelta(days=28), d28),
                 "reach_28d": acct_total("reach", d28, now), "engaged_28d": acct_total("accounts_engaged", d28, now),
                 "profile_views_28d": acct_total("profile_views", d28, now)}
rd = get(f"{acct.get('id', 'me')}/insights", metric="reach", period="day", since=int((now - timedelta(days=29)).timestamp()), until=int(now.timestamp()))
reach_daily = [{"d": x["end_time"][:10], "v": x["value"]} for x in ((rd.get("data") or [{}])[0].get("values") or [])]
reel_views = sorted(p.get("views", 0) for p in posts if p["type"] == "reel")
median_views = reel_views[len(reel_views) // 2] if reel_views else 0
# eligibility: proven by a successful trial post, disproven by Instagram's follower error, otherwise a follower estimate
if any(q.get("trial") and q.get("status") == "posted" for q in queue):
    trial_eligible = True
elif any("2207081" in q.get("last_error", "") for q in queue) or CFG.get("trial_eligible") is False:
    trial_eligible = False
else:
    trial_eligible = (acct.get("followers_count") or 0) >= CFG.get("trial_followers_estimate", 1000)
dash = {"updated": now.isoformat(timespec="minutes"), "account": {k: acct.get(k) for k in ("username", "followers_count", "media_count")},
        "stats": {**account_stats, "median_reel_views": median_views}, "reach_daily": reach_daily,
        "theme": CFG.get("theme"),
        "trial_eligible": trial_eligible,
        "batches": sorted(out_batches, key=lambda b: b["id"], reverse=True),
        "posts": sorted([p for p in posts if not p["batch"]], key=lambda p: p["posted_at"] or "", reverse=True),
        "batch_meta": {b["id"]: {"live": sum(1 for v in b["variants"]), "total": sum(1 for q in queue if q.get("batch") == b["id"]),
                                  "oldest": min((v["posted_at"] for v in b["variants"] if v["posted_at"]), default=None)} for b in out_batches},
        "queue": [{"id": q["id"], "post_at": q["post_at"], "status": q["status"], "trial": bool(q.get("trial"))} for q in queue if q["status"] != "posted"]}
(DATA / "dashboard.json").write_text(json.dumps(dash, indent=1, ensure_ascii=False), encoding="utf-8")
print(f"insights: {len(posts)} posts, {len(out_batches)} batches")
