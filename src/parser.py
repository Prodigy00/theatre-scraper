import re 
import logging
from models import Category
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from dateutil import parser 

logger = logging.getLogger('scraper.parser')

def _clean_date_text(raw_text: str) -> str:
    """
    Removes marketing/UI noise from scraped date blocks.
    """
    #this regex fixes cases like "& 3.30pm, Monday 10th August 2026"
    date_text = re.sub(r"\s*&\s*", " ", raw_text)
    date_text = re.sub(r"\s{2,}", " ", date_text)
    date_text = date_text.strip(",;:- ").strip()

    if not raw_text:
        return ""

    text = raw_text

    #Remove labels
    text = re.sub(r"Dates?\s*&?\s*Times?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Date:", "", text, flags=re.IGNORECASE)

    #Remove pricing
    text = re.sub(
        r"Price:.*",
        "",
        text,
        flags=re.IGNORECASE
    )

    #Remove CTA junk e.g. BOOK NOW
    text = re.sub(r"BOOK\s+NOW", "", text, flags=re.IGNORECASE)

    #Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _normalize_date(date_str:str, year:str) -> str:
    """Normalizes dates like Wednesday, 9th December 2026 to 2026-12-09(Y-M-D)"""
    if not date_str or not date_str.strip():
        return ""

    clean = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", date_str, flags=re.IGNORECASE) 
    clean = re.sub(r"\s+", " ", clean).strip()

    if not re.search(r"\b\d{4}\b", clean):
        clean = f"{clean} {year}"
    
    try:
        dt = parser.parse(clean, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""

def _normalize_time(time_str:str) -> str:
    """
    Normalizes times like '7.30pm', '19.30pm', or '2pm' into 24-hour HH:MM strings.
    Defaults to 19:30 for durations or missing clock values.
    """
    clean = time_str.lower().replace(".", ":").strip()
    if ("pm" in clean or "am" in clean) and ":" not in clean:
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
        
    # Default to standard UK evening curtain time if no clock pattern matches
    return "19:30" #MalthousTheatre doesn't give specific times so we default to uk theatre opening times

def _expand_date_range(start_date_str: str, end_date_str: str) -> List[str]:
    """Generates a continuous sequence of standard YYYY-MM-DD date strings."""
    if not start_date_str:
        return []
    if not end_date_str or start_date_str == end_date_str:
        return [start_date_str]
        
    try:
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return [start_date_str]

    if start_dt > end_dt:
        return [start_date_str]

    delta_days = (end_dt - start_dt).days
    if delta_days > 365:
        return [start_date_str]
    
    date_list = []
    for i in range(delta_days + 1):
        current_date = start_dt + timedelta(days=i)
        date_list.append(current_date.strftime("%Y-%m-%d"))
        
    return date_list

class MalthouseParser:
    @staticmethod
    def parse_listing_page(html_content: str) -> List[str]:
        """
        Parses the 'whats-on' listing grid to extract unique event URLs.
        """
        soup = BeautifulSoup(html_content, "html.parser")
        urls = []

        # Malthouse targets standard event loops and anchor detail blocks
        for anchor in soup.find_all("a", href=True):
            url = anchor.get("href", "")
            if isinstance(url, list):
                url = "".join(url)

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
        soup = BeautifulSoup(html_content, "html.parser")
        title_node = soup.select_one("h2.elementor-heading-title, h2")
        title = "Unknown Event"
        if title_node:
            title=title_node.get_text(strip=True)
        
        #fallback to title tag in case title can't be scraped from class
        if title == "Unknown Event" or not title:
            meta_title = soup.find("title")
            if meta_title:
                title = meta_title.get_text(strip=True).split("&#8211;")[0].split("–")[0].strip()

        #body_text for global scan
        body_text = soup.get_text() or ""
        body_text_lower = body_text.lower()

        #map categories
        category = ""
        if ": music" in body_text_lower or "opera" in body_text_lower:
            category = Category.MUSICAL
        elif ": theatre" in body_text_lower or "drama" in body_text_lower or "comedy" in body_text_lower:
            category = Category.PLAY


        date_text = ""

        for element in soup.find_all(["div", "p", "span", "li"]):
            text = element.get_text(" ", strip=True)
            if len(text) > 250:
                continue

            lowered = text.lower()

            if (
                "date:" in lowered or 
                "dates & times" in lowered or
                "dates and times" in lowered
            ):
                date_text = text
                date_text = _clean_date_text(date_text)
                break
        
        date_text = re.sub(r"[\s\xa0\t\r\n]+", " ", date_text).strip()
        normalized_time = "19:30" # Baseline default fallback time

        #check for mashed inline times (e.g. "Sunday 24th May 2026 7.30PM")
        clock_match = re.search(r"(\b\d{1,2}[:.]\d{2}\s*(?:am|pm)\b|\b\d{1,2}\s*(?:am|pm)\b)", date_text, re.IGNORECASE)
        if clock_match:
            raw_time_string = clock_match.group(1)
            normalized_time = _normalize_time(raw_time_string)
            #remove time from date_text
            date_text = date_text.replace(raw_time_string, "").strip()


        open_date_raw = ""
        close_date_raw = ""

        split_dates = re.split(r"\s+[–—-]\s+", date_text)

        if len(split_dates) > 1:
            open_date_raw = split_dates[0].strip()
            close_date_raw = split_dates[1].strip()
           
            # If first date lacks month/year, inherit from second date
            close_month_year = re.search(
                r"([A-Za-z]+)\s+(20\d{2})",
                close_date_raw
            )

            if close_month_year:
                month = close_month_year.group(1)
                year = close_month_year.group(2)

                if not re.search(r"[A-Za-z]+", open_date_raw):
                    open_date_raw = f"{open_date_raw} {month}"

                if not re.search(r"\b20\d{2}\b", open_date_raw):
                    open_date_raw = f"{open_date_raw} {year}"

        elif len(split_dates) == 1:
            open_date_raw = close_date_raw = split_dates[0].strip()
        
        year_match = re.search(r"\b(20\d{2})\b", date_text + " " + title)
        current_year = year_match.group(1) if year_match else str(datetime.now().year)

        # Remove trailing/leading punctuation commas left over from scrubbing
        open_date_raw = open_date_raw.strip(", ").strip()
        close_date_raw = close_date_raw.strip(", ").strip()

        open_date = _normalize_date(open_date_raw, current_year)
        close_date = _normalize_date(close_date_raw, current_year)
        
        
        if not open_date:
            logger.warning(f"failed to parse open date from {date_text}, defaulting to empty string")
            open_date = ""
        if not close_date:
            close_date = open_date

        #Fallback block: Scan 'Running Time:' elements ONLY if a time wasn't already caught inline
        if normalized_time == "19:30":
            time_text = ""
            for element in soup.find_all(["div"]):
                text = element.get_text()
                if "running time:" in text.lower() and len(text) < 150:
                    time_text = text
                    break
                    
            if time_text:
                fallback_clock_match = re.search(r"(\b\d{1,2}[:.]\d{2}\s*(?:am|pm)\b|\b\d{1,2}\s*(?:am|pm)\b)", time_text, re.IGNORECASE)
                if fallback_clock_match:
                    normalized_time = _normalize_time(fallback_clock_match.group(1))

        # Build dynamic day matrix mapping rows
        upcoming_performances: List[Dict[str, str]] = []
        _active_dates = _expand_date_range(open_date, close_date)

        for single_date in _active_dates:
            upcoming_performances.append({
                "date": single_date,
                 "time": normalized_time
            })
        
        seat_pricing: Dict[str, List[Dict[str, Any]]] = {}

        for perf in upcoming_performances:
            perf_key = f"{perf['date']} {perf['time']}"

            # Sold-out/not-on-sale/general-admission-safe shape
            seat_pricing[perf_key] = []
        
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
            "is_limited_run": open_date != close_date,
            "seat_pricing": seat_pricing,
            "scrape_datetime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        }