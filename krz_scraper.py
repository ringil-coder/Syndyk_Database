"""
Skrypt scrapujący obwieszczenia dotyczące masy upadłości
ze strony https://krz.ms.gov.pl/

Wymagania:
    pip install selenium openpyxl webdriver-manager

Uwaga:
    Strona https://krz.ms.gov.pl/ jest aplikacją Angular, więc do pobrania
    danych konieczny jest Selenium (sama biblioteka requests nie wystarczy).
    Selektory CSS zawierają dynamiczne identyfikatory Angulara (ng-tns-c15-XX),
    dlatego w miarę możliwości używamy zapytań niezależnych od tych numerów.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


BASE_URL = "https://krz.ms.gov.pl/"
OUTPUT_FILE = Path(__file__).parent / "obwieszczenia_masa_upadlosci.xlsx"
DEFAULT_WAIT = 20


def build_driver(headless: bool = True) -> webdriver.Chrome:
    """Tworzy sterownik Chrome."""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=pl-PL")
    return webdriver.Chrome(options=opts)


def safe_click(driver, element) -> None:
    """Klika element; jeśli jest przesłonięty, używa JS."""
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def set_date_range_last_month(driver) -> None:
    """Ustawia zakres dat: ostatni miesiąc -> dziś."""
    wait = WebDriverWait(driver, DEFAULT_WAIT)

    today = date.today()
    month_ago = today - timedelta(days=30)
    fmt = "%d-%m-%Y"

    # Inputy dat (są wewnątrz panelu "Zakres dat publikacji") — pierwsze dwa
    # inputy typu text w panelu to data od i data do.
    date_inputs = wait.until(
        EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "p-calendar input.ui-inputtext")
        )
    )
    if len(date_inputs) < 2:
        raise RuntimeError("Nie znaleziono pól zakresu dat.")

    for inp, value in zip(date_inputs[:2], (month_ago.strftime(fmt), today.strftime(fmt))):
        inp.click()
        inp.send_keys(Keys.CONTROL, "a")
        inp.send_keys(Keys.DELETE)
        inp.send_keys(value)
        inp.send_keys(Keys.ESCAPE)
        time.sleep(0.3)


def expand_panel_by_header(driver, header_text: str) -> None:
    """Rozwija panel p-panel po tekście nagłówka (jeśli nie jest rozwinięty)."""
    header = driver.find_element(
        By.XPATH,
        f"//p-panel//span[contains(@class,'ui-panel-title') and "
        f"contains(normalize-space(.), \"{header_text}\")]",
    )
    toggler = header.find_element(
        By.XPATH,
        "./ancestor::div[contains(@class,'ui-panel-titlebar')]"
        "//a[contains(@class,'ui-panel-titlebar-icon')]",
    )
    # rozwiń tylko jeżeli jest zwinięty (ikona ma "ui-icon-plusthick")
    cls = toggler.find_element(By.TAG_NAME, "span").get_attribute("class") or ""
    if "plus" in cls:
        safe_click(driver, toggler)
        time.sleep(0.5)


def collapse_panel_by_header(driver, header_text: str) -> None:
    """Zwija panel p-panel po tekście nagłówka (jeśli jest rozwinięty)."""
    header = driver.find_element(
        By.XPATH,
        f"//p-panel//span[contains(@class,'ui-panel-title') and "
        f"contains(normalize-space(.), \"{header_text}\")]",
    )
    toggler = header.find_element(
        By.XPATH,
        "./ancestor::div[contains(@class,'ui-panel-titlebar')]"
        "//a[contains(@class,'ui-panel-titlebar-icon')]",
    )
    cls = toggler.find_element(By.TAG_NAME, "span").get_attribute("class") or ""
    if "minus" in cls:
        safe_click(driver, toggler)
        time.sleep(0.5)


def scrape() -> list[list]:
    driver = build_driver(headless=True)
    try:
        wait = WebDriverWait(driver, DEFAULT_WAIT)

        # 1) Strona główna
        driver.get(BASE_URL)

        # 2) Menu -> "Tablica obwieszczeń"
        menu_item = wait.until(
            EC.element_to_be_clickable((By.XPATH, '//*[@id="item-4"]/div'))
        )
        safe_click(driver, menu_item)

        # Czekaj na formularz wyszukiwania
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "app-wyszukiwanie-obwieszczen-view")
            )
        )
        time.sleep(2)

        # 3) Zakres dat: ostatni miesiąc
        set_date_range_last_month(driver)

        # 4) Rozwiń dodatkowe parametry
        extra = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "div.dodatkoweParametry")
            )
        )
        safe_click(driver, extra)
        time.sleep(1)

        # 5-7) Zwiń / odkliknij trzy podpanele kategorii (odznacz)
        #    (tu: "odklikięcie" = zwinięcie / wyczyszczenie — zachowujemy
        #    domyślne zaznaczenia, zwijając panele aby nie zaznaczać
        #    dodatkowych kategorii)
        for header in (
            "Postępowania restrukturyzacyjne",
            "Postępowania upadłościowe",
            "Postępowania w przedmiocie ogłoszenia upadłości",
        ):
            try:
                collapse_panel_by_header(driver, header)
            except Exception:
                pass

        # 8) W kategoriach obwieszczeń wybierz TYLKO pozycję dotyczącą
        #    obwieszczeń o masie upadłości (9. checkbox w panelu kategorii
        #    obwieszczeń).
        category_panel = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "[id^='ui-panel-'][id$='-content']")
            )
        )
        # Najpierw odznacz wszystko w panelu kategorii obwieszczeń,
        # potem zaznacz 9. pozycję.
        checkboxes = driver.find_elements(
            By.CSS_SELECTOR,
            "app-wyszukiwanie-obwieszczen-view p-checkbox .ui-chkbox-box",
        )
        # odznacz zaznaczone
        for cb in checkboxes:
            cls = cb.get_attribute("class") or ""
            if "ui-state-active" in cls:
                safe_click(driver, cb)
                time.sleep(0.05)

        # zaznacz pozycję "Obwieszczenie o ustaleniu składu masy upadłości"
        target = None
        for cb in driver.find_elements(
            By.XPATH,
            "//label[contains(translate(., 'MASY UPADŁOŚCI', 'masy upadłości'),"
            " 'masy upadłości')]",
        ):
            target = cb
            break
        if target is not None:
            safe_click(driver, target)
        else:
            # fallback — 9. checkbox w ostatnim panelu
            if len(checkboxes) >= 9:
                safe_click(driver, checkboxes[8])
        time.sleep(0.5)

        # 9) Kliknij przycisk "Szukaj"
        search_btn = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(@class,'primary') and "
                    ".//span[contains(normalize-space(.),'Szukaj')]]",
                )
            )
        )
        safe_click(driver, search_btn)

        # 10) Rozwiń panel z wynikami
        time.sleep(2)
        results_panel_title = wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//p-panel//span[contains(@class,'ui-panel-title') and "
                    "contains(normalize-space(.),'Wyniki')]",
                )
            )
        )
        toggler = results_panel_title.find_element(
            By.XPATH,
            "./ancestor::div[contains(@class,'ui-panel-titlebar')]"
            "//a[contains(@class,'ui-panel-titlebar-icon')]",
        )
        icon_cls = toggler.find_element(By.TAG_NAME, "span").get_attribute("class") or ""
        if "plus" in icon_cls:
            safe_click(driver, toggler)
        time.sleep(2)

        # 11) Zbierz tabelę wyników
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "p-table table")
            )
        )
        time.sleep(1)

        rows_data: list[list] = []
        # Nagłówki
        header_cells = driver.find_elements(
            By.CSS_SELECTOR, "p-table table thead th"
        )
        headers = [th.text.strip() for th in header_cells] or [
            "Lp.", "Data publikacji", "Sygnatura", "Kategoria",
            "Podmiot", "Szczegóły",
        ]

        # Iteracja po wszystkich stronach paginacji
        seen_page_signatures: set[str] = set()
        while True:
            body_rows = driver.find_elements(
                By.CSS_SELECTOR, "p-table table tbody tr"
            )
            page_signature = "|".join(r.text for r in body_rows)
            if page_signature in seen_page_signatures:
                break
            seen_page_signatures.add(page_signature)

            for tr in body_rows:
                cells = tr.find_elements(By.TAG_NAME, "td")
                row = []
                for td in cells:
                    links = td.find_elements(By.TAG_NAME, "a")
                    if links:
                        href = links[0].get_attribute("href") or ""
                        label = links[0].text.strip() or td.text.strip() or href
                        row.append({"text": label, "href": href})
                    else:
                        row.append(td.text.strip())
                rows_data.append(row)

            # Następna strona
            next_btns = driver.find_elements(
                By.CSS_SELECTOR,
                "p-paginator .ui-paginator-next:not(.ui-state-disabled)",
            )
            if not next_btns:
                break
            safe_click(driver, next_btns[0])
            time.sleep(1.5)

        return [headers, *rows_data]
    finally:
        driver.quit()


def save_to_excel(data: list[list], path: Path) -> None:
    if not data:
        print("Brak danych do zapisania.")
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "Obwieszczenia"

    headers = data[0]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    link_font = Font(color="0563C1", underline="single")
    for row in data[1:]:
        ws.append([
            cell["text"] if isinstance(cell, dict) else cell for cell in row
        ])
        r = ws.max_row
        for col_idx, cell in enumerate(row, start=1):
            if isinstance(cell, dict) and cell.get("href"):
                xcell = ws.cell(row=r, column=col_idx)
                xcell.hyperlink = cell["href"]
                xcell.font = link_font

    for col_idx, _ in enumerate(headers, start=1):
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        max_len = max(
            (len(str(ws.cell(row=i, column=col_idx).value or ""))
             for i in range(1, ws.max_row + 1)),
            default=10,
        )
        ws.column_dimensions[col_letter].width = min(max_len + 2, 60)

    wb.save(path)
    print(f"Zapisano {ws.max_row - 1} wierszy do {path}")


def main() -> None:
    data = scrape()
    save_to_excel(data, OUTPUT_FILE)


if __name__ == "__main__":
    main()
