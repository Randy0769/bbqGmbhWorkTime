// Live-Anzeige kapseln, damit interne Zustandswerte nicht im globalen Namensraum landen.
(() => {
    const counters = Array.from(document.querySelectorAll("[data-live-start]"))
        // Elemente mit ihrem als Zeitstempel gelesenen Check-in verknuepfen.
        .map((element) => ({ element, startedAt: Date.parse(element.dataset.liveStart) }))
        // Zaehler mit unlesbarem Startzeitpunkt nicht aktualisieren.
        .filter((counter) => Number.isFinite(counter.startedAt));

    const maximumSeconds = 24 * 60 * 60;
    let refreshScheduled = false;
    const totals = Array.from(document.querySelectorAll("[data-live-total]"));
    const watchdog = document.querySelector("[data-live-watchdog]");
    const warningPanel = document.querySelector("[data-live-warnings]");
    // Ausblenden gilt nur für diesen Browser-Tab und Zugang, nicht für die gespeicherten Buchungen.
    const warningStorageKey = `timetracker_v10_dismissed_warnings:${warningPanel?.dataset.warningUser || ""}`;
    let dismissedWarnings = new Set();
    try {
        const stored = JSON.parse(window.sessionStorage.getItem(warningStorageKey) || "[]");
        // Nur gespeicherte Warnungskennungen vom Typ Zeichenkette uebernehmen.
        if (Array.isArray(stored)) dismissedWarnings = new Set(stored.filter(value => typeof value === "string"));
    } catch {}
    let currentWarnings = [];
    let lastWarnings = "";
    let totalsPending = false;

    // Datum, Meldungscode und Schweregrad zu einer stabilen Warnungskennung verbinden.
    function warningIdentity(warning) {
        // Ein neuer Tag oder Schweregrad macht die Meldung wieder sichtbar.
        return JSON.stringify([warning.date, warning.code, warning.severity]);
    }

    // Ausgeblendete Warnungskennungen ausschliesslich fuer diese Tab-Sitzung speichern.
    function saveDismissedWarnings() {
        try {
            window.sessionStorage.setItem(warningStorageKey, JSON.stringify(Array.from(dismissedWarnings)));
        } catch {}
    }

    // Geaenderte Warnungen sicher als DOM-Elemente aufbauen und Ausblendungen beruecksichtigen.
    function updateWarnings(warnings) {
        if (!warningPanel || !Array.isArray(warnings)) return;
        currentWarnings = warnings;
        const snapshot = JSON.stringify(warnings);
        if (snapshot === lastWarnings) return;
        const levels = { violation: "Kritisch", warning: "Warnung", info: "Hinweis" };
        // Bereits ausgeblendete Warnungen anhand ihrer Kennung herausfiltern.
        const visibleWarnings = warnings.filter(warning => !dismissedWarnings.has(warningIdentity(warning)));
        // Jede sichtbare Warnung mit Meldung, Schweregrad und Ausblendeschaltflaeche darstellen.
        const items = visibleWarnings.map((warning) => {
            const item = document.createElement("li");
            const severity = Object.hasOwn(levels, warning.severity) ? warning.severity : "info";
            item.className = `warning-item warning-${severity}`;
            const label = document.createElement("span");
            label.className = "warning-level";
            label.textContent = levels[severity];
            const message = document.createElement("p");
            message.className = "warning-message";
            message.textContent = warning.message;
            const dismiss = document.createElement("button");
            dismiss.type = "button";
            dismiss.className = "warning-dismiss";
            dismiss.textContent = "\u00d7";
            dismiss.setAttribute("aria-label", "Meldung ausblenden");
            dismiss.title = "Meldung ausblenden";
            // Die ausgewaehlte Meldung ausblenden und den Tastaturfokus sinnvoll weitergeben.
            dismiss.addEventListener("click", () => {
                dismissedWarnings.add(warningIdentity(warning));
                saveDismissedWarnings();
                lastWarnings = "";
                updateWarnings(currentWarnings);
                const nextButton = warningPanel.querySelector(".warning-dismiss") || warningPanel.querySelector("[data-restore-warnings]");
                if (nextButton && !nextButton.hidden) nextButton.focus();
            });
            item.append(label, message, dismiss);
            return item;
        });
        warningPanel.querySelector("[data-warning-list]").replaceChildren(...items);
        warningPanel.querySelector("[data-warning-count]").textContent = String(items.length);
        const empty = warningPanel.querySelector("[data-warning-empty]");
        empty.hidden = items.length > 0;
        empty.textContent = warnings.length > 0 ? "Alle aktuellen Meldungen sind ausgeblendet." : "Keine aktuellen Warnungen.";
        warningPanel.querySelector("[data-restore-warnings]").hidden = visibleWarnings.length === warnings.length;
        lastWarnings = snapshot;
    }

    // Serverwerte auf Farbe, Balken, Sollmarkierung und barrierefreie Zeitbeschreibung uebertragen.
    function updateWatchdog(values) {
        if (!watchdog || !values.watchdog) return;
        const status = values.watchdog;
        watchdog.classList.remove("watchdog-green", "watchdog-yellow", "watchdog-red");
        watchdog.classList.add(`watchdog-${status.color}`);
        watchdog.querySelector("[data-watchdog-time]").textContent = values.today_duration;
        const track = watchdog.querySelector(".watchdog-track");
        track.setAttribute("aria-valuenow", String(Math.min(values.today_minutes, 600)));
        track.setAttribute("aria-valuetext", status.value_text);
        track.style.setProperty("--target-position", `${status.target_percent}%`);
        watchdog.querySelector(".watchdog-fill").style.width = `${status.fill_percent}%`;
        watchdog.querySelector(".watchdog-scale").style.setProperty("--target-position", `${status.target_percent}%`);
        watchdog.querySelector(".watchdog-target").textContent = `Tagessoll ${status.target_duration}`;
    }

    // Aktuelle Summen laden und bei geaenderten Buchungen die gesamte Ansicht erneuern.
    async function updateTotals() {
        if (totals.length === 0 || totalsPending || document.hidden || refreshScheduled) return;
        totalsPending = true;
        try {
            const response = await fetch("/api/clock/today", {
                cache: "no-store",
                headers: { Accept: "application/json" },
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) return;
            const values = await response.json();
            // Bei geändertem Buchungsstand auch Verlauf und abgeschlossene Salden neu laden.
            if (!values.active || String(values.revision) !== totals[0].dataset.clockRevision) {
                refreshScheduled = true;
                window.location.reload();
                return;
            }
            for (const element of totals) {
                const value = values[element.dataset.liveTotal];
                if (typeof value === "string") element.textContent = value;
            }
            updateWatchdog(values);
            updateWarnings(values.warnings);
        } catch {
            return;
        } finally {
            totalsPending = false;
        }
    }

    // Laufende Dauern lokal sekundengenau anzeigen und bei 24 Stunden ein Neuladen planen.
    function updateCounters() {
        const now = Date.now();
        for (const { element, startedAt } of counters) {
            const actualSeconds = Math.max(0, Math.floor((now - startedAt) / 1000));
            const elapsed = Math.min(maximumSeconds, actualSeconds);
            const hours = Math.floor(elapsed / 3600);
            const minutes = String(Math.floor(elapsed / 60) % 60).padStart(2, "0");
            const seconds = String(elapsed % 60).padStart(2, "0");
            element.textContent = `${hours} h ${minutes} min ${seconds} s`;
            if (actualSeconds >= maximumSeconds && !refreshScheduled) {
                refreshScheduled = true;
                // Nach kurzer Wartezeit den serverseitigen automatischen Abschluss sichtbar machen.
                window.setTimeout(() => window.location.reload(), 1000);
            }
        }
    }

    if (warningPanel) {
        // Die vom Server gerenderten Warnungen als Ausgangsstand fuer Live-Updates einlesen.
        const initialWarnings = Array.from(warningPanel.querySelectorAll("[data-warning-code]")).map(item => ({
            code: item.dataset.warningCode,
            date: item.dataset.warningDate,
            severity: item.dataset.warningSeverity,
            message: item.querySelector(".warning-message").textContent,
        }));
        // Ausblendungen zuruecksetzen und alle aktuellen Meldungen wieder anbieten.
        warningPanel.querySelector("[data-restore-warnings]").addEventListener("click", () => {
            dismissedWarnings.clear();
            saveDismissedWarnings();
            lastWarnings = "";
            updateWarnings(currentWarnings);
            warningPanel.querySelector(".warning-dismiss")?.focus();
        });
        updateWarnings(initialWarnings);
    }

    if (counters.length === 0) return;

    updateCounters();
    updateTotals();
    // Sekunden lokal anzeigen; Nettozeit und Warnungen weiterhin vom Server berechnen lassen.
    window.setInterval(updateCounters, 1000);
    window.setInterval(updateTotals, 10000);
    // Nach einem Sichtbarkeitswechsel lokale Dauer und Serverwerte sofort aktualisieren.
    document.addEventListener("visibilitychange", () => {
        updateCounters();
        updateTotals();
    });
    // Auch bei Rueckkehr aus dem Browser-Seitencache die Anzeige synchronisieren.
    window.addEventListener("pageshow", () => {
        updateCounters();
        updateTotals();
    });
})();