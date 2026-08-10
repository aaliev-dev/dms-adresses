#!/usr/bin/env python3
"""Ищет официальные сайты клиник через поисковую выдачу и дописывает их
в data/clinics.csv (колонка website).

Бесплатного API «Поиск по организациям» нет, поэтому берём выдачу поисковика:
пробуем Bing, запасной вариант — DuckDuckGo. Результаты фильтруем от
агрегаторов (2ГИС, Продокторов, DocDoc и т.п.) и берём первую подходящую
ссылку.

Использование:
    python3 fetch_websites.py              # искать все недостающие сайты
    python3 fetch_websites.py --limit 5    # быстрая проверка на первых
    python3 fetch_websites.py --dry-run    # показать запросы без поиска

Результат кэшируется в data/websites.json (название клиники -> url),
повторные запуски не тратят лимиты на уже найденное.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).parent
CSV_FILE = ROOT / "data" / "clinics.csv"
SITES_FILE = ROOT / "data" / "websites.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Каталоги-агрегаторы: их сайт нам не нужен
AGGREGATORS = (
    "2gis", "prodoctorov", "docdoc", "napopravku", "medbooking", "zoon",
    "flamp", "yell", "health.mail", "yandex.ru/maps", "yandex.by", "yandex.kz",
    "sberhealth", "instadoc", "vse-zabolevaniya", "lookmedbook", "mamadeti",
    "spr.ru", "medicina.ru", "doctorpiter", "medweb", "medsovet", "sprosivracha",
    "google.com", "google.ru",
)

ORG_PREFIXES = (
    "ФГБУЗ", "ФГАОУ", "ФГБНУ", "ФГБОУ", "ГБУЗ", "ГАУЗ", "ФГБУ", "ООО", "МУЗ",
    "ГКУ", "ЗАО", "ФБУЗ", "ФБУ", "ЧУЗ", "ИММА", "ФГАУ", "АО", "ПК", "ГКБ",
    "Больница", "Медицинское", "Частное", "Общество", "Филиал",
)


def load_sites() -> dict[str, str]:
    if SITES_FILE.exists():
        try:
            return json.loads(SITES_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_sites(sites: dict[str, str]) -> None:
    SITES_FILE.write_text(
        json.dumps(sites, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def search_query(clinic: str) -> str:
    """Человекочитаемое имя для поиска: без юрформы и кавычек."""
    q = clinic.strip()
    for tok in ORG_PREFIXES:
        if q.startswith(tok):
            q = q[len(tok):].strip()
            q = re.sub(
                r"^(МО|Московской области|города Москвы|города Подмосковья)\s*",
                "", q,
            )
            break
    q = re.sub(r"\s*\([^()]*\)", "", q)      # убрать «(АО «ЦБЭЛИС»)»
    q = q.strip('«»"() :,.-')
    q = re.sub(r"\s*ДЗМ\s*$", "", q)  # «…Кончаловского ДЗМ» -> «…Кончаловского»
    return q or clinic.strip()


def yandex_results(query: str) -> list[tuple[str, str]]:
    """Органическая выдача Яндекса. Работает с российского IP; лучше всего
    находит сайты российских клиник."""
    resp = requests.get("https://yandex.ru/search/",
                        params={"text": query}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    out = []
    for m in re.finditer(r'<a\b[^>]*class="[^"]*organic__url[^"]*"[^>]*>', resp.text):
        href = re.search(r'href="([^"]+)"', m.group(0))
        if href:
            url = href.group(1)
            if url.startswith("/"):
                url = "https://yandex.ru" + url
            out.append((url, ""))
    return out


def bing_decode(href: str) -> str:
    """Bing-ссылки-редиректы вида /ck/a?...&u=a1<base64> -> настоящий URL."""
    if "bing.com/ck/" in href:
        m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", href)
        if m:
            try:
                return base64.urlsafe_b64decode(m.group(1) + "==").decode("utf-8", "ignore")
            except Exception:
                pass
    return href


def google_results(query: str) -> list[tuple[str, str]]:
    """Выдача Google: ссылки вида /url?q=<url>. Часто требует обхода капчи,
    поэтому это запасной вариант — основной источник Яндекс."""
    resp = requests.get("https://www.google.com/search",
                        params={"q": query, "hl": "ru"}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    out = []
    for m in re.finditer(r'<a[^>]*href="/url\?q=([^&"]+)', resp.text):
        url = unquote(m.group(1))
        if url.startswith("http"):
            out.append((url, ""))
    return out


def bing_results(query: str) -> list[tuple[str, str]]:
    resp = requests.get("https://www.bing.com/search",
                        params={"q": query}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    out = []
    for m in re.finditer(
        r'<li class="b_algo".*?<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        resp.text, re.S,
    ):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        out.append((bing_decode(href), title.strip()))
    return out


def ddg_results(query: str) -> list[tuple[str, str]]:
    resp = requests.get("https://html.duckduckgo.com/html/",
                        params={"q": query}, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"', resp.text):
        href = m.group(1)
        inner = re.search(r"uddg=([^&]+)", href)
        url = unquote(inner.group(1)) if inner else href
        title_m = re.search(r">([^<]+)</a>", resp.text[m.start():m.start() + 400])
        title = unquote(title_m.group(1)) if title_m else ""
        out.append((url, title.strip()))
    return out


def is_aggregator(url: str) -> bool:
    host = (urlparse(url).netloc + urlparse(url).path).lower()
    return any(a in host for a in AGGREGATORS) or "bing.com" in host


def find_website(clinic: str) -> str:
    """Ищет сайт клиники по поисковой выдаче; возвращает url или ''."""
    human = search_query(clinic)
    queries = [f"{human} официальный сайт", human]
    for query in queries:
        for fetcher in (yandex_results, google_results, bing_results, ddg_results):
            try:
                results = fetcher(query)
            except requests.RequestException:
                results = []
            for url, _ in results:
                if url and not is_aggregator(url) and url.startswith("http"):
                    return url
            time.sleep(0.7)
        time.sleep(0.7)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0,
                        help="обработать только первые N клиник (для проверки)")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, какие имена будем искать, без запросов")
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
        print(f"Всего уникальных клиник для поиска: {len(unique_clinics)}")
        return 0

    sites = load_sites()
    missing = [c for c in unique_clinics if c not in sites]
    print(f"Уже есть сайтов: {len(sites)}, ищем: {len(missing)}")

    for i, name in enumerate(missing, start=1):
        url = find_website(name)
        sites[name] = url
        save_sites(sites)
        print(f"[{i}/{len(missing)}] {url or '-':40} {name[:50]}")
        time.sleep(0.5)  # бережём поисковик от троттлинга

    for r in rows:
        r["website"] = sites.get(r["clinic"], "")
    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["address", "clinic", "phone", "hours", "services", "website"]
        )
        writer.writeheader()
        writer.writerows(rows)

    found = sum(1 for r in rows if r["website"])
    print(f"Готово: сайтов найдено у {found} из {len(rows)} записей")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
