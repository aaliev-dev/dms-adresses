#!/usr/bin/env python3
"""Парсит перечень ДМС-клиник из текстового файла в структурированные данные.

Исходник — data/raw-dms-list.txt (текст, извлечённый из памятки
застрахованного). Документ — это распечатка таблицы, в которой поля часто
склеены в одну строку («д.5ООО «МедиАрт» т.:8495...»), адреса и телефоны
переносятся через строки, а между записями встречаются номера страниц.

Формат одной записи:
    АДРЕС (1-2 строки) | КЛИНИКА (1-4 строки) | ТЕЛЕФОН (т.:...) | ЧАСЫ (опц.) | УСЛУГИ (1-3 строки)

Выход:
    data/clinics.csv — полные структурированные данные
    addresses.txt    — адреса по одному на строку (вход для geocode.py)

Личной информации в исходнике нет — только публичные данные о клиниках.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
RAW_FILE = ROOT / "data" / "raw-dms-list.txt"
CSV_FILE = ROOT / "data" / "clinics.csv"
ADDRESSES_FILE = ROOT / "addresses.txt"

# Строка начинает адрес (регион/город в начале строки)
ADDRESS_START = re.compile(r"^(г Москва|Московская обл|Московская область|Москва)[\s,]")

# Строка начинает название клиники (организационно-правовая форма)
CLINIC_TOKENS = (
    "ФГБУЗ", "ФГАОУ", "ФГБНУ", "ФГБОУ", "ГБУЗ", "ГАУЗ", "ФГБУ", "ООО",
    "МУЗ", "ГКУ", "ЗАО", "ФБУЗ", "ФБУ", "ЧУЗ", "ИММА", "ФГАУ", "АО", "ПК",
    "Медицинское", "Частное", "Общество", "Филиал",
)
CLINIC_START = re.compile(
    r"^(ФГБУЗ|ФГАОУ|ФГБНУ|ФГБОУ|ГБУЗ|ГАУЗ|ФГБУ|ООО|МУЗ|ГКУ|ЗАО|ФБУЗ|ФБУ|ЧУЗ|ИММА|ФГАУ|АО|ПК|Медицинское|Частное|Общество|Филиал)(?=[\s«\":]|$)"
)
CLINIC_INLINE = re.compile(
    r"(?:ФГБУЗ|ФГАОУ|ФГБНУ|ФГБОУ|ГБУЗ|ГАУЗ|ФГБУ|ООО|МУЗ|ГКУ|ЗАО|ФБУЗ|ФБУ|ЧУЗ|ИММА|ФГАУ|АО|ПК|Медицинское|Частное|Общество|Филиал)(?=[\s«\":]|$)"
)

PHONE_START = re.compile(r"^т[\s.]*[:.]")
PHONE_ANY = re.compile(r"т[\s.]*:[\s]*[+\d]")
HOURS_START = re.compile(r"^(пн|вт|ср|чт|пт|сб|вс|круглосуточно)")
SERVICE_START = re.compile(r"^(Амбулаторно|Медицинская помощь|Стоматологическая помощь|Запись на приём|-\s*Запись)")
# inline-граница услуги — только если услуга реально приклеена к клинике/телефону;
# полные фразы, чтобы не задевать названия вроде «Стоматологическая клиника»
SERVICE_ANY = re.compile(r"(?:^|\s)(Амбулаторно|Медицинская помощь|Стоматологическая помощь)")

HEADER_MARK = re.compile(r"Условия|Телефон и режим|Адрес клиники|Перечень медицинских")


def preprocess(lines: list[str]) -> list[str]:
    """Убирает служебные строки: шапки таблицы, номера страниц, «Памятка…»."""
    cleaned: list[str] = []
    n = len(lines)
    i = 0
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s == "Памятка Застрахованного":
            i += 1
            continue
        if HEADER_MARK.search(s):
            i += 1
            continue
        if s.isdigit():
            # номер страницы — если следующая значащая строка «Памятка…»
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and lines[j].strip() == "Памятка Застрахованного":
                i += 1
                continue
        cleaned.append(lines[i])
        i += 1
    return cleaned


def split_segments(text: str) -> list[str]:
    """Разбивает строку со склеенными полями на отдельные сегменты.

    Примеры: «д.5ООО «МедиАрт» т.:8495...» -> [адрес, клиника, телефон];
    «ООО «МедиАрт» Амбулаторно-поликлиническое» -> [клиника, услуга].
    """
    t = text.strip()
    segments: list[str] = []
    while t:
        # ищем самую раннюю границу ПОСЛЕ начала строки
        best_pos = None
        for pattern in (PHONE_ANY, SERVICE_ANY):
            m = pattern.search(t)
            if m and m.start() > 0 and (best_pos is None or m.start() < best_pos):
                best_pos = m.start()
        for m in CLINIC_INLINE.finditer(t):
            if m.start() > 0 and (best_pos is None or m.start() < best_pos):
                best_pos = m.start()
        if best_pos is None:
            segments.append(t)
            break
        segments.append(t[:best_pos])
        t = t[best_pos:]
    return [s.strip() for s in segments if s.strip()]


def classify(cur: dict, phase: str, seg: str) -> str:
    """Кладёт сегмент в нужное поле записи, возвращает новую фазу."""
    if PHONE_START.match(seg):
        cur["phone"].append(seg)
        return "phone"
    if HOURS_START.match(seg):
        cur["hours"].append(seg)
        return "hours"
    if SERVICE_START.match(seg):
        cur["services"].append(seg)
        return "service"
    if CLINIC_START.match(seg):
        cur["clinic"].append(seg)
        return "clinic"
    # продолжение текущего поля
    target = {"address": "address", "clinic": "clinic",
              "phone": "phone", "hours": "hours"}.get(phase, "services")
    cur[target].append(seg)
    return phase


def parse(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    cur: dict | None = None
    phase = "address"
    for line in lines:
        for seg in split_segments(line):
            if ADDRESS_START.match(seg):
                if cur is not None:
                    entries.append(cur)
                cur = {"address": [seg], "clinic": [], "phone": [], "hours": [], "services": []}
                phase = "address"
                continue
            if cur is None:
                continue  # мусор до первой записи (заголовок и т.п.)
            # Новая клиника без адреса (например, «(VIP отделение)» той же клиники)
            # — считаем её отдельной записью с тем же адресом.
            if CLINIC_START.match(seg) and phase not in ("address", "clinic"):
                entries.append(cur)
                cur = {"address": list(cur["address"]), "clinic": [seg],
                       "phone": [], "hours": [], "services": []}
                phase = "clinic"
                continue
            phase = classify(cur, phase, seg)
    if cur is not None:
        entries.append(cur)
    return entries


def normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"-\s", "-", text)  # перенос после дефиса: «летно- испытательного»
    return text.strip().strip(",").strip()


def clean_address(address: str) -> str:
    """Убирает осколки названия клиники, приклеенные к концу адреса.

    В PDF-извлечении иногда строка выглядит как
    «...д.29А (м.Беговая,Шелепиха) ООО» — хвост «ООО»/«О» — мусор.
    """
    address = re.sub(r"\)\s*(?:ООО|ОО|О)\s*$", ")", address)
    return address.strip()


def group_services(items: list[str]) -> list[str]:
    """Собирает перенесённые строки услуги обратно в одну.

    «Амбулаторно-поликлиническое» + «обслуживание» -> одна услуга;
    а «Медицинская помощь на дому» остаётся отдельной услугой.
    """
    groups: list[str] = []
    for item in items:
        if SERVICE_START.match(item) or not groups:
            groups.append(item)
        else:
            groups[-1] += " " + item
    return [normalize(g).lstrip("- ").strip() for g in groups]


def normalize_hours(parts: list[str]) -> str:
    """Склеивает переносы в часах: «сб 09: 00-14:00» -> «сб 09:00-14:00»."""
    joined = " ".join(parts)
    joined = re.sub(r"([:-])\s+", r"\1", joined)
    return normalize(joined)


def main() -> int:
    if not RAW_FILE.exists():
        print(f"Не найден исходник: {RAW_FILE}", file=sys.stderr)
        return 1

    lines = RAW_FILE.read_text(encoding="utf-8").splitlines()
    cleaned = preprocess(lines)
    entries = parse(cleaned)

    rows = []
    problems = []
    for idx, e in enumerate(entries, start=1):
        address = clean_address(normalize(" ".join(e["address"])))
        clinic = normalize(" ".join(e["clinic"]))
        phone = normalize(" ".join(e["phone"]))
        hours = normalize_hours(e["hours"])
        services = "; ".join(group_services(e["services"]))
        if not clinic:
            problems.append(f"{idx}: нет названия клиники -> {address}")
        if len(address) < 10:
            problems.append(f"{idx}: подозрительный адрес -> {address}")
        rows.append({
            "address": address,
            "clinic": clinic,
            "phone": phone,
            "hours": hours,
            "services": services,
        })

    # CSV
    with CSV_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["address", "clinic", "phone", "hours", "services"])
        writer.writeheader()
        writer.writerows(rows)

    # addresses.txt — по одному адресу на строку
    ADDRESSES_FILE.write_text(
        "\n".join(r["address"] for r in rows) + "\n", encoding="utf-8"
    )

    print(f"Записей: {len(rows)}")
    print(f"Без телефона: {sum(1 for r in rows if not r['phone'])}")
    print(f"Без часов: {sum(1 for r in rows if not r['hours'])}")
    print(f"CSV: {CSV_FILE}")
    print(f"Адреса: {ADDRESSES_FILE}")
    if problems:
        print("\nПроблемные записи:")
        for p in problems:
            print(" ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
