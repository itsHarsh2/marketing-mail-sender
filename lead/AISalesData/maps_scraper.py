"""
maps_scraper.py - Scrapes business listings from Google Maps.

v4 FIX - WinError 3 ".wdm path" resolved permanently:
  Dropped webdriver_manager entirely.
  Selenium 4.6+ ships its own selenium-manager binary that downloads
  ChromeDriver automatically.  Usage: webdriver.Chrome(options=options)
  with no Service argument.  Selenium handles driver location internally
  with no reference to os.getcwd() or any user-writable path.

  If selenium-manager fails (corporate proxy, offline), we fall back to
  looking for chromedriver.exe already on PATH.
"""

import time
import os
import urllib.parse
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    StaleElementReferenceException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)

from utils import log_info, log_error, log_success, log_warning, random_delay

# ── SELECTORS ─────────────────────────────────────────────────────────────────

_LISTING_SELECTORS = [
    "a.hfpxzc",
    "div[role='feed'] a[href*='maps/place']",
]

_WEBSITE_SELECTORS = [
    "a[data-item-id='authority']",
    "a[data-tooltip='Open website']",
    "a[jsaction*='pane.website']",
    "button[data-item-id='authority']",
]

_FEED_SELECTOR = "div[role='feed']"


# ── DRIVER ────────────────────────────────────────────────────────────────────

def _chrome_binary() -> str | None:
    """
    Locate the Chrome executable.

    On shared hosting there is no root to install Chrome system-wide, so it
    is unpacked into the user's home directory instead. CHROME_BINARY points
    at it; otherwise the usual system locations are tried and Selenium falls
    back to whatever is on PATH.
    """
    explicit = (os.environ.get("CHROME_BINARY") or "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit

    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, "aisales/chrome/opt/google/chrome/google-chrome"),
        "/opt/google/chrome/google-chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _build_options(headless: bool) -> Options:
    """Build Chrome options. Separated so it can be reused on retry."""
    options = Options()

    binary = _chrome_binary()
    if binary:
        options.binary_location = binary
        log_info(f"Using Chrome at {binary}")

    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--mute-audio")
    options.add_argument("--remote-debugging-port=0")
    # Long scrapes click through dozens of listings in one session, and
    # background services (sync, GCM push registration, translate) accumulate
    # work in the renderer until a tab crashes. None of it is needed for
    # scraping, and switching it off makes long runs noticeably steadier.
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-translate")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    return options


def _create_driver(headless: bool = True) -> webdriver.Chrome:
    """
    Create a Chrome WebDriver.

    Strategy (tried in order):
      1. Selenium 4.6+ built-in selenium-manager — no extra packages,
         no path issues, downloads the right ChromeDriver automatically.
      2. chromedriver already on system PATH — for corporate environments
         where selenium-manager can't reach the internet.

    webdriver_manager is intentionally NOT used — it calls os.getcwd()
    internally and produces broken paths when running inside a frozen exe.
    """
    options = _build_options(headless)

    # ── Attempt 1: selenium built-in manager ─────────────────────────────
    # Just call webdriver.Chrome(options=options) with NO service argument.
    # Selenium 4.6+ selenium-manager handles everything internally.
    try:
        driver = webdriver.Chrome(options=options)
        _patch_driver(driver)
        log_info("ChromeDriver started via selenium built-in manager.")
        return driver
    except WebDriverException as exc:
        log_warning(f"Selenium built-in manager failed: {exc}. Trying PATH fallback...")

    # ── Attempt 2: chromedriver on PATH ────────────────────────────────
    # Works in corporate/offline environments where chromedriver is
    # installed system-wide.
    try:
        import shutil
        cd_path = shutil.which("chromedriver") or shutil.which("chromedriver.exe")
        if cd_path:
            service = Service(executable_path=cd_path)
            driver  = webdriver.Chrome(service=service, options=options)
            _patch_driver(driver)
            log_info(f"ChromeDriver started from PATH: {cd_path}")
            return driver
    except WebDriverException as exc:
        log_warning(f"PATH chromedriver failed: {exc}")

    raise WebDriverException(
        "Could not start ChromeDriver.\n"
        "Make sure Google Chrome is installed on this computer.\n"
        "If you are behind a corporate firewall, install chromedriver manually "
        "and add it to your system PATH."
    )


def _patch_driver(driver: webdriver.Chrome) -> None:
    """Apply anti-detection patches after driver is created."""
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
    )
    driver.set_page_load_timeout(30)


def _get_logs_dir() -> str:
    """Return the absolute logs directory — never a relative path."""
    return os.environ.get(
        "SALES_LOG_DIR",
        os.path.join(os.path.expanduser("~"), "AISalesData", "logs"),
    )


def _save_screenshot(driver: webdriver.Chrome, label: str = "crash") -> None:
    """Save a debug screenshot — always to an absolute path."""
    try:
        logs_dir = _get_logs_dir()
        os.makedirs(logs_dir, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(logs_dir, f"screenshot_{label}_{ts}.png")
        driver.save_screenshot(path)
        log_info(f"Screenshot saved: {path}")
    except Exception:
        pass


# ── SCROLL ────────────────────────────────────────────────────────────────────

def _scroll_results_panel(driver: webdriver.Chrome, max_scrolls: int = 15, stop_event=None) -> None:
    log_info(f"Scrolling results panel (max {max_scrolls} scrolls)...")

    try:
        feed = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, _FEED_SELECTOR))
        )
    except TimeoutException:
        log_warning("Results feed not found — cannot scroll.")
        return

    last_height  = -1
    stagnant     = 0

    for i in range(max_scrolls):
        if stop_event is not None and stop_event.is_set():
            log_info("Scroll paused by user signal.")
            break
        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollHeight", feed
            )
            time.sleep(2.0)
            new_height = driver.execute_script(
                "return arguments[0].scrollHeight", feed
            )
            if new_height == last_height:
                stagnant += 1
                if stagnant >= 2:
                    log_info(f"End of results after {i + 1} scroll(s).")
                    break
            else:
                stagnant = 0
            last_height = new_height
        except (WebDriverException, StaleElementReferenceException) as exc:
            log_warning(f"Scroll interrupted at step {i + 1}: {exc}")
            break


