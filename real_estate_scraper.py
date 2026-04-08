"""
Skrypt scrapujacy ogloszenia nieruchomosci ze stron:
otodom.pl, olx.pl, gratka.pl, nieruchomosci-online.pl, lento.pl, sprzedajemy.pl

Wymagania:
    pip install selenium openpyxl webdriver-manager

Dane zapisywane sa do bazy SQLite (nieruchomosci.db).
Klucz unikalny: URL ogloszenia.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
)

# --------------- Konfiguracja ---------------

DB_FILE = Path(__file__).parent / "nieruchomosci.db"
LINKS_DIR = Path(__file__).parent / "links"
HEADLESS = os.environ.get("RE_HEADLESS", "").lower() in ("1", "true", "yes")
DEFAULT_WAIT = 15
MAX_PAGES = 50  # zabezpieczenie przed nieskonczona paginacja


# --------------- Driver ---------------

def build_driver(headless: bool = True) -> webdriver.Chrome:
    """Tworzy sterownik Chrome z ustawieniami anty-detekcji."""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=pl-PL")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        service = ChromeService(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=opts)
    except Exception as exc:
        print(f"[info] webdriver-manager niedostepny ({exc}); "
              f"probuje Selenium Manager.")

    return webdriver.Chrome(service=ChromeService(), options=opts)


def safe_click(driver, element) -> None:
    """Klika element; jesli jest przesloniety, uzywa JS."""
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


# --------------- Narzedzia ---------------

def dismiss_cookie_consent(driver, portal: str = "") -> bool:
    """Probuje zamknac baner cookies. Zwraca True jesli udalo sie kliknac.

    Uzywa selektorow specyficznych dla portalu + generycznych.
    Jesli nie znajdzie przycisku, probuje JS-owe podejscie do usuwania overlay.
    """
    # Selektory specyficzne dla portali
    portal_selectors: dict[str, list[str]] = {
        "gratka": [
            "#didomi-notice-agree-button",
            "button[aria-label='Zaakceptuj i zamknij']",
            "button.didomi-components-button--color",
            "#didomi-popup .didomi-button",
            "button.sc-dcJsrY",  # gratka styled-components
            "span.didomi-continue-without-agreeing",
        ],
        "lento": [
            "button.fc-cta-consent",
            "button.fc-button-background",
            ".fc-consent-root button[aria-label*='consent' i]",
            ".fc-consent-root .fc-cta-consent",
            "button[title='Akceptuję' i]",
            "a.fc-cta-consent",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "button[id*='rodo' i]",
            "button[class*='rodo' i]",
            "div.cc-window button.cc-btn",
            "div.cc-window a.cc-btn",
        ],
        "nieruchomosci": [
            "button.fc-cta-consent",
            "button.fc-button-background",
            ".fc-consent-root .fc-cta-consent",
            "button[title='Akceptuję' i]",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "button.cmp-button_button",
            "button[class*='cmp' i]",
            "#cookieConsentAcceptButton",
            "button[class*='cookie-accept']",
            "div.cc-window button.cc-btn",
        ],
    }

    # Generyczne selektory (fallback)
    generic_selectors = [
        "button#onetrust-accept-btn-handler",
        "button[data-testid='accept-all']",
        "button.cmp-button_button",
        "#didomi-notice-agree-button",
        "button.fc-cta-consent",
        "button[id*='cookie' i]",
        "button[class*='accept' i]",
        "button[class*='consent' i]",
        "button[class*='agree' i]",
        "a[class*='accept' i]",
        ".cookie-close",
        "[data-action='accept-cookies']",
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button[class*='cookie-accept']",
    ]

    # Najpierw selektory specyficzne dla portalu
    selectors = portal_selectors.get(portal, []) + generic_selectors

    for sel in selectors:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                try:
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
                time.sleep(1.0)
                return True
        except Exception:
            continue

    # Fallback: wyszukaj przycisk po tekscie (polskie portale)
    accept_texts = [
        "Akceptuję", "Akceptuj", "Zgadzam się", "Zgadzam sie",
        "Wyrażam zgodę", "Wyrazam zgode", "Przejdź do serwisu",
        "Przejdz do serwisu", "OK", "Rozumiem",
        "Zaakceptuj", "Akceptuj wszystko", "Accept all",
        "Kontynuuj", "Przejdź dalej",
    ]
    for text in accept_texts:
        try:
            btns = driver.find_elements(
                By.XPATH,
                f"//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                f"'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')] | "
                f"//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                f"'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"
            )
            for btn in btns:
                if btn.is_displayed():
                    try:
                        btn.click()
                    except Exception:
                        driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1.0)
                    return True
        except Exception:
            continue

    # Ostateczny fallback: usun overlay i iframe cookie z DOM
    try:
        driver.execute_script("""
            // Usun popularne cookie overlay
            var selectors = [
                '#didomi-host', '.fc-consent-root', '#CybotCookiebotDialog',
                '#onetrust-consent-sdk', '.cc-window', '#cookie-law-info-bar',
                '[class*="cookie-overlay"]', '[class*="consent-overlay"]',
                '[id*="cookie-banner"]', '[class*="cookie-banner"]',
                '[class*="gdpr"]', '[class*="rodo"]'
            ];
            selectors.forEach(function(s) {
                document.querySelectorAll(s).forEach(function(el) { el.remove(); });
            });
            // Usun backdrop/overlay blokujacy klikniecia
            document.querySelectorAll('[class*="overlay"]').forEach(function(el) {
                var style = window.getComputedStyle(el);
                if (style.position === 'fixed' || style.position === 'absolute') {
                    if (parseFloat(style.zIndex) > 100 || style.zIndex === 'auto') {
                        el.remove();
                    }
                }
            });
            // Przywroc scroll na body
            document.body.style.overflow = 'auto';
            document.documentElement.style.overflow = 'auto';
        """)
        time.sleep(0.5)
        return True  # overlay usuniete JS-em
    except Exception:
        pass

    return False


def normalize_price(price_text: str) -> tuple[float | None, str]:
    """Parsuje tekst ceny -> (wartosc, waluta).
    Np. '450 000 zl' -> (450000.0, 'PLN'), '1 200 EUR' -> (1200.0, 'EUR')
    """
    if not price_text:
        return None, "PLN"
    text = price_text.strip().lower()

    waluta = "PLN"
    if "eur" in text or "\u20ac" in text:
        waluta = "EUR"
    elif "usd" in text or "$" in text:
        waluta = "USD"

    # Usun wszystko poza cyframi, kropkami i przecinkami
    cleaned = re.sub(r"[^\d,.]", "", text)
    # Zamien przecinek na kropke (format polski)
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")

    try:
        return float(cleaned), waluta
    except (ValueError, TypeError):
        return None, waluta


def normalize_area(area_text: str) -> float | None:
    """Parsuje tekst powierzchni -> float m2.
    Np. '65,5 m2' -> 65.5, '1 200 m2' -> 1200.0
    """
    if not area_text:
        return None
    cleaned = re.sub(r"[^\d,.]", " ", area_text)
    cleaned = cleaned.strip()
    # Zamien przecinek na kropke
    cleaned = cleaned.replace(",", ".")
    # Usun spacje wewnatrz liczby
    parts = cleaned.split()
    if parts:
        try:
            return float(parts[0])
        except (ValueError, TypeError):
            pass
    return None


def normalize_rooms(rooms_text: str) -> int | None:
    """Parsuje tekst pokoi -> int."""
    if not rooms_text:
        return None
    match = re.search(r"\d+", rooms_text)
    if match:
        return int(match.group())
    return None


def compute_price_per_m2(cena: float | None, area: float | None) -> float | None:
    """Oblicza cene za m2."""
    if cena and area and area > 0:
        return round(cena / area, 2)
    return None


def extract_syndyk_info(text: str) -> str | None:
    """Wyciaga frazy zwiazane z syndykiem z opisu."""
    if not text:
        return None
    patterns = [
        r"syndyk\s+masy\s+upad[l\u0142]o[s\u015b]ciowej\s+[\w\s]+",
        r"sygn\.?\s*akt\s*[\w\s/\-]+",
        r"post[e\u0119]powanie\s+upad[l\u0142]o[s\u015b]ciowe\s*[\w\s]*",
        r"s[a\u0105]d\s+rejonowy\s+[\w\s]+",
        r"KRZ\s*[\d/]+",
    ]
    found = []
    for pat in patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        found.extend(m.strip() for m in matches)
    return "; ".join(found) if found else None


def random_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    """Losowe opoznienie anty-botowe."""
    time.sleep(random.uniform(min_s, max_s))


# --------------- Baza danych ---------------

DB_COLUMNS = [
    "url", "portal", "typ_nieruchomosci", "tytul", "opis",
    "cena", "cena_za_m2", "waluta",
    "powierzchnia_m2", "liczba_pokoi", "pietro",
    "rok_budowy", "typ_budynku", "stan_wykonczenia",
    "forma_wlasnosci", "rynek",
    "miasto", "dzielnica", "ulica", "wojewodztwo",
    "ogloszeniodawca", "numer_oferty",
    "data_dodania", "data_scrape", "aktywne",
]


def init_db(db_path: Path = DB_FILE) -> sqlite3.Connection:
    """Tworzy baze danych i tabele jesli nie istnieje."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nieruchomosci (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            url                 TEXT UNIQUE NOT NULL,
            portal              TEXT NOT NULL,
            typ_nieruchomosci   TEXT,
            tytul               TEXT,
            opis                TEXT,
            cena                REAL,
            cena_za_m2          REAL,
            waluta              TEXT DEFAULT 'PLN',
            powierzchnia_m2     REAL,
            liczba_pokoi        INTEGER,
            pietro              TEXT,
            rok_budowy          INTEGER,
            typ_budynku         TEXT,
            stan_wykonczenia    TEXT,
            forma_wlasnosci     TEXT,
            rynek               TEXT,
            miasto              TEXT,
            dzielnica           TEXT,
            ulica               TEXT,
            wojewodztwo         TEXT,
            ogloszeniodawca     TEXT,
            numer_oferty        TEXT,
            data_dodania        TEXT,
            data_scrape         TEXT NOT NULL,
            aktywne             INTEGER DEFAULT 1
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_portal ON nieruchomosci(portal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_typ ON nieruchomosci(typ_nieruchomosci)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cena ON nieruchomosci(cena)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_miasto ON nieruchomosci(miasto)")
    conn.commit()
    return conn


def save_to_db(records: list[dict], db_path: Path = DB_FILE) -> int:
    """Zapisuje rekordy do SQLite. Pomija duplikaty (INSERT OR IGNORE).
    Zwraca liczbe nowo dodanych wierszy."""
    conn = init_db(db_path)
    cols = [c for c in DB_COLUMNS if c != "aktywne"]
    placeholders = ", ".join(["?"] * len(cols))
    cols_sql = ", ".join(cols)
    inserted = 0
    for rec in records:
        url = rec.get("url", "")
        if not url:
            continue
        values = tuple(rec.get(col, None) for col in cols)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO nieruchomosci ({cols_sql}) "
            f"VALUES ({placeholders})",
            values,
        )
        inserted += cur.rowcount
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM nieruchomosci").fetchone()[0]
    conn.close()
    print(f"[db] Dodano {inserted} nowych rekordow (lacznie w bazie: {total}).")
    return inserted


