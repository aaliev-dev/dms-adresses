#!/usr/bin/env python3
"""Построение публичной ссылки на Яндекс Карты с коллекцией адресов.

Идея (вариант №1 из обсуждения): официального API, которое создаёт
«коллекцию адресов» внутри аккаунта Яндекс Карт, не существует. Зато есть
официальная URL-схема Яндекс Карт: адреса превращаются в координаты через
HTTP Геокодер, затем собирается ссылка вида

    https://yandex.ru/maps/?ll=долгота,широта&z=масштаб&pt=...~...~...

Такую ссылку можно шарить — она откроется в вебе и в мобильном приложении,
и на ней будут видны все метки. Ключ API нужен только для геокодирования
(адрес -> координаты), для открытия ссылки ключ не требуется.

Использование:
    python3 geocode.py                       # адреса из addresses.txt
    python3 geocode.py my-list.txt           # адреса из своего файла
    python3 geocode.py --style blue          # цвет меток (red по умолчанию)
    python3 geocode.py --dry-run             # показать адреса, не геокодируя
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/"

# Кэш координат: адрес -> [долгота, широта].
# Геокодирование — медленное и с лимитами, поэтому результат сохраняем
# на диск и переиспользуем между запусками (и между geocode.py и build_map.py).
COORDS_FILE = Path(__file__).parent / "data" / "coords.json"

# Коды стилей меток в параметре pt URL-схемы Яндекс Карт.
# Полный список: https://yandex.ru/dev/maps/urlscheme
MARKER_STYLES = {
    "red": "pm2rdl",
    "blue": "pm2blu",
    "green": "pm2gnl",
    "yellow": "pm2ylw",
    "violet": "pm2vlt",
    "orange": "pm2orl",
}


class GeocodeError(RuntimeError):
    """Не удалось превратить адрес в координаты."""


@dataclass
class Point:
    """Одна метка на карте."""

    address: str
    lon: float
    lat: float
    style: str  # код стиля из MARKER_STYLES


def read_env() -> dict[str, str]:
    """Читает переменные из окружения и из файла .env рядом со скриптом.

    Приоритет у переменных окружения; файл .env — для локальной разработки.
    """
    values: dict[str, str] = {}
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip()
    for name, value in os.environ.items():
        if value:
            values[name] = value.strip()
    return values


def load_api_key() -> str:
    """Ключ HTTP Геокодера: YANDEX_GEOCODER_KEY, запасной вариант —
    YANDEX_API_KEY (устаревшее имя)."""
    env = read_env()
    key = env.get("YANDEX_GEOCODER_KEY") or env.get("YANDEX_API_KEY", "")
    if key:
        return key
    raise SystemExit(
        "Не найден ключ геокодера.\n"
        "Задайте YANDEX_GEOCODER_KEY в .env (шаблон — в .env.example)."
    )


def load_js_api_key() -> str:
    """Ключ JavaScript API (для страницы карты): YANDEX_MAPS_JS_KEY.

    Клиентский ключ — он встраивается в HTML и попадает в браузер по задумке
    Яндекса. Если отдельного JS-ключа нет, можно использовать ключ геокодера.
    """
    env = read_env()
    return env.get("YANDEX_MAPS_JS_KEY") or load_api_key()


def geocode(address: str, api_key: str, retries: int = 3) -> tuple[float, float]:
    """Превращает адрес в координаты (долгота, широта) через HTTP Геокодер.

    Запрос возвращает JSON, координаты лежат в первой найденной геообъекте
    как строка "долгота широта" (в поле Point.pos).
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                GEOCODER_URL,
                params={"apikey": api_key, "geocode": address, "format": "json"},
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            members = (
                data.get("response", {})
                .get("GeoObjectCollection", {})
                .get("featureMember", [])
            )
            if not members:
                raise GeocodeError(f"адрес не найден: {address!r}")
            lon, lat = members[0]["GeoObject"]["Point"]["pos"].split()
            return float(lon), float(lat)
        except (requests.RequestException, KeyError, ValueError, GeocodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)  # пауза с ростом между повторами
    raise GeocodeError(f"геокодирование не удалось для {address!r}: {last_error}")


def read_addresses(path: Path) -> list[tuple[str, str | None]]:
    """Читает адреса из файла.

    Формат: по одному адресу на строку. Необязательно через ';' можно
    указать цвет метки: "Москва, Арбат, 1; blue". Комментарии (#) и пустые
    строки игнорируются.
    """
    entries: list[tuple[str, str | None]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ";" in line:
            address, _, color = line.partition(";")
            entries.append((address.strip(), color.strip() or None))
        else:
            entries.append((line, None))
    return entries


def center_and_zoom(points: list[Point]) -> tuple[float, float, int]:
    """Центр — середина bounding box всех точек; зум подбирается так, чтобы
    все метки попали в кадр (с запасом в один уровень масштаба).

    Логика: на зуме z весь мир (360° долготы) укладывается в 256*2^z
    пикселей; по широте масштаб умножается на cos(широты) из-за проекции
    Меркатора. Берём минимальный зум из двух осей.
    """
    lons = [p.lon for p in points]
    lats = [p.lat for p in points]
    center_lon = (min(lons) + max(lons)) / 2
    center_lat = (min(lats) + max(lats)) / 2

    span_lon = max(lons) - min(lons)
    span_lat = max(lats) - min(lats)
    if span_lon == 0 and span_lat == 0:
        return center_lon, center_lat, 16  # одна точка — уровень города

    cos_lat = max(math.cos(math.radians(center_lat)), 0.2)  # защита у полюсов
    zoom_lon = math.log2(360 / span_lon) if span_lon > 0 else 18
    zoom_lat = math.log2(360 / (span_lat * cos_lat)) if span_lat > 0 else 18
    zoom = int(min(zoom_lon, zoom_lat)) - 1  # -1: поля вокруг меток
    zoom = max(3, min(17, zoom))
    return center_lon, center_lat, zoom


def build_link(points: list[Point]) -> str:
    """Собирает публичную ссылку по URL-схеме Яндекс Карт."""
    center_lon, center_lat, zoom = center_and_zoom(points)
    markers = "~".join(f"{p.lon},{p.lat},{p.style}" for p in points)
    return (
        f"https://yandex.ru/maps/?ll={center_lon},{center_lat}"
        f"&z={zoom}&pt={markers}"
    )


def build_links(points: list[Point], max_len: int = 1800) -> list[str]:
    """Разбивает точки на несколько ссылок, если одна не влезает в URL.

    Лимит длины URL в браузерах — ~2000 символов; pt с метками занимает
    ~25 символов на точку, поэтому для десятков адресов нужен чанкинг.
    """
    chunks: list[list[Point]] = []
    current: list[Point] = []
    for p in points:
        if len(build_link(current + [p])) > max_len and current:
            chunks.append(current)
            current = [p]
        else:
            current.append(p)
    if current:
        chunks.append(current)
    return [build_link(c) for c in chunks]


def load_cache() -> dict[str, list[float]]:
    if COORDS_FILE.exists():
        try:
            return json.loads(COORDS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict[str, list[float]]) -> None:
    COORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    COORDS_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def resolve_coordinates(
    addresses: list[str],
    api_key: str,
    cache: dict[str, list[float]] | None = None,
    on_error: callable | None = None,
) -> list[tuple[str, float, float]]:
    """Геокодирует адреса, используя кэш; пропуская те, что не найдены.

    Возвращает список (адрес, долгота, широта) только для успешных адресов.
    """
    cache = load_cache() if cache is None else cache
    result: list[tuple[str, float, float]] = []
    failed: list[str] = []
    for i, address in enumerate(addresses, start=1):
        if address in cache:
            lon, lat = cache[address]
        else:
            print(f"[{i}/{len(addresses)}] {address} ...", file=sys.stderr)
            try:
                lon, lat = geocode(address, api_key)
            except GeocodeError as exc:
                failed.append(address)
                if on_error:
                    on_error(address, exc)
                continue
            cache[address] = [lon, lat]
            save_cache(cache)  # сохраняем после каждого, чтобы не терять прогресс
        result.append((address, lon, lat))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "file",
        nargs="?",
        default="addresses.txt",
        help="файл с адресами (по одному на строку; цвет через ';'), "
        "по умолчанию addresses.txt",
    )
    parser.add_argument(
        "--style",
        default="red",
        choices=sorted(MARKER_STYLES),
        help="цвет меток (по умолчанию red); цвет из файла переопределяет его",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать список адресов, без геокодирования",
    )
    parser.add_argument(
        "--max-url-len",
        type=int,
        default=1800,
        help="максимальная длина одной ссылки, при превышении разбиваем "
        "на несколько (по умолчанию 1800)",
    )
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        parser.error(f"файл не найден: {path}")

    entries = read_addresses(path)
    if not entries:
        parser.error(f"в файле {path} нет ни одного адреса")

    if args.dry_run:
        for address, color in entries:
            print(f"{address}\t{color or '-'}")
        return 0

    api_key = load_api_key()
    default_style = MARKER_STYLES[args.style]

    cache = load_cache()
    points: list[Point] = []
    for index, (address, color) in enumerate(entries, start=1):
        if address in cache:
            lon, lat = cache[address]
        else:
            print(f"[{index}/{len(entries)}] {address} ...", file=sys.stderr)
            try:
                lon, lat = geocode(address, api_key)
            except GeocodeError as exc:
                print(f"  ! пропущено: {exc}", file=sys.stderr)
                continue
            cache[address] = [lon, lat]
            save_cache(cache)
        style = MARKER_STYLES.get(color, default_style) if color else default_style
        points.append(Point(address=address, lon=lon, lat=lat, style=style))

    links = build_links(points, max_len=args.max_url_len)
    if not links:
        parser.error("ни один адрес не удалось геокодировать")
    print(f"\nГотово: {len(points)} из {len(entries)} адресов, ссылок: {len(links)}")
    for i, link in enumerate(links, start=1):
        if len(links) > 1:
            print(f"\nСсылка {i}/{len(links)}:")
        else:
            print("\nПубличная ссылка (откроется в вебе и в приложении Яндекс Карт):")
        print(link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
