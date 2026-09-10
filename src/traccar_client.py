"""
Skickar hundpositioner till Traccar via OsmAnd-protokollet.
Endpoint: http://<server>:5055/?id=X&lat=Y&lon=Z

Sändningen ligger på en egen tråd. send_position() lägger bara i kön, för den
anropas från openants avkodningstråd: blockerar man där stannar avkodningen
för alla hundar samtidigt, och omförsöken nedan kan ta ~19 s när WAN-länken
krånglar i skogen.

Hundens id i Traccar härleds ur namnet — "hund-sampo". Det betyder att samma
hund får samma enhet oavsett vilken handenhet eller brygga som hör den, och
att två bryggor som hör samma hund fyller på samma spår i stället för att
slåss om en enhet. Priset: hunden måste ha ett eget namn i Alphan. Alphans
egna uppräkningsnamn ("Hundar 3") återanvänds mellan hundar och duger inte.
"""

import logging
import queue
import re
import threading
import time
import unicodedata

import requests

logger = logging.getLogger(__name__)

# Bounded so a long outage cannot grow without limit. When full the oldest
# position is dropped: recent track matters more than a stale backlog.
_QUEUE_MAX = 200

_queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_worker_lock = threading.Lock()
_dropped = 0

# Enheter vi redan bekräftat i Traccar den här körningen — id -> namnet vi
# senast satte. Så vi bara slår mot admin-API:et en gång per hund, inte per
# position.
_registered = {}
_registered_lock = threading.Lock()

# Adressen till admin-API:et som senast svarade. Bryggan flyttar mellan
# hemmanätet och en delad uppkoppling i skogen, och LAN-adressen finns bara på
# det ena — därför en lista, och därför minns vi vilken som gick fram.
_admin_ok_url = None
_admin_url_lock = threading.Lock()

# När ingen admin-adress svarar slutar vi fråga en stund, så en oåtkomlig
# server inte kostar en timeout per position på sändartråden.
_ADMIN_PAUS = 300.0
_admin_nasta_forsok = 0.0

# Alphans egna uppräkningsnamn: "Hundar", "Hundar 1", "Hundar 2". De är unika
# för stunden men återanvänds över tid — tar man bort en hund får nästa man
# lägger till samma namn. Ett sådant namn säger inte vilken hund det är, så
# det duger inte som id.
_AUTONAMN = re.compile(r"^(hundar|hund|dog|dogs)\s*\d*$", re.IGNORECASE)


def ar_autonamn(name):
    return bool(_AUTONAMN.match((name or "").strip()))


def _slug(name):
    """Namnet till en id-vänlig form: gemener, ASCII, bindestreck. 'Måns' ->
    'mans'. Visningsnamnet i Traccar behåller å, ä, ö — det är bara nyckeln
    som förenklas, så den är förutsägbar i URL:er och loggrader."""
    s = name.strip().lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"),
                 ("ø", "o"), ("æ", "ae"), ("ü", "u")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def hund_id(name):
    """Traccar-id för en hund, härlett ur Garmin-namnet — 'hund-sampo'.

    Returnerar None för namnlösa halsband och för Alphans uppräkningsnamn:
    inget av dem pekar ut en bestämd hund, och den som skickade på ett sådant
    id hade fått olika hundars spår att blandas ihop.
    """
    if not name or name.startswith("Dog ") or ar_autonamn(name):
        return None
    s = _slug(name)
    return "hund-" + s if s else None


def _admin_urls(admin):
    urls = admin.get("urls")
    if urls:
        return [u for u in urls if u]
    return [admin["url"]] if admin.get("url") else []


def _admin_request(admin, method, path, **kwargs):
    """Anropar admin-API:et på första adressen som svarar. Den som gick fram
    provas först nästa gång, så vi inte betalar en timeout per position när
    bryggan står utanför hemmanätet."""
    global _admin_ok_url
    urls = _admin_urls(admin)
    if not urls:
        raise requests.RequestException("ingen admin-adress konfigurerad")

    with _admin_url_lock:
        senast = _admin_ok_url
    ordnade = ([senast] if senast in urls else []) + [u for u in urls if u != senast]

    fel = None
    for url in ordnade:
        try:
            r = requests.request(method, url + path, auth=admin["auth"], timeout=5, **kwargs)
        except requests.RequestException as e:
            fel = e
            continue
        with _admin_url_lock:
            if _admin_ok_url != url:
                _admin_ok_url = url
                logger.info("Traccars admin-API nås via %s", url)
        return r
    raise fel or requests.RequestException("ingen admin-adress svarade")


