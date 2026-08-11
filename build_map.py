#!/usr/bin/env python3
"""Собирает статичную карту клиник для GitHub Pages.

Конвейер:
    1. Читает data/clinics.csv (разобранный перечень ДМС-клиник).
    2. Геокодирует адреса через HTTP Геокодер (с кэшем из data/coords.json).
    3. Пишет в docs/:
         - clinics.json — данные клиник с координатами (для страницы);
         - index.html  — карта на Яндекс JS API: кластеризация, список
                         слева, поиск, фильтр по услугам, балуны с
                         телефоном/часами/услугами.

Карта открывается по адресу https://<user>.github.io/<repo>/ — достаточно
включить GitHub Pages для ветки main (папка /docs).

Ключ для JS API берётся из .env: YANDEX_MAPS_JS_KEY (можно использовать
тот же ключ, что и для геокодера). Ключ JS API — клиентский, он публикуется
в браузере по задумке Яндекс.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import geocode

ROOT = Path(__file__).parent
CSV_FILE = ROOT / "data" / "clinics.csv"
DOCS = ROOT / "docs"
CLINICS_JSON = DOCS / "clinics.json"
INDEX_HTML = DOCS / "index.html"
CLEANING_FILE = ROOT / "data" / "cleaning.txt"
PERMALINKS_FILE = ROOT / "data" / "permalinks.json"


def read_permalinks() -> dict[str, str]:
    if not PERMALINKS_FILE.exists():
        return {}
    try:
        import json as _json
        return _json.loads(PERMALINKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_cleaning() -> set[str]:
    """Адреса клиник с чисткой зубов по ДМС (точные адреса из CSV)."""
    if not CLEANING_FILE.exists():
        return set()
    out = set()
    for line in CLEANING_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def read_clinics() -> list[dict]:
    with CSV_FILE.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def digits_only(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=0,
        help="геокодировать только первые N адресов (для быстрой проверки)",
    )
    parser.add_argument(
        "--cache-only", action="store_true",
        help="не геокодировать, использовать только уже сохранённый кэш "
        "(удобно для тестов и для ручной правки координат)",
    )
    args = parser.parse_args(argv)

    if not CSV_FILE.exists():
        print(f"Не найден {CSV_FILE}. Сначала запустите parse_clinics.py", file=sys.stderr)
        return 1

    rows = read_clinics()
    cleaning = read_cleaning()
    permalinks = read_permalinks()
    # Геокодируем каждый уникальный адрес один раз
    unique_addresses = list(dict.fromkeys(r["address"] for r in rows))
    if args.limit:
        unique_addresses = unique_addresses[: args.limit]

    cache = geocode.load_cache()
    coords: dict[str, tuple[float, float]] = {}
    failed: list[str] = []

    for address in unique_addresses:
        if address in cache:
            lon, lat = cache[address]
            coords[address] = (lon, lat)
        elif not args.cache_only:
            print(f"{address} ...", file=sys.stderr)
            try:
                lon, lat = geocode.geocode(address, geocode.load_api_key())
            except geocode.GeocodeError as exc:
                failed.append(address)
                print(f"  ! пропущено: {exc}", file=sys.stderr)
                continue
            cache[address] = [lon, lat]
            geocode.save_cache(cache)
            coords[address] = (lon, lat)
        else:
            print(f"  ! нет в кэше (--cache-only): {address}", file=sys.stderr)

    clinics = []
    for i, r in enumerate(rows, start=1):
        if r["address"] not in coords:
            continue  # адрес не нашёлся — без метки, но остаётся в CSV
        lon, lat = coords[r["address"]]
        # телефоны: список для кликабельных ссылок
        phones = [p.strip() for p in re.split(r"[;,]", r["phone"]) if p.strip()]
        clinics.append({
            "id": i,
            "address": r["address"],
            "clinic": r["clinic"],
            "lat": lat,
            "lon": lon,
            "phones": phones,
            "hours": r["hours"],
            "services": r["services"],
            "website": r.get("website", "").strip(),
            "rating": r.get("rating", "").strip(),
            "cleaning": r["address"] in cleaning,
            "ymaps": permalinks.get(r["clinic"], ""),
        })

    DOCS.mkdir(parents=True, exist_ok=True)
    CLINICS_JSON.write_text(
        json.dumps(clinics, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    api_key = geocode.load_js_api_key()
    # Безопасная вставка JSON в <script>: экранируем "</"
    data_json = json.dumps(clinics, ensure_ascii=False).replace("</", "<\\/")
    index_html = render_page(data_json, api_key)
    INDEX_HTML.write_text(index_html, encoding="utf-8")

    print(f"Клиник с метками: {len(clinics)} из {len(rows)}")
    if failed:
        print(f"Не геокодировано ({len(failed)}):")
        for a in failed:
            print("  -", a)
    print(f"Данные: {CLINICS_JSON}")
    print(f"Карта:  {INDEX_HTML}")
    return 0


def render_page(data_json: str, api_key: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Клиники по ДМС Совкомбанка</title>
<style>
  :root {{
    --accent: #2a6df4;
    --bg: #f4f6fa;
    --panel: #ffffff;
    --border: #e2e6ee;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }}
  #app {{ display: flex; height: 100vh; }}

  /* Панель слева */
  #sidebar {{
    width: 360px; min-width: 360px; background: var(--panel);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
  }}
  #sidebar header {{ padding: 14px 16px; border-bottom: 1px solid var(--border); }}
  #sidebar h1 {{ margin: 0 0 4px; font-size: 17px; }}
  #sidebar .sub {{ color: #667; font-size: 12.5px; }}
  #search {{ margin: 10px 16px 6px; padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; }}
  #filters {{ display: flex; gap: 6px; padding: 0 16px 10px; flex-wrap: wrap; }}
  .filter-dot {{
    display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    margin-right: 5px; vertical-align: -1px;
  }}
  #filters2 {{ padding: 0 16px 10px; }}
  #ratingFilter {{
    width: 100%; padding: 7px 10px; border: 1px solid var(--border);
    border-radius: 8px; font-size: 13px; background: #fff; color: #223;
  }}
  #filters button {{
    border: 1px solid var(--border); background: #fff; border-radius: 20px;
    padding: 5px 12px; font-size: 12.5px; cursor: pointer; color: #445;
  }}
  #filters button.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
  #list {{ flex: 1; overflow-y: auto; padding: 4px 8px 16px; }}
  .item {{
    padding: 10px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent;
  }}
  .item:hover, .item.active {{ background: #eef3ff; border-color: var(--accent); }}
  .item .name {{ font-size: 13.5px; font-weight: 600; color: #1a1d24; }}
  .item .addr {{ font-size: 12px; color: #556; margin-top: 2px; }}
  .item .meta {{ font-size: 11.5px; color: #889; margin-top: 3px; }}
  .item.hidden {{ display: none; }}
  #count {{ padding: 0 16px 10px; font-size: 12px; color: #889; }}

  /* Карта */
  #map {{ flex: 1; position: relative; }}
  #mapContainer {{ width: 100%; height: 100%; }}

  /* Кнопка-переключатель панели на узких экранах */
  #toggle {{ display: none; position: absolute; z-index: 1000; top: 10px; left: 10px;
    padding: 8px 12px; border: none; border-radius: 8px; background: var(--accent); color: #fff; cursor: pointer; }}

  @media (max-width: 760px) {{
    #sidebar {{ position: absolute; z-index: 900; height: 100%; transform: translateX(-100%);
      transition: transform .2s; box-shadow: 0 0 20px rgba(0,0,0,.25); }}
    #sidebar.open {{ transform: translateX(0); }}
    #toggle {{ display: block; }}
  }}
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <header>
      <h1>Клиники по ДМС Совкомбанка</h1>
      <div class="sub">Москва и Московская область</div>
    </header>
    <input id="search" type="text" placeholder="Поиск: клиника или адрес…">
    <div id="filters">
      <button data-filter="all" class="active">Все</button>
      <button data-filter="amb"><span class="filter-dot" style="background:#1d98ff"></span>Амбулаторные</button>
      <button data-filter="home"><span class="filter-dot" style="background:#31b53f"></span>На дому</button>
      <button data-filter="stomat"><span class="filter-dot" style="background:#8b4cff"></span>Стоматология</button>
      <button data-filter="urgent"><span class="filter-dot" style="background:#e64848"></span>Экстренные</button>
      <button data-filter="cleaning"><span class="filter-dot" style="background:#ffb400"></span>🦷 Чистка зубов</button>
    </div>
    <div id="filters2">
      <select id="ratingFilter" aria-label="Фильтр по рейтингу">
        <option value="0">Любой рейтинг</option>
        <option value="4.5">⭐ от 4.5</option>
        <option value="4.0">⭐ от 4.0</option>
      </select>
    </div>
    <div id="count"></div>
    <div id="list"></div>
  </div>
  <div id="map">
    <button id="toggle">☰ Список</button>
    <div id="mapContainer"></div>
  </div>
</div>

<script src="https://api-maps.yandex.ru/2.1/?apikey={api_key}&lang=ru_RU"></script>
<script>
const CLINICS = {data_json};

const map = document.getElementById('mapContainer');
const listEl = document.getElementById('list');
const searchEl = document.getElementById('search');
const countEl = document.getElementById('count');
const sidebar = document.getElementById('sidebar');
const ratingFilter = document.getElementById('ratingFilter');
document.getElementById('toggle').addEventListener('click', () => sidebar.classList.toggle('open'));

let activeFilter = 'all';
let mapInstance, clusterer;
const allObjects = [];   // все метки: id + pm + c

function markerColor(c) {{
  if (c.cleaning) return 'islands#goldCircleDotIcon';
  const s = (c.services || '').toLowerCase();
  if (s.includes('экстренн')) return 'islands#redMedicalIcon';
  if (s.includes('стоматолог')) return 'islands#violetMedicalIcon';
  if (s.includes('на дому')) return 'islands#greenMedicalIcon';
  return 'islands#blueMedicalIcon';
}}

function balloonContent(c) {{
  const phones = (c.phones || []).map(p => {{
    const pretty = p.replace(/^т[\\s.:]*/i, '');
    return `<div>☎ <a href="tel:${{pretty.replace(/\\D/g, '')}}">${{pretty}}</a></div>`;
  }}).join('');
  const hours = c.hours ? `<div>🕒 ${{c.hours}}</div>` : '';
  const services = c.services ? `<div>🏥 ${{c.services}}</div>` : '';
  const rating = c.rating ? `<div>⭐ ${{c.rating}}</div>` : '';
  const cleaning = c.cleaning ? `<div>🦷 Чистка зубов по ДМС</div>` : '';
  const website = c.website ? `<div>🌐 <a href="${{c.website}}" target="_blank" rel="noopener">${{c.website}}</a></div>` : '';
  const yalink = c.ymaps || `https://yandex.ru/maps/?text=${{encodeURIComponent(c.clinic + ', ' + c.address)}}`;
  const yandex = `<div>🗺 <a href="${{yalink}}" target="_blank" rel="noopener">Подробнее в Яндекс Картах</a></div>`;
  return `<b>${{c.clinic}}</b><br><div>${{c.address}}</div>${{rating}}${{cleaning}}${{phones}}${{hours}}${{services}}${{website}}${{yandex}}`;
}}

function visibleClinics() {{
  const q = searchEl.value.trim().toLowerCase();
  const minRating = parseFloat(ratingFilter.value) || 0;
  return CLINICS.filter(c => {{
    const text = (c.clinic + ' ' + c.address).toLowerCase();
    const s = (c.services || '').toLowerCase();
    const byFilter =
      activeFilter === 'all' ? true :
      activeFilter === 'amb' ? s.includes('амбулаторно') :
      activeFilter === 'home' ? s.includes('на дому') :
      activeFilter === 'stomat' ? s.includes('стоматолог') :
      activeFilter === 'urgent' ? s.includes('экстренн') :
      activeFilter === 'cleaning' ? c.cleaning : true;
    const r = parseFloat((c.rating || '').replace(',', '.'));
    const byRating = !c.rating || r >= minRating;
    return byFilter && byRating && (!q || text.includes(q));
  }});
}}

function renderList() {{
  const visible = visibleClinics();
  listEl.innerHTML = '';
  visible.forEach(c => {{
    const div = document.createElement('div');
    div.className = 'item';
    div.innerHTML =
      `<div class="name">${{c.cleaning ? '🦷 ' : ''}}${{c.clinic}}</div>` +
      `<div class="addr">${{c.address}}</div>` +
      `<div class="meta">${{c.hours || ''}}${{c.hours ? ' · ' : ''}}${{c.services}}</div>`;
    div.addEventListener('click', () => showOnMap(c.id));
    listEl.appendChild(div);
  }});
  countEl.textContent = `Показано: ${{visible.length}} из ${{CLINICS.length}}`;
}}

function applyFilters() {{
  if (!clusterer) return;
  const visible = new Set(visibleClinics().map(c => c.id));
  clusterer.remove(allObjects.map(o => o.pm));
  clusterer.add(allObjects.filter(o => visible.has(o.id)).map(o => o.pm));
  renderList();
}}

function showOnMap(id) {{
  const obj = allObjects.find(x => x.id === id);
  if (!obj) return;
  mapInstance.setCenter([obj.c.lat, obj.c.lon], 15, {{duration: 400}});
  mapInstance.balloon.open(obj.pm.geometry.getCoordinates(), balloonContent(obj.c));
  document.querySelectorAll('.item').forEach(el => el.classList.remove('active'));
  const items = document.querySelectorAll('.item');
  const idx = visibleClinics().findIndex(c => c.id === id);
  if (items[idx]) items[idx].classList.add('active');
}}

function initMap() {{
  mapInstance = new ymaps.Map('mapContainer', {{
    center: [55.65, 37.62],
    zoom: 10,
    controls: ['zoomControl', 'fullscreenControl', 'geolocationControl']
  }});

  clusterer = new ymaps.Clusterer({{
    preset: 'islands#invertedVioletClusterIcons',
    clusterDisableClickZoom: false
  }});

  CLINICS.forEach(c => {{
    const pm = new ymaps.Placemark([c.lat, c.lon], {{
      hintContent: c.clinic,
      balloonContent: balloonContent(c)
    }}, {{ preset: markerColor(c) }});
    allObjects.push({{ id: c.id, pm: pm, c: c }});
  }});
  clusterer.add(allObjects.map(o => o.pm));
  mapInstance.geoObjects.add(clusterer);
  applyFilters();
}}

searchEl.addEventListener('input', applyFilters);
ratingFilter.addEventListener('change', applyFilters);
document.querySelectorAll('#filters button').forEach(btn => btn.addEventListener('click', () => {{
  document.querySelectorAll('#filters button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeFilter = btn.dataset.filter;
  applyFilters();
}}));

// Список рисуем сразу — он не зависит от загрузки карты.
renderList();
ymaps.ready(initMap);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
