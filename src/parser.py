import re 
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from selectolax.parser import HTMLParser

def _normalize_date(date_str:str, year:str) -> str:
    """Normalizes dates like 9th December 2026 to 2026-12-09(Y-M-D)"""
    clean = re.sub(r"(st|nd|rd|th)", "", date_str, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()
    for fmt in ("%d %B", "%B %d", "%d %b", "%b %d"):
        try:
            return datetime.strptime(f"{clean} {year}", f"{fmt} %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""

def _normalize_time(time_str:str) -> str:
    """Normalizes times like '7.30pm', '19.30pm', or '2pm' into 24-hour HH:MM strings.
    Defaults to 19:30 for incompatible times."""
    clean = time_str.lower().replace(".", ":").strip()
    if("pm" in clean or "am" in clean) and ":" not in clean:
        clean = re.sub(r"(\d+)", r"\1:00", clean)
    match = re.search(r"(\d{1,2}):(\d{2})", clean)
    if match:
        hours = int(match.group(1))
        minutes = match.group(2)
        if "pm" in clean and hours < 12:
            hours += 12
        elif "am" in clean and hours == 12:
            hours = 0
        return f"{hours:02d}:{minutes}"
    return "19:30" #MalthousTheatre doesn't give specific times so we default to uk theatre opening times

def _expand_date_range(start_date_str: str, end_date_str: str) -> List[str]:
    """
    Generates a continuous sequence of standard YYYY-MM-DD date strings 
    inclusive of both the start and end boundary markers.
    """
    if not start_date_str:
        return []
    if not end_date_str or start_date_str == end_date_str:
        return [start_date_str]
        
    try:
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    except ValueError:
        # Emergency safety return if a malformed date slips past initial checks
        return [start_date_str]

    # If timelines were inverted on the page, return the initial boundary to prevent infinite loops
    if start_dt > end_dt:
        return [start_date_str]

    date_list = []
    current_dt = start_dt
    while current_dt <= end_dt:
        date_list.append(current_dt.strftime("%Y-%m-%d"))
        current_dt += timedelta(days=1)
        
    return date_list

class MalthousParser:
    @staticmethod
    def parse_listing_page(html_content: str) -> List[str]:
        """
        Parses the 'whats-on' listing grid to extract unique event URLs.
        """
        parser = HTMLParser(html_content)
        urls = []

        # Malthouse targets standard event loops and anchor detail blocks
        for anchor in parser.css("a[href*='/event/'], .elementor-button-wrapper a, a.btn"):
            url = anchor.attributes.get("href")
            if url and "/event/" in url:
                clean_url = url.split("?")[0].rstrip("/") + "/"
                if clean_url not in urls:
                    urls.append(clean_url)
        return urls
    
    @staticmethod
    def parse_show_detail(html_content:str, url:str) -> Dict[str, Any]:
        """
        Parses specific Malthouse event structural layouts.
        Translates inconsistent line texts into valid normalized csv values.
        """
        parser = HTMLParser(html_content)
        title_node = parser.css_first("h2.elementor-heading-title, h2")
        title = "Unknown Event"
        if title_node:
            title=title_node.text(strip=True)
        
        #fallback to title tag in case title can't be scraped from class
        if title == "Unknown Effect" or not title:
            meta_title = parser.css_first("title")
            if meta_title:
                title = meta_title.text(strip=True).split("&#8211;")[0].strip()

        #body_text for global scan
        body_text = parser.text(deep=True) or ""
        body_text_lower = body_text.lower()

        #map categories
        category = ""
        if ": music" in body_text_lower or "opera" in body_text_lower:
            category = "Musical"
        elif ": theatre" in body_text_lower or "drama" in body_text_lower or "comedy" in body_text_lower:
            category = "Play"

        #extract dates
        date_text = ""
        date_node = parser.css_first("div:contains('Date:')")
        if date_node:
            date_text = date_node.text(strip=True)
        
        open_date = ""
        close_date = ""

        #format open and close dates
        dates_list = date_text.split("-")
        if len(dates_list) > 1:
            open_date = dates_list[0]
            close_date = dates_list[1]
        elif len(dates_list) == 1:
            open_date = close_date = dates_list[0]
        
        #find year
        year_match = re.search(r"\b(202[6-9])\b", date_text + " " + title)
        current_year = year_match.group(1) if year_match else str(datetime.now().year)

        open_date = _normalize_date(open_date, current_year)
        close_date = _normalize_date(close_date, current_year)

        #match time
        time_node = parser.css_first("div:contains('Running Time:')")
        time_raw = ""
        if time_node:
            time_raw = time_node.text(strip=True).split(":")[1]
            time_raw = time_raw.split("minutes")[0]
        #malthousparser displays time in minutes
        normalized_time = _normalize_time(time_raw)

        upcoming_performances: List[Dict[str, str]] = []
        _active_dates = _expand_date_range(open_date, close_date)

        for single_date in _active_dates:
            upcoming_performances.append({"date": single_date, "time": normalized_time})
        
        return {
            "title": title,
            "venue_url": url,
            "category": category,
            "venue": "The Malthouse Theatre",
            "address": "Malthouse Rd",
            "city": "Canterbury",
            "country": "UK",
            "open_date": open_date,
            "close_date": close_date,
            "booking_start_date": open_date,
            "booking_end_date": close_date,
            "upcoming_performances": upcoming_performances,
            "capacity": "",
            "currency": "GBP",
            "is_limited_run": "True" if open_date != close_date else "False",
            "seat_pricing": {},
            "scrape_datetime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        }