def load_links(links_dir: Path = LINKS_DIR) -> dict[str, list[str]]:
    """Czyta pliki *_links.txt i zwraca {nazwa_portalu: [url, ...]}."""
    result = {}
    for fpath in sorted(links_dir.glob("*_links.txt")):
        portal = fpath.stem.replace("_links", "")
        urls = [line.strip() for line in fpath.read_text().splitlines()
                if line.strip() and line.strip().startswith("http")]
        if urls:
            result[portal] = urls
    return result


# --------------- Bazowa klasa scrapera ---------------

class PortalScraper(ABC):
    """Bazowa klasa dla scraperow poszczegolnych portali."""

    PORTAL_NAME: str = ""

    def __init__(self, driver: webdriver.Chrome):
        self.driver = driver
        self.wait = WebDriverWait(driver, DEFAULT_WAIT)
        self._cookies_dismissed = False

    def scrape_all(self, urls: list[str]) -> list[dict]:
        """Scrapuje wszystkie URL-e dla tego portalu."""
        all_listings: list[dict] = []
        for url in urls:
            try:
                listings = self._scrape_search_url(url)
                all_listings.extend(listings)
                print(f"[{self.PORTAL_NAME}] {url[:80]}... -> {len(listings)} ogloszen")
            except Exception as exc:
                print(f"[error] {self.PORTAL_NAME} blad dla {url[:80]}: {exc}")
        # Usun duplikaty po URL
        seen: set[str] = set()
        unique: list[dict] = []
        for rec in all_listings:
            u = rec.get("url", "")
            if u and u not in seen:
                seen.add(u)
                unique.append(rec)
        return unique

    def _scrape_search_url(self, url: str) -> list[dict]:
        """Scrapuje jedna strone wynikow z obsluga paginacji."""
        listings: list[dict] = []
        self.driver.get(url)
        time.sleep(3)

        # Probuj zamknac cookies na kazdej nowej stronie (baner moze sie pojawic ponownie)
        if not self._cookies_dismissed:
            dismissed = dismiss_cookie_consent(self.driver, portal=self.PORTAL_NAME)
            if dismissed:
                self._cookies_dismissed = True
                time.sleep(1)
            else:
                # Probuj jeszcze raz po krotkim oczekiwaniu (baner laduje sie asynchronicznie)
                time.sleep(2)
                dismissed = dismiss_cookie_consent(self.driver, portal=self.PORTAL_NAME)
                if dismissed:
                    self._cookies_dismissed = True
                    time.sleep(1)

        property_type = self._detect_property_type(url)
        today = date.today().isoformat()

        for page_num in range(1, MAX_PAGES + 1):
            try:
                page_listings = self._extract_listings_from_page()
            except Exception as exc:
                print(f"[{self.PORTAL_NAME}] Blad ekstrakcji strona {page_num}: {exc}")
                break

            for listing in page_listings:
                listing.setdefault("portal", self.PORTAL_NAME)
                listing.setdefault("typ_nieruchomosci", property_type)
                listing.setdefault("data_scrape", today)
                # Oblicz cene za m2 jesli brak
                if not listing.get("cena_za_m2"):
                    listing["cena_za_m2"] = compute_price_per_m2(
                        listing.get("cena"), listing.get("powierzchnia_m2")
                    )

            listings.extend(page_listings)
            print(f"  [{self.PORTAL_NAME}] strona {page_num}: "
                  f"{len(page_listings)} ogloszen")

            if not page_listings:
                break

            try:
                if not self._go_to_next_page():
                    break
            except Exception:
                break

            random_delay(2.0, 4.0)

        return listings

    @abstractmethod
    def _extract_listings_from_page(self) -> list[dict]:
        """Wyciaga ogloszenia z biezacej strony wynikow."""
        ...

    @abstractmethod
    def _go_to_next_page(self) -> bool:
        """Przechodzi do nastepnej strony. Zwraca False jesli brak."""
        ...

    @abstractmethod
    def _detect_property_type(self, url: str) -> str:
        """Okresla typ nieruchomosci z URL."""
        ...


