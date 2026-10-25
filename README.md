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

Quelle ist standardmäßig die Bibliothek `trendspyg`, die die Google-Trends-Seite über einen Browser abfragt. Das ist langsam und kann von Google gebremst werden. Ist im Repository das Geheimnis `SERPAPI_KEY` hinterlegt, wird stattdessen SerpApi genutzt (kostenpflichtig, stabiler).

## Dateien

- `config.json`: Begriffe pro Nische, frei änderbar
- `scripts/collect.py`: Sammelskript
- `.github/workflows/collect.yml`: Zeitplan (Sonntag 18:00 UTC) und manueller Start
- `data/jobs_history.csv`, `data/trends_history.csv`: Verlauf
- `data/latest.json`: letzter Lauf mit Veränderung gegenüber der Vorwoche und einer Fehlerliste

## Manuell starten

Unter **Actions → Wöchentliche Datensammlung → Run workflow**. Mit der Option „Google Trends überspringen“ läuft nur die Stellenabfrage, das dauert wenige Minuten.
