#!/usr/bin/env python3
"""Извлекает раздел «Стоматологическая помощь» из PDF памятки и добавляет
его в data/clinics.csv.

Зачем: в текстовой вставке пользователя был только амбулаторный раздел,
а в PDF (полная памятка) есть ещё отдельная таблица стоматологических
клиник (~270 записей, стр. 36–48).

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

# Страницы стоматологического раздела (1-based). Таблица начинается внизу
# стр. 35, поэтому берём её целиком и отфильтровываем амбулаторные записи.
STOMAT_PAGES = range(35, 49)


def extract_stomatology(pdf_path: Path) -> list[dict]:
    reader = PdfReader(str(pdf_path))
    text = "\n".join(
        (reader.pages[i].extract_text() or "") for i in range(len(reader.pages))
        if i + 1 in STOMAT_PAGES
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
        if "Стоматологическая" in joined and "Амбулаторно" not in joined:
            rows.append({
                "address": pc.clean_address(pc.normalize(" ".join(e["address"]))),
                "clinic": pc.normalize(" ".join(e["clinic"])),
                "phone": pc.normalize(" ".join(e["phone"])),
                "hours": pc.normalize_hours(e["hours"]),
                "services": joined,
            })
    return rows


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

    stomat = extract_stomatology(pdf_path)
    print(f"Извлечено стоматологических записей: {len(stomat)}")
    for r in stomat[:5]:
        print(f"  * {r['address']} | {r['clinic']}")

    if args.dry_run:
        return 0

    ambulatory: list[dict] = []
    if CSV_FILE.exists():
        with CSV_FILE.open(encoding="utf-8", newline="") as f:
            ambulatory = list(csv.DictReader(f))
    print(f"Уже есть в CSV (амбулаторные): {len(ambulatory)}")

    # Пересборка: амбулаторные записи остаются как были, стоматология — свежая.
    # Так повторные запуски не плодят дубли и подхватывают правки парсера.
    ambulatory = [r for r in ambulatory if "Амбулаторно" in r["services"]]
    merged = ambulatory + stomat
    print(f"Амбулаторных: {len(ambulatory)}, стоматология: {len(stomat)}, всего: {len(merged)}")

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
