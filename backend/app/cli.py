"""Seed hubs and compute one score snapshot from the command line."""

from __future__ import annotations

import sys

from app.db.repository import seed_reference_data
from app.risk.engine import public_report, score_selection
from app.db import repository


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "help"
    if command in {"-h", "--help", "help"}:
        print("usage: python -m app.cli seed|score-once")
        return 0
    if command == "seed":
        seed_reference_data()
        print(f"Seeded {len(repository.list_hubs())} hubs.")
        return 0
    if command == "score-once":
        seed_reference_data()
        selection = score_selection(None, None, None, force=True)
        reports = selection.reports
        print(f"Window: {selection.window_label}")
        if selection.unavailable:
            print("No live weather, left out: " + ", ".join(selection.unavailable))
        print(f"{'hub':<18}{'overall':>10}{'winter':>10}{'flood':>10}{'hurricane':>12}{'heat':>10}{'severe':>10}  note")
        for report in sorted(reports, key=lambda item: item.overall_score, reverse=True):
            payload = public_report(report)
            repository.save_score_snapshot(payload)
            note = "fallback" if report.weather_fallback else report.sources[0].source_type
            print(
                f"{report.name:<18}"
                f"{report.overall_score:>10.1f}"
                f"{report.hazards['winter'].score:>10.1f}"
                f"{report.hazards['flood'].score:>10.1f}"
                f"{report.hazards['hurricane'].score:>12.1f}"
                f"{report.hazards['heat'].score:>10.1f}"
                f"{report.hazards['severe_storm'].score:>10.1f}  {note}"
            )
        return 0
    print(f"Unknown command: {command}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
