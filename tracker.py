#!/usr/bin/env python3
"""Monitoron statusin e dërgesës (CIG) dhe pozicionin e anijes.

Burimet e pozicionit të anijes, me radhë: VesselFinder (cloudscraper),
MarineTraffic (cloudscraper), MyShipTracking (cloudscraper), dhe
Selenium -> VesselFinder (opsionale, fikur si parazgjedhje).

Ekzekutohet çdo 2 orë nga GitHub Actions. Dërgon njoftime në Telegram kur:
  - statusi i dërgesës në CIG ndryshon
  - anija lëviz mbi një prag (km, llogaritur me Haversine)
  - sistemi dështon 2+ herë rresht (njoftim urgjence, injoron heshtjen e natës)

Heshtje njoftimesh normale: 22:00 - 06:00 (orë lokale, config TIMEZONE).
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
VF_URL = f"https://www.vesselfinder.com/?imo={IMO}"
MT_URL = f"https://www.marinetraffic.com/en/ais/details/ships/imo:{IMO}"

# MyShipTracking përdor URL me "slug" (emri i anijes + ID), jo vetëm IMO si
# VesselFinder/MarineTraffic. Anija aktuale është GMT ASTRO (IMO 8606056,
# MMSI 373817000). Nëse dërgesa juaj kalon ndonjëherë në një anije tjetër,
# ky URL duhet përditësuar manualisht (kërko emrin e ri të anijes në sitin
# përkatës dhe kopjo URL-në e re këtu).
MST_URL = "https://www.myshiptracking.com/vessels/gmt-astro-mmsi-373817000-imo-8606056"

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

# Selenium është OPSIONAL dhe FIKUR si parazgjedhje (tracker.yml e vendos
# ENABLE_SELENIUM=false). Mund të aktivizohet vendosur ENABLE_SELENIUM=true
# nëse VesselFinder/MarineTraffic nuk japin koordinata vetëm me cloudscraper.
ENABLE_SELENIUM = os.environ.get("ENABLE_SELENIUM", "false").lower() == "true"

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


def is_quiet_hours(now: datetime | None = None) -> bool:
    """True nëse ora aktuale lokale bie brenda 22:00-06:00."""
    tz = ZoneInfo(TIMEZONE)
    now = (now or datetime.now(tz)).astimezone(tz)
    t = now.time()
    if QUIET_START <= QUIET_END:
        return QUIET_START <= t < QUIET_END
    # intervali kalon mesnatën (rasti ynë: 22:00 -> 06:00)
    return t >= QUIET_START or t < QUIET_END


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


# --------------------------------------------------------- VESSELFINDER ----

def parse_vesselfinder(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True)
            value = cells[1].get_text(strip=True)
            if not label or not value:
                continue
            key = label.lower()
            if "coordinate" in key:
                data["coordinates_raw"] = value
            elif key == "destination":
                data["destination"] = value
            elif "status" in key:
                data["nav_status"] = value
            elif "eta" in key:
                data["eta"] = value

    coords = data.get("coordinates_raw", "")
    m = re.search(r"(-?\d{1,3}\.\d+)\D+(-?\d{1,3}\.\d+)", coords)
    if not m:
        # provo edhe brenda skripteve inline (shpesh koordinatat vijnë si JSON: "lat":..,"lon":..)
        m = re.search(r'"lat"\s*:\s*(-?\d{1,3}\.\d+)\s*,\s*"lon(?:g)?"\s*:\s*(-?\d{1,3}\.\d+)', html)
    if not m:
        m = re.search(r"(-?\d{1,3}\.\d+)\D+(-?\d{1,3}\.\d+)", soup.get_text(" "))
    if m:
        data["lat"] = float(m.group(1))
        data["lon"] = float(m.group(2))

    return data


def fetch_vesselfinder_cloudscraper() -> dict:
    html = http_get(VF_URL, use_cloudscraper=True)
    data = parse_vesselfinder(html)
    if data.get("lat") is None:
        raise RuntimeError("VesselFinder (cloudscraper): s'u gjetën koordinata")
    return data


# --------------------------------------------------------- MARINETRAFFIC ---

def parse_marinetraffic(html: str) -> dict:
    """MarineTraffic e ndërton pjesën më të madhe të faqes me JavaScript, por
    shpesh koordinatat/destinacioni gjenden edhe si JSON i ngulitur në HTML
    (p.sh. brenda <script> ose meta tags). Kjo funksion provon disa modele
    të njohura; nëse struktura e faqes ndryshon, rregulloje shprehjen
    rregullare më poshtë (kontrollo view-source: e faqes për referencë).
    """
    data = {}
    soup = BeautifulSoup(html, "html.parser")

    m = re.search(r'"lat"\s*:\s*(-?\d{1,3}\.\d+)\s*,\s*"lon(?:g)?"\s*:\s*(-?\d{1,3}\.\d+)', html)
    if not m:
        m = re.search(r"lat[=:]\s*(-?\d{1,3}\.\d+)[^0-9-]+lon[a-z]*[=:]\s*(-?\d{1,3}\.\d+)", html, re.I)
    if m:
        data["lat"] = float(m.group(1))
        data["lon"] = float(m.group(2))

    dest_meta = soup.find("meta", attrs={"name": "description"})
    if dest_meta and dest_meta.get("content"):
        data["destination_hint"] = dest_meta["content"][:200]

    return data


def fetch_marinetraffic_cloudscraper() -> dict:
    html = http_get(MT_URL, use_cloudscraper=True)
    data = parse_marinetraffic(html)
    if data.get("lat") is None:
        raise RuntimeError("MarineTraffic (cloudscraper): s'u gjetën koordinata")
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


# --------------------------------------------------------- SELENIUM ---
# Fallback opsional, fikur si parazgjedhje. Kërkon Chrome + chromedriver
# (shto hapa shtesë në workflow-n YAML nëse e aktivizon, shih udhëzimet).

def fetch_vesselfinder_selenium() -> dict:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(30)
        driver.get(VF_URL)
        time.sleep(6)  # lë kohë JS-it/Cloudflare challenge-it të përfundojë
        html = driver.page_source
    finally:
        driver.quit()

    data = parse_vesselfinder(html)
    if data.get("lat") is None:
        raise RuntimeError("VesselFinder (selenium): s'u gjetën koordinata")
    return data


# ------------------------------------------------------- VESSEL PIPELINE ---

def get_vessel_position() -> tuple[dict, str]:
    """Provon me radhë: cloudscraper -> VesselFinder, cloudscraper ->
    MarineTraffic, cloudscraper -> MyShipTracking, dhe (nëse aktivizuar)
    Selenium -> VesselFinder.
    Kthen (të dhëna, emri_burimit) ose ({}, "asnjë") nëse dështojnë të gjitha.
    """
    attempts = [
        ("VesselFinder/cloudscraper", fetch_vesselfinder_cloudscraper),
        ("MarineTraffic/cloudscraper", fetch_marinetraffic_cloudscraper),
        ("MyShipTracking/cloudscraper", fetch_myshiptracking_cloudscraper),
    ]
    if ENABLE_SELENIUM:
        attempts.append(("VesselFinder/selenium", fetch_vesselfinder_selenium))

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

    # --- Vessel (VesselFinder -> MarineTraffic -> Selenium) ---
    vf_data, source = get_vessel_position()
    if vf_data.get("lat") is not None and vf_data.get("lon") is not None:
        vessel_ok = True
        old_lat = prev_vessel.get("lat")
        old_lon = prev_vessel.get("lon")
        moved = True
        dist_km = None
        if old_lat is not None and old_lon is not None:
            dist_km = haversine_km(old_lat, old_lon, vf_data["lat"], vf_data["lon"])
            moved = dist_km > MOVE_THRESHOLD_KM
        if moved:
            maps_link = f"https://www.google.com/maps?q={vf_data['lat']},{vf_data['lon']}"
            dist_line = f"Lëvizje: {dist_km:.1f} km\n" if dist_km is not None else ""
            notifications.append(
                "🛳 <b>Anija ndryshoi pozicionin</b>\n"
                f"IMO: {IMO}\n"
                f"Burimi: {source}\n"
                f"Koordinata: {vf_data['lat']}, {vf_data['lon']}\n"
                f"{dist_line}"
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
            f"CIG dhe VesselFinder/MarineTraffic dështuan {status['consecutive_failures']} herë rresht.\n"
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
