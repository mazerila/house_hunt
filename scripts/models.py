"""Normalized dossier schema.

Every section carries its own `status` so consumers can render partial data and
show which sources failed, never blanking the whole dossier on one failure.
The inner `data` is left as a free-form dict to keep the source clients simple.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SectionStatus = Literal["ok", "partial", "error", "skipped"]


class Section(BaseModel):
    status: SectionStatus = "ok"
    data: Any | None = None
    errors: list[dict] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    cached: bool = False


class Dossier(BaseModel):
    query: dict
    overall_status: SectionStatus = "ok"
    location: Section = Field(default_factory=Section)
    cadastre: Section = Field(default_factory=Section)
    building: Section = Field(default_factory=Section)
    dpe: Section = Field(default_factory=Section)
    price: Section = Field(default_factory=Section)
    risk: Section = Field(default_factory=Section)
    listing: Section = Field(default_factory=lambda: Section(status="skipped"))


# --- constructor helpers used by the source clients -----------------------

def ok(data: Any, *, url: str | None = None, urls: list[str] | None = None,
       cached: bool = False, partial: bool = False) -> Section:
    return Section(
        status="partial" if partial else "ok",
        data=data,
        source_urls=([url] if url else []) + (urls or []),
        cached=cached,
    )


def error(message: str, *, url: str | None = None, status: int | None = None,
          skipped: bool = False) -> Section:
    return Section(
        status="skipped" if skipped else "error",
        errors=[{"message": message, "url": url, "status": status}],
        source_urls=[url] if url else [],
    )


# --- listing extraction output (also the Anthropic structured-output schema) -

class ListingFields(BaseModel):
    address: str | None = Field(None, description="Full street address of the property")
    asking_price: int | None = Field(None, description="Asking price in euros, digits only")
    price_per_m2: int | None = Field(None, description="Price per m² in euros if stated")
    surface_m2: float | None = Field(None, description="Marketed living surface in m²")
    rooms: int | None = Field(None, description="Number of rooms (pièces)")
    bedrooms: int | None = Field(None, description="Number of bedrooms (chambres)")
    dpe_class: str | None = Field(None, description="DPE energy class letter A-G if shown")
    energy_value: int | None = Field(None, description="Primary energy kWhEP/m²/an if shown")
    property_type: str | None = Field(None, description="e.g. maison, appartement")
    agency: str | None = Field(None, description="Listing agency or seller name")
    description: str | None = Field(None, description="Short summary of the listing text")
    features: list[str] = Field(default_factory=list, description="Notable features/amenities")
