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
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs",
    "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
]
BA_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "demand-radar/1.0"}

DEBUG: dict = {}

JOBS_FIELDS = ["date", "niche", "term", "count", "sample", "median_age_days", "share_older_60d", "top_employers"]
TRENDS_FIELDS = ["date", "niche", "term", "recent", "baseline", "ratio", "punkte", "raster", "source", "status"]
RELATED_FIELDS = ["date", "niche", "term", "related", "kind", "value", "formatted"]

# Google liefert bis zu 25 verwandte Anfragen je Art. Gespeichert wird nur die Spitze:
# "rising" zeigt, woran das Interesse gerade waechst, "top" den stabilen Kern.
RELATED_LIMIT = {"rising": 10, "top": 5}


# ---------- Hilfen ----------

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    new = not path.exists()
    if not new:
        with path.open(newline="", encoding="utf-8") as f:
            kopf = next(csv.reader(f), [])
        # Kamen Spalten hinzu, muss die Datei einmal mit neuer Kopfzeile
        # umgeschrieben werden - sonst haetten die neuen Zeilen mehr Felder
        # als die Kopfzeile ankuendigt.
        if kopf and kopf != fields:
            alt = read_csv(path)
            path.unlink()
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for r in alt:
                    w.writerow({k: r.get(k, "") for k in fields})
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
    for key in ("datumErsteVeroeffentlichung", "ersteVeroeffentlichungsdatum", "aktuelleVeroeffentlichungsdatum", "aenderungsdatum", "modifikationsTimestamp"):
        v = ad.get(key)
        if v:
            try:
                d = datetime.fromisoformat(str(v)[:10]).date()
                return (date.fromisoformat(TODAY) - d).days
            except ValueError:
                continue
    return None


def parse_jobs(payload: dict) -> dict:
    ads = payload.get("stellenangebote") or payload.get("ergebnisliste") or []
    try:
        count = int(str(payload.get("maxErgebnisse", len(ads))).replace(".", ""))
    except ValueError:
        count = len(ads)
    ages = [a for a in (age_days(ad) for ad in ads) if a is not None and a >= 0]
    employers: dict[str, int] = {}
    for ad in ads:
        emp = ad.get("firma") or ad.get("arbeitgeber") or ""
        if isinstance(emp, dict):
            emp = emp.get("name") or emp.get("anzeigeName") or ""
        name = str(emp).strip()
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
                payload = r.json()
                DEBUG.setdefault("endpunkt", url)
                return parse_jobs(payload)
            last_err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
    raise RuntimeError(last_err or "unbekannter Fehler")


# ---------- Google Trends ----------

def summarize_series(values: list[float]) -> tuple[float, float, float | None, str]:
    """recent = Mittel der letzten 2 vollen Wochen, baseline = Mittel der 8 Wochen davor.

    Google liefert fuer ein Dreimonatsfenster je nach Begriff taegliche oder
    woechentliche Punkte. Bei taeglichen Punkten sind zwei Wochen 14 Werte, nicht
    zwei - sonst vergliche man zwei Tage mit acht Tagen, und ein einzelner Tag ohne
    Suchanfragen sieht dann wie ein Einbruch aus. Das Raster wird mitgegeben, damit
    im Verlauf erkennbar bleibt, worauf eine Zeile beruht.
    """
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 4:
        raise RuntimeError("zu wenige Datenpunkte")
    # Ein Dreimonatsfenster hat rund 13 Wochen oder rund 90 Tage.
    taeglich = len(vals) > 20
    raster = "taeglich" if taeglich else "woechentlich"
    schritt = 7 if taeglich else 1
    # Der letzte Punkt ist bei Google oft unvollständig: weglassen.
    body = vals[:-1]
    n_recent, n_base = 2 * schritt, 8 * schritt
    recent = statistics.mean(body[-n_recent:])
    base_slice = body[-(n_recent + n_base):-n_recent] or body[:-n_recent]
    baseline = statistics.mean(base_slice) if base_slice else 0.0
    ratio = round(recent / baseline, 2) if baseline else None
    return round(recent, 1), round(baseline, 1), ratio, raster


