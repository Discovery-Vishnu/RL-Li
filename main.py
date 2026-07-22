import asyncio
import json
import logging
import random
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from camoufox.sync_api import Camoufox
from pydantic_settings import BaseSettings, SettingsConfigDict

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("linkedin_scaler")

class Settings(BaseSettings):
    """
    Configuration via Environment Variables.
    """
    database_url: str = "postgresql://user:pass@host:port/dbname"
    supabase_table_name: str = "linkedin_profiles"
    max_workers: int = 1
    batch_size: int = 10

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

class DatabaseManager:
    """Manages direct PostgreSQL database interactions."""
    
    def __init__(self, db_url: str, table_name: str):
        self.db_url = db_url
        self.table_name = table_name

    def get_connection(self):
        """Returns a new connection to the database."""
        return psycopg2.connect(self.db_url)

    def fetch_pending_urls(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch rows that haven't been scraped yet."""
        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    query = f"SELECT * FROM {self.table_name} WHERE status = 'pending' LIMIT %s;"
                    cur.execute(query, (limit,))
                    results = cur.fetchall()
                    # Convert RealDictRow to standard dict
                    return [dict(row) for row in results]
        except Exception as e:
            logger.error(f"Failed to fetch pending URLs from PostgreSQL: {e}")
            return []

    def update_result(self, url: str, data: Dict[str, Any]):
        """Update a row in PostgreSQL with the scraped data."""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Construct dynamic SET clause
                    columns = list(data.keys())
                    set_clause = ", ".join([f"{col} = %s" for col in columns])
                    
                    values = list(data.values())
                    values.append(url) # For the WHERE clause
                    
                    query = f"UPDATE {self.table_name} SET {set_clause} WHERE url = %s;"
                    cur.execute(query, values)
            return True
        except Exception as e:
            logger.error(f"Failed to update record for {url} in PostgreSQL: {e}")
            return False


def check_internet() -> bool:
    """Check for internet connectivity."""
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        return False

def wait_for_internet():
    """Block execution until internet is restored."""
    if not check_internet():
        logger.warning("Internet outage detected. Waiting for network to return...")
        while not check_internet():
            time.sleep(5)
        logger.info("Network restored. Resuming...")


class LinkedInScraper:
    """Encapsulates the scraping logic using Camoufox."""

    @staticmethod
    def human_scroll(page):
        """Mimics human scrolling behavior."""
        try:
            current_scroll = 0
            while current_scroll < 800:
                step = random.randint(200, 400)
                current_scroll += step
                page.evaluate(f"window.scrollTo(0, {current_scroll})")
                time.sleep(random.uniform(0.1, 0.3))
        except Exception as e:
            logger.debug(f"Scrolling error (ignored): {e}")

    @staticmethod
    def human_interaction(page):
        """Random mouse movements or waits."""
        try:
            for _ in range(random.randint(1, 2)):
                x, y = random.randint(100, 700), random.randint(100, 500)
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.1, 0.2))
        except Exception as e:
            logger.debug(f"Interaction error (ignored): {e}")

    @classmethod
    def scrape_url(cls, url: str) -> Dict[str, Any]:
        """Worker function to scrape a single URL with high stealth."""
        max_retries = 3
        for attempt in range(max_retries):
            wait_for_internet()
            try:
                with Camoufox(headless=True) as browser:
                    page = browser.new_page()
                    time.sleep(random.uniform(0.5, 1.5))
                    
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        if "ERR_INTERNET_DISCONNECTED" in str(e) or "ERR_NAME_NOT_RESOLVED" in str(e):
                            logger.warning(f"Internet error on {url}. Retrying...")
                            wait_for_internet()
                            continue
                        raise e
                    
                    page_content = page.content().lower()
                    if "error 1015" in page_content or ("cloudflare" in page.title().lower() and "1015" in page_content):
                        logger.warning(f"Cloudflare Error 1015 detected for {url}. Retrying ({attempt + 1}/{max_retries})...")
                        time.sleep(random.uniform(5, 10))
                        continue
                    
                    cls.human_interaction(page)
                    cls.human_scroll(page)
                    
                    if "authwall" in page.url or "login" in page.url:
                        if attempt < max_retries - 1:
                            logger.warning(f"Login wall detected for {url}. Retrying ({attempt + 1}/{max_retries})...")
                            time.sleep(random.uniform(2, 5))
                            continue
                        return {"url": url, "error": "Login wall", "status": "error"}

                    # Extraction Logic
                    name = page.locator("h1").first.inner_text().strip() if page.locator("h1").count() > 0 else "N/A"
                    if name == "N/A":
                        if attempt < max_retries - 1:
                            logger.warning(f"Failed to scrape data (Name not found) for {url}. Retrying ({attempt + 1}/{max_retries})...")
                            time.sleep(random.uniform(2, 5))
                            continue
                        return {"url": url, "error": "Name not found (scraping failed)", "status": "error"}
                    
                    bio = "N/A"
                    for sel in ["p.about-us__description", "section.about-us p", ".top-card-layout__second-subline"]:
                        if page.locator(sel).count() > 0:
                            bio = page.locator(sel).first.inner_text().strip()
                            break

                    website = "N/A"
                    if page.locator("a.about-us__link").count() > 0:
                        website = page.locator("a.about-us__link").first.get_attribute("href")
                    if website == "N/A" or "linkedin.com" in website:
                        all_links = page.locator("a").all()
                        for link in all_links:
                            href = link.get_attribute("href")
                            if href and "http" in href and "linkedin.com" not in href:
                                website = href
                                break

                    location = "N/A"
                    sublines = page.locator(".top-card-layout__first-subline, .top-card-layout__second-subline").all_inner_texts()
                    for line in sublines:
                        cleaned = re.sub(r"\s+\d+(?:,\d+)?(?:\s+followers)?.*", "", line, flags=re.IGNORECASE).strip()
                        if "," in cleaned:
                            location = cleaned
                            break

                    founded = page.evaluate('''() => {
                        let dts = document.querySelectorAll('dt');
                        for (let i = 0; i < dts.length; i++) {
                            if (dts[i].innerText.toLowerCase().includes('founded')) {
                                let next = dts[i].nextElementSibling;
                                if (next && next.tagName.toLowerCase() === 'dd') {
                                    return next.innerText.trim();
                                }
                            }
                        }
                        return "N/A";
                    }''')

                    if founded is None:
                        founded = "N/A"

                    if website != "N/A":
                        match = re.search(r"https?://(?:www\.)?([^/]+)", website)
                        if match:
                            website = match.group(1)

                    data = {
                        "url": url,
                        "name": name,
                        "bio": bio,
                        "website": website,
                        "location": location,
                        "founded": founded,
                        "status": "completed",
                        "error": None
                    }
                    
                    logger.info(f"Finished scraping: {name} ({url})")
                    return data

            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Error {url}: {str(e)}. Retrying ({attempt + 1}/{max_retries})...")
                    wait_for_internet()
                    time.sleep(random.uniform(2, 5))
                else:
                    logger.error(f"Error {url}: {str(e)}. Max retries reached.")
                    return {"url": url, "error": str(e), "status": "error"}
                    
        return {"url": url, "error": "Max retries exceeded", "status": "error"}

def run_scraper():
    logger.info("Starting LinkedIn Scaler with PostgreSQL integration...")
    
    # Initialize DB manager
    db = DatabaseManager(
        db_url=settings.database_url,
        table_name=settings.supabase_table_name
    )

    # 1. Fetch pending rows
    logger.info(f"Fetching up to {settings.batch_size} pending URLs from table '{settings.supabase_table_name}'...")
    pending_records = db.fetch_pending_urls(limit=settings.batch_size)

    if not pending_records:
        logger.info("No pending URLs found to scrape.")
        return

    logger.info(f"Found {len(pending_records)} URLs to scrape. Starting {settings.max_workers} concurrent workers...")

    # 2. Scrape with ThreadPoolExecutor
    results = []
    with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
        # submit tasks (pass url)
        future_to_record = {
            executor.submit(LinkedInScraper.scrape_url, rec.get("url")): rec 
            for rec in pending_records if rec.get("url")
        }
        
        for future in as_completed(future_to_record):
            record = future_to_record[future]
            url = record.get("url")
            try:
                data = future.result()
                results.append(data)
                
                # 3. Write result back to PostgreSQL in real-time
                db.update_result(url, data)
                logger.info(f"Updated record {url} in database.")
            except Exception as exc:
                logger.error(f"Record ({url}) generated an exception: {exc}")
                # Try to write error status back
                db.update_result(url, {"error": str(exc), "status": "error"})

    logger.info(f"Batch complete. Processed {len(results)} records.")

if __name__ == "__main__":
    while True:
        try:
            run_scraper()
            logger.info("Sleeping for 60 seconds before next batch...")
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully...")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            time.sleep(60)
