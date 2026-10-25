"""Sammelt wöchentlich Stellenanzeigen (Jobsuche der Bundesagentur) und
Google-Trends-Verläufe für die Begriffe in config.json.

Ausgaben (alle unter data/):
  jobs_history.csv    eine Zeile pro Lauf und Begriff
  trends_history.csv  eine Zeile pro Lauf und Begriff
  latest.json         Zusammenfassung des letzten Laufs mit Veränderungen

Fehler bei einzelnen Begriffen brechen den Lauf nicht ab; sie stehen in
latest.json unter "fehler".
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TODAY = os.environ.get("RUN_DATE") or date.today().isoformat()

BA_URLS = [
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
]
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "demand-radar/1.0"}

JOBS_FIELDS = ["date", "niche", "term", "count", "sample", "median_age_days", "share_older_60d", "top_employers"]
TRENDS_FIELDS = ["date", "niche", "term", "recent", "baseline", "ratio", "source", "status"]


# ---------- Hilfen ----------

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def last_value(history: list[dict], niche: str, term: str, field: str):
    """Letzter Wert eines Begriffs vor dem heutigen Lauf."""
    prev = [r for r in history if r["niche"] == niche and r["term"] == term and r["date"] < TODAY and r.get(field) not in ("", None)]
    if not prev:
        return None
    try:
        return float(sorted(prev, key=lambda r: r["date"])[-1][field])
    except ValueError:
        return None


def pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return round((new - old) / old * 100, 1)


# ---------- Stellenanzeigen ----------

def age_days(ad: dict) -> int | None:
    for key in ("aktuelleVeroeffentlichungsdatum", "veroeffentlichungsdatum", "modifikationsTimestamp"):
        v = ad.get(key)
        if v:
            try:
                d = datetime.fromisoformat(str(v)[:10]).date()
                return (date.fromisoformat(TODAY) - d).days
            except ValueError:
                continue
    return None


def parse_jobs(payload: dict) -> dict:
    ads = payload.get("stellenangebote") or []
    try:
        count = int(str(payload.get("maxErgebnisse", len(ads))).replace(".", ""))
    except ValueError:
        count = len(ads)
    ages = [a for a in (age_days(ad) for ad in ads) if a is not None and a >= 0]
    employers: dict[str, int] = {}
    for ad in ads:
        name = (ad.get("arbeitgeber") or "").strip()
        if name:
            employers[name] = employers.get(name, 0) + 1
    top = sorted(employers.items(), key=lambda kv: -kv[1])[:3]
    return {
        "count": count,
        "sample": len(ads),
        "median_age_days": statistics.median(ages) if ages else "",
        "share_older_60d": round(sum(a > 60 for a in ages) / len(ages), 2) if ages else "",
        "top_employers": "; ".join(f"{n} ({c})" for n, c in top),
    }


def fetch_jobs(term: str, session: requests.Session) -> dict:
    params = {"was": term, "size": 100, "page": 1, "angebotsart": 1}
    last_err = None
    for url in BA_URLS:
        try:
            r = session.get(url, params=params, headers=BA_HEADERS, timeout=30)
            if r.status_code == 200:
                return parse_jobs(r.json())
            last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
    raise RuntimeError(last_err or "unbekannter Fehler")


# ---------- Google Trends ----------

def summarize_series(values: list[float]) -> tuple[float, float, float | None]:
    """recent = Mittel der letzten 2 vollen Wochen, baseline = Mittel der 8 Wochen davor."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 4:
        raise RuntimeError("zu wenige Datenpunkte")
    # Die letzte Woche ist bei Google oft unvollständig: weglassen.
    body = vals[:-1]
    recent = statistics.mean(body[-2:])
    base_slice = body[-10:-2] or body[:-2]
    baseline = statistics.mean(base_slice) if base_slice else 0.0
    ratio = round(recent / baseline, 2) if baseline else None
    return round(recent, 1), round(baseline, 1), ratio


