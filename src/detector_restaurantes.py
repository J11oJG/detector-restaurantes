"""
Detector de restaurantes sin sistema de reservas digital.

1. Busca restaurantes con Google Places API (New) - Text Search.
2. Descarga la web de cada uno (home + página de reservas si existe).
3. Busca firmas de widgets de reservas conocidos (CoverManager, TheFork, etc.).
4. Clasifica, prioriza y escribe todo en una Google Sheet.

Requisitos:
    pip install requests gspread

Variables de entorno:
    GOOGLE_PLACES_API_KEY   API key con "Places API (New)" habilitada
    SHEET_ID                ID de la Google Sheet (lo que va entre /d/ y /edit en la URL)
    GOOGLE_SA_FILE          Ruta al JSON de la service account (por defecto service_account.json)
"""

import csv
import os
import re
import time
import concurrent.futures as cf
from urllib.parse import urljoin, urlparse

import gspread
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

API_KEY = os.environ["GOOGLE_PLACES_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SA_FILE", "service_account.json")
WORKSHEET_NAME = "resultados"
CSV_BACKUP = "resultados_restaurantes.csv"

# Text Search devuelve máximo 60 resultados por consulta,
# así que conviene dividir por barrio y tipo de cocina.
QUERIES = [
    "restaurantes en Les Corts, Barcelona",
    "bares de tapas en Les Corts, Barcelona",
    "restaurante mediterráneo en Les Corts, Barcelona",
    "restaurante italiano en Les Corts, Barcelona",
    "restaurante japonés en Les Corts, Barcelona",
    "restaurante latinoamericano en Les Corts, Barcelona",
    "restaurantes en Pedralbes, Barcelona",
    "restaurantes en La Maternitat i Sant Ramon, Barcelona",
]

# Umbrales de prioridad según número de reseñas en Google (proxy de volumen de llamadas)
UMBRAL_ALTA = 300
UMBRAL_MEDIA = 100

MAX_WORKERS = 10
TIMEOUT = 10

# Firmas de sistemas de reservas (se buscan en el HTML en minúsculas)
BOOKING_SIGNATURES = {
    "CoverManager": ["covermanager"],
    "TheFork": ["thefork", "lafourchette", "eltenedor"],
    "Zenchef": ["zenchef"],
    "OpenTable": ["opentable"],
    "SevenRooms": ["sevenrooms"],
    "Resy": ["resy.com"],
    "Tock": ["exploretock"],
    "DISH (Metro)": ["reservation.dish.co", "dish.co/"],
    "Restoo": ["restoo"],
    "Qamarero": ["qamarero"],
    "QuickSit": ["quicksit"],
    "Resmio": ["resmio"],
    "Bookitit": ["bookitit"],
    "GloriaFood": ["gloriafood"],
    "Tablein": ["tablein"],
    "MesaBot": ["mesabot"],
}

# Señales de reserva manual (español y catalán)
MANUAL_SIGNALS = {
    "WhatsApp": ["wa.me/", "api.whatsapp.com", "whatsapp"],
    "Teléfono": [
        "reservas por teléfono", "reserva por teléfono", "llámanos", "llamanos",
        "reservas al", "reserves per telèfon", "truca'ns", "trucan's",
    ],
}

RESERVATION_LINK_HINTS = ("reserv", "booking", "book", "mesa")

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9,ca;q=0.8",
}

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.rating",
    "places.userRatingCount",
    "places.googleMapsUri",
    "places.reservable",
    "nextPageToken",
])

HEADER = [
    "Nombre", "Dirección", "Teléfono", "Web", "Rating", "Nº reseñas",
    "Estado", "Sistemas detectados", "Señales manuales",
    "Reservable en Google", "Prioridad", "Google Maps", "Place ID",
]


# ---------------------------------------------------------------------------
# Google Places
# ---------------------------------------------------------------------------

def search_places(query: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {"textQuery": query, "languageCode": "es", "pageSize": 20}
    results = []

    while True:
        resp = requests.post(PLACES_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("places", []))

        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
        time.sleep(1)

    print(f"  '{query}': {len(results)} resultados")
    return results


# ---------------------------------------------------------------------------
# Análisis de webs
# ---------------------------------------------------------------------------

def fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        return r.text.lower()
    except requests.RequestException:
        return None


def find_reservation_pages(html: str, base_url: str, limit: int = 2) -> list[str]:
    """Links de la misma web que parecen página de reservas."""
    base_domain = urlparse(base_url).netloc
    links = []
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', html):
        full = urljoin(base_url, href)
        if urlparse(full).netloc != base_domain:
            continue
        if any(h in full.lower() for h in RESERVATION_LINK_HINTS) and full not in links:
            links.append(full)
        if len(links) >= limit:
            break
    return links


def detect(html: str, signatures: dict) -> list[str]:
    return [name for name, sigs in signatures.items() if any(s in html for s in sigs)]


def prioridad(estado: str, reviews: int) -> str:
    if estado == "Usa sistema":
        return "Descartar"
    if reviews >= UMBRAL_ALTA:
        return "Alta"
    if reviews >= UMBRAL_MEDIA:
        return "Media"
    return "Baja"


def analyze(place: dict) -> list:
    website = place.get("websiteUri", "")
    reviews = place.get("userRatingCount", 0) or 0
    sistemas, manuales = [], []

    if not website:
        estado = "Sin web"
    else:
        html = fetch(website)
        if html is None:
            estado = "Web no accesible"
        else:
            pages = [html]
            for link in find_reservation_pages(html, website):
                sub = fetch(link)
                if sub:
                    pages.append(sub)
            full_html = "\n".join(pages)
            sistemas = detect(full_html, BOOKING_SIGNATURES)
            manuales = detect(full_html, MANUAL_SIGNALS)
            estado = "Usa sistema" if sistemas else "Sin sistema detectado"

    reservable = place.get("reservable")
    return [
        place.get("displayName", {}).get("text", ""),
        place.get("formattedAddress", ""),
        place.get("nationalPhoneNumber", ""),
        website,
        place.get("rating", ""),
        reviews,
        estado,
        ", ".join(sistemas),
        ", ".join(manuales),
        "Sí" if reservable else ("No" if reservable is False else ""),
        prioridad(estado, reviews),
        place.get("googleMapsUri", ""),
        place.get("id", ""),
    ]


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def write_csv(rows: list[list]) -> None:
    with open(CSV_BACKUP, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)


def write_sheet(rows: list[list]) -> None:
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=len(rows) + 10, cols=len(HEADER))
    ws.clear()
    ws.update(values=[HEADER] + rows, range_name="A1")
    ws.freeze(rows=1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Buscando restaurantes...")
    places = {}
    for q in QUERIES:
        for p in search_places(q):
            places[p["id"]] = p  # deduplicar entre consultas
    print(f"Total únicos: {len(places)}")

    print("Analizando webs...")
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        rows = list(ex.map(analyze, places.values()))

    orden = {"Alta": 0, "Media": 1, "Baja": 2, "Descartar": 3}
    rows.sort(key=lambda r: (orden[r[10]], -r[5]))

    write_csv(rows)
    print(f"Backup guardado en {CSV_BACKUP}")

    write_sheet(rows)
    print("Google Sheet actualizada.")

    resumen = {}
    for r in rows:
        resumen[r[6]] = resumen.get(r[6], 0) + 1
    for estado, n in sorted(resumen.items(), key=lambda x: -x[1]):
        print(f"  {estado}: {n}")


if __name__ == "__main__":
    main()
