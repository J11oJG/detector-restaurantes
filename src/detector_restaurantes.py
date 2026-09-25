"""
Detector de restaurantes sin sistema de reservas digital.

1. Busca restaurantes con Google Places API (New) - Text Search.
2. Descarga la web de cada uno (home + páginas de reservas si existen).
3. Busca firmas de widgets de reservas conocidos (CoverManager, TheFork, etc.).
4. Prioriza por percentiles de reseñas calculados sobre los propios datos.
5. Cuenta los dominios externos (scripts/iframes) de todas las webs para
   descubrir proveedores que no están en la lista de firmas.
6. Guarda una foto del total de reseñas de cada restaurante en la pestaña
   "historial" (se acumula entre ejecuciones) y, cuando hay historia suficiente,
   calcula las reseñas por mes reales.
7. Escribe todo en Google Sheets (pestañas "resultados", "dominios_externos"
   e "historial") y en CSV de respaldo.

Requisitos:
    pip install requests gspread python-dotenv

Variables de entorno (.env):
    GOOGLE_PLACES_API_KEY   API key con "Places API (New)" habilitada
    SHEET_ID                ID de la Google Sheet (lo que va entre /d/ y /edit)
    GOOGLE_SA_FILE          Ruta al JSON de la service account
"""

import csv
import os
import re
import statistics
import time
import concurrent.futures as cf
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import urljoin, urlparse

import gspread
import requests
from dotenv import load_dotenv
from gspread.utils import ValidationConditionType

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

API_KEY = os.environ["GOOGLE_PLACES_API_KEY"]
SHEET_ID = os.environ["SHEET_ID"]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SA_FILE", "service_account.json")

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

# Text Search no se limita a la zona pedida: devuelve también locales vecinos
# (~15% fuera de Les Corts en la primera ejecución). Solo se conservan los de
# estos distritos (componente sublocality_level_1 de la dirección). El código
# postal no sirve: 08014, 08028, 08029 y 08034 se comparten con otros distritos.
# Conjunto vacío = sin filtro.
DISTRITOS = {"Les Corts"}

# Prioridad por percentiles de reseñas, calculados sobre los resultados de cada
# ejecución: se adapta solo a la zona (Barcelona vs. Vilanova, por ejemplo).
#   reseñas >= percentil 75 -> Alta   (el 25% con más reseñas)
#   reseñas >= percentil 25 -> Media  (el 50% del medio)
#   resto                   -> Baja
PERCENTIL_ALTA = 75
PERCENTIL_MEDIA = 25
# Si hay muy pocos datos para percentiles, se usan estos valores fijos.
UMBRAL_ALTA_FALLBACK = 300
UMBRAL_MEDIA_FALLBACK = 100

MAX_WORKERS = 10
TIMEOUT = 10

# Reintentos ante errores temporales de Places (429 por ráfaga, 5xx).
# Espera 2, 4 y 8 s: suficiente para una ráfaga, sin alargar mucho la ejecución.
# Si se agotó la cuota diaria, el 429 persiste y el error se propaga igual.
PLACES_REINTENTOS = 3
PLACES_ESPERA_BASE = 2

# Días mínimos entre la primera foto del historial y hoy para calcular
# reseñas/mes (con menos días, la cifra es demasiado ruidosa).
MIN_DIAS_HISTORIAL = 14

# Un dominio externo se reporta si aparece en al menos este número de restaurantes.
MIN_RESTAURANTES_DOMINIO = 2

# Firmas de sistemas de reservas (se buscan en el HTML en minúsculas).
# Amplía esta lista con lo que descubras en la pestaña "dominios_externos".
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
    # Solo enlaces de chat con un número. La palabra "whatsapp" suelta, wa.me/?text=
    # y api.whatsapp.com/send?text= aparecen en botones de compartir.
    "WhatsApp": ["wa.me/3", "wa.me/+", "wa.me/6", "wa.me/7", "send?phone="],
    "Teléfono": [
        "reservas por teléfono", "reserva por teléfono", "llámanos", "llamanos",
        "reservas al", "reserves per telèfon", "truca'ns", "trucan's",
    ],
}

