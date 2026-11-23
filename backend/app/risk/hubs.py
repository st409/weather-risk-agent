"""Fixed logistics hubs. The demo does not geocode arbitrary cities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Hub:
    hub_id: str
    name: str
    city: str
    state: str
    region: str
    lat: float
    lon: float
    county: str
    fips: str
    timezone: str
    # Tropical-cyclone surge, not merely "near a shoreline".
    surge_exposed: bool
    business_importance_weight: float = 1.0


HUBS: tuple[Hub, ...] = (
    Hub("chicago", "Chicago", "Chicago", "IL", "Midwest", 41.8781, -87.6298, "Cook", "17031", "America/Chicago", False),
    Hub("minneapolis", "Minneapolis", "Minneapolis", "MN", "Midwest", 44.9778, -93.2650, "Hennepin", "27053", "America/Chicago", False),
    Hub("detroit", "Detroit", "Detroit", "MI", "Midwest", 42.3314, -83.0458, "Wayne", "26163", "America/Detroit", False),
    Hub("denver", "Denver", "Denver", "CO", "Mountain", 39.7392, -104.9903, "Denver", "08031", "America/Denver", False),
    Hub("dallas", "Dallas", "Dallas", "TX", "South", 32.7767, -96.7970, "Dallas", "48113", "America/Chicago", False),
    Hub("houston", "Houston", "Houston", "TX", "South", 29.7604, -95.3698, "Harris", "48201", "America/Chicago", True),
    Hub("miami", "Miami", "Miami", "FL", "South", 25.7617, -80.1918, "Miami-Dade", "12086", "America/New_York", True),
    Hub("atlanta", "Atlanta", "Atlanta", "GA", "South", 33.7490, -84.3880, "Fulton", "13121", "America/New_York", False),
    Hub("newyork", "New York/Newark", "Newark", "NJ", "Northeast", 40.7357, -74.1724, "Essex", "34013", "America/New_York", True),
    Hub("losangeles", "Los Angeles", "Los Angeles", "CA", "West", 34.0522, -118.2437, "Los Angeles", "06037", "America/Los_Angeles", False),
)

HUB_BY_ID: dict[str, Hub] = {hub.hub_id: hub for hub in HUBS}

REGIONS: tuple[str, ...] = ("Midwest", "Northeast", "South", "West", "Mountain")

# Longer aliases first when matching text.
ALIASES: tuple[tuple[str, str], ...] = (
    ("new york/newark", "newyork"),
    ("los angeles", "losangeles"),
    ("new york", "newyork"),
    ("minneapolis", "minneapolis"),
    ("chicago", "chicago"),
    ("detroit", "detroit"),
    ("houston", "houston"),
    ("atlanta", "atlanta"),
    ("dallas", "dallas"),
    ("denver", "denver"),
    ("newark", "newyork"),
    ("miami", "miami"),
)

# Matched only as typed, in capitals. Lower case "mia" or "den" is not a hub.
AIRPORT_CODES: tuple[tuple[str, str], ...] = (
    ("NYC", "newyork"),
    ("DFW", "dallas"),
    ("EWR", "newyork"),
    ("ORD", "chicago"),
    ("LAX", "losangeles"),
    ("MSP", "minneapolis"),
    ("ATL", "atlanta"),
    ("IAH", "houston"),
    ("DTW", "detroit"),
    ("MIA", "miami"),
)

OUTSIDE_PLACES: tuple[tuple[str, str], ...] = (
    ("san francisco", "San Francisco"),
    ("salt lake city", "Salt Lake City"),
    ("kansas city", "Kansas City"),
    ("new orleans", "New Orleans"),
    ("las vegas", "Las Vegas"),
    ("saint louis", "St. Louis"),
    ("st louis", "St. Louis"),
    ("indianapolis", "Indianapolis"),
    ("milwaukee", "Milwaukee"),
    ("nashville", "Nashville"),
    ("charlotte", "Charlotte"),
    ("columbus", "Columbus"),
    ("phoenix", "Phoenix"),
    ("seattle", "Seattle"),
    ("portland", "Portland"),
    ("orlando", "Orlando"),
    ("memphis", "Memphis"),
    ("boston", "Boston"),
    ("austin", "Austin"),
    ("tampa", "Tampa"),
)


def hub_names() -> str:
    return ", ".join(hub.name for hub in HUBS)


def hubs_in_region(region: str | None) -> list[Hub]:
    if not region:
        return list(HUBS)
    return [hub for hub in HUBS if hub.region.lower() == region.lower()]
