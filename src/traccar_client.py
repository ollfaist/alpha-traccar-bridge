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


def _register_device(admin, device_id, name):
    """Skapar enheten i Traccar via admin-API:et. Körs bara när OsmAnd-porten
    svarat 400 på ett halsband vi inte känner igen sedan tidigare — dvs. aldrig
    på skräptrafik som redan avvisas där."""
    r = _admin_request(admin, "POST", "/api/devices",
                       json={"name": name, "uniqueId": str(device_id)})
    if r.status_code in (200, 201):
        logger.info("Registrerade nytt halsband %s i Traccar som '%s'", device_id, name)
        return True
    if "duplicate" in r.text.lower() or "unique" in r.text.lower():
        # Redan skapad (t.ex. av en tidigare körning av bryggan) — inte
        # ett fel, bara att vi inte visste om det än.
        return True
    logger.warning("Kunde inte registrera halsband %s i Traccar: %s %s",
                   device_id, r.status_code, r.text[:200])
    return False


def _claim_by_name(admin, device_id, name):
    """Låter hundens befintliga Traccar-enhet ta över det nya platsnumret.

    Returnerar True om enheten nu pekar på device_id, False om försöket
    misslyckades, och None om ingen enhet heter så (då ska den skapas).

    Alternativet — att skapa en ny enhet per platsnummer — ger en ny hund i
    kartan varje gång Alphas lista numreras om, och delar upp spåret på flera
    enheter. Här följer identiteten hunden i stället för platsen.
    """
    for d in _devices(admin):
        if (d.get("name") or "").strip().lower() != name.strip().lower():
            continue
        if str(d.get("uniqueId")) == str(device_id):
            return True
        gammalt = d.get("uniqueId")
        d["uniqueId"] = str(device_id)
        r = _admin_request(admin, "PUT", "/api/devices/" + str(d["id"]), json=d)
        if r.status_code not in (200, 204) and _krock(r):
            # Platsen är upptagen av en annan enhet — flytta undan den och
            # försök en gång till. Se _frigor_plats.
            if _frigor_plats(admin, device_id, d["id"]):
                r = _admin_request(admin, "PUT", "/api/devices/" + str(d["id"]), json=d)
        if r.status_code in (200, 204):
            logger.info("'%s' bytte plats i Alphas hundlista: %s -> %s "
                        "(samma Traccar-enhet, historiken följer med)",
                        name, gammalt, device_id)
            return True
        logger.warning("Kunde inte flytta '%s' från %s till %s: %s %s",
                       name, gammalt, device_id, r.status_code, r.text[:200])
        return False
    return None


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


def _rename_device(admin, device_id, name):
    """Döper om enheten som redan bär det här platsnumret — i praktiken den
    platshållare vi själva skapade när namnfristen gick ut. Returnerar False
    om ingen sådan enhet finns."""
    r = _admin_request(admin, "GET", "/api/devices", params={"uniqueId": str(device_id)})
    r.raise_for_status()
    # Filtrera själva: uniqueId-parametern stöds inte av alla Traccar-versioner
    # och en ofiltrerad lista hade annars döpt om fel enhet.
    traffar = [d for d in r.json() if str(d.get("uniqueId")) == str(device_id)]
    if not traffar:
        return False
    dev = traffar[0]
    if (dev.get("name") or "") == name:
        return True
    gammalt = dev.get("name")
    dev["name"] = name
    r = _admin_request(admin, "PUT", "/api/devices/" + str(dev["id"]), json=dev)
    if r.status_code in (200, 204):
        logger.info("Döpte om halsband %s från '%s' till '%s'", device_id, gammalt, name)
        return True
    logger.warning("Kunde inte döpa om halsband %s till '%s': %s %s",
                   device_id, name, r.status_code, r.text[:200])
    return False


def _ensure_registered(admin, device_id, name, is_real_name):
    """Ser till att positionen har en Traccar-enhet att landa i.

    Med ett riktigt Garmin-namn letar vi först efter hunden på namnet och
    flyttar den enheten hit; först om ingen sådan finns skapas en ny. Utan
    namn (namnfristen har gått ut) skapas en platshållare som döps om så fort
    identifikationssidorna kommit in.
    """
    with _registered_lock:
        if _registered_names.get(device_id) == name:
            return

    try:
        if is_real_name:
            klart = _claim_by_name(admin, device_id, name)
            if klart is None:
                # Ingen hund heter så. Bär platsnumret redan en enhet är det
                # platshållaren från namnfristen — döp om den i stället för att
                # lägga en andra enhet på samma halsband.
                klart = (_rename_device(admin, device_id, name)
                         or _register_device(admin, device_id, name))
        else:
            klart = _register_device(admin, device_id, name)
    except requests.RequestException as e:
        logger.warning("Kunde inte nå Traccars admin-API för halsband %s: %s", device_id, e)
        return

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