def _krock(r):
    """Traccar avvisar två enheter med samma uniqueId — ordalydelsen skiljer
    mellan versioner, så vi tittar efter båda. Att det redan finns en enhet
    med id:t är inget fel här: en annan brygga kan ha hunnit skapa den."""
    text = (r.text or "").lower()
    return "duplicate" in text or "unique" in text


def _devices(admin):
    r = _admin_request(admin, "GET", "/api/devices")
    r.raise_for_status()
    return r.json()


def _ensure_device(admin, unique_id, name):
    """Ser till att enheten finns och att den heter det Garmin säger.

    Görs en gång per hund och körning (se _registered). Skapar enheten om den
    saknas, och rättar visningsnamnet om någon döpt om den i Traccar — namnen
    ska komma från handenheten, ingen annanstans.
    """
    global _admin_nasta_forsok
    with _registered_lock:
        if _registered.get(unique_id) == name:
            return
    if time.time() < _admin_nasta_forsok:
        return

    try:
        befintlig = next((d for d in _devices(admin)
                          if str(d.get("uniqueId")) == unique_id), None)
        if befintlig is None:
            r = _admin_request(admin, "POST", "/api/devices",
                               json={"name": name, "uniqueId": unique_id})
            if r.status_code in (200, 201):
                logger.info("La till '%s' i Traccar (%s)", name, unique_id)
            elif _krock(r):
                pass
            else:
                logger.warning("Kunde inte lägga till '%s' (%s): %s %s",
                               name, unique_id, r.status_code, r.text[:200])
                return
        elif (befintlig.get("name") or "") != name:
            gammalt = befintlig.get("name")
            befintlig["name"] = name
            r = _admin_request(admin, "PUT", "/api/devices/" + str(befintlig["id"]),
                               json=befintlig)
            if r.status_code in (200, 204):
                logger.info("Enheten %s hette '%s' i Traccar — Garmin säger '%s', "
                            "rättat", unique_id, gammalt, name)
            else:
                logger.warning("Kunde inte rätta namnet på %s: %s %s",
                               unique_id, r.status_code, r.text[:200])
                return
    except requests.RequestException as e:
        _admin_nasta_forsok = time.time() + _ADMIN_PAUS
        logger.warning("Nådde inte Traccars admin-API för '%s': %s — pausar "
                       "registreringen i %d min", name, e, int(_ADMIN_PAUS / 60))
        return

    _admin_nasta_forsok = 0.0
    with _registered_lock:
        _registered[unique_id] = name


def _deliver(server_url, device_id, lat, lon, extras, admin=None):
    params = {"id": device_id, "lat": lat, "lon": lon}
    if extras:
        params.update(extras)

    for attempt in range(3):
        try:
            r = requests.get(server_url, params=params, timeout=5)
            # dogName finns bara för hundar. Är den satt ser vi till att
            # enheten finns och heter rätt — cachen i _ensure_device gör att
            # det bara blir ett riktigt anrop per hund och körning.
            if admin and (extras or {}).get("dogName"):
                _ensure_device(admin, device_id, extras["dogName"])
            r.raise_for_status()
            logger.debug("Skickade position för %s: %.6f, %.6f", device_id, lat, lon)
            return
        except requests.RequestException as e:
            if attempt < 2:
                logger.warning("Traccar-sändning misslyckades (försök %d): %s — försöker igen",
                               attempt + 1, e)
                time.sleep(2)
            else:
                logger.warning("Kunde inte skicka till Traccar: %s", e)


def _run():
    while True:
        item = _queue.get()
        try:
            _deliver(*item)
        except Exception:
            logger.exception("Oväntat fel vid leverans av position")
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="traccar-sender", daemon=True)
            _worker.start()


def send_position(server_url: str, device_id: str, lat: float, lon: float,
                   extras: dict = None, admin: dict = None):
    """Köar en position för leverans. Återvänder direkt — blockerar aldrig.

    admin, om satt, är {"urls": [...], "auth": (user, pass)} och används bara
    för att skapa/rätta enheter, aldrig för själva positionsleveransen (den
    går alltid via OsmAnd-porten)."""
    global _dropped
    _ensure_worker()
    item = (server_url, device_id, lat, lon, extras, admin)
    try:
        _queue.put_nowait(item)
        return
    except queue.Full:
        pass

    try:
        _queue.get_nowait()
        _queue.task_done()
        _dropped += 1
        if _dropped % 50 == 1:
            logger.warning("Traccar-kön full — kastade %d äldsta position(er)", _dropped)
    except queue.Empty:
        pass

    try:
        _queue.put_nowait(item)
    except queue.Full:
        pass


def pending() -> int:
    """Positioner som väntar på leverans — användbart när länken hänger."""
    return _queue.qsize()
