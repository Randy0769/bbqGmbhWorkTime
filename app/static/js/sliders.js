"use strict";

// Ausschließlich Live-Anzeige der Regler; Validierung/Speicherung bleiben in Python.
// Jeden Regler unabhaengig mit eigener Wertblase und Ereignisbehandlung initialisieren.
document.querySelectorAll(".slider-control").forEach((control) => {
    const slider = control.querySelector('input[type="range"]');
    const bubble = control.querySelector("output");

    // Den Minutenwert lesbar formatieren und die Wertblase innerhalb der Spur positionieren.
    function update() {
        const value = Number(slider.value);
        const absolute = Math.abs(value);
        const minutes = absolute % 60;
        const sign = value < 0 ? "−" : "+";
        const text = `${sign}${Math.floor(absolute / 60)} h${minutes ? ` ${String(minutes).padStart(2, "0")} min` : ""}`;
        bubble.textContent = text;
        slider.setAttribute("aria-valuetext", text);

        const fraction = (value - Number(slider.min)) / (Number(slider.max) - Number(slider.min));
        // Der Griffmittelpunkt bewegt sich innerhalb der um den Griffradius
        // verkürzten Spur. Die Blase bleibt auch an beiden Enden innerhalb der Karte.
        const center = 13 + fraction * Math.max(0, slider.clientWidth - 26);
        const half = bubble.offsetWidth / 2;
        const position = Math.max(half, Math.min(slider.clientWidth - half, center));
        bubble.style.left = `${position}px`;
        bubble.style.setProperty("--arrow-offset", `${center - position}px`);
    }

    slider.addEventListener("input", update);
    window.addEventListener("pageshow", update);
    window.addEventListener("resize", update);
    update();
});