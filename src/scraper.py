import asyncio
import csv
import logging
import sys
from pydantic import ValidationError
import httpx

from models import TheatreShow
from pipeline import AsyncPipeline
from parser import MalthouseParser

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s %(asctime)s - %(message)s]",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("scraper.orchestrator")

async def scrape_malthouse_theatre():
    base_url = "https://malthousetheatre.co.uk"
    listing_url = f"{base_url}/whats-on/"
    output_filename = "output.csv"

    #initialize async pipeline
    pipeline = AsyncPipeline(requests_per_second=1,max_retries=3)

    logger.info(f"Starting Malthouse Theatre pipeline execution. Target: {listing_url}")

    async with httpx.AsyncClient() as client:
        listing_html = await pipeline.fetch_html(client, listing_url)
        if not listing_html:
            logger.error("Failed to recover main html listing. Terminating run.")
            sys.exit(1)
        
        #extract unique show urls from html listing
        show_urls = MalthouseParser.parse_listing_page(listing_html)
        if not show_urls:
            logger.error("No upcoming event routes uncovered on target grid. Terminating run.")
        
        logger.info(f"Successfully discovered {len(show_urls)} upcoming routes to deep-crawl.")

        parsed_shows = []

        for idx, target_show_url in enumerate(show_urls, start=1):
            if not target_show_url.startswith("http"):
                target_show_url = base_url + target_show_url

            logger.info(f"Processing show [{idx}/{len(show_urls)}]: {target_show_url}")

            detail_html = await pipeline.fetch_html(client, target_show_url)
            if not detail_html:
                logger.warning(f"Skipping corrupt link node. Unable to fetch details for: {target_show_url}")
                continue

            raw_payload = MalthouseParser.parse_show_detail(detail_html, target_show_url)

            try:
                show_model = TheatreShow(**raw_payload)
                csv_compatible_row = show_model.model_dump(mode="json")
                parsed_shows.append(csv_compatible_row)
            except ValidationError as err:
                logger.error(f"Pydantic Validation failure on extraction mapping for {target_show_url}")
                logger.error(f"Error Context: {err.json()}")
                continue
        
        if not parsed_shows:
            logger.error("Zero records successfully converted. File writing skipped.")
            sys.exit(1)
        
        csv_headers = list(parsed_shows[0].keys())

        try:
            with open(output_filename, mode="w", encoding="utf-8", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=csv_headers, quoting=csv.QUOTE_MINIMAL)
                writer.writeheader()
                for row_data in parsed_shows:
                    writer.writerow(row_data)
                
            logger.info(f"Execution complete. {len(parsed_shows)} rows written to '{output_filename}'.")
        except IOError as err:
            logger.error(f"Error writing to target output: {str(err)}")
            sys.exit(1)
        

def main():
    asyncio.run(scrape_malthouse_theatre())

if __name__ == "__main__":
    main()