#!/usr/bin/env python3
"""Monitoron statusin e dërgesës (CIG) dhe pozicionin e anijes (VesselFinder)."""

import json
import logging
import math
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- CONFIG ---

VIN = "WDCTG0CB2GJ240705"
IMO = "8606056"

CIG_URL = f"https://www.cigbooking.com/track/{VIN}"
VF_URL = f"https://www.vesselfinder.com/?imo={IMO}"

STATUS_FILE = Path("status.json")
MOVE_THRESHOLD_DEG = 0.1

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")


# ------------------------------------------------------------- HELPERS -----

def fetch(url: str, timeout: int = 20) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("status.json i pavlefshëm, rifillo nga zero")
    return {"cig": {}, "vessel": {}}


def save_status(data: dict) -> None:
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram(message: str) -> None:
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
        m = re.search(r"(-?\d{1,3}\.\d+)\D+(-?\d{1,3}\.\d+)", soup.get_text(" "))
    if m:
        data["lat"] = float(m.group(1))
        data["lon"] = float(m.group(2))

    return data


# ------------------------------------------------------------------ MAIN ---

def main() -> int:
    status = load_status()
    prev_cig = status.get("cig", {})
    prev_vessel = status.get("vessel", {})
    notifications = []

    # --- CIG ---
    try:
        resp = fetch(CIG_URL)
        cig_data = parse_cig(resp.text)
    except Exception as e:
        log.error(f"Gabim gjatë leximit të CIG: {e}")
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

    # --- VesselFinder ---
    try:
        resp = fetch(VF_URL)
        vf_data = parse_vesselfinder(resp.text)
    except Exception as e:
        log.error(f"Gabim gjatë leximit të VesselFinder: {e}")
        vf_data = {}

    if vf_data.get("lat") is not None and vf_data.get("lon") is not None:
        old_lat = prev_vessel.get("lat")
        old_lon = prev_vessel.get("lon")
        moved = True
        if old_lat is not None and old_lon is not None:
            dist = math.hypot(vf_data["lat"] - old_lat, vf_data["lon"] - old_lon)
            moved = dist > MOVE_THRESHOLD_DEG
        if moved:
            maps_link = f"https://www.google.com/maps?q={vf_data['lat']},{vf_data['lon']}"
            notifications.append(
                "🛳 <b>MV GMT ASTRO ndryshoi pozicionin</b>\n"
                f"IMO: {IMO}\n"
                f"Koordinata: {vf_data['lat']}, {vf_data['lon']}\n"
                f"Destinacioni: {vf_data.get('destination', '—')}\n"
                f"Statusi: {vf_data.get('nav_status', '—')}\n"
                f"Harta: {maps_link}"
            )
        status["vessel"] = {**prev_vessel, **vf_data}

    for msg in notifications:
        send_telegram(msg)

    if notifications:
        save_status(status)
        log.info(f"U dërguan {len(notifications)} njoftim(e), status.json u përditësua")
    else:
        log.info("Asnjë ndryshim")

    return 0


if __name__ == "__main__":
    sys.exit(main())