# Dominios de infraestructura que aparecen en casi todas las webs y no aportan
# nada (analítica, CDNs, redes sociales, constructores de webs, cookies).
IGNORED_DOMAINS = {
    "google.com", "googleapis.com", "gstatic.com", "googletagmanager.com",
    "google-analytics.com", "doubleclick.net", "googlesyndication.com",
    "recaptcha.net", "facebook.net", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "tiktok.com", "pinterest.com", "linkedin.com",
    "youtube.com", "youtube-nocookie.com", "vimeo.com",
    "cloudflare.com", "cloudflareinsights.com", "jsdelivr.net", "unpkg.com",
    "jquery.com", "bootstrapcdn.com", "fontawesome.com", "typekit.net",
    "wp.com", "wordpress.com", "wixstatic.com", "parastorage.com",
    "squarespace.com", "sqspcdn.com", "shopify.com", "gravatar.com",
    "cookiebot.com", "onetrust.com", "cookielaw.org", "hotjar.com",
}

# Si la "web" registrada en Google es un perfil de estos dominios, el local no
# tiene web propia. No se descarga: solo devolvería una página de login.
REDES_SOCIALES = {"instagram.com", "facebook.com", "tiktok.com", "linktr.ee"}

RESERVATION_LINK_HINTS = ("reserv", "booking", "book", "mesa")
SRC_RE = re.compile(r'<(?:script|iframe)[^>]+?src=["\']([^"\']+)["\']', re.I)
HREF_RE = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)

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
    "places.addressComponents",
    "nextPageToken",
])

ESTADO_USA = "Usa sistema"
ESTADO_SIN_SISTEMA = "Sin sistema detectado"
ESTADO_GOOGLE = "Reservable en Google (proveedor no identificado)"
ESTADO_SIN_WEB = "Sin web"
ESTADO_NO_ACCESIBLE = "Web no accesible"
ESTADO_REDES = "Solo redes sociales"

# Candidato = restaurante sin sistema de reservas conocido (ver CONTEXT.md).
ESTADOS_CANDIDATO = {ESTADO_SIN_SISTEMA, ESTADO_SIN_WEB, ESTADO_REDES}

# Veredictos de la validación manual (lista cerrada, ver CONTEXT.md).
VEREDICTO_CONFIRMADO = "Confirmado sin sistema"
VEREDICTO_TIENE_SISTEMA = "Tiene sistema"
VEREDICTOS = [VEREDICTO_CONFIRMADO, VEREDICTO_TIENE_SISTEMA, "No acepta reservas", "Cerrado"]
VEREDICTOS_DESCARTE = set(VEREDICTOS) - {VEREDICTO_CONFIRMADO}

# Columnas de la pestaña "resultados": (encabezado, clave del diccionario)
COLUMNAS = [
    ("Nombre", "nombre"),
    ("Dirección", "direccion"),
    ("Teléfono", "telefono"),
    ("Web", "web"),
    ("Rating", "rating"),
    ("Nº reseñas", "resenas"),
    ("Reseñas/mes (historial)", "velocidad"),
    ("Estado", "estado"),
    ("Prioridad", "prioridad"),
    ("Validación", "validacion"),
    ("Sistemas detectados", "sistemas"),
    ("Señales manuales", "manuales"),
    ("Reservable en Google", "reservable"),
    ("Dominios externos", "dominios"),
    ("Google Maps", "maps"),
    ("Place ID", "place_id"),
]
HEADER_VALIDACION = ["Place ID", "Nombre", "Veredicto", "Nota", "Fecha"]
HEADER_HISTORIAL = ["Fecha", "Place ID", "Nombre", "Nº reseñas", "Rating"]
HEADER_DOMINIOS = [
    "Dominio", "Nº restaurantes", "En restaurantes sin sistema detectado",
    "Firma conocida", "Ejemplos",
]


# ---------------------------------------------------------------------------
# Google Places
# ---------------------------------------------------------------------------

