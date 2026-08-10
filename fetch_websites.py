#!/usr/bin/env python3
"""Ищет официальные сайты клиник через «Поиск по организациям» Яндекса
и дописывает их в data/clinics.csv (колонка website).

В памятке сайтов нет (там только sovcomins.ru — сайт страховой), поэтому
берём их из каталога организаций Яндекса по названию клиники.

Нужен ключ API с продуктом «Поиск по организациям» — положите его в .env
как YANDEX_ORG_KEY. Как и геокодер, API работает с российского IP.

Использование:
    python3 fetch_websites.py              # искать все недостающие сайты
    python3 fetch_websites.py --limit 5    # быстрая проверка на первых
    python3 fetch_websites.py --dry-run    # показать, что будем искать

Результат кэшируется в data/websites.json (название клиники -> url),
повторные запуски не тратят лимиты на уже найденное.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

import geocode

ROOT = Path(__file__).parent
CSV_FILE = ROOT / "data" / "clinics.csv"
SITES_FILE = ROOT / "data" / "websites.json"

ORG_SEARCH_URL = "https://search-maps.yandex.ru/v1/"

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
    """Человекочитаемое имя для поиска: без юрформы и кавычек.

    «ГБУЗ МО «Красногорская больница»» -> «Красногорская больница».
    """
    q = clinic.strip()
    for tok in ORG_PREFIXES:
        if q.startswith(tok):
            q = q[len(tok):].strip()
            q = re.sub(
                r"^(МО|Московской области|города Москвы|города Подмосковья)\s*",
                "", q,
            )
            break
    q = q.strip("«»\"() :,.-")
    return q or clinic.strip()


def find_website(key: str, clinic: str) -> str:
    """Ищет сайт организации; возвращает url или пустую строку."""
    queries = [search_query(clinic), clinic]
    seen: set[str] = set()
    for q in queries:
        if q in seen:
            continue
        seen.add(q)
        try:
            resp = requests.get(
                ORG_SEARCH_URL,
                params={"apikey": key, "text": q, "type": "biz",
                        "lang": "ru_RU", "results": 5},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            for feature in data.get("features", []):
                meta = feature.get("properties", {}).get("CompanyMetaData", {})
                url = (meta.get("url") or "").strip()
                if url:
                    return url
        except requests.RequestException:
            continue
        time.sleep(0.3)
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

    # уникальные названия клиник в порядке появления
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

    if missing:
        key = ""
        try:
            key = geocode.read_env().get("YANDEX_ORG_KEY", "")
        except Exception:
            key = ""
        if not key:
            print(
                "Не задан YANDEX_ORG_KEY (ключ «Поиск по организациям»). "
                "Добавьте его в .env — см. .env.example.",
                file=sys.stderr,
            )
            return 1

        for i, name in enumerate(missing, start=1):
            url = find_website(key, name)
            sites[name] = url
            save_sites(sites)  # сохраняем после каждого, чтобы не терять прогресс
            print(f"[{i}/{len(missing)}] {url or '-':35} {name[:50]}")

    # дописываем сайты в CSV
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