def trends_serpapi(term: str, key: str, session: requests.Session) -> list[float]:
    r = session.get("https://serpapi.com/search.json", params={
        "engine": "google_trends", "q": term, "geo": "DE", "date": "today 3-m",
        "data_type": "TIMESERIES", "api_key": key,
    }, timeout=60)
    r.raise_for_status()
    timeline = (r.json().get("interest_over_time") or {}).get("timeline_data") or []
    out = []
    for p in timeline:
        vals = p.get("values") or [{}]
        out.append(float(vals[0].get("extracted_value", 0)))
    return out


def trends_trendspyg(term: str) -> list[float]:
    from trendspyg import download_google_trends_interest_over_time  # type: ignore
    series = download_google_trends_interest_over_time(term, geo="DE", timeframe="today 3-m")
    return [float(p["value"]) for p in series]


def fetch_trend(term: str, session: requests.Session) -> tuple[list[float], str]:
    key = os.environ.get("SERPAPI_KEY")
    if key:
        return trends_serpapi(term, key, session), "serpapi"
    return trends_trendspyg(term), "trendspyg"


# ---------- Lauf ----------

def main() -> int:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["nischen"]
    DATA.mkdir(exist_ok=True)
    jobs_path, trends_path = DATA / "jobs_history.csv", DATA / "trends_history.csv"
    jobs_hist, trends_hist = read_csv(jobs_path), read_csv(trends_path)
    session = requests.Session()
    errors: list[str] = []
    skip_trends = os.environ.get("SKIP_TRENDS") == "1"

    job_rows, trend_rows = [], []
    for niche, c in cfg.items():
        for term in c.get("jobs", []):
            try:
                res = fetch_jobs(term, session)
                job_rows.append({"date": TODAY, "niche": niche, "term": term, **res})
            except Exception as e:  # noqa: BLE001
                errors.append(f"Jobs '{term}': {e}")
            time.sleep(1)

    if not skip_trends:
        for niche, c in cfg.items():
            for term in c.get("trends", []):
                try:
                    values, source = fetch_trend(term, session)
                    recent, baseline, ratio = summarize_series(values)
                    trend_rows.append({"date": TODAY, "niche": niche, "term": term, "recent": recent,
                                       "baseline": baseline, "ratio": ratio if ratio is not None else "",
                                       "source": source, "status": "ok"})
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Trends '{term}': {e}")
                    trend_rows.append({"date": TODAY, "niche": niche, "term": term, "status": "fehler"})
                time.sleep(8)

    # Heutige Zeilen nicht doppelt schreiben, falls der Lauf wiederholt wird.
    if any(r["date"] == TODAY for r in jobs_hist) or any(r["date"] == TODAY for r in trends_hist):
        jobs_hist = [r for r in jobs_hist if r["date"] != TODAY]
        trends_hist = [r for r in trends_hist if r["date"] != TODAY]
        for p, fields, rows in ((jobs_path, JOBS_FIELDS, jobs_hist), (trends_path, TRENDS_FIELDS, trends_hist)):
            p.unlink(missing_ok=True)
            append_csv(p, fields, rows)
    append_csv(jobs_path, JOBS_FIELDS, job_rows)
    append_csv(trends_path, TRENDS_FIELDS, trend_rows)

    latest = {
        "date": TODAY,
        "hinweis": "count = Treffer in der Jobsuche der Bundesagentur; median_age_days und share_older_60d aus einer Stichprobe der ersten 100 Anzeigen. Trends: recent = Mittel der letzten 2 vollen Wochen, baseline = 8 Wochen davor, ratio = recent/baseline (Werte relativ, 0–100 je Begriff).",
        "jobs": [{**r, "prev_count": last_value(jobs_hist, r["niche"], r["term"], "count"),
                  "change_pct": pct_change(float(r["count"]), last_value(jobs_hist, r["niche"], r["term"], "count"))}
                 for r in job_rows],
        "trends": [r for r in trend_rows if r.get("status") == "ok"],
        "fehler": errors,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(job_rows)} Jobbegriffe, {len(latest['trends'])} Trendbegriffe, {len(errors)} Fehler")
    for e in errors:
        print("  -", e)
    # Nur scheitern, wenn gar nichts ankam.
    return 0 if job_rows or latest["trends"] else 1


if __name__ == "__main__":
    sys.exit(main())
