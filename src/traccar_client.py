"""
Forwards GPS positions to Traccar via OsmAnd HTTP protocol.
Traccar endpoint: http://<server>:5055/?id=X&lat=Y&lon=Z

Sending runs on a background thread. send_position() only enqueues, because it
is called from openant's data-dispatch thread: blocking there stops page
decoding for every dog at once, and the retry path below can take ~19s — which
is exactly what happens when the WAN link is flaky in the forest.
"""

import logging
import queue
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

# Bounded so a long outage cannot grow without limit. When full the oldest
# position is dropped: recent track matters more than a stale backlog.
_QUEUE_MAX = 200

_queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_worker_lock = threading.Lock()
_dropped = 0

# Halsband som redan bekräftats finnas i Traccar (eller som vi själva just
# skapat) under den här körningen — namn vi känner till per device_id, så vi
# bara registrerar/döper om en gång och inte vid varje position.
_registered_names = {}
_registered_lock = threading.Lock()

# Första gången ett okänt device_id dök upp, för namnfristen nedan.
_first_seen = {}

# Hur länge vi väntar på Garmin-namnet innan ett namnlöst halsband ändå läggs
# in. device_id är Alphas platsnummer i hundlistan (96 + plats), inte hunden —
# lägger man till en hund i handenheten numreras listan om och samma hund
# kommer in under ett nytt nummer. Namnet är det enda stabila vi har, så det
# är värt att vänta de sekunder identifikationssidorna behöver: hinner vi
# skapa "Ny hund 98" först får laget två Traccar-enheter för samma hund, med
# historiken delad mellan dem.
_NAME_GRACE = 60.0

# Adressen till admin-API:et som senast svarade. Bryggan flyttar mellan
# hemmanätet och en delad uppkoppling i skogen, och LAN-adressen finns bara på
# det ena — därför en lista, och därför minns vi vilken som gick fram.
_admin_ok_url = None
_admin_url_lock = threading.Lock()

# När ingen admin-adress svarar slutar vi fråga en stund. Uppslagningen görs
# numera även när positionen gick fram, och utan paus hade varje position i
# skogen — där ingen av adresserna går att nå — kostat en timeout per adress
# på sändartråden och proppat kön bakom sig.
_ADMIN_PAUS = 300.0
_admin_nasta_forsok = 0.0


def _admin_urls(admin):
    urls = admin.get("urls")
    if urls:
        return [u for u in urls if u]
    return [admin["url"]] if admin.get("url") else []


def _admin_request(admin, method, path, **kwargs):
    """Anropar admin-API:et på första adressen som svarar.

    Den som gick fram provas först nästa gång, så vi inte betalar en timeout
    per position när bryggan står utanför hemmanätet.
    """
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
    sig mellan versioner, så vi tittar efter båda."""
    text = (r.text or "").lower()
    return "duplicate" in text or "unique" in text


def _devices(admin):
    r = _admin_request(admin, "GET", "/api/devices")
    r.raise_for_status()
    return r.json()


def _garmin_namn(d):
    """Vilket Garmin-namn enheten är kopplad till, enligt Traccar-attributet."""
    return ((d.get("attributes") or {}).get("garminName") or "").strip().lower()


# Alphan döper nya halsband till "Hundar", "Hundar 1", "Hundar 2" — bara en
# uppräkning. Tar man bort en hund återanvänds namnet till nästa. Ett sådant
# namn säger alltså ingenting om VILKEN hund det är, och får inte användas för
# att känna igen en hund som bytt plats i listan: då hade den nya hunden ärvt
# den gamlas Traccar-enhet och deras spår blandats ihop. Ger man hunden ett
# eget namn i Alphan blir namnet unikt och duger som identitet.
_AUTONAMN = re.compile(r"^(hundar|hund|dog|dogs)\s*\d*$", re.IGNORECASE)


def _ar_autonamn(name):
    return bool(_AUTONAMN.match((name or "").strip()))


def _ar_halsband(d):
    """Om enheten kan vara ett ANT+-halsband. Alphas platsnummer är 96 och
    uppåt, alltså högst tre siffror — jägarnas enheter heter jakt-<namn> eller
    bär ett långt id. Utan den här spärren hade namnsökningen nedan kunnat ta
    över en jägares enhet om en hund råkar heta samma sak i handenheten."""
    if (d.get("attributes") or {}).get("garminParkerad"):
        return True
    uid = str(d.get("uniqueId") or "")
    return uid.isdigit() and len(uid) <= 3


def _hitta_hund(devices, name):
    """Hundens enhet, sökt på namnet — för att känna igen den på en ny plats.

    Attributet garminName går före visningsnamnet så kopplingen håller även om
    enheten hunnit döpas om. Autonamn duger inte som identitet, se _AUTONAMN.
    """
    if _ar_autonamn(name):
        return None
    n = name.strip().lower()
    for d in devices:
        if _garmin_namn(d) == n:
            return d
    for d in devices:
        if _ar_halsband(d) and (d.get("name") or "").strip().lower() == n:
            return d
    return None


def _pa_platsen(devices, device_id):
    """Enheten som bär det här platsnumret just nu — för att känna igen en
    hund som fått ett nytt namn i Alphan utan att byta plats."""
    for d in devices:
        if str(d.get("uniqueId")) == str(device_id):
            return d
    return None


def _register_device(admin, device_id, name):
    """Skapar enheten i Traccar via admin-API:et. Körs bara när OsmAnd-porten
    svarat 400 på ett halsband vi inte känner igen sedan tidigare — dvs. aldrig
    på skräptrafik som redan avvisas där."""
    r = _admin_request(admin, "POST", "/api/devices",
                       json={"name": name, "uniqueId": str(device_id),
                             "attributes": {"garminName": name}})
    if r.status_code in (200, 201):
        logger.info("Registrerade nytt halsband %s i Traccar som '%s'", device_id, name)
        return True
    if _krock(r):
        # Redan skapad (t.ex. av en tidigare körning av bryggan) — inte
        # ett fel, bara att vi inte visste om det än.
        return True
    logger.warning("Kunde inte registrera halsband %s i Traccar: %s %s",
                   device_id, r.status_code, r.text[:200])
    return False


def _frigor_plats(admin, device_id, behall_id):
    """Parkerar enheten som blockerar platsnumret på ett tillfälligt id.

    Två hundar kan byta plats med varandra i Alphas lista. Då vill båda ha den
    andras nummer samtidigt, och Traccar tillåter inte två enheter med samma
    uniqueId — utan det här kommer ingen av dem loss. Den blockerande enheten
    flyttas undan och tar sitt riktiga nummer själv nästa gång den skickar.
    Det tillfälliga id:t hålls numeriskt så kartan fortsätter se den som hund.
    """
    for d in _devices(admin):
        if str(d.get("uniqueId")) != str(device_id) or d.get("id") == behall_id:
            continue
        # Markeras som parkerad: det tillfälliga id:t är för långt för att
        # kännas igen som ett halsband, och utan markeringen tappar vi den
        # här enheten när den sedan ska hitta tillbaka på sitt namn.
        d.setdefault("attributes", {})["garminParkerad"] = str(device_id)
        d["uniqueId"] = str(900000 + int(d["id"]))
        r = _admin_request(admin, "PUT", "/api/devices/" + str(d["id"]), json=d)
        if r.status_code in (200, 204):
            logger.info("Parkerade '%s' tillfälligt på %s för att frigöra plats %s",
                        d.get("name"), d["uniqueId"], device_id)
            return True
        logger.warning("Kunde inte frigöra plats %s: %s %s",
                       device_id, r.status_code, r.text[:200])
        return False
    return False


def _synka(admin, dev, device_id, name):
    """Ser till att enheten bär hundens Garmin-namn och nuvarande platsnummer.

    Namnet i Traccar ska alltid vara det som står i Alphan: döper laget om en
    hund i handenheten ska den heta så på kartan också.
    """
    parkerad = (dev.get("attributes") or {}).get("garminParkerad")
    if (str(dev.get("uniqueId")) == str(device_id)
            and (dev.get("name") or "") == name
            and _garmin_namn(dev) == name.strip().lower()
            and not parkerad):
        return True

    gammalt_id = dev.get("uniqueId")
    gammalt_namn = dev.get("name")
    dev["uniqueId"] = str(device_id)
    dev["name"] = name
    dev.setdefault("attributes", {})["garminName"] = name
    dev["attributes"].pop("garminParkerad", None)

    r = _admin_request(admin, "PUT", "/api/devices/" + str(dev["id"]), json=dev)
    if r.status_code not in (200, 204) and _krock(r):
        # Platsen är upptagen av en annan enhet — flytta undan den och
        # försök en gång till. Se _frigor_plats.
        if _frigor_plats(admin, device_id, dev["id"]):
            r = _admin_request(admin, "PUT", "/api/devices/" + str(dev["id"]), json=dev)

    if r.status_code in (200, 204):
        if str(gammalt_id) != str(device_id):
            logger.info("'%s' bytte plats i Alphas hundlista: %s -> %s "
                        "(samma Traccar-enhet, historiken följer med)",
                        name, gammalt_id, device_id)
        if gammalt_namn != name:
            logger.info("Halsband %s heter '%s' i Alphan — hette '%s' i Traccar",
                        device_id, name, gammalt_namn)
        return True
    logger.warning("Kunde inte uppdatera '%s' (plats %s): %s %s",
                   name, device_id, r.status_code, r.text[:200])
    return False


def _ensure_registered(admin, device_id, name, is_real_name):
    """Ser till att positionen har en Traccar-enhet som heter rätt.

    Två saker kan ha ändrats sedan sist, men i praktiken aldrig samtidigt:
    hunden kan ha bytt plats i Alphas lista (någon lade till eller tog bort
    ett halsband), eller fått ett nytt namn i handenheten. Därför söks hunden
    först på namnet — hittas den är det en omnumrering — och annars på
    platsnumret, vilket är en omdöpning.
    """
    global _admin_nasta_forsok
    with _registered_lock:
        if _registered_names.get(device_id) == name:
            return
    if time.time() < _admin_nasta_forsok:
        return

    try:
        if is_real_name:
            devices = _devices(admin)
            dev = _hitta_hund(devices, name) or _pa_platsen(devices, device_id)
            if dev is not None:
                klart = _synka(admin, dev, device_id, name)
            else:
                klart = _register_device(admin, device_id, name)
        else:
            klart = _register_device(admin, device_id, name)
    except requests.RequestException as e:
        _admin_nasta_forsok = time.time() + _ADMIN_PAUS
        logger.warning("Kunde inte nå Traccars admin-API för halsband %s: %s "
                       "— pausar registreringen i %d minuter",
                       device_id, e, int(_ADMIN_PAUS / 60))
        return

    _admin_nasta_forsok = 0.0
    if klart:
        with _registered_lock:
            _registered_names[device_id] = name


def _behover_namnfrist(device_id):
    """True så länge vi ännu väntar på Garmin-namnet för ett okänt halsband."""
    nu = time.time()
    forst = _first_seen.setdefault(device_id, nu)
    return (nu - forst) < _NAME_GRACE


def _deliver(server_url, device_id, lat, lon, extras, admin=None):
    params = {"id": device_id, "lat": lat, "lon": lon}
    if extras:
        params.update(extras)

    for attempt in range(3):
        try:
            r = requests.get(server_url, params=params, timeout=5)
            if admin:
                real_name = (extras or {}).get("dogName")
                if r.status_code == 400:
                    if real_name:
                        _ensure_registered(admin, device_id, real_name, True)
                    elif not _behover_namnfrist(device_id):
                        _ensure_registered(admin, device_id,
                                           "Ny hund {}".format(device_id), False)
                elif real_name:
                    # Positionen gick fram — men enheten kan bära platshållarens
                    # namn, eller tillhöra en annan hund sedan Alpha numrerat om
                    # listan. Kontrolleras en gång per halsband och namn (se
                    # _registered_names), inte per position.
                    _ensure_registered(admin, device_id, real_name, True)
            r.raise_for_status()
            logger.debug("Sent position for %s: %.6f, %.6f", device_id, lat, lon)
            return
        except requests.RequestException as e:
            if attempt < 2:
                logger.warning("Traccar send failed (attempt %d): %s — retrying", attempt + 1, e)
                time.sleep(2)
            else:
                logger.warning("Failed to send to Traccar: %s", e)


def _run():
    while True:
        item = _queue.get()
        try:
            _deliver(*item)
        except Exception:
            # Never let the worker die — positions would stop silently.
            logger.exception("Unexpected error delivering position")
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
    """Queue a position for delivery. Returns immediately — never blocks the caller.

    admin, if given, is {"urls": [<Traccar-adresser med adminAPI>], "auth": (user, pass)} —
    används bara för att auto-registrera/döpa om okända halsband, aldrig för
    själva positionsleveransen (den går alltid via OsmAnd-porten som förut)."""
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
            logger.warning("Traccar backlog full — dropped %d oldest position(s)", _dropped)
    except queue.Empty:
        pass

    try:
        _queue.put_nowait(item)
    except queue.Full:
        pass


def pending() -> int:
    """Positions waiting to be delivered — useful for diagnosing a stalled link."""
    return _queue.qsize()
