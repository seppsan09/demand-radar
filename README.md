# demand-radar

Datensammlung für das Bedarfsradar: Jeden Sonntag holt ein GitHub-Workflow Zahlen zu Stellenanzeigen und Google-Suchtrends für die Begriffe in `config.json`. Der Montagslauf des Bedarfsradars liest `data/latest.json` und trägt auffällige Veränderungen als Fundstellen ein.

## Was gesammelt wird

**Stellenanzeigen** aus der Jobsuche der Bundesagentur für Arbeit, pro Begriff:
- `count`: Zahl der Treffer
- `median_age_days`, `share_older_60d`: Alter der Anzeigen in einer Stichprobe der ersten 100 Treffer. Viele alte Anzeigen deuten auf schwer zu besetzende Stellen hin.
- `top_employers`: häufigste Arbeitgeber in der Stichprobe

Die Jobsuche hat keine offizielle, dokumentierte Schnittstelle. Genutzt wird der öffentliche Zugang, den auch die Website verwendet ([Dokumentation der Community](https://github.com/bundesAPI/jobsuche-api)). Er kann sich ohne Vorwarnung ändern.

**Google Trends** für Deutschland, Zeitraum 3 Monate, pro Begriff:
- `recent`: Mittel der letzten zwei vollen Wochen
- `baseline`: Mittel der acht Wochen davor
- `ratio`: `recent / baseline`. Werte über 1,3 oder unter 0,7 gelten als auffällig.

Abgefragt werden pro Woche die Begriffe aller Fokus- und Kandidaten-Nischen sowie im Wechsel die Hälfte der beobachteten Nischen; pausierte Nischen gar nicht (siehe `status` in `config.json`). Zwischen den Anfragen liegen 25 Sekunden. Sperrt Google, werden die restlichen Begriffe der Woche übersprungen. Ein Wert von 0 nach deutlichem Interesse wird als `verdaechtig` markiert und nicht ausgewertet.

**Verwandte Suchanfragen** zu jedem Trends-Begriff, im selben Aufruf mitgeliefert:
- `rising`: Formulierungen, deren Interesse rund um den Begriff gerade wächst, mit Wachstumsrate oder `Breakout`
- `top`: der stabile Kern der Anfragen zum Thema

Gespeichert werden die zehn stärksten `rising`- und fünf stärksten `top`-Anfragen je Begriff. Sie zeigen, woran die Leute tatsächlich hängen, ohne dass man die Formulierung vorher erraten muss. In `latest.json` stehen sie unter `related_rising`, neue Anfragen zuerst; `neu` heißt, dass die Anfrage in keinem früheren Lauf vorkam. Über SerpApi gibt es sie nicht — dort stecken sie in einer zweiten, kostenpflichtigen Abfrage.

## Quelle der Trends-Daten

Ohne hinterlegten Schlüssel nutzt das Skript die Bibliothek `trendspyg`, die die Google-Trends-Seite über einen Browser abfragt. Von den Adressen der GitHub-Rechner sperrt Google diesen Weg nach wenigen Begriffen (HTTP 429, „unusual traffic"); der Rest der Woche wird dann übersprungen. In den Läufen von Oktober und November 2026 kamen so nur vier bis acht Begriffe pro Woche durch.

Ist das Geheimnis `SERPAPI_KEY` hinterlegt, läuft die Abfrage stattdessen über SerpApi — eine normale Schnittstelle ohne Browser, die nicht gesperrt wird. Jede erfolgreiche Abfrage zählt im Tarif; fehlgeschlagene zählen nicht.

Jeder Tarif hat neben dem Monatskontingent einen **Stundendurchsatz** (kostenlos 50, Starter 200, Developer 1000). Die Pause zwischen den Begriffen wird daraus berechnet, damit der Lauf nicht dagegen stößt. Ohne Angabe wird der kostenlose Tarif angenommen, also 50 pro Stunde und rund 76 Sekunden Pause. Bei einem anderen Tarif:

```bash
gh variable set SERPAPI_THROUGHPUT --body 200
```

`TRENDS_PAUSE` überschreibt die Rechnung, wenn du die Pause selbst setzen willst. Die überschlagene Laufzeit steht am Anfang der Trends-Phase im Protokoll; wird sie zu lang für das Zeitlimit des Workflows, steht ein Hinweis in `latest.json`.

Schlüssel hinterlegen:

```bash
gh secret set SERPAPI_KEY
```

Der kostenlose Tarif umfasst 250 Abfragen im Monat bei einer Durchsatzgrenze von 50 pro Stunde. Die Begriffe pro Woche sind darauf ausgelegt: rund 40, also höchstens 215 in einem Monat mit fünf Sonntagen. Ein manuell gestarteter Lauf zählt mit. Reicht der Durchsatz nicht, lässt sich die Pause hochsetzen — bei 75 Sekunden bleibt der Lauf unter 50 Abfragen pro Stunde und dauert gut eine Stunde:

```bash
gh variable set TRENDS_PAUSE --body 75
```

Verwandte Suchanfragen sind über SerpApi eine **zweite** Abfrage je Begriff und verdoppeln damit den Verbrauch. Sie sind deshalb abgeschaltet, bis die Repository-Variable `SERPAPI_RELATED` auf `1` steht:

```bash
gh variable set SERPAPI_RELATED --body 1
```

## Dateien

- `config.json`: Begriffe pro Nische, frei änderbar
- `scripts/collect.py`: Sammelskript
- `.github/workflows/collect.yml`: Zeitplan (Sonntag 18:00 UTC) und manueller Start
- `data/jobs_history.csv`, `data/trends_history.csv`, `data/related_history.csv`: Verlauf
- `data/latest.json`: letzter Lauf mit Veränderung gegenüber der Vorwoche und einer Fehlerliste

## Manuell starten

Unter **Actions → Wöchentliche Datensammlung → Run workflow**. Mit der Option „Google Trends überspringen“ läuft nur die Stellenabfrage, das dauert wenige Minuten.
