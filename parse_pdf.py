#!/usr/bin/env python3
"""Извлекает из PDF памятки разделы «Стоматология» и «Экстренная стационарная
помощь» и добавляет их в data/clinics.csv.

Зачем: в текстовой вставке пользователя был только амбулаторный раздел,
а в PDF (полная памятка) есть ещё отдельные таблицы стоматологических клиник
(стр. 36–48) и стационаров экстренной помощи (стр. 48–54).

Сам PDF в git не попадает (*.pdf в .gitignore) — в репозитории остаются
только разобранные данные.

Использование:
    python3 parse_pdf.py "Алиев А..pdf"
    python3 parse_pdf.py --dry-run   # показать, что извлечётся, без записи
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from pathlib import Path

from pypdf import PdfReader

import parse_clinics as pc

ROOT = Path(__file__).parent
CSV_FILE = ROOT / "data" / "clinics.csv"
ADDRESSES_FILE = ROOT / "addresses.txt"

# Страницы разделов (1-based). Таблицы начинаются внизу предыдущей страницы,
# поэтому берём их целиком и отфильтровываем записи по услуге.
STOMAT_PAGES = range(35, 49)
URGENT_PAGES = range(48, 55)

URGENT_SERVICE = "Экстренная стационарная помощь"


def extract_section(
    pdf_path: Path,
    pages: range,
    keep: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> list[dict]:
    reader = PdfReader(str(pdf_path))
    text = "\n".join(
        (reader.pages[i].extract_text() or "")
        for i in range(len(reader.pages))
        if i + 1 in pages
    )
    marker = text.find("Условия")
    if marker != -1:
        text = text[marker:]

    cleaned = pc.preprocess(text.splitlines())
    entries = pc.parse(cleaned)

    rows = []
    for e in entries:
        services = pc.group_services(e["services"])
        joined = "; ".join(services)
        if any(k in joined for k in keep) and not any(x in joined for x in exclude):
            rows.append({
                "address": pc.clean_address(pc.normalize(" ".join(e["address"]))),
                "clinic": pc.normalize(" ".join(e["clinic"])),
                "phone": pc.normalize(" ".join(e["phone"])),
                "hours": pc.normalize_hours(e["hours"]),
                "services": joined,
            })
    return rows


def merge_urgent(rows: list[dict], urgent: list[dict]) -> list[dict]:
    """Добавляет экстренную помощь.

    Если стационар уже есть на карте (тот же адрес) — просто дописываем ему
    услугу «Экстренная стационарная помощь». Если адрес новый — добавляем
    запись целиком.
    """
    merged = list(rows)
    seen = {r["address"] for r in merged}
    for u in urgent:
        if u["address"] in seen:
            for r in merged:
                if r["address"] == u["address"]:
                    if URGENT_SERVICE not in r["services"]:
                        r["services"] = (r["services"] + "; " + URGENT_SERVICE).strip("; ")
                    break
        else:
            u["services"] = URGENT_SERVICE
            merged.append(u)
            seen.add(u["address"])
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", default=None, help="путь к PDF памятки")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать результат без записи в CSV")
    args = parser.parse_args(argv)

    if args.pdf:
        pdf_path = Path(args.pdf)
    else:
        found = glob.glob(str(ROOT / "*.pdf"))
        if not found:
            print("Не найден PDF рядом со скриптом. Укажите путь: python3 parse_pdf.py <файл.pdf>",
                  file=sys.stderr)
            return 1
        pdf_path = Path(found[0])

    stomat = extract_section(pdf_path, STOMAT_PAGES, ("Стоматологическая",), ("Амбулаторно",))
    urgent = extract_section(pdf_path, URGENT_PAGES, ("Экстренная",))
    print(f"Стоматология: {len(stomat)}, экстренная помощь: {len(urgent)}")
    for r in stomat[:3]:
        print(f"  стомат: {r['address']} | {r['clinic']}")
    for r in urgent[:3]:
        print(f"  экстр.:  {r['address']} | {r['clinic']}")

    if args.dry_run:
        return 0

    ambulatory: list[dict] = []
    if CSV_FILE.exists():
        with CSV_FILE.open(encoding="utf-8", newline="") as f:
            ambulatory = list(csv.DictReader(f))
    print(f"Уже есть в CSV (амбулаторные): {len(ambulatory)}")

    # Пересборка: амбулаторные записи остаются как были, стоматология и
    # экстренная помощь — свежие. Так повторные запуски не плодят дубли.
    ambulatory = [r for r in ambulatory if "Амбулаторно" in r["services"]]
    merged = merge_urgent(ambulatory + stomat, urgent)
    print(f"Амбулаторных: {len(ambulatory)}, стоматология: {len(stomat)}, "
          f"экстренная: {len(urgent)}, всего после слияния: {len(merged)}")

    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["address", "clinic", "phone", "hours", "services"])
        writer.writeheader()
        writer.writerows(merged)

    ADDRESSES_FILE.write_text(
        "\n".join(r["address"] for r in merged) + "\n", encoding="utf-8"
    )
    print(f"Готово: {len(merged)} записей в {CSV_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
