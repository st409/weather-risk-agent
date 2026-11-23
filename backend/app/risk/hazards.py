"""FEMA National Risk Index snapshot for the ten hub counties."""

from __future__ import annotations

import csv
import json
from functools import lru_cache

from app.config import BACKEND_ROOT
from app.risk.types import HazardBaseline

CSV_PATH = BACKEND_ROOT / "data" / "fema_nri_snapshot.csv"
META_PATH = BACKEND_ROOT / "data" / "fema_nri_snapshot_meta.json"

SOURCE_NOTE = (
    "Published FEMA hazard-type risk index scores for the hub county. "
    "Large urban counties often score near 100 because the index includes expected loss, "
    "not only how often the hazard occurs. Blank cells were null in the service and are treated as 0."
)


def _blank(value: str | None) -> tuple[float, bool]:
    text = (value or "").strip()
    if text == "":
        return 0.0, False
    return float(text), True


@lru_cache(maxsize=1)
def snapshot_meta() -> dict:
    if not META_PATH.exists():
        return {"retrieved_at": "unknown", "service_url": "", "version_note": SOURCE_NOTE}
    return json.loads(META_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_baselines() -> dict[str, HazardBaseline]:
    meta = snapshot_meta()
    retrieved = str(meta.get("retrieved_at") or "unknown")
    note = SOURCE_NOTE
    version = meta.get("version_note")
    if version:
        note = f"{SOURCE_NOTE} {version}"
    baselines: dict[str, HazardBaseline] = {}
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            coastal, coastal_published = _blank(row.get("coastal_flood"))
            hurricane, hurricane_published = _blank(row.get("hurricane"))
            baselines[row["hub_id"]] = HazardBaseline(
                hub_id=row["hub_id"],
                county=row["county"],
                state=row["state"],
                fips=row["fips"],
                winter_weather=float(row["winter_weather"]),
                inland_flood=float(row["inland_flood"]),
                coastal_flood=coastal,
                hurricane=hurricane,
                heat_wave=float(row["heat_wave"]),
                tornado=float(row["tornado"]),
                lightning=float(row["lightning"]),
                hail=float(row["hail"]),
                strong_wind=float(row["strong_wind"]),
                coastal_flood_published=coastal_published,
                hurricane_published=hurricane_published,
                retrieved_at=retrieved,
                source_note=note,
            )
    return baselines


def get_baseline(hub_id: str) -> HazardBaseline:
    try:
        return load_baselines()[hub_id]
    except KeyError as exc:
        raise KeyError(f"No FEMA snapshot row for hub '{hub_id}'") from exc