# --------------- OTODOM ---------------

class OtodomScraper(PortalScraper):
    """Scraper dla otodom.pl - najbogatszy w dane portal."""

    PORTAL_NAME = "otodom"

    _TYPE_MAP = {
        "mieszkanie": "mieszkanie",
        "kawalerka": "kawalerka",
        "dom": "dom",
        "inwestycja": "inwestycja",
        "pokoj": "pokoj",
        "dzialka": "dzialka",
        "lokal": "lokal",
        "haleimagazyny": "magazyn",
        "garaz": "garaz",
    }

    def _detect_property_type(self, url: str) -> str:
        path = urlparse(url).path
        # /pl/wyniki/sprzedaz/{typ}/...
        parts = [p for p in path.split("/") if p]
        for part in parts:
            if part in self._TYPE_MAP:
                return self._TYPE_MAP[part]
        return "inne"

    def _extract_listings_from_page(self) -> list[dict]:
        listings: list[dict] = []

        # Probuj parsowac __NEXT_DATA__ (Next.js)
        try:
            script = self.driver.find_element(By.ID, "__NEXT_DATA__")
            data = json.loads(script.get_attribute("innerHTML"))
            items = (data.get("props", {}).get("pageProps", {})
                     .get("data", {}).get("searchAds", {}).get("items", []))
            for item in items:
                try:
                    listing = self._parse_nextdata_item(item)
                    if listing and listing.get("url"):
                        listings.append(listing)
                except Exception:
                    continue
            if listings:
                return listings
        except Exception:
            pass

        # Fallback: DOM parsing
        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "[data-cy='search.listing.organic'] article, "
            "[data-cy='search.listing'] a[href*='/oferta/'], "
            "ul[data-cy='search.listing.organic'] li article, "
            "a[data-cy='listing-item-link']"
        )
        if not cards:
            cards = self.driver.find_elements(By.CSS_SELECTOR, "article")

        for card in cards:
            try:
                listing = self._parse_dom_card(card)
                if listing and listing.get("url"):
                    listings.append(listing)
            except Exception:
                continue

        return listings

    def _parse_nextdata_item(self, item: dict) -> dict:
        """Parsuje element z __NEXT_DATA__ JSON."""
        slug = item.get("slug", "")
        item_id = item.get("id", "")
        url = f"https://www.otodom.pl/pl/oferta/{slug}" if slug else ""

        price_obj = item.get("totalPrice", {}) or {}
        cena = price_obj.get("value")
        waluta = price_obj.get("currency", "PLN")

        area = item.get("areaInSquareMeters")
        rooms = item.get("roomsNumber")

        price_per_m2 = None
        if cena and area and area > 0:
            price_per_m2 = round(cena / area, 2)

        location = item.get("location", {}) or {}
        address = location.get("address", {}) or {}
        city_obj = address.get("city", {}) or {}
        district_obj = address.get("district", {}) or {}
        street_obj = address.get("street", {}) or {}
        province_obj = address.get("province", {}) or {}

        title = item.get("title", "")
        desc = item.get("description", "") or item.get("shortDescription", "")

        estate = item.get("estate", {}) or {}

        return {
            "url": url,
            "tytul": title,
            "opis": desc[:2000] if desc else None,
            "cena": cena,
            "cena_za_m2": price_per_m2,
            "waluta": waluta or "PLN",
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "pietro": None,
            "rok_budowy": estate.get("buildYear"),
            "typ_budynku": estate.get("buildingType"),
            "stan_wykonczenia": estate.get("constructionStatus"),
            "forma_wlasnosci": item.get("ownershipType"),
            "rynek": item.get("market"),
            "miasto": city_obj.get("name"),
            "dzielnica": district_obj.get("name"),
            "ulica": street_obj.get("name"),
            "wojewodztwo": province_obj.get("name"),
            "ogloszeniodawca": item.get("agency", {}).get("name") if item.get("agency") else None,
            "numer_oferty": str(item_id) if item_id else None,
            "data_dodania": item.get("createdAt", "")[:10] if item.get("createdAt") else None,
        }

    def _parse_dom_card(self, card) -> dict:
        """Parsuje karte ogloszenia z DOM."""
        # URL
        url = ""
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='/oferta/']")
        if not links:
            links = card.find_elements(By.TAG_NAME, "a")
        if links:
            href = links[0].get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.otodom.pl" + href
            url = href

        # Tytul
        title = ""
        for sel in ["[data-cy='listing-item-title']", "h3", "h2", "p"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                title = els[0].text.strip()
                if title:
                    break

        # Cena
        cena, waluta = None, "PLN"
        for sel in ["[data-cy='listing-item-price']", "span[class*='price']", "strong"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                cena, waluta = normalize_price(els[0].text)
                if cena:
                    break

        # Powierzchnia i pokoje z tekstu karty
        card_text = card.text or ""
        area = None
        rooms = None
        area_match = re.search(r"(\d[\d\s,]*[,.]?\d*)\s*m[2\u00b2]", card_text)
        if area_match:
            area = normalize_area(area_match.group(1))
        rooms_match = re.search(r"(\d+)\s*poko[ji]", card_text, re.IGNORECASE)
        if rooms_match:
            rooms = int(rooms_match.group(1))

        # Lokalizacja
        location_text = ""
        for sel in ["[class*='location']", "p[class*='subtitle']"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                location_text = els[0].text.strip()
                break

        miasto, dzielnica = "", ""
        if location_text:
            loc_parts = [p.strip() for p in location_text.split(",")]
            if len(loc_parts) >= 2:
                miasto = loc_parts[-1]
                dzielnica = loc_parts[0]
            elif loc_parts:
                miasto = loc_parts[0]

        return {
            "url": url,
            "tytul": title,
            "cena": cena,
            "waluta": waluta,
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "miasto": miasto,
            "dzielnica": dzielnica,
        }

    def _go_to_next_page(self) -> bool:
        """Paginacja otodom - przycisk nastepna strona."""
        try:
            next_btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "[data-cy='pagination.next-page'], "
                "a[aria-label='next page'], "
                "button[aria-label='next page'], "
                "li.pagination-next a"
            )
            for btn in next_btns:
                if btn.is_displayed() and btn.is_enabled():
                    disabled = btn.get_attribute("disabled")
                    aria_disabled = btn.get_attribute("aria-disabled")
                    if disabled or aria_disabled == "true":
                        return False
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    safe_click(self.driver, btn)
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False


# --------------- OLX ---------------

class OlxScraper(PortalScraper):
    """Scraper dla olx.pl."""

    PORTAL_NAME = "olx"

    def _detect_property_type(self, url: str) -> str:
        path = urlparse(url).path.lower()
        if "mieszkania" in path:
            return "mieszkanie"
        elif "domy" in path:
            return "dom"
        elif "dzialki" in path:
            return "dzialka"
        elif "biura-lokale" in path:
            return "lokal"
        elif "garaze-parkingi" in path:
            return "garaz"
        return "inne"

    def _extract_listings_from_page(self) -> list[dict]:
        listings: list[dict] = []

        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "[data-cy='l-card'], div[data-testid='l-card']"
        )
        if not cards:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div.css-1sw7q4x, div[class*='offer-wrapper']"
            )

        for card in cards:
            try:
                listing = self._parse_card(card)
                if listing and listing.get("url"):
                    listings.append(listing)
            except Exception:
                continue

        return listings

    def _parse_card(self, card) -> dict:
        # URL
        url = ""
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='/oferta/'], a[href*='/d/']")
        if not links:
            links = card.find_elements(By.TAG_NAME, "a")
        if links:
            href = links[0].get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.olx.pl" + href
            # Odfiltruj reklamy/promowane linki zewnetrzne
            if "olx.pl" in href or href.startswith("/"):
                url = href.split("#")[0].split("?")[0]

        # Tytul
        title = ""
        for sel in ["h6", "h4", "[data-cy='ad-card-title']", "a"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                title = els[0].text.strip()
                break

        # Cena
        cena, waluta = None, "PLN"
        for sel in ["[data-testid='ad-price']", "p[data-testid='ad-price']",
                     "[class*='price']", "p.price"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                cena, waluta = normalize_price(els[0].text)
                if cena:
                    break

        # Lokalizacja i data
        location_text = ""
        data_dodania = ""
        for sel in ["[data-testid='location-date']", "p[class*='location']",
                     "span[class*='breadcrumb']"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                full = els[0].text.strip()
                parts = full.split(" - ")
                location_text = parts[0].strip() if parts else full
                if len(parts) > 1:
                    data_dodania = parts[-1].strip()
                break

        miasto, dzielnica = "", ""
        if location_text:
            loc_parts = [p.strip() for p in location_text.split(",")]
            if len(loc_parts) >= 2:
                miasto = loc_parts[0]
                dzielnica = loc_parts[1]
            elif loc_parts:
                miasto = loc_parts[0]

        # Parametry (powierzchnia, pokoje) - z etykiet
        card_text = card.text or ""
        area = None
        rooms = None
        area_match = re.search(r"(\d[\d\s,]*[,.]?\d*)\s*m[2\u00b2]", card_text)
        if area_match:
            area = normalize_area(area_match.group(1))
        rooms_match = re.search(r"(\d+)\s*poko[ji]", card_text, re.IGNORECASE)
        if rooms_match:
            rooms = int(rooms_match.group(1))

        return {
            "url": url,
            "tytul": title,
            "cena": cena,
            "waluta": waluta,
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "miasto": miasto,
            "dzielnica": dzielnica,
            "data_dodania": data_dodania or None,
        }

    def _go_to_next_page(self) -> bool:
        try:
            next_btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "[data-cy='pagination-forward'], "
                "a[data-testid='pagination-forward'], "
                "a[class*='pagination-forward']"
            )
            for btn in next_btns:
                if btn.is_displayed():
                    href = btn.get_attribute("href")
                    if href:
                        self.driver.get(href)
                        time.sleep(3)
                        return True
                    safe_click(self.driver, btn)
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False


# --------------- GRATKA ---------------

class GratkaScraper(PortalScraper):
    """Scraper dla gratka.pl."""

    PORTAL_NAME = "gratka"

    _TYPE_MAP = {
        "mieszkania": "mieszkanie",
        "domy": "dom",
        "lokale-uzytkowe": "lokal",
        "dzialki-grunty": "dzialka",
        "garaze": "garaz",
        "pokoje": "pokoj",
    }

    def _detect_property_type(self, url: str) -> str:
        path = urlparse(url).path.lower()
        for key, val in self._TYPE_MAP.items():
            if key in path:
                return val
        return "inne"

    def _ensure_no_overlay(self) -> None:
        """Upewnia sie ze cookie overlay nie blokuje strony Gratka."""
        # Gratka uzywa Didomi - sprawdz czy iframe z consent nadal istnieje
        try:
            iframes = self.driver.find_elements(By.CSS_SELECTOR,
                "iframe[id*='didomi'], iframe[src*='didomi'], "
                "iframe[id*='consent'], iframe[src*='consent']")
            for iframe in iframes:
                if iframe.is_displayed():
                    # Przelacz sie do iframe i kliknij accept
                    self.driver.switch_to.frame(iframe)
                    try:
                        btns = self.driver.find_elements(By.CSS_SELECTOR,
                            "button[class*='agree'], button[class*='accept'], "
                            "button#didomi-notice-agree-button")
                        for btn in btns:
                            if btn.is_displayed():
                                btn.click()
                                time.sleep(1)
                                break
                    finally:
                        self.driver.switch_to.default_content()
        except Exception:
            pass

        # Usun overlay JS-em na wszelki wypadek
        try:
            self.driver.execute_script("""
                document.querySelectorAll(
                    '#didomi-host, .didomi-popup-container, '
                    + '[class*="consent-overlay"], [class*="cookie-overlay"], '
                    + '.didomi-popup-backdrop'
                ).forEach(function(el) { el.remove(); });
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            """)
        except Exception:
            pass

    def _extract_listings_from_page(self) -> list[dict]:
        listings: list[dict] = []

        self._ensure_no_overlay()

        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "article[data-cy='listing'], "
            "article.teaserUnified, "
            "div.listing__item, "
            "article[class*='teaser'], "
            "div[class*='listing'] article"
        )
        if not cards:
            cards = self.driver.find_elements(By.CSS_SELECTOR, "article")

        for card in cards:
            try:
                listing = self._parse_card(card)
                if listing and listing.get("url"):
                    listings.append(listing)
            except Exception:
                continue
        return listings

    def _parse_card(self, card) -> dict:
        # URL
        url = ""
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='gratka.pl']")
        if not links:
            links = card.find_elements(By.TAG_NAME, "a")
        if links:
            href = links[0].get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://gratka.pl" + href
            url = href

        # Tytul
        title = ""
        for sel in ["h2", "h3", "[class*='title']", "a"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                title = els[0].text.strip()
                break

        # Cena
        cena, waluta = None, "PLN"
        for sel in ["[class*='price']", "span.teaserUnified__price",
                     "p[class*='price']", "strong"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                cena, waluta = normalize_price(els[0].text)
                if cena:
                    break

        # Powierzchnia, pokoje
        card_text = card.text or ""
        area = None
        rooms = None
        area_match = re.search(r"(\d[\d\s,]*[,.]?\d*)\s*m[2\u00b2]", card_text)
        if area_match:
            area = normalize_area(area_match.group(1))
        rooms_match = re.search(r"(\d+)\s*poko[ji]", card_text, re.IGNORECASE)
        if rooms_match:
            rooms = int(rooms_match.group(1))

        # Lokalizacja
        location_text = ""
        for sel in ["[class*='location']", "span[class*='address']", "p.teaserUnified__location"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                location_text = els[0].text.strip()
                break

        miasto, dzielnica = "", ""
        if location_text:
            loc_parts = [p.strip() for p in location_text.split(",")]
            if len(loc_parts) >= 2:
                miasto = loc_parts[0]
                dzielnica = loc_parts[1]
            elif loc_parts:
                miasto = loc_parts[0]

        return {
            "url": url,
            "tytul": title,
            "cena": cena,
            "waluta": waluta,
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "miasto": miasto,
            "dzielnica": dzielnica,
        }

    def _go_to_next_page(self) -> bool:
        try:
            next_btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a[class*='pagination__nextPage'], "
                "a[aria-label='next'], "
                "li.pagination-next a, "
                "a.pagination__next"
            )
            for btn in next_btns:
                if btn.is_displayed():
                    href = btn.get_attribute("href")
                    if href:
                        self.driver.get(href)
                        time.sleep(3)
                        return True
                    safe_click(self.driver, btn)
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False


# --------------- NIERUCHOMOSCI-ONLINE ---------------

class NieruchomosciOnlineScraper(PortalScraper):
    """Scraper dla nieruchomosci-online.pl."""

    PORTAL_NAME = "nieruchomosci"

    _TYPE_MAP = {
        "dzialka": "dzialka",
        "mieszkanie": "mieszkanie",
        "lokal-uzytkowy": "lokal",
        "budynek-uzytkowy": "lokal",
        "dom": "dom",
    }

    def _detect_property_type(self, url: str) -> str:
        url_lower = url.lower()
        for key, val in self._TYPE_MAP.items():
            if key in url_lower:
                return val
        return "inne"

    def _ensure_no_overlay(self) -> None:
        """Upewnia sie ze cookie overlay nie blokuje strony nieruchomosci-online."""
        # Sprawdz iframe consent (FundingChoices / CMP)
        try:
            iframes = self.driver.find_elements(By.CSS_SELECTOR,
                "iframe[id*='fc-iframe'], iframe[src*='fundingchoices'], "
                "iframe[id*='consent'], iframe[src*='consent']")
            for iframe in iframes:
                if iframe.is_displayed():
                    self.driver.switch_to.frame(iframe)
                    try:
                        btns = self.driver.find_elements(By.CSS_SELECTOR,
                            "button.fc-cta-consent, button[class*='accept'], "
                            "button[class*='agree']")
                        for btn in btns:
                            if btn.is_displayed():
                                btn.click()
                                time.sleep(1)
                                break
                    finally:
                        self.driver.switch_to.default_content()
        except Exception:
            pass

        # Usun overlay JS-em
        try:
            self.driver.execute_script("""
                document.querySelectorAll(
                    '.fc-consent-root, .fc-dialog-overlay, '
                    + '#CybotCookiebotDialog, [class*="consent-overlay"], '
                    + '[class*="cookie-overlay"], .cc-window, '
                    + '[class*="cookie-banner"]'
                ).forEach(function(el) { el.remove(); });
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            """)
        except Exception:
            pass

    def _extract_listings_from_page(self) -> list[dict]:
        listings: list[dict] = []

        self._ensure_no_overlay()

        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "div.offer, div.box-offer, div[class*='offer-item'], "
            "article[class*='offer'], div.column-container"
        )
        if not cards:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div[class*='listing'] > div"
            )

        for card in cards:
            try:
                listing = self._parse_card(card)
                if listing and listing.get("url"):
                    listings.append(listing)
            except Exception:
                continue
        return listings

    def _parse_card(self, card) -> dict:
        # URL
        url = ""
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='nieruchomosci-online.pl']")
        if not links:
            links = card.find_elements(By.TAG_NAME, "a")
        if links:
            href = links[0].get_attribute("href") or ""
            if "/szukaj.html" not in href and href.startswith("http"):
                url = href

        # Tytul
        title = ""
        for sel in ["h2", "h3", "[class*='title']", "a[class*='title']"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                title = els[0].text.strip()
                break

        # Cena
        cena, waluta = None, "PLN"
        for sel in ["[class*='price']", "span.price", "strong.price", "p.price"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                cena, waluta = normalize_price(els[0].text)
                if cena:
                    break

        # Parametry
        card_text = card.text or ""
        area = None
        rooms = None
        area_match = re.search(r"(\d[\d\s,]*[,.]?\d*)\s*m[2\u00b2]", card_text)
        if area_match:
            area = normalize_area(area_match.group(1))
        rooms_match = re.search(r"(\d+)\s*poko[ji]", card_text, re.IGNORECASE)
        if rooms_match:
            rooms = int(rooms_match.group(1))

        # Lokalizacja
        location_text = ""
        for sel in ["[class*='location']", "[class*='address']", "span.city"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                location_text = els[0].text.strip()
                break

        miasto, dzielnica = "", ""
        if location_text:
            loc_parts = [p.strip() for p in location_text.split(",")]
            if len(loc_parts) >= 2:
                miasto = loc_parts[0]
                dzielnica = loc_parts[1]
            elif loc_parts:
                miasto = loc_parts[0]

        return {
            "url": url,
            "tytul": title,
            "cena": cena,
            "waluta": waluta,
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "miasto": miasto,
            "dzielnica": dzielnica,
        }

    def _go_to_next_page(self) -> bool:
        try:
            next_btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a.pagination-next, a[rel='next'], "
                "li.next a, a[class*='next']"
            )
            for btn in next_btns:
                if btn.is_displayed():
                    href = btn.get_attribute("href")
                    if href:
                        self.driver.get(href)
                        time.sleep(3)
                        return True
                    safe_click(self.driver, btn)
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False


# --------------- LENTO ---------------

class LentoScraper(PortalScraper):
    """Scraper dla lento.pl."""

    PORTAL_NAME = "lento"

    def _detect_property_type(self, url: str) -> str:
        path = urlparse(url).path.lower()
        if "mieszkania" in path:
            return "mieszkanie"
        elif "domy" in path:
            return "dom"
        elif "dzialki" in path:
            return "dzialka"
        elif "lokale" in path:
            return "lokal"
        elif "garaze" in path:
            return "garaz"
        return "inne"

    def _ensure_no_overlay(self) -> None:
        """Upewnia sie ze cookie overlay nie blokuje strony Lento."""
        # Lento uzywa FundingChoices (fc-*) lub CookieBot
        try:
            iframes = self.driver.find_elements(By.CSS_SELECTOR,
                "iframe[id*='fc-iframe'], iframe[src*='fundingchoices'], "
                "iframe[id*='consent']")
            for iframe in iframes:
                if iframe.is_displayed():
                    self.driver.switch_to.frame(iframe)
                    try:
                        btns = self.driver.find_elements(By.CSS_SELECTOR,
                            "button.fc-cta-consent, button.fc-button-background, "
                            "button[class*='accept'], button[class*='agree']")
                        for btn in btns:
                            if btn.is_displayed():
                                btn.click()
                                time.sleep(1)
                                break
                    finally:
                        self.driver.switch_to.default_content()
        except Exception:
            pass

        # Usun overlay JS-em
        try:
            self.driver.execute_script("""
                document.querySelectorAll(
                    '.fc-consent-root, .fc-dialog-overlay, '
                    + '#CybotCookiebotDialog, .cc-window, '
                    + '[class*="consent-overlay"], [class*="cookie-overlay"], '
                    + '[class*="cookie-banner"], [class*="rodo"]'
                ).forEach(function(el) { el.remove(); });
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            """)
        except Exception:
            pass

    def _extract_listings_from_page(self) -> list[dict]:
        listings: list[dict] = []

        self._ensure_no_overlay()

        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "div.main-list > div[class*='item'], "
            "div[class*='offer'], "
            "article, "
            "div.ad-list-item"
        )
        if not cards:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div[id*='list'] > div"
            )

        for card in cards:
            try:
                listing = self._parse_card(card)
                if listing and listing.get("url"):
                    listings.append(listing)
            except Exception:
                continue
        return listings

    def _parse_card(self, card) -> dict:
        # URL
        url = ""
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='lento.pl']")
        if not links:
            links = card.find_elements(By.TAG_NAME, "a")
        if links:
            href = links[0].get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://lento.pl" + href
            if "lento.pl" in href and "/q-" not in href:
                url = href

        # Tytul
        title = ""
        for sel in ["h2 a", "h3 a", "[class*='title']", "a"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                title = els[0].text.strip()
                break

        # Cena
        cena, waluta = None, "PLN"
        for sel in ["[class*='price']", "span.price", "strong"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                cena, waluta = normalize_price(els[0].text)
                if cena:
                    break

        # Parametry z tekstu
        card_text = card.text or ""
        area = None
        rooms = None
        area_match = re.search(r"(\d[\d\s,]*[,.]?\d*)\s*m[2\u00b2]", card_text)
        if area_match:
            area = normalize_area(area_match.group(1))
        rooms_match = re.search(r"(\d+)\s*poko[ji]", card_text, re.IGNORECASE)
        if rooms_match:
            rooms = int(rooms_match.group(1))

        # Lokalizacja
        location_text = ""
        for sel in ["[class*='location']", "[class*='city']", "span.address"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                location_text = els[0].text.strip()
                break

        miasto = ""
        if location_text:
            loc_parts = [p.strip() for p in location_text.split(",")]
            if loc_parts:
                miasto = loc_parts[0]

        return {
            "url": url,
            "tytul": title,
            "cena": cena,
            "waluta": waluta,
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "miasto": miasto,
        }

    def _go_to_next_page(self) -> bool:
        try:
            next_btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a[rel='next'], a.next, "
                "li.next a, a[class*='next']"
            )
            for btn in next_btns:
                if btn.is_displayed():
                    href = btn.get_attribute("href")
                    if href:
                        self.driver.get(href)
                        time.sleep(3)
                        return True
                    safe_click(self.driver, btn)
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False


# --------------- SPRZEDAJEMY ---------------

class SprzedajemyScraper(PortalScraper):
    """Scraper dla sprzedajemy.pl."""

    PORTAL_NAME = "sprzedajemy"

    def _detect_property_type(self, url: str) -> str:
        path = urlparse(url).path.lower()
        if "mieszkania" in path:
            return "mieszkanie"
        elif "domy" in path:
            return "dom"
        elif "dzialki" in path:
            return "dzialka"
        elif "lokale" in path:
            return "lokal"
        elif "garaze" in path:
            return "garaz"
        return "inne"

    def _extract_listings_from_page(self) -> list[dict]:
        listings: list[dict] = []

        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "div[class*='offer'], "
            "article[class*='offer'], "
            "div.normal, "
            "div[class*='advertisement']"
        )
        if not cards:
            cards = self.driver.find_elements(
                By.CSS_SELECTOR, "div.listing > div, ul.listing > li"
            )

        for card in cards:
            try:
                listing = self._parse_card(card)
                if listing and listing.get("url"):
                    listings.append(listing)
            except Exception:
                continue
        return listings

    def _parse_card(self, card) -> dict:
        # URL
        url = ""
        links = card.find_elements(By.CSS_SELECTOR, "a[href*='sprzedajemy.pl']")
        if not links:
            links = card.find_elements(By.TAG_NAME, "a")
        if links:
            href = links[0].get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://sprzedajemy.pl" + href
            if "sprzedajemy.pl" in href:
                url = href

        # Tytul
        title = ""
        for sel in ["h2 a", "h3 a", "[class*='title']", "a[class*='name']", "a"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                title = els[0].text.strip()
                break

        # Cena
        cena, waluta = None, "PLN"
        for sel in ["[class*='price']", "span.price", "strong.price"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                cena, waluta = normalize_price(els[0].text)
                if cena:
                    break

        # Parametry z tekstu
        card_text = card.text or ""
        area = None
        rooms = None
        area_match = re.search(r"(\d[\d\s,]*[,.]?\d*)\s*m[2\u00b2]", card_text)
        if area_match:
            area = normalize_area(area_match.group(1))
        rooms_match = re.search(r"(\d+)\s*poko[ji]", card_text, re.IGNORECASE)
        if rooms_match:
            rooms = int(rooms_match.group(1))

        # Lokalizacja
        location_text = ""
        for sel in ["[class*='location']", "[class*='city']", "span.lokalizacja"]:
            els = card.find_elements(By.CSS_SELECTOR, sel)
            if els:
                location_text = els[0].text.strip()
                break

        miasto = ""
        if location_text:
            loc_parts = [p.strip() for p in location_text.split(",")]
            if loc_parts:
                miasto = loc_parts[0]

        return {
            "url": url,
            "tytul": title,
            "cena": cena,
            "waluta": waluta,
            "powierzchnia_m2": area,
            "liczba_pokoi": rooms,
            "miasto": miasto,
        }

    def _go_to_next_page(self) -> bool:
        try:
            next_btns = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a[rel='next'], a.next, "
                "a[class*='pagination-next'], "
                "li.next a"
            )
            for btn in next_btns:
                if btn.is_displayed():
                    href = btn.get_attribute("href")
                    if href:
                        self.driver.get(href)
                        time.sleep(3)
                        return True
                    safe_click(self.driver, btn)
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False


# --------------- Rejestracja portali ---------------

PORTAL_SCRAPERS: dict[str, type[PortalScraper]] = {
    "otodom": OtodomScraper,
    "olx": OlxScraper,
    "gratka": GratkaScraper,
    "nieruchomosci": NieruchomosciOnlineScraper,
    "lento": LentoScraper,
    "sprzedajemy": SprzedajemyScraper,
}


# --------------- Main ---------------

def main() -> None:
    print("=" * 60)
    print("Scraper nieruchomosci syndyk")
    print(f"Data: {date.today().isoformat()}")
    print("=" * 60)

    all_links = load_links()
    if not all_links:
        print("[error] Brak linkow w folderze links/")
        return

    for portal, urls in all_links.items():
        print(f"  {portal}: {len(urls)} URL(i)")

    # Inicjalizacja bazy danych
    init_db()

    driver = build_driver(headless=HEADLESS)
    driver.set_page_load_timeout(45)

    try:
        all_records: list[dict] = []

        for portal_name, urls in all_links.items():
            scraper_cls = PORTAL_SCRAPERS.get(portal_name)
            if not scraper_cls:
                print(f"[warn] Brak scrapera dla portalu: {portal_name}")
                continue

            print(f"\n{'─' * 40}")
            print(f"Portal: {portal_name} ({len(urls)} URL-i)")
            print(f"{'─' * 40}")

            scraper = scraper_cls(driver)
            try:
                records = scraper.scrape_all(urls)
                all_records.extend(records)
                print(f"[{portal_name}] SUMA: {len(records)} unikalnych ogloszen")
            except Exception as exc:
                print(f"[error] {portal_name} calkowity blad: {exc}")

            random_delay(3.0, 6.0)

        print(f"\n{'=' * 60}")
        print(f"Laczna liczba ogloszen: {len(all_records)}")

        if all_records:
            inserted = save_to_db(all_records)
            print(f"Nowe rekordy w bazie: {inserted}")
        else:
            print("Brak ogloszen do zapisania.")

    finally:
        driver.quit()

    print("Scraping zakonczony.")


if __name__ == "__main__":
    main()
