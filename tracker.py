"""
Vinted Go locker tracker.

Reads the free compartments (XS / S / M) of the lockers listed in lockers.json
from the public Vinted Go map (vintedgo.com/fr/carrier-locations) and appends
one row per locker to data/history.csv.

Standard library only: no pip install needed.
"""

import csv
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://vintedgo.com"
PAGE = BASE + "/fr/carrier-locations"
ROOT = Path(__file__).parent
LOCKERS_FILE = ROOT / "lockers.json"
CSV_FILE = ROOT / "data" / "history.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal locker availability tracker)"}
SIZES = ["XS", "S", "M"]


def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={**HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def discover_action_ids():
    """The locker detail call is a Next.js server action whose ID can change
    at every Vinted Go deploy, so we read the current ID(s) from the page's JS."""
    html = http(PAGE + "?country=fr&region=europe")
    chunks = set(re.findall(r'/_next/static/chunks/app/[^"\']*carrier-locations/page-[a-f0-9]+\.js', html))
    ids = []
    for chunk in chunks:
        js = http(BASE + chunk)
        ids += re.findall(r'"([a-f0-9]{40,44})"', js)
    return list(dict.fromkeys(ids))


def fetch_locker(locker_id, action_id):
    body = json.dumps([str(locker_id)]).encode()
        text = http(PAGE + "?country=fr&region=europe", data=body, headers={
        "Accept": "text/x-component",
        "Next-Action": action_id,
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": BASE,
        "Referer": PAGE + "?country=fr&region=europe",
    })
    for line in text.splitlines():
        if line.startswith("1:{"):
            data = json.loads(line[2:])
            if "available_sizes" in data:
                return data
    print(f"  [debug] action {action_id[:10]}...: no locker data. Response starts with: {text[:300]!r}")
    return None


def main():
    lockers = json.loads(LOCKERS_FILE.read_text())
    action_ids = discover_action_ids()
    if not action_ids:
        sys.exit("Could not find the server action ID: the Vinted Go page structure changed.")
    print(f"[debug] action IDs found: {[a[:10] + '...' for a in action_ids]}")

    now = datetime.now(timezone.utc)
    paris = now.astimezone(ZoneInfo("Europe/Paris"))
    rows, failures = [], []

    for locker in lockers:
        data = None
        for aid in action_ids:
            try:
                data = fetch_locker(locker["id"], aid)
            except urllib.error.HTTPError as e:
                print(f"  [debug] action {aid[:10]}...: HTTP {e.code}. Body starts with: {e.read()[:300]!r}")
                data = None
            except Exception as e:
                print(f"  [debug] action {aid[:10]}...: {type(e).__name__}: {e}")
                data = None
            if data:
                break
        if not data:
            failures.append(locker["id"])
            continue
        sizes = data.get("available_sizes") or {}
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
            "vinted_status": status,
        })
        print(f'{data.get("name")}: ' + ", ".join(f"{s}={sizes.get(s)}" for s in SIZES) + f" ({status})")

    if rows:
        CSV_FILE.parent.mkdir(exist_ok=True)
        new_file = not CSV_FILE.exists()
        with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            if new_file:
                w.writeheader()
            w.writerows(rows)

    if failures:
        sys.exit(f"Failed to read lockers: {failures}")


if __name__ == "__main__":
    main()