def post_con_reintentos(url: str, headers: dict, body: dict) -> dict:
    for intento in range(PLACES_REINTENTOS + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        except requests.RequestException:
            if intento == PLACES_REINTENTOS:
                raise
        else:
            temporal = resp.status_code == 429 or resp.status_code >= 500
            if not temporal or intento == PLACES_REINTENTOS:
                resp.raise_for_status()
                return resp.json()
        espera = PLACES_ESPERA_BASE * 2 ** intento
        print(f"  Error temporal en Places, reintento en {espera} s...")
        time.sleep(espera)


def search_places(query: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {"textQuery": query, "languageCode": "es", "pageSize": 20}
    results = []

    while True:
        data = post_con_reintentos(PLACES_URL, headers, body)
        results.extend(data.get("places", []))

        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
        time.sleep(1)

    print(f"  '{query}': {len(results)} resultados")
    return results


def distrito(place: dict) -> str:
    for comp in place.get("addressComponents", []):
        if "sublocality_level_1" in comp.get("types", []):
            return comp.get("longText", "")
    return ""


# ---------------------------------------------------------------------------
# Análisis de webs
# ---------------------------------------------------------------------------

def fetch(url: str) -> tuple[str, str] | None:
    """Devuelve (URL final tras redirecciones, HTML en minúsculas)."""
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            return None
        return r.url, r.text.lower()
    except requests.RequestException:
        return None


def normalizar_dominio(netloc: str) -> str:
    netloc = netloc.lower().split(":")[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def pertenece(dominio: str, dominios: set[str]) -> bool:
    return any(dominio == d or dominio.endswith("." + d) for d in dominios)


def es_ignorado(dominio: str) -> bool:
    return pertenece(dominio, IGNORED_DOMAINS)


def find_reservation_pages(html: str, base_url: str, limit: int = 2) -> list[str]:
    """Links de la misma web que parecen página de reservas."""
    base_domain = normalizar_dominio(urlparse(base_url).netloc)
    links = []
    for href in HREF_RE.findall(html):
        full = urljoin(base_url, href)
        if normalizar_dominio(urlparse(full).netloc) != base_domain:
            continue
        if any(h in full.lower() for h in RESERVATION_LINK_HINTS) and full not in links:
            links.append(full)
        if len(links) >= limit:
            break
    return links


def dominios_externos(html: str, page_url: str, dominio_propio: str) -> set[str]:
    """Dominios de terceros cargados vía <script src> o <iframe src>."""
    encontrados = set()
    for src in SRC_RE.findall(html):
        dominio = normalizar_dominio(urlparse(urljoin(page_url, src)).netloc)
        if not dominio or es_ignorado(dominio):
            continue
        if dominio == dominio_propio or dominio.endswith("." + dominio_propio):
            continue
        encontrados.add(dominio)
    return encontrados


def detect(html: str, signatures: dict) -> list[str]:
    return [name for name, sigs in signatures.items() if any(s in html for s in sigs)]


def analyze(place: dict) -> dict:
    website = place.get("websiteUri", "")
    reservable = place.get("reservable")
    sistemas, manuales, dominios = [], [], set()

    if not website:
        estado = ESTADO_GOOGLE if reservable else ESTADO_SIN_WEB
    elif pertenece(normalizar_dominio(urlparse(website).netloc), REDES_SOCIALES):
        estado = ESTADO_GOOGLE if reservable else ESTADO_REDES
    else:
        home = fetch(website)
        if home is None:
            estado = ESTADO_GOOGLE if reservable else ESTADO_NO_ACCESIBLE
        else:
            url_final, html = home
            dominio_propio = normalizar_dominio(urlparse(url_final).netloc)
            paginas = [home]
            for link in find_reservation_pages(html, url_final):
                sub = fetch(link)
                if sub:
                    paginas.append(sub)

            full_html = "\n".join(h for _, h in paginas)
            sistemas = detect(full_html, BOOKING_SIGNATURES)
            manuales = detect(full_html, MANUAL_SIGNALS)
            for url, h in paginas:
                dominios |= dominios_externos(h, url, dominio_propio)

            if sistemas:
                estado = ESTADO_USA
            elif reservable:
                estado = ESTADO_GOOGLE
            else:
                estado = ESTADO_SIN_SISTEMA

    return {
        "nombre": place.get("displayName", {}).get("text", ""),
        "direccion": place.get("formattedAddress", ""),
        "telefono": place.get("nationalPhoneNumber", ""),
        "web": website,
        "rating": place.get("rating", ""),
        "resenas": place.get("userRatingCount", 0) or 0,
        "velocidad": "",
        "estado": estado,
        "prioridad": "",
        "validacion": "",
        "sistemas": sistemas,
        "manuales": manuales,
        "reservable": "Sí" if reservable else ("No" if reservable is False else ""),
        "dominios": dominios,
        "maps": place.get("googleMapsUri", ""),
        "place_id": place.get("id", ""),
    }


# ---------------------------------------------------------------------------
# Priorización y descubrimiento de proveedores
# ---------------------------------------------------------------------------

def asignar_prioridad(filas: list[dict]) -> tuple[float, float]:
    resenas = [f["resenas"] for f in filas if f["resenas"] > 0]

    if len(resenas) >= 4:
        cortes = statistics.quantiles(resenas, n=100, method="inclusive")
        umbral_alta = cortes[PERCENTIL_ALTA - 1]
        umbral_media = cortes[PERCENTIL_MEDIA - 1]
    else:
        umbral_alta, umbral_media = UMBRAL_ALTA_FALLBACK, UMBRAL_MEDIA_FALLBACK

    for f in filas:
        # El veredicto manual manda, salvo que el script detecte un sistema
        # (en ese caso la validación ya quedó marcada como desactualizada).
        if f["estado"] == ESTADO_USA or f["validacion"] in VEREDICTOS_DESCARTE:
            f["prioridad"] = "Descartar"
            continue
        # Reserve with Google solo funciona vía partners: casi seguro tienen un
        # sistema que no detectamos. Baja fija hasta validar a mano algunos casos.
        if f["estado"] == ESTADO_GOOGLE and f["validacion"] != VEREDICTO_CONFIRMADO:
            f["prioridad"] = "Baja"
            continue
        if f["resenas"] >= umbral_alta:
            p = "Alta"
        elif f["resenas"] >= umbral_media:
            p = "Media"
        else:
            p = "Baja"
        f["prioridad"] = p

    return umbral_alta, umbral_media


def contar_dominios(filas: list[dict]) -> list[list]:
    total, sin_sistema = Counter(), Counter()
    ejemplos = defaultdict(list)

    for f in filas:
        for d in f["dominios"]:
            total[d] += 1
            if f["estado"] != ESTADO_USA:
                sin_sistema[d] += 1
            if len(ejemplos[d]) < 3:
                ejemplos[d].append(f["nombre"])

    salida = []
    for dominio, n in total.most_common():
        if n < MIN_RESTAURANTES_DOMINIO:
            break
        firma = next(
            (nombre for nombre, sigs in BOOKING_SIGNATURES.items()
             if any(s.rstrip("/") in dominio for s in sigs)),
            "",
        )
        salida.append([dominio, n, sin_sistema[dominio], firma, ", ".join(ejemplos[dominio])])
    return salida


# ---------------------------------------------------------------------------
# Validación manual
# ---------------------------------------------------------------------------

def leer_validacion(sh) -> dict[str, str]:
    """Devuelve place_id -> veredicto. Si hay varias filas, gana la última."""
    try:
        ws = sh.worksheet("validacion")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="validacion", rows=1000, cols=len(HEADER_VALIDACION))
        ws.update(values=[HEADER_VALIDACION], range_name="A1")
        ws.freeze(rows=1)
        ws.add_validation(
            "C2:C1000", ValidationConditionType.one_of_list, VEREDICTOS,
            strict=True, showCustomUi=True,
        )
        return {}

    validacion = {}
    for n, fila in enumerate(ws.get_all_values()[1:], start=2):
        if len(fila) < 3 or not fila[0].strip():
            continue
        if fila[2] not in VEREDICTOS:
            print(f"  Aviso: veredicto desconocido en validacion fila {n}: '{fila[2]}'")
            continue
        validacion[fila[0].strip()] = fila[2]
    return validacion


def aplicar_validacion(filas: list[dict], validacion: dict[str, str]) -> None:
    for f in filas:
        veredicto = validacion.get(f["place_id"])
        if not veredicto:
            continue
        # Si el script detecta ahora un sistema, gana el script.
        if f["estado"] == ESTADO_USA and veredicto != VEREDICTO_TIENE_SISTEMA:
            f["validacion"] = f"{veredicto} (desactualizado)"
        else:
            f["validacion"] = veredicto


# ---------------------------------------------------------------------------
# Historial de reseñas
# ---------------------------------------------------------------------------

def leer_historial(sh) -> tuple[object, dict[str, list[tuple[date, int]]]]:
    """Devuelve la pestaña 'historial' y un dict place_id -> [(fecha, reseñas)]."""
    try:
        ws = sh.worksheet("historial")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="historial", rows=1000, cols=len(HEADER_HISTORIAL))
        ws.update(values=[HEADER_HISTORIAL], range_name="A1")
        ws.freeze(rows=1)
        return ws, {}

    historial = defaultdict(list)
    for fila in ws.get_all_values()[1:]:
        try:
            historial[fila[1]].append((date.fromisoformat(fila[0]), int(fila[3])))
        except (IndexError, ValueError):
            continue
    return ws, historial


def calcular_velocidad(filas: list[dict], historial: dict) -> None:
    """Reseñas por mes desde la foto más antigua del historial."""
    hoy = date.today()
    for f in filas:
        fotos = historial.get(f["place_id"])
        if not fotos:
            continue
        fecha_ini, resenas_ini = min(fotos)
        dias = (hoy - fecha_ini).days
        if dias >= MIN_DIAS_HISTORIAL:
            f["velocidad"] = round((f["resenas"] - resenas_ini) / dias * 30, 1)


def guardar_historial(ws, filas: list[dict], historial: dict) -> list[list]:
    """Agrega la foto de hoy (una sola por restaurante y día)."""
    hoy = date.today()
    nuevas = [
        [hoy.isoformat(), f["place_id"], f["nombre"], f["resenas"], f["rating"]]
        for f in filas
        if not any(fecha == hoy for fecha, _ in historial.get(f["place_id"], []))
    ]
    if nuevas:
        ws.append_rows(nuevas, value_input_option="RAW")
    return nuevas


def append_csv(path: str, header: list[str], rows: list[list]) -> None:
    existe = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not existe:
            writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def fila_a_lista(f: dict) -> list:
    fila = []
    for _, clave in COLUMNAS:
        valor = f[clave]
        if isinstance(valor, (list, set)):
            valor = ", ".join(sorted(valor))
        fila.append(valor)
    return fila


def write_csv(path: str, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def write_worksheet(sh, nombre: str, header: list[str], rows: list[list]) -> None:
    try:
        ws = sh.worksheet(nombre)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=nombre, rows=len(rows) + 10, cols=len(header))
    ws.clear()
    ws.update(values=[header] + rows, range_name="A1")
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
    if DISTRITOS:
        places = {pid: p for pid, p in places.items() if distrito(p) in DISTRITOS}
        print(f"En {', '.join(sorted(DISTRITOS))}: {len(places)}")

    print("Analizando webs...")
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        filas = list(ex.map(analyze, places.values()))

    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    sh = gc.open_by_key(SHEET_ID)
    ws_historial, historial = leer_historial(sh)
    calcular_velocidad(filas, historial)
    aplicar_validacion(filas, leer_validacion(sh))

    umbral_alta, umbral_media = asignar_prioridad(filas)
    print(f"Umbrales de reseñas: Alta >= {umbral_alta:.0f}, Media >= {umbral_media:.0f}")

    orden = {"Alta": 0, "Media": 1, "Baja": 2, "Descartar": 3}
    filas.sort(key=lambda f: (orden[f["prioridad"]], -f["resenas"]))

    header = [h for h, _ in COLUMNAS]
    rows = [fila_a_lista(f) for f in filas]
    dominios = contar_dominios(filas)

    write_csv("resultados_restaurantes.csv", header, rows)
    write_csv("dominios_externos.csv", HEADER_DOMINIOS, dominios)
    print("Backups CSV guardados.")

    write_worksheet(sh, "resultados", header, rows)
    write_worksheet(sh, "dominios_externos", HEADER_DOMINIOS, dominios)
    nuevas = guardar_historial(ws_historial, filas, historial)
    append_csv("historial_resenas.csv", HEADER_HISTORIAL, nuevas)
    print(f"Google Sheet actualizada ({len(nuevas)} fotos nuevas en historial).")

    con_velocidad = sum(1 for f in filas if f["velocidad"] != "")
    if con_velocidad:
        print(f"Reseñas/mes calculadas para {con_velocidad} restaurantes.")
    else:
        print(f"Aún sin historia suficiente para reseñas/mes "
              f"(se necesitan al menos {MIN_DIAS_HISTORIAL} días entre ejecuciones).")

    candidatos = sum(1 for f in filas if f["estado"] in ESTADOS_CANDIDATO)
    print(f"\nCandidatos: {candidatos} de {len(filas)}")
    print("Resumen por estado:")
    for estado, n in Counter(f["estado"] for f in filas).most_common():
        print(f"  {estado}: {n}")

    desconocidos = [d for d in dominios if not d[3] and d[2] > 0][:10]
    if desconocidos:
        print("\nDominios externos frecuentes sin firma conocida (revisar):")
        for d in desconocidos:
            print(f"  {d[0]}: {d[1]} restaurantes")


if __name__ == "__main__":
    main()