# ── EXTRACT ───────────────────────────────────────────────────────────────────

def _get_website_from_panel(driver: webdriver.Chrome):
    for sel in _WEBSITE_SELECTORS:
        for _ in range(2):
            try:
                el   = WebDriverWait(driver, 2).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                href = el.get_attribute("href")
                if href and href.startswith("http"):
                    return href
                break
            except (TimeoutException, NoSuchElementException,
                    StaleElementReferenceException):
                break
    return None


def _extract_listings(driver: webdriver.Chrome, stop_event=None) -> list:
    results    = []
    seen_names = set()

    listing_elements = []
    for sel in _LISTING_SELECTORS:
        listing_elements = driver.find_elements(By.CSS_SELECTOR, sel)
        if listing_elements:
            log_info(f"Found {len(listing_elements)} listings with selector '{sel}'")
            break

    if not listing_elements:
        log_warning("No listing elements found on page.")
        return results

    for idx, element in enumerate(listing_elements):
        if stop_event is not None and stop_event.is_set():
            log_info(f"Extraction paused by user — returning {len(results)} business(es) gathered so far.")
            break
        name    = None
        website = None

        try:
            for attempt in range(3):
                try:
                    name = element.get_attribute("aria-label")
                    break
                except StaleElementReferenceException:
                    if attempt == 2:
                        raise
                    time.sleep(0.5)
                    fresh = driver.find_elements(By.CSS_SELECTOR, _LISTING_SELECTORS[0])
                    if idx < len(fresh):
                        element = fresh[idx]

            if not name or name.strip() in seen_names:
                continue

            name = name.strip()
            seen_names.add(name)

            driver.execute_script("arguments[0].scrollIntoView(true);", element)
            time.sleep(0.3)
            driver.execute_script("arguments[0].click();", element)
            time.sleep(1.8)

            website = _get_website_from_panel(driver)
            results.append({"company_name": name, "website": website})
            log_info(f"  [{idx + 1}] {name[:55]:<55} | {website or '—'}")
            random_delay(0.8, 1.8)

        except StaleElementReferenceException:
            log_warning(f"Stale element at index {idx}, skipping.")
            continue
        except (WebDriverException, Exception) as exc:
            log_warning(f"Extraction error at index {idx}: {exc}")
            continue

    return results


# ── PUBLIC API ────────────────────────────────────────────────────────────────

def scrape_google_maps(
    business_type: str,
    location: str,
    max_scrolls: int = 15,
    headless: bool = True,
    stop_event=None,
) -> list:
    """
    Scrape Google Maps for businesses matching business_type in location.

    Returns a list of dicts: [{company_name, website}, ...]
    Returns [] on total failure — never raises.
    """
    query = f"{business_type} in {location}"
    log_info(f"Google Maps scrape started: '{query}'")

    driver  = None
    results = []

    try:
        # ── Launch browser ────────────────────────────────────────────────
        try:
            driver = _create_driver(headless=headless)
        except WebDriverException as exc:
            log_error(
                f"Could not start Chrome: {exc}\n"
                "Make sure Google Chrome is installed."
            )
            return results

        # ── Open Maps ─────────────────────────────────────────────────────
        encoded  = urllib.parse.quote_plus(query)
        maps_url = f"https://www.google.com/maps/search/{encoded}"
        driver.get(maps_url)
        log_info("Google Maps opened.")
        time.sleep(3.5)

        # ── Consent banner ────────────────────────────────────────────────
        for xpath in [
            "//button[contains(.,'Accept all')]",
            "//button[contains(.,'Accept')]",
            "//button[contains(.,'Agree')]",
        ]:
            try:
                btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                log_info("Consent banner dismissed.")
                time.sleep(1)
                break
            except TimeoutException:
                continue

        # ── Wait for feed ──────────────────────────────────────────────────
        try:
            WebDriverWait(driver, 18).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, _FEED_SELECTOR))
            )
            log_info("Results feed loaded.")
        except TimeoutException:
            log_error(
                "Google Maps results did not load within 18 seconds.\n"
                "Possible causes: slow internet, search returned no results, "
                "or Maps layout changed."
            )
            _save_screenshot(driver, "feed_timeout")
            return results

        # ── Scroll + extract ───────────────────────────────────────────────
        _scroll_results_panel(driver, max_scrolls=max_scrolls, stop_event=stop_event)
        results = _extract_listings(driver, stop_event=stop_event)
        log_success(f"Scraped {len(results)} businesses.")

    except Exception as exc:
        log_error(f"Scraper encountered an unexpected error: {exc}")
        if driver:
            _save_screenshot(driver, "unexpected_error")

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
            log_info("Browser closed.")

    return results


# ── CLI TEST ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    items = scrape_google_maps("dentist", "Chicago IL", max_scrolls=4, headless=False)
    print(f"\nFound {len(items)} results:")
    for item in items:
        print(f"  {item['company_name']:<50}  {item.get('website') or '—'}")