def serpapi_get(data_type: str, term: str, key: str, session: requests.Session) -> dict:
    """Eine Abfrage an SerpApi. Jede erfolgreiche Abfrage zaehlt im Tarif."""
    r = session.get("https://serpapi.com/search.json", params={
        "engine": "google_trends", "q": term, "geo": "DE", "date": "today 3-m",
        "hl": "de", "data_type": data_type, "api_key": key,
    }, timeout=60)
    r.raise_for_status()
    body = r.json()
    # SerpApi meldet Kontingent- und Parameterfehler mit HTTP 200 im Feld "error".
    if body.get("error"):
        raise RuntimeError(str(body["error"])[:200])
    return body


def trends_serpapi(term: str, key: str, session: requests.Session) -> list[float]:
    timeline = (serpapi_get("TIMESERIES", term, key, session).get("interest_over_time") or {}).get("timeline_data") or []
    out = []
    for point in timeline:
        vals = point.get("values") or [{}]
        out.append(float(vals[0].get("extracted_value", 0)))
    return out


def related_serpapi(term: str, key: str, session: requests.Session) -> list[dict]:
    """Verwandte Anfragen. Zweite, eigene Abfrage: verdoppelt den Verbrauch,
    darum nur mit SERPAPI_RELATED=1."""
    blocks = serpapi_get("RELATED_QUERIES", term, key, session).get("related_queries") or {}
    related = []
    for kind, limit in RELATED_LIMIT.items():
        for q in (blocks.get(kind) or [])[:limit]:
            query = (q.get("query") or "").strip()
            if not query:
                continue
            related.append({"related": query, "kind": kind,
                            "value": q.get("extracted_value", ""), "formatted": q.get("value", "")})
    return related


def trends_trendspyg(term: str) -> tuple[list[float], list[dict]]:
    """Explore-Aufruf: Zeitreihe und verwandte Suchanfragen in einem Seitenaufruf.

    Kostet nicht mehr als die reine Zeitreihe, liefert aber zusaetzlich die
    Formulierungen, die um den Begriff herum wachsen.
    """
    from trendspyg import download_google_trends_explore  # type: ignore
    env = download_google_trends_explore(term, geo="DE", timeframe="today 3-m", include_geo=False)
    values = [float(p["value"]) for p in env.get("interest_over_time") or []]
    related = []
    for kind, limit in RELATED_LIMIT.items():
        for q in ((env.get("related_queries") or {}).get(kind) or [])[:limit]:
            query = (q.get("query") or "").strip()
            if not query:
                continue
            related.append({"related": query, "kind": kind,
                            "value": q.get("value", ""), "formatted": q.get("formatted_value", "")})
    return values, related


def fetch_trend(term: str, session: requests.Session) -> tuple[list[float], list[dict], str]:
    """Zeitreihe, verwandte Anfragen und Quelle.

    Ueber SerpApi kosten die verwandten Anfragen eine zweite Abfrage je Begriff,
    also den doppelten Verbrauch. Darum nur mit SERPAPI_RELATED=1.
    """
    key = os.environ.get("SERPAPI_KEY")
    if key:
        values = trends_serpapi(term, key, session)
        related = related_serpapi(term, key, session) if os.environ.get("SERPAPI_RELATED") == "1" else []
        return values, related, "serpapi"
    return (*trends_trendspyg(term), "trendspyg")


# ---------- Lauf ----------

