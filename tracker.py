"""
Vinted Go locker tracker (v3) with phone alerts.

Every run, for each locker in lockers.json:
1. Detail counts (XS / S / M): the numbers shown when you click a locker.
2. Filter check (XS_ok / S_ok / M_ok): does the locker still appear on the map
   when the "Compartiments disponibles" filter for that size is on?
3. Alert: for each size listed in "watch", sends a push notification (ntfy app)
   when the filter check flips from "no" to "yes" (and back).

Results are appended to data/history.csv, last known state in data/state.json.
Standard library only.
"""

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

BASE = "https://vintedgo.com"
PAGE = BASE + "/fr/carrier-locations"
QS = "?country=fr&region=europe"
ROOT = Path(__file__).parent
LOCKERS_FILE = ROOT / "lockers.json"
CSV_FILE = ROOT / "data" / "history.csv"
STATE_FILE = ROOT / "data" / "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal locker availability tracker)"}
SIZES = ["XS", "S", "M"]
BOX = 0.003  # half-size of the map area searched around each locker (about 300 m)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={**HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


# ---------- Signal 1: detail counts (server action) ----------

def discover_action_ids():
    """The detail call is a Next.js server action whose ID can change at every
    Vinted Go deploy, so we read the current ID(s) from the page's JS."""
    html = http(PAGE + QS)
    chunks = set(re.findall(r'/_next/static/chunks/app/[^"\']*carrier-locations/page-[a-f0-9]+\.js', html))
    ids = []
    for chunk in chunks:
        ids += re.findall(r'"([a-f0-9]{40,44})"', http(BASE + chunk))
    return list(dict.fromkeys(ids))


def fetch_detail(locker_id, action_id):
    body = json.dumps([str(locker_id)]).encode()
    text = http(PAGE + QS, data=body, headers={
        "Accept": "text/x-component",
        "Next-Action": action_id,
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": BASE,
        "Referer": PAGE + QS,
    })
    for line in text.splitlines():
        if line.startswith("1:{"):
            data = json.loads(line[2:])
            if "available_sizes" in data:
                return data
    print(f"  [debug] action {action_id[:10]}...: no locker data. Response starts with: {text[:300]!r}")
    return None


def get_detail(locker_id, action_ids):
    for aid in action_ids:
        try:
            data = fetch_detail(locker_id, aid)
            if data:
                return data
        except urllib.error.HTTPError as e:
            print(f"  [debug] action {aid[:10]}...: HTTP {e.code}. Body starts with: {e.read()[:300]!r}")
        except Exception as e:
            print(f"  [debug] action {aid[:10]}...: {type(e).__name__}: {e}")
    return None


# ---------- Signal 2: size filter on the map ----------

def appears_on_map(locker_id, lat, lng, size=None):
    bounds = json.dumps({"south": lat - BOX, "west": lng - BOX, "north": lat + BOX, "east": lng + BOX},
                        separators=(",", ":"))
    url = PAGE + QS + "&bounds=" + quote(bounds)
    if size:
        url += "&sizes=" + size
    text = http(url, headers={"RSC": "1"})
    # The ID can appear as "id":123 (RSC payload) or \"id\":123 (inside HTML).
    return re.search(r'\\?"id\\?":%d\b' % int(locker_id), text) is not None


def filter_check(locker_id, lat, lng):
    """Returns {"XS_ok": "yes"/"no"/"?", ...}. "?" = could not tell."""
    out = {f"{s}_ok": "?" for s in SIZES}
    try:
        if not appears_on_map(locker_id, lat, lng):
            print("  [debug] locker not found on unfiltered map, filter check skipped")
            return out
        for s in SIZES:
            out[f"{s}_ok"] = "yes" if appears_on_map(locker_id, lat, lng, s) else "no"
    except Exception as e:
        print(f"  [debug] filter check failed: {type(e).__name__}: {e}")
    return out


# ---------- Notifications (ntfy) ----------

def notify(title, message, lat=None, lng=None, priority="default"):
    if not NTFY_TOPIC:
        print(f"  [notify skipped, no NTFY_TOPIC] {title}: {message}")
        return
    headers = {"Title": title, "Priority": priority, "Tags": "package"}
    if lat is not None:
        headers["Click"] = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
    try:
        req = urllib.request.Request("https://ntfy.sh/" + NTFY_TOPIC, data=message.encode("utf-8"),
                                     headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        print(f"  [notified] {title}")
    except Exception as e:
        print(f"  [debug] notification failed: {type(e).__name__}: {e}")


def check_alerts(locker, data, ok, state, time_str):
    key = str(data["id"])
    name = data.get("name", "").strip()
    prev = state.get(key, {})
    first_run = not prev
    for size in locker.get("watch", []):
        now_v, before = ok.get(f"{size}_ok"), prev.get(size)
        if now_v not in ("yes", "no"):
            continue  # "?" = could not tell, keep the previous state
        if first_run:
            label = "AVAILABLE" if now_v == "yes" else "not available"
            notify(f"Tracker started: {name}",
                   f"Watching size {size}. Right now: {label} ({time_str}). "
                   f"You will get an alert when this changes.", data["lat"], data["lng"])
        elif before == "no" and now_v == "yes":
            notify(f"{size} available at {name}",
                   f"The {size} filter shows {name} again ({time_str}). Go check the locker screen!",
                   data["lat"], data["lng"], priority="high")
        elif before == "yes" and now_v == "no":
            notify(f"{size} gone at {name}",
                   f"The {size} filter hides {name} again ({time_str}).", data["lat"], data["lng"])
        state.setdefault(key, {})[size] = now_v


# ---------- CSV ----------

FIELDS = ["timestamp_utc", "date_paris", "time_paris", "weekday", "locker_id", "name", "address",
          "XS", "S", "M", "XS_ok", "S_ok", "M_ok", "vinted_status"]


def save(rows):
    CSV_FILE.parent.mkdir(exist_ok=True)
    if CSV_FILE.exists():
        with CSV_FILE.open(encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        if header != FIELDS:  # old format: keep it aside and start a new file
            CSV_FILE.rename(CSV_FILE.with_name("history_v1.csv"))
    new_file = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


# ---------- Main ----------

def main():
    lockers = json.loads(LOCKERS_FILE.read_text())
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    action_ids = discover_action_ids()
    if not action_ids:
        sys.exit("Could not find the server action ID: the Vinted Go page structure changed.")

    now = datetime.now(timezone.utc)
    paris = now.astimezone(ZoneInfo("Europe/Paris"))
    rows, failures = [], []

    for locker in lockers:
        data = get_detail(locker["id"], action_ids)
        if not data:
            failures.append(locker["id"])
            continue
        sizes = data.get("available_sizes") or {}
        ok = filter_check(data["id"], data["lat"], data["lng"])
        status = (data.get("operational_status") or {}).get("status", "")
        rows.append({
            "timestamp_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date_paris": paris.strftime("%Y-%m-%d"),
            "time_paris": paris.strftime("%H:%M"),
            "weekday": paris.strftime("%A"),
            "locker_id": data["id"],
            "name": data.get("name", "").strip(),
            "address": data.get("address", ""),
            **{s: sizes.get(s, "") for s in SIZES},
            **ok,
            "vinted_status": status,
        })
        print(f'{data.get("name", "").strip()}: '
              + ", ".join(f"{s}={sizes.get(s)} (filter: {ok[s + '_ok']})" for s in SIZES))
        check_alerts(locker, data, ok, state, paris.strftime("%H:%M"))

    if rows:
        save(rows)
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if failures:
        sys.exit(f"Failed to read lockers: {failures}")


if __name__ == "__main__":
    main()
