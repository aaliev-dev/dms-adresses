#!/usr/bin/env python3
"""Ищет сайты и рейтинги клиник и дописывает их в data/clinics.csv.

Два источника, оба через настоящий Chromium (Playwright, видимый режим —
иначе поисковики отдают капчу):

1. Сайт: выдача DuckDuckGo html (официальный сайт клиники).
2. Рейтинг: Яндекс.Карты (?text=<название>) — первая организация в выдаче,
   рейтинг вида «4,8».

Результаты кэшируются в data/websites.json и data/ratings.json — повторные
запуски не тратят время на уже найденное.

Использование:
    python3 fetch_websites.py              # все клиники
    python3 fetch_websites.py --limit 5    # первые N (проверка)
    python3 fetch_websites.py --dry-run    # показать запросы
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
CSV_FILE = ROOT / "data" / "clinics.csv"
SITES_FILE = ROOT / "data" / "websites.json"
RATINGS_FILE = ROOT / "data" / "ratings.json"

ORG_PREFIXES = (
    "ФГБУЗ", "ФГАОУ", "ФГБНУ", "ФГБОУ", "ГБУЗ", "ГАУЗ", "ФГБУ", "ООО", "МУЗ",
    "ГКУ", "ЗАО", "ФБУЗ", "ФБУ", "ЧУЗ", "ИММА", "ФГАУ", "АО", "ПК", "ГКБ",
    "Больница", "Медицинское", "Частное", "Общество", "Филиал",
)

AGGREGATORS = (
    "2gis", "prodoctorov", "docdoc", "napopravku", "medbooking", "zoon",
    "flamp", "yell", "health.mail", "yandex.ru/maps", "yandex.by", "yandex.kz",
    "sberhealth", "instadoc", "vse-zabolevaniya", "lookmedbook", "mamadeti",
    "spr.ru", "medicina.ru", "doctorpiter", "medweb", "medsovet", "sprosivracha",
    "google.com", "google.ru", "duckduckgo.com",
)

CHROME_ARGS = ["--disable-blink-features=AutomationControlled", "--lang=ru-RU"]


def load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def search_query(clinic: str) -> str:
    """Человекочитаемое имя для поиска: без юрформы, кавычек и хвостов."""
    q = clinic.strip()
    for tok in ORG_PREFIXES:
        if q.startswith(tok):
            q = q[len(tok):].strip()
            q = re.sub(r"^(МО|Московской области|города Москвы|города Подмосковья)\s*", "", q)
            break
    q = re.sub(r"\s*\([^()]*\)", "", q)
    q = q.strip('«»"() :,.-')
    q = re.sub(r"\s*ДЗМ\s*$", "", q)
    return q or clinic.strip()


def is_aggregator(url: str) -> bool:
    try:
        host = (urlparse(url).netloc + urlparse(url).path).lower()
    except Exception:
        return True
    return any(a in host for a in AGGREGATORS)


def find_website(page, human: str) -> str:
    """Сайт клиники через DDG html."""
    q = f"{human} официальный сайт"
    try:
        page.goto("https://html.duckduckgo.com/html/?q=" + q.replace(" ", "+"), timeout=35000)
        page.wait_for_timeout(2500)
        n = page.locator("a.result__a").count()
        for i in range(min(n, 8)):
            href = page.locator("a.result__a").nth(i).get_attribute("href") or ""
            m = re.search(r"uddg=([^&]+)", href)
            url = unquote(m.group(1)) if m else href
            if url.startswith("http") and not is_aggregator(url):
                return url
    except Exception:
        pass
    return ""


def find_rating(page, human: str) -> float | None:
    """Рейтинг клиники из Яндекс.Карт: первая организация в выдаче."""
    try:
        page.goto("https://yandex.ru/maps/?text=" + human.replace(" ", "+"), timeout=40000)
        page.wait_for_timeout(6000)
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        # ищем рейтинг первой карточки: имя организации, затем «4,8»
        pos = body.find(human)
        if pos == -1:
            # поиск по первым 25 символам имени
            pos = body.find(human[:25]) if len(human) > 25 else -1
        window_start = max(0, pos) if pos != -1 else 0
        window = body[window_start: window_start + 200]
        m = re.search(r"([1-5][.,]\d)", window)
        if m:
            return float(m.group(1).replace(",", "."))
        # если имя не нашли — первый рейтинг в тексте вообще
        m2 = re.search(r"([1-5][.,]\d)", body[:600])
        if m2:
            return float(m2.group(1).replace(",", "."))
    except Exception:
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="только первые N клиник")
    parser.add_argument("--dry-run", action="store_true", help="показать запросы без поиска")
    args = parser.parse_args(argv)

    if not CSV_FILE.exists():
        print(f"Не найден {CSV_FILE}. Сначала запустите parse_clinics.py", file=sys.stderr)
        return 1

    with CSV_FILE.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    unique_clinics = list(dict.fromkeys(r["clinic"] for r in rows if r["clinic"]))
    if args.limit:
        unique_clinics = unique_clinics[: args.limit]

    if args.dry_run:
        for name in unique_clinics[:10]:
            print(f"  {name!r:60} -> {search_query(name)!r}")
        print(f"Всего уникальных клиник: {len(unique_clinics)}")
        return 0

    sites = load_json(SITES_FILE)
    ratings = load_json(RATINGS_FILE)
    todo = [c for c in unique_clinics if c not in sites or c not in ratings]
    print(f"Сайтов в кэше: {len(sites)}, рейтингов: {len(ratings)}, осталось: {len(todo)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=CHROME_ARGS)
        ctx = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        for i, name in enumerate(todo, start=1):
            human = search_query(name)
            if name not in sites:
                sites[name] = find_website(page, human)
                save_json(SITES_FILE, sites)
            if name not in ratings:
                ratings[name] = find_rating(page, human)
                save_json(RATINGS_FILE, ratings)
            print(f"[{i}/{len(todo)}] сайт={sites[name] or '-':32} рейтинг={ratings[name]} {name[:40]}")

        browser.close()

    for r in rows:
        r["website"] = sites.get(r["clinic"], "")
        r["rating"] = ratings.get(r["clinic"], "")
    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["address", "clinic", "phone", "hours", "services", "website", "rating"])
        writer.writeheader()
        writer.writerows(rows)

    found_sites = sum(1 for r in rows if r["website"])
    found_ratings = sum(1 for r in rows if r["rating"])
    print(f"Готово: сайтов у {found_sites}, рейтингов у {found_ratings} из {len(rows)} записей")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