def main() -> int:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["nischen"]
    DATA.mkdir(exist_ok=True)
    jobs_path, trends_path = DATA / "jobs_history.csv", DATA / "trends_history.csv"
    related_path = DATA / "related_history.csv"
    jobs_hist, trends_hist = read_csv(jobs_path), read_csv(trends_path)
    related_hist = read_csv(related_path)
    session = requests.Session()
    errors: list[str] = []
    skip_trends = os.environ.get("SKIP_TRENDS") == "1"

    job_rows, trend_rows, related_rows = [], [], []
    for niche, c in cfg.items():
        for term in c.get("jobs", []):
            try:
                res = fetch_jobs(term, session)
                job_rows.append({"date": TODAY, "niche": niche, "term": term, **res})
            except Exception as e:  # noqa: BLE001
                errors.append(f"Jobs '{term}': {e}")
            time.sleep(1)

    if not skip_trends:
        week = date.fromisoformat(TODAY).isocalendar()[1]
        queue = []
        for niche, c in cfg.items():
            st = c.get("status", "beobachten")
            if st == "pausiert":
                continue
            for i, term in enumerate(c.get("trends", [])):
                # Fokus und Kandidaten jede Woche, beobachtete Nischen im Wechsel (gerade/ungerade Woche).
                if st == "beobachten" and (i + len(niche)) % 2 != week % 2:
                    continue
                queue.append((niche, term))
        # Der Browserweg braucht eine Hoeflichkeitspause. Bei SerpApi richtet sie
        # sich nach dem Stundendurchsatz des Tarifs (SERPAPI_THROUGHPUT; Free 50,
        # Starter 200, Developer 1000) - sonst laufen die Abfragen in das
        # Stundenlimit. Mit verwandten Anfragen kostet ein Begriff zwei Abfragen.
        if os.environ.get("SERPAPI_KEY"):
            je_begriff = 2 if os.environ.get("SERPAPI_RELATED") == "1" else 1
            durchsatz = float(os.environ.get("SERPAPI_THROUGHPUT") or 50)
            standard = round(3600 * je_begriff / durchsatz * 1.05, 1) if durchsatz > 0 else 1.0
        else:
            standard = 25.0
        pause = float(os.environ.get("TRENDS_PAUSE") or standard)
        dauer = len(queue) * pause / 60
        print(f"{len(queue)} Trendbegriffe, {pause:g} s Pause, ueberschlagen {dauer:.0f} min")
        if dauer > 150:
            errors.append(f"Trends: geplante Laufzeit {dauer:.0f} min liegt nahe am "
                          "Zeitlimit des Workflows (170 min)")
        blocked = False
        for niche, term in queue:
            if blocked:
                trend_rows.append({"date": TODAY, "niche": niche, "term": term, "status": "uebersprungen"})
                continue
            try:
                values, related, source = fetch_trend(term, session)
                recent, baseline, ratio, raster = summarize_series(values)
                # Null nach deutlichem Interesse ist fast immer ein Abrufproblem, kein echter Einbruch.
                status = "verdaechtig" if recent == 0 and baseline >= 10 else "ok"
                trend_rows.append({"date": TODAY, "niche": niche, "term": term, "recent": recent,
                                   "baseline": baseline, "ratio": ratio if ratio is not None else "",
                                   "punkte": len(values), "raster": raster,
                                   "source": source, "status": status})
                for q in related:
                    related_rows.append({"date": TODAY, "niche": niche, "term": term, **q})
            except Exception as e:  # noqa: BLE001
                msg = str(e).splitlines()[0][:200]
                errors.append(f"Trends '{term}': {msg}")
                trend_rows.append({"date": TODAY, "niche": niche, "term": term, "status": "fehler"})
                # Bei einer Sperre durch Google den Rest auslassen statt weiter anzufragen.
                low = msg.lower()
                if "429" in msg or "blocking" in low or "unusual traffic" in low:
                    blocked = True
                    errors.append("Trends: Sperre durch Google, restliche Begriffe dieser Woche übersprungen")
                elif any(w in low for w in ("run out of searches", "exhaust", "invalid api key", "unauthorized")):
                    blocked = True
                    errors.append(f"Trends: SerpApi verweigert weitere Abfragen ({msg}), restliche Begriffe übersprungen")
            time.sleep(pause)

    if trend_rows and not related_rows:
        ok_quellen = {r.get("source") for r in trend_rows if r.get("status") == "ok"}
        erwartet = "trendspyg" in ok_quellen or (
            "serpapi" in ok_quellen and os.environ.get("SERPAPI_RELATED") == "1")
        if erwartet:
            errors.append("Trends: keine verwandten Suchanfragen geliefert "
                          "(bei trendspyg oft Folge der Bremse durch Google)")

    # Heutige Zeilen nicht doppelt schreiben, falls der Lauf wiederholt wird.
    if any(r["date"] == TODAY for r in jobs_hist + trends_hist + related_hist):
        jobs_hist = [r for r in jobs_hist if r["date"] != TODAY]
        trends_hist = [r for r in trends_hist if r["date"] != TODAY]
        related_hist = [r for r in related_hist if r["date"] != TODAY]
        for path, fields, rows in ((jobs_path, JOBS_FIELDS, jobs_hist),
                                   (trends_path, TRENDS_FIELDS, trends_hist),
                                   (related_path, RELATED_FIELDS, related_hist)):
            path.unlink(missing_ok=True)
            append_csv(path, fields, rows)
    append_csv(jobs_path, JOBS_FIELDS, job_rows)
    append_csv(trends_path, TRENDS_FIELDS, trend_rows)
    if related_rows:
        append_csv(related_path, RELATED_FIELDS, related_rows)

    # Verwandte Anfragen fuer latest.json: wachsende Formulierungen, neue zuerst.
    bekannt = {(r["niche"], r["term"], r["related"]) for r in related_hist if r["date"] < TODAY}

    def rising_value(r) -> float:
        try:
            return float(r["value"])
        except (TypeError, ValueError):
            return 0.0

    rising = [{"niche": r["niche"], "term": r["term"], "related": r["related"],
               "wachstum": r["formatted"],
               "neu": (r["niche"], r["term"], r["related"]) not in bekannt}
              for r in related_rows if r["kind"] == "rising"]
    rising.sort(key=lambda r: (not r["neu"], r["term"]))

    latest = {
        "date": TODAY,
        "hinweis": "count = Treffer in der Jobsuche der Bundesagentur; median_age_days und share_older_60d aus einer Stichprobe der ersten 100 Anzeigen. Trends: recent = Mittel der letzten 2 vollen Wochen, baseline = 8 Wochen davor, ratio = recent/baseline (Werte relativ, 0–100 je Begriff). related_rising = Suchanfragen, die rund um einen Begriff wachsen; 'neu' heisst, dass die Anfrage in keinem frueheren Lauf auftauchte.",
        "jobs": [{**r, "prev_count": last_value(jobs_hist, r["niche"], r["term"], "count"),
                  "change_pct": pct_change(float(r["count"]), last_value(jobs_hist, r["niche"], r["term"], "count"))}
                 for r in job_rows],
        "trends": [r for r in trend_rows if r.get("status") == "ok"],
        "trends_status": {k: sum(1 for r in trend_rows if r.get("status") == k) for k in ("ok", "verdaechtig", "fehler", "uebersprungen")},
        "related_rising": rising[:60],
        "fehler": errors,
        "jobsuche_endpunkt": DEBUG.get("endpunkt"),
    }
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(job_rows)} Jobbegriffe, {len(latest['trends'])} Trendbegriffe, "
          f"{len(related_rows)} verwandte Anfragen ({sum(1 for r in rising if r['neu'])} neu), {len(errors)} Fehler")
    for e in errors:
        print("  -", e)
    # Nur scheitern, wenn gar nichts ankam.
    return 0 if job_rows or latest["trends"] else 1


if __name__ == "__main__":
    sys.exit(main())
