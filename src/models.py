from datetime import date, datetime
from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel, Field, field_validator, model_serializer
from enum import Enum

class Category(str, Enum):
    """Represents the category of the show, either a Musical, a Play, or blank"""
    MUSICAL="Musical"
    PLAY="Play"
    BLANK=""

class Performance(BaseModel):
    """Represents an individual performance instance."""
    date: str 
    time: str 

    @field_validator('date')
    @classmethod
    def validate_date_format(cls, v:str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Performance date must be in YYYY-MM-DD format")
        return v
    
    @field_validator('time')
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%H:%M")
        except ValueError:
            raise ValueError("Performance time must be in HH:MM format")
        return v
    
class TheatreShow(BaseModel):
    """Represents a full show row conforming precisely to the 18-column 
    schema required by csv-validator.md."""
    
    title: str = Field(..., min_length=1)
    venue_url: str = Field(..., min_length=1)
    category: Optional[Category]
    venue: str = Field(..., min_length=1)
    address: str = Field(..., min_length=1)
    city: str = Field(..., min_length=1)
    country: str = Field(..., min_length=1)
    open_date: Optional[str] = None
    close_date: Optional[str] = None
    booking_start_date: Optional[str] = None
    booking_end_date: Optional[str] = None
    upcoming_performances: List[Performance] = Field(default_factory=list)
    capacity:Optional[Union[int, str]] = None
    currency:str
    is_limited_run: Union[bool, int, str]
    seat_pricing: Dict[str, Any] = Field(default_factory=dict)
    scrape_datetime: str = Field(..., min_length=1)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v:str) -> str:
        cleaned = v.strip()
        if cleaned and cleaned not in ("Musical", "Play"):
            return ""
        return cleaned
    
    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        if v:
            return v.strip().upper()
        return v
    
    @field_validator("open_date", "close_date", "booking_start_date", "booking_end_date")
    @classmethod
    def validate_date_strings(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return ""
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Dates must be in YYYY-MM-DD format")
        return v
    
    @model_serializer(mode='wrap')
    def serialize_to_csv_row(self, handler:Any) -> Dict[str, Any]:
        """
        Intercepts serialization to format python objects into the precise 
        string representation required by the CSV writer, fulfilling Rule 17.
        """
        raw_data = handler(self)
        perf_list = [perf.model_dump() for perf in self.upcoming_performances]

        # Enforce Rule 17: Format complex fields as single-quoted Python literals.
        # repr() on standard Python dicts/lists outputs single quotes natively.
        raw_data["upcoming_performances"] = repr(perf_list)
        raw_data["seat_pricing"] = repr(raw_data["seat_pricing"])

        for key in ["open_date", "close_date", "booking_start_date", "booking_end_date", "capacity"]:
            if raw_data[key] is None:
                raw_data[key] = ""
        if isinstance(raw_data["is_limited_run"], bool):
            raw_data["is_limited_run"] = "True" if raw_data["is_limited_run"] else "False"
        
        canonical_order = [
            "title", "venue_url", "category", "venue", "address", "city", "country",
            "open_date", "close_date", "booking_start_date", "booking_end_date",
            "upcoming_performances", "capacity", "currency", "is_limited_run",
            "seat_pricing", "scrape_datetime"
        ]
        return {column: raw_data[column] for column in canonical_order}
