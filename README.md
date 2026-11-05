# BBQ TimeTracker

Lokaler Arbeitszeit-Tracker mit Anmeldung, Arbeitszeiterfassung, Verlauf und konfigurierbaren Einstellungen. Die Daten werden in einer SQLite-Datenbank gespeichert.

## Starten

**Release für Windows:** `dist/TimeTracker.exe` starten. Beim ersten Aufruf ein Konto einrichten.

**Aus dem Quellcode:**

Python installieren, Abhängigkeiten einrichten und den Server starten:

```powershell
python -m pip install -r requirements.txt
python start.py
```

Anschließend im Browser [http://127.0.0.1:8000](http://127.0.0.1:8000) öffnen. Beim ersten Aufruf ein Konto einrichten.

Die Datenbank liegt beim Quellstart unter `data/timetracker.sqlite3`, beim EXE-Release im lokalen App-Datenordner. Ein anderer Pfad kann über `TIMETRACKER_DB_PATH` festgelegt werden.
