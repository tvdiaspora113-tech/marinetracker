# Udhëzime testimi — Shipment Tracker

## 1. Çfarë ndryshoi

- **CIG**: funksionaliteti ekzistues u ruajt plotësisht (asnjë ndryshim në logjikën e parsimit).
- **VesselFinder**: tani provohet fillimisht me `cloudscraper` (jo `requests` të thjeshtë), që bën bypass të Cloudflare JS challenge bazë.
- **MarineTraffic**: shtuar si alternativë e dytë nëse VesselFinder dështon.
- **Selenium**: shtuar si fallback i tretë, por **i fikur si parazgjedhje** (siç kërkove). Aktivizohet me `ENABLE_SELENIUM=true`.
- **Retry logic**: çdo burim provon deri në 3 herë me 30 sekonda pauzë mes tyre.
- **Logging**: gjithçka shkruhet edhe në `tracker.log` (jo vetëm në stdout). Workflow-i e ngarkon si artifact të GitHub Actions pas çdo run.
- **Heshtje njoftimesh 22:00–06:00**: kontrollohet me orën lokale (`TRACKER_TZ`, parazgjedhje `Europe/Tirane`). Njoftimet normale (ndryshim statusi / pozicioni) shtyhen; njoftimi i urgjencës e injoron këtë.
- **Haversine**: lëvizja e anijes tani matet në km (jo në gradë si më parë), me prag `MOVE_THRESHOLD_KM` (parazgjedhje 5 km).
- **Historik 10 përditësimesh**: `status.json` tani ka një fushë `"history"` me deri në 10 hyrjet e fundit (timestamp, status CIG, koordinata, burimi).
- **Njoftim urgjence**: nëse **të dyja** burimet (CIG dhe anija) dështojnë në të njëjtin run, 2 herë rresht ose më shumë, dërgohet një mesazh urgjence në Telegram — edhe gjatë orëve të heshtjes.

## 2. Instalimi lokal (para se ta vësh në GitHub Actions)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Test i shpejtë pa Telegram

Për të parë nëse parsimi funksionon pa dërguar njoftime reale, thjesht mos vendos `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` — skripti do të vazhdojë normalisht dhe do të bëjë vetëm log paralajmërimi "s'dërgoj njoftim".

```bash
python tracker.py
cat tracker.log
cat status.json
```

Kontrollo në `status.json`:
- `cig.current_status` — a u mor statusi nga CIG?
- `vessel.lat` / `vessel.lon` / `vessel.source` — a u mor pozicioni, dhe nga cili burim (VesselFinder apo MarineTraffic)?
- `history` — a u shtua një hyrje e re?

## 4. Testim i pjesëve individuale (REPL)

```python
from tracker import fetch_cig, fetch_vesselfinder_cloudscraper, fetch_marinetraffic_cloudscraper

print(fetch_cig())
print(fetch_vesselfinder_cloudscraper())
print(fetch_marinetraffic_cloudscraper())
```

**E rëndësishme:** VesselFinder dhe MarineTraffic ndryshojnë shpesh mbrojtjen e tyre anti-bot. `cloudscraper` zgjidh shumë raste të Cloudflare "JS challenge" bazë, por **jo domosdoshmërisht çdo variant** (p.sh. Turnstile me verifikim sjelljeje mund të mos kalohet). Nëse `fetch_vesselfinder_cloudscraper()` vazhdon të dështojë pas testeve:

1. Kontrollo manualisht në browser nëse faqja hapet normalisht (jo pas login/captcha).
2. Nëse funksionon në browser por jo në script, ka gjasa të jetë Turnstile/mbrojtje e avancuar — në atë rast opsioni më i qëndrueshëm është Selenium (Seksioni 5) ose MarineTraffic si burim kryesor.
3. Nëse struktura HTML e faqes ka ndryshuar, rregullo `parse_vesselfinder()` / `parse_marinetraffic()` — më lehtë: hap faqen, "View Page Source", kërko `lat`/`lon`/`coordinate` dhe përshtat shprehjen regex.

## 5. Aktivizimi i Selenium (opsional, vetëm nëse cloudscraper s'mjafton)

Në `tracker.yml`, hiq komentet te hapat "Install Chrome" dhe "Install selenium", shto `selenium` në `requirements.txt`, dhe vendos:

```yaml
ENABLE_SELENIUM: "true"
```

Selenium është më i ngadaltë (hap një browser real) por më i qëndrueshëm kundër mbrojtjeve JS të forta.

## 6. Testimi i heshtjes së natës

```python
from tracker import is_quiet_hours
from datetime import datetime
from zoneinfo import ZoneInfo

tz = ZoneInfo("Europe/Tirane")
print(is_quiet_hours(datetime(2026, 8, 31, 23, 0, tzinfo=tz)))  # True
print(is_quiet_hours(datetime(2026, 8, 31, 10, 0, tzinfo=tz)))  # False
```

## 7. Testimi i njoftimit të urgjencës

Fshi/prish përkohësisht URL-të (p.sh. shto një gabim shtypi te `CIG_URL`) dhe xhiro skriptin **dy herë rresht** — pas herës së dytë duhet të shohësh mesazhin 🚨 në Telegram edhe nëse je brenda orëve 22:00–06:00.

## 8. Vendosja në GitHub Actions

1. Ngarko `tracker.py`, `requirements.txt`, `tracker.yml` (te `.github/workflows/tracker.yml`) në repo.
2. Shto `TELEGRAM_TOKEN` dhe `TELEGRAM_CHAT_ID` si **Repository Secrets**.
3. Xhiro manualisht një herë me "Run workflow" (workflow_dispatch) para se ta lësh të presë cron-in çdo 2 orë.
4. Kontrollo tab-in "Actions" → run i fundit → artifact `tracker-log` për detaje nëse diçka dështon.

## 9. Kufizime për t'i mbajtur në mend

- Sitet si VesselFinder/MarineTraffic ndryshojnë shpesh HTML-in dhe mbrojtjen anti-bot; skripti mund të kërkojë rregullime periodike të regex-eve të parsimit.
- `cloudscraper` nuk garanton 100% bypass — nëse të dyja burimet (VesselFinder + MarineTraffic) dështojnë vazhdimisht, Selenium është rruga tjetër më e sigurt.
- IP-të e GitHub Actions runners ndonjëherë bllokohen më shpesh nga shërbimet anti-bot sesa IP-të "normale" shtëpiake — nëse ke probleme këmbëngulëse vetëm në CI (jo lokalisht), kjo mund të jetë shkaku.
