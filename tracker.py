#!/usr/bin/env python3
"""Monitoron statusin e dërgesës (CIG) dhe pozicionin e anijes.

Burimi i vetëm i pozicionit të anijes: MyShipTracking (cloudscraper).

Ekzekutohet çdo 2 orë nga GitHub Actions. Dërgon njoftime në Telegram kur:
  - statusi i dërgesës në CIG ndryshon
  - anija lëviz mbi një prag (km, llogaritur me Haversine)
  - sistemi dështon 2+ herë rresht (njoftim urgjence)

Orët e heshtjes janë ÇAKTIVIZUAR: njoftimet dërgohen 24/7, pa kufizim orari.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:  # duhet të jetë instaluar nga requirements.txt
    cloudscraper = None

# ---------------------------------------------------------------- CONFIG ---

VIN = "WDCTG0CB2GJ240705"
IMO = "8606056"

CIG_URL = f"https://www.cigbooking.com/track/{VIN}"

# MyShipTracking përdor URL me "slug" (emri i anijes + ID), jo vetëm IMO.
# Anija aktuale është GMT ASTRO (IMO 8606056, MMSI 373817000). Nëse dërgesa
# juaj kalon ndonjëherë në një anije tjetër,
# ky URL duhet përditësuar manualisht (kërko emrin e ri të anijes në sitin
# përkatës dhe kopjo URL-në e re këtu).
MST_URL = "https://www.myshiptracking.com/vessels/gmt-astro-mmsi-373817000-imo-8606056"

# Koordinatat e Durrësit, Shqipëri (destinacioni final i rrugës detare).
DURRES_LAT = 41.3233
DURRES_LON = 19.4413

# Rruga detare tipike Incheon -> Durrës, si listë pikash kyçe (waypoints).
# Përdoret nga sea_route_distance_km() në vend të Haversine drejt,
# sepse anija nuk lëviz në vijë të drejtë (kalon ngushtica dhe kanale).
#
# Krahasuar me rrugën e propozuar fillimisht (vetëm Incheon -> Singapor ->
# Colombo -> Port Said -> Durrës), këtu janë shtuar edhe:
#   - Ngushtica Bab-el-Mandeb (mes Detit Arabik dhe Detit të Kuq) - pa këtë
#     pikë, harku i madh Colombo -> Port Said do të kalonte mbi Gadishullin
#     Arabik (tokë), jo mbi det.
#   - Hyrja jugore e Kanalit të Suezit (qyteti Suez) - shtuar veç Port Said
#     (dalja veriore), që kanali të trajtohet si dy pika, jo një.
#   - Ngushtica e Otrantos (mes Italisë dhe Shqipërisë) - pika hyrëse
#     realiste në Adriatik para Durrësit, në vend të një harku të madh
#     direkt nga Mesdheu Lindor që mund të kalonte pranë ishujve grekë.
ROUTE_WAYPOINTS: list[tuple[str, float, float]] = [
    ("Incheon", 37.4623, 126.6117),
    ("Ngushtica e Malakës (Singapor)", 1.3521, 103.8198),
    ("Colombo", 6.9271, 79.8612),
    ("Ngushtica Bab-el-Mandeb", 12.5, 43.4),
    ("Kanali i Suezit (hyrja jugore)", 29.9668, 32.5498),
    ("Kanali i Suezit (dalja - Port Said)", 31.2653, 32.3019),
    ("Ngushtica e Otrantos", 40.0, 18.8),
    ("Durrës", DURRES_LAT, DURRES_LON),
]

STATUS_FILE = Path("status.json")
LOG_FILE = Path("tracker.log")

TIMEZONE = os.environ.get("TRACKER_TZ", "Europe/Tirane")
QUIET_START = dtime(3, 0)
QUIET_END = dtime(6, 0)

MOVE_THRESHOLD_KM = float(os.environ.get("MOVE_THRESHOLD_KM", "5"))
HISTORY_LIMIT = 10
FAILURE_ALERT_THRESHOLD = 2

RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ------------------------------------------------------------- LOGGING -----

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tracker")


# ------------------------------------------------------------- HELPERS -----

def retry_fetch(fetch_fn, *args, attempts: int = RETRY_ATTEMPTS,
                 delay: int = RETRY_DELAY_SECONDS, **kwargs):
    """Thërret fetch_fn(*args, **kwargs) me deri në `attempts` përpjekje.

    Rikthen rezultatin në përpjekjen e parë të suksesshme, ose ngre
    përjashtimin e fundit nëse dështojnë të gjitha.
    """
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            return fetch_fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - duam të kapim çdo lloj gabimi rrjeti/parsimi
            last_exc = e
            log.warning(f"Përpjekja {i}/{attempts} dështoi për {fetch_fn.__name__}: {e}")
            if i < attempts:
                time.sleep(delay)
    log.error(f"Të gjitha {attempts} përpjekjet dështuan për {fetch_fn.__name__}: {last_exc}")
    raise last_exc


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanca në km mes dy koordinatave (formula Haversine)."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _initial_bearing_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Kursi fillestar (radianë) nga (lat1,lon1) drejt (lat2,lon2), përgjatë
    harkut të madh (great circle)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return math.atan2(y, x)


def _destination_point(lat1: float, lon1: float, bearing_rad: float, distance_km: float) -> tuple[float, float]:
    """Pika (lat,lon) e arritur duke lëvizur `distance_km` nga (lat1,lon1)
    përgjatë kursit `bearing_rad`, mbi harkun e madh."""
    r = 6371.0088
    phi1 = math.radians(lat1)
    lam1 = math.radians(lon1)
    delta = distance_km / r
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(bearing_rad)
    )
    lam2 = lam1 + math.atan2(
        math.sin(bearing_rad) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), math.degrees(lam2)


def _cross_along_track_km(lat1: float, lon1: float, lat2: float, lon2: float,
                           lat3: float, lon3: float) -> tuple[float, float]:
    """Distanca pingul (cross-track) dhe përgjatë (along-track), në km, të
    pikës (lat3,lon3) në lidhje me harkun e madh nga (lat1,lon1) te
    (lat2,lon2). Formula standarde e navigacionit sferik."""
    r = 6371.0088
    d13 = haversine_km(lat1, lon1, lat3, lon3) / r
    theta13 = _initial_bearing_rad(lat1, lon1, lat3, lon3)
    theta12 = _initial_bearing_rad(lat1, lon1, lat2, lon2)
    sin_dxt = _clamp(math.sin(d13) * math.sin(theta13 - theta12), -1.0, 1.0)
    dxt = math.asin(sin_dxt) * r
    cos_dxt_r = math.cos(dxt / r)
    if abs(cos_dxt_r) < 1e-12:
        dat = d13 * r
    else:
        cos_dat = _clamp(math.cos(d13) / cos_dxt_r, -1.0, 1.0)
        dat = math.acos(cos_dat) * r
    return dxt, dat


def _route_segment_lengths() -> list[float]:
    lengths = []
    for i in range(len(ROUTE_WAYPOINTS) - 1):
        _, lat1, lon1 = ROUTE_WAYPOINTS[i]
        _, lat2, lon2 = ROUTE_WAYPOINTS[i + 1]
        lengths.append(haversine_km(lat1, lon1, lat2, lon2))
    return lengths


_ROUTE_SEGMENT_LENGTHS = _route_segment_lengths()
_ROUTE_TOTAL_KM = sum(_ROUTE_SEGMENT_LENGTHS)

# Distanca reale detare Incheon -> Durrës (nga AIS/ portale detare, siç e
# ke dhënë ti: ~16,486 km / 8,902 milje detare). Shuma e segmenteve me
# Haversine (_ROUTE_TOTAL_KM) del pak më e shkurtër, sepse harku i madh
# mes çdo dy waypoint-esh është gjithmonë pak më i shkurtër se korsia
# reale e lundrimit (të cilat lakohen, ndjekin skema ndarëse trafiku,
# etj.). Ky faktor e "kalibron" rrugën tonë me pikë-referencë ndaj
# distancës reale, që rezultatet të mos jenë sistematikisht nën vlerën e
# vërtetë - pa prekur formën e rrugës (waypoints mbeten ashtu siç janë).
KNOWN_REAL_SEA_KM = 16486.0
_ROUTE_CALIBRATION = KNOWN_REAL_SEA_KM / _ROUTE_TOTAL_KM if _ROUTE_TOTAL_KM else 1.0


def sea_route_distance_km(lat: float, lon: float) -> tuple[float, float]:
    """Llogarit distancën DETARE (jo në vijë të drejtë) të anijes përgjatë
    rrugës së përcaktuar në ROUTE_WAYPOINTS.

    Metoda: anija "shkëputet" (snap) te segmenti i rrugës më të afërt me
    pozicionin e saj aktual (duke përdorur cross-track/along-track mbi
    harkun e madh për çdo segment), pastaj distanca e përshkuar llogaritet
    si shuma e segmenteve të plota përpara + pjesa e përshkuar e segmentit
    aktual. Distanca e mbetur është gjithsej rruga minus e përshkuara.

    Kthen (distanca_e_përshkuar_km, distanca_e_mbetur_km deri në Durrës).
    Nëse llogaritja dështon për ndonjë arsye (rast ekstrem numerik), bie
    mbrapa te një përafrim i thjeshtë me Haversine drejt Durrësit.
    """
    best_dist = None
    best_index = None
    best_along = None

    for i in range(len(ROUTE_WAYPOINTS) - 1):
        _, lat1, lon1 = ROUTE_WAYPOINTS[i]
        _, lat2, lon2 = ROUTE_WAYPOINTS[i + 1]
        seg_len = _ROUTE_SEGMENT_LENGTHS[i]
        try:
            _, dat = _cross_along_track_km(lat1, lon1, lat2, lon2, lat, lon)
        except (ValueError, ZeroDivisionError):
            continue
        dat_clamped = _clamp(dat, 0.0, seg_len)
        theta12 = _initial_bearing_rad(lat1, lon1, lat2, lon2)
        clat, clon = _destination_point(lat1, lon1, theta12, dat_clamped)
        dist_to_point = haversine_km(lat, lon, clat, clon)
        if best_dist is None or dist_to_point < best_dist:
            best_dist = dist_to_point
            best_index = i
            best_along = dat_clamped

    if best_index is None:
        direct = haversine_km(lat, lon, DURRES_LAT, DURRES_LON) * _ROUTE_CALIBRATION
        return max(0.0, KNOWN_REAL_SEA_KM - direct), direct

    traveled = sum(_ROUTE_SEGMENT_LENGTHS[:best_index]) + best_along
    traveled = _clamp(traveled, 0.0, _ROUTE_TOTAL_KM) * _ROUTE_CALIBRATION
    remaining = max(0.0, KNOWN_REAL_SEA_KM - traveled)
    return traveled, remaining


def format_km(km: float) -> str:
    """Formaton km: me presje mijësh nëse > 1000 km (p.sh. 8,452 km),
    përndryshe me 1 shifër pas presjes (p.sh. 342.7 km)."""
    if km > 1000:
        return f"{km:,.0f} km"
    return f"{km:.1f} km"


# Formate të mundshme të ETA-s ashtu si vijnë nga CIG / MyShipTracking
# (tekst i lirë, jo gjithmonë i standardizuar).
_ETA_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%b %d, %Y %H:%M",
    "%d %b %Y %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y",
)


def parse_eta(eta_raw: str | None) -> datetime | None:
    """Provon të kthejë tekstin e ETA-s (nga CIG ose MyShipTracking) në
    `datetime`, duke provuar disa formate të njohura. Kthen None nëse
    s'arrin ta parsojë.
    """
    if not eta_raw:
        return None
    cleaned = eta_raw.strip()
    # heq etiketa/zona orare shtesë të zakonshme në fund (UTC, LT, Local...)
    cleaned = re.sub(r"\s*\((?:UTC|LT)\)\s*$", "", cleaned, flags=re.I).strip()
    cleaned = re.sub(r"\s*(UTC|LT|Local time)\s*$", "", cleaned, flags=re.I).strip()
    for fmt in _ETA_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def format_eta_line(eta_raw: str | None) -> str:
    """Ndërton rreshtin '📅 ETA: ...' me ditët e mbetura deri në mbërritje,
    të llogaritura nga data aktuale (UTC). Nëse ETA mungon ose s'mund të
    parsohet, kthen '📅 ETA: e panjohur'.
    """
    eta_dt = parse_eta(eta_raw)
    if eta_dt is None:
        return "📅 ETA: e panjohur"

    eta_str = eta_dt.strftime("%d.%m.%Y %H:%M")
    days_left = (eta_dt.date() - datetime.utcnow().date()).days
    days_left = max(days_left, 0)
    return f"📅 ETA: {eta_str} | ⏳ Kanë mbetur edhe {days_left} ditë"


def is_quiet_hours(now: datetime | None = None) -> bool:
    """Orët e heshtjes janë ÇAKTIVIZUAR. Njoftimet dërgohen 24/7, pa
    asnjë kufizim orari. Funksioni mbahet (kthen gjithmonë False) vetëm
    për kompatibilitet me pjesën tjetër të kodit që e thërret."""
    return False


def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            data.setdefault("cig", {})
            data.setdefault("vessel", {})
            data.setdefault("history", [])
            data.setdefault("consecutive_failures", 0)
            return data
        except json.JSONDecodeError:
            log.warning("status.json i pavlefshëm, rifillo nga zero")
    return {"cig": {}, "vessel": {}, "history": [], "consecutive_failures": 0}


def save_status(data: dict) -> None:
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def push_history(status: dict, cig_data: dict, vessel_data: dict, source: str) -> None:
    entry = {
        "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "cig_status": cig_data.get("current_status", status.get("cig", {}).get("current_status")),
        "vessel_lat": vessel_data.get("lat", status.get("vessel", {}).get("lat")),
        "vessel_lon": vessel_data.get("lon", status.get("vessel", {}).get("lon")),
        "vessel_source": source,
    }
    status["history"].append(entry)
    status["history"] = status["history"][-HISTORY_LIMIT:]


def send_telegram(message: str, force: bool = False) -> None:
    """Dërgon një mesazh Telegram. Nëse jemi në orët e heshtjes dhe `force`
    është False, mesazhi thjesht regjistrohet (log) dhe nuk dërgohet.
    """
    if is_quiet_hours() and not force:
        log.info(f"Orë heshtjeje ({TIMEZONE}) - njoftimi u shty/u anashkalua: {message[:60]}...")
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID mungojnë, s'dërgoj njoftim")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Dështoi dërgimi në Telegram: {e}")


# --------------------------------------------------------- HTTP CLIENTS ----

_scraper = None


def get_scraper():
    """Kthen një sesion cloudscraper (bën bypass Cloudflare JS challenge
    bazë). Rikthen None nëse cloudscraper s'është i instaluar.
    """
    global _scraper
    if cloudscraper is None:
        return None
    if _scraper is None:
        _scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
    return _scraper


def http_get(url: str, use_cloudscraper: bool = False, timeout: int = 25) -> str:
    if use_cloudscraper:
        scraper = get_scraper()
        if scraper is None:
            raise RuntimeError("cloudscraper s'është i instaluar (shih requirements.txt)")
        resp = scraper.get(url, headers=HEADERS, timeout=timeout)
    else:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    if len(resp.text) < 500:
        # faqe bosh / bllok Cloudflare pa status code gabimi
        raise RuntimeError(f"Përgjigje shumë e shkurtër ({len(resp.text)} bytes), ka gjasa bllok anti-bot")
    return resp.text


# ------------------------------------------------------------- CIG PARSER --

def parse_cig(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [l for l in text.split("\n") if l]

    def after(label: str) -> str:
        for i, l in enumerate(lines):
            if l == label and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    def find_line_containing(marker: str) -> str:
        for l in lines:
            if marker in l:
                return l
        return ""

    data = {
        "shipper": after("Shipper"),
        "model": after("Model"),
        "destination": after("Destination"),
        "current_status": find_line_containing("Current Status") and after("Current Status"),
        "vessel_name": after("Vessel"),
        "etd": after("ETD (Departure)"),
    }

    eta_label = next((l for l in lines if l.startswith("ETA")), "")
    if eta_label:
        data["eta"] = after(eta_label)

    pos_match = re.search(r"Position\s*\n?\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)", text)
    if not pos_match:
        pos_match = re.search(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", text)
    if pos_match:
        data["lat"] = float(pos_match.group(1))
        data["lon"] = float(pos_match.group(2))

    return data


def fetch_cig() -> dict:
    html = http_get(CIG_URL, use_cloudscraper=False)
    data = parse_cig(html)
    if not data.get("current_status") and not data.get("lat"):
        raise RuntimeError("CIG: s'u gjet asnjë fushë e njohur në faqe")
    return data


# --------------------------------------------------------- MYSHIPTRACKING --
# MyShipTracking e shkruan pozicionin aktual edhe si tekst i thjeshtë brenda
# një paragrafi "Current position of X is in Y with coordinates LAT° / LON°"
# që renderizohet server-side (jo vetëm në JS), prandaj cloudscraper/requests
# e sheh normalisht pa nevojë për Selenium.

def parse_myshiptracking(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    data = {}

    m = re.search(
        r"coordinates\D{0,10}(-?\d{1,3}\.\d+)°?\s*/\s*(-?\d{1,3}\.\d+)°?",
        text,
    )
    if not m:
        # rezervë: ndonjë hyrje në tabelën e "Events" (LAT / LON)
        m = re.search(r"(-?\d{1,2}\.\d{4,6})\s*/\s*(-?\d{1,3}\.\d{4,6})", text)
    if m:
        data["lat"] = float(m.group(1))
        data["lon"] = float(m.group(2))

    dest_m = re.search(r"heading at the port of\s+([A-Z][A-Za-z0-9 .\-']+?)[\.\n]", text)
    if dest_m:
        data["destination"] = dest_m.group(1).strip()

    eta_m = re.search(
        r"estimated time of arrival[^0-9]*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text
    )
    if eta_m:
        data["eta"] = eta_m.group(1)

    status_m = re.search(r"\bStatus\b\s+([A-Za-z][A-Za-z ]+?)\s+Speed", text)
    if status_m:
        data["nav_status"] = status_m.group(1).strip()

    return data


def fetch_myshiptracking_cloudscraper() -> dict:
    html = http_get(MST_URL, use_cloudscraper=True)
    data = parse_myshiptracking(html)
    if data.get("lat") is None:
        raise RuntimeError("MyShipTracking (cloudscraper): s'u gjetën koordinata")
    return data


# ------------------------------------------------------- VESSEL PIPELINE ---

def get_vessel_position() -> tuple[dict, str]:
    """Burimi i vetëm: MyShipTracking / cloudscraper.

    Kthen (të dhëna, emri_burimit) ose ({}, "asnjë") nëse dështon.
    """
    attempts = [("MyShipTracking/cloudscraper", fetch_myshiptracking_cloudscraper)]

    for name, fn in attempts:
        try:
            data = retry_fetch(fn, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY_SECONDS)
            log.info(f"Pozicioni i anijes u mor me sukses nga {name}")
            return data, name
        except Exception as e:
            log.warning(f"Burimi {name} dështoi plotësisht: {e}")

    return {}, "asnjë"


# ------------------------------------------------------------------ MAIN ---

def main() -> int:
    status = load_status()
    prev_cig = status.get("cig", {})
    prev_vessel = status.get("vessel", {})
    notifications = []

    cig_ok = False
    vessel_ok = False

    # --- CIG ---
    try:
        cig_data = retry_fetch(fetch_cig, attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY_SECONDS)
        cig_ok = True
    except Exception as e:
        log.error(f"CIG dështoi plotësisht pas {RETRY_ATTEMPTS} përpjekjesh: {e}")
        cig_data = {}

    if cig_data:
        new_status_text = cig_data.get("current_status", "")
        old_status_text = prev_cig.get("current_status", "")
        if new_status_text and new_status_text != old_status_text:
            notifications.append(
                "🚗 <b>Statusi i veturës u ndryshua</b>\n"
                f"VIN: {VIN}\n"
                f"Status i ri: {new_status_text}\n"
                f"Anija: {cig_data.get('vessel_name', '—')}\n"
                f"ETD: {cig_data.get('etd', '—')} | ETA: {cig_data.get('eta', '—')}"
            )
        status["cig"] = {**prev_cig, **cig_data}

    # --- Vessel (MyShipTracking) ---
    vf_data, source = get_vessel_position()
    if vf_data.get("lat") is not None and vf_data.get("lon") is not None:
        vessel_ok = True
        old_lat = prev_vessel.get("lat")
        old_lon = prev_vessel.get("lon")
        is_first_position = old_lat is None or old_lon is None
        dist_km = None
        if is_first_position:
            # Herën e parë që kemi koordinata të anijes (s'ka koordinata të
            # vjetra për krahasim) - njoftimi dërgohet GJITHMONË.
            moved = True
            log.info("Pozicioni i parë i regjistruar për anijen - dërgoj njoftim pa krahasim distance")
        else:
            dist_km = haversine_km(old_lat, old_lon, vf_data["lat"], vf_data["lon"])
            moved = dist_km > MOVE_THRESHOLD_KM
        if moved:
            maps_link = f"https://www.google.com/maps?q={vf_data['lat']},{vf_data['lon']}"
            if is_first_position:
                dist_line = "Pozicioni i parë i regjistruar\n"
            else:
                dist_line = f"Lëvizje: {dist_km:.1f} km\n"

            _, durres_remaining_km = sea_route_distance_km(vf_data["lat"], vf_data["lon"])
            durres_line = f"📏 Distanca detare nga Durrësi: {format_km(durres_remaining_km)}\n"

            eta_raw = cig_data.get("eta") or vf_data.get("eta")
            eta_line = f"{format_eta_line(eta_raw)}\n"

            title = (
                "🛳 <b>Pozicioni i parë i anijes u regjistrua</b>\n"
                if is_first_position
                else "🛳 <b>Anija ndryshoi pozicionin</b>\n"
            )
            notifications.append(
                title +
                f"IMO: {IMO}\n"
                f"Burimi: {source}\n"
                f"Koordinata: {vf_data['lat']}, {vf_data['lon']}\n"
                f"{dist_line}"
                f"{durres_line}"
                f"{eta_line}"
                f"Destinacioni: {vf_data.get('destination', vf_data.get('destination_hint', '—'))}\n"
                f"Statusi: {vf_data.get('nav_status', '—')}\n"
                f"Harta: {maps_link}"
            )
        status["vessel"] = {**prev_vessel, **vf_data, "source": source}

    push_history(status, cig_data, vf_data, source)

    # --- Failure tracking & njoftim urgjence ---
    system_failed = not (cig_ok or vessel_ok)
    if system_failed:
        status["consecutive_failures"] = status.get("consecutive_failures", 0) + 1
    else:
        status["consecutive_failures"] = 0

    if status["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
        send_telegram(
            "🚨 <b>Sistemi i monitorimit dështoi</b>\n"
            f"CIG dhe MyShipTracking dështuan {status['consecutive_failures']} herë rresht.\n"
            "Kontrollo tracker.log në GitHub Actions.",
            force=True,
        )

    for msg in notifications:
        send_telegram(msg)

    save_status(status)

    if notifications:
        log.info(f"U dërguan/vlerësuan {len(notifications)} njoftim(e), status.json u përditësua")
    else:
        log.info("Asnjë ndryshim")

    log.info(
        f"Përfundoi: CIG={'OK' if cig_ok else 'DËSHTOI'}, "
        f"Anija={'OK (' + source + ')' if vessel_ok else 'DËSHTOI'}, "
        f"dështime rresht={status['consecutive_failures']}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
