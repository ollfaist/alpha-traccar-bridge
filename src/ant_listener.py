"""
ANT+ listener for Garmin Alpha 100 dog tracking data.

Protocol verified against live Alpha 100 hardware.
Byte layout confirmed via raw capture session and the ANT+ Tracker Device Profile.
"""

import logging
import os
import threading
import time
from openant.easy.node import Node
from openant.easy.channel import Channel

logger = logging.getLogger(__name__)

# ANT+ Managed Network Key — licensed under the ANT+ Adopter's Agreement, must
# not be committed to source. Set via env var; get your own from thisisant.com.
_NETWORK_KEY_HEX = os.environ.get("ANT_NETWORK_KEY", "")
if not _NETWORK_KEY_HEX:
    raise RuntimeError(
        "ANT_NETWORK_KEY env var not set. Provide the ANT+ Managed Network Key "
        "as 16 hex chars, e.g. ANT_NETWORK_KEY=XXXXXXXXXXXXXXXX"
    )
NETWORK_KEY = [int(_NETWORK_KEY_HEX[i:i + 2], 16) for i in range(0, 16, 2)]

# Namnen hamnar i event-strängen ("Treed dist=843m") och kartan läser första
# ordet. "Not connected" med mellanslag blev därför "not" på andra sidan och
# matchade ingenting — därav ett enda ord.
SITUATIONS = {
    0: "Sitting",
    1: "Moving",
    2: "Pointed",
    3: "Treed",
    4: "Unknown",
    7: "NotConnected",
}

BATTERY_STATUS = {0: "New", 1: "Good", 2: "Ok", 3: "Low", 4: "Critical"}

sync_buffer = {}


def _semi_to_deg(semi):
    if semi > 0x7FFFFFFF:
        semi -= 0x100000000
    return (float(semi) / 2147483648.0) * 180.0


def _avkoda_namn(rador):
    """Hundnamnet ur de tio råa bytarna från identifikationssidorna.

    Tidigare avkodades varje halva för sig som ASCII med errors="ignore", och
    då försvann å, ä och ö tyst — "Måns" blev "Mns". Två saker rättas här:
    halvorna slås ihop innan avkodning, och teckenuppsättningen tillåter mer
    än sju bitar. Skulle Alphan skicka UTF-8 kan ett å ligga med sin ena byte
    i första halvan och sin andra i den andra, och per-halva-avkodning hade
    förstört det oavsett teckenuppsättning.

    UTF-8 provas först och latin-1 som reserv. Svensk latin-1-text är nästan
    aldrig giltig UTF-8 (0xE5 0xE4 saknar de fortsättningsbytes UTF-8 kräver),
    så ordningen är säker — och latin-1 kan aldrig misslyckas, så vi står
    aldrig utan ett namn.
    """
    rensad = rador.strip(b"\x00")
    try:
        return rensad.decode("utf-8")
    except UnicodeDecodeError:
        return rensad.decode("iso-8859-1")


def _update_name(asset_id):
    p1 = sync_buffer.get(str(asset_id) + "_name1")
    p2 = sync_buffer.get(str(asset_id) + "_name2")
    if p1 is None or p2 is None:
        return
    # Both identification pages seen. Mark it resolved even when the name is
    # blank, or an unnamed dog would keep the request loop running forever.
    sync_buffer[str(asset_id) + "_name_done"] = True
    name = _avkoda_namn(p1 + p2).strip()
    if not name:
        return
    tidigare = sync_buffer.get(str(asset_id) + "_name")
    sync_buffer[str(asset_id) + "_name"] = name
    _namn_tid[asset_id] = time.time()
    if tidigare != name:
        logger.info("Plats %d heter %s%s", asset_id, name,
                    "" if tidigare is None else " (hette %s)" % tidigare)


# Identification pages (0x10/0x11, carrying the Garmin dog name) are only sent by
# the Alpha in response to a data-request page (0x46). Without asking we never see
# the name and stay stuck on "Dog N". Command type 4 requests the identification
# set for every asset, so one request covers all pending dogs.
_REQUEST_ID_PAYLOAD = [0x46, 0xFF, 0xFF, 0xFF, 0xFF, 0x04, 0x10, 0x04]
_NAME_REQUEST_INTERVAL = 5

# Ett namn gäller bara en stund. ANT+-profilen pekar ut en hund med sin plats
# i handenhetens lista, och den listan numreras om när en hund läggs till eller
# tas bort. Platsen är alltså inte hunden — bara namnet är det.
#
# Jakten 12 sep 2026: Alphan flyttade om listan 09:35. "Hundar 8" gled från
# plats 9 till plats 8 och sedan till plats 2, och eftersom bryggan aldrig
# frågade om namnen igen fortsatte hon rapportera som "Hundar 6" och sedan som
# "Hundar" — samma koordinat, tre identiteter. Två platser skrev samtidigt till
# hund-hundar, som därmed hoppade 20,9 km mellan två rapporter. Tre av dagens
# Traccar-enheter fick två hundar var, och spåren blev obrukbara.
# ANT_IDLOG=1 loggar identifikationssidorna rått. Till skillnad från
# ANT_DEBUG, som loggar varenda ram (~16/s), rör det här bara de sidor som
# kommer när vi frågar efter namn — någon rad per hund och minut.
#
# Vad vi letar efter: profilen kallar byte 2 i 0x10 "färg" och i 0x11 "typ",
# och vi kastar båda. Är det i själva verket hundens id i handenheten (0-19,
# det du anger när du lägger till hunden) har vi en identitet som varken
# glider när en hund tas bort eller byter namn när Garmin döper om. Hundar 8
# har id 15 — dyker 0x0F upp i någon av byten är frågan besvarad.
_ID_LOGG = os.environ.get("ANT_IDLOG") == "1"

# Två skilda siffror, och det är skillnaden mellan dem som gör susen.
#
# Vi FRÅGAR ofta: namnen kostar en kvittens var 15:e sekund och Alphan svarar
# inom en sekund, så en plats som bytt hund rättar sig nästan direkt.
#
# Vi SLUTAR LITA först efter 90 s. Hade samma siffra styrt båda skulle varje
# förnyelse ha inneburit ett kort hål där ingen hund fick skickas — hundarna
# hade blinkat bort ett par sekunder var 15:e sekund. Nu fylls namnen på i
# bakgrunden utan att någon märker det, och tystnaden sparas till de lägen
# där vi faktiskt är osäkra: ny plats, tystnad plats, krock, namnbyte.
_NAMN_FRAGA = 15.0          # be om namnet på nytt så här ofta
_NAMN_TTL = 90.0            # äldre bekräftelse än så litar vi inte på
_PLATS_TYST = 15.0          # en plats som inte hörts på så länge har lämnat listan
_namn_tid = {}              # plats -> när namnet senast bekräftades
_aktiva_platser = set()     # platser som rapporterat position den här omgången
_plats_sedd = {}            # plats -> när den senast rapporterade

_pending_names = set()
_pending_lock = threading.Lock()
_name_thread = None
_active_channel = None

# När den sista ANT+-sidan kom in, och den nod som tar emot dem just nu.
#
# En ANT+-kanal som slutat spåra sin master säger inte till. openant anropar
# aldrig on_close (sök i paketet — träffarna är noll), vi sätter ingen
# söktidsgräns, och återanslutningen i start() körs bara om node.start()
# returnerar eller kastar. En kanal som stängts, t.ex. för att Alphan var
# avstängd när bryggan startade, lämnar alltså processen vid liv med
# "listening" i loggen och ingenting som kommer in — bara en omstart hjälpte.
# Det var det som hände på jakten 7 sep: laget fick starta om Pi:n mitt i
# jakten, och sedan gick 2942 positioner in i rad utan ett enda avbrott.
_last_page = 0.0
_active_node = None
_SILENCE_LIMIT = 1200.0    # 20 min utan en enda sida = bygg om kanalen
_watchdog_thread = None


def namn_farskt(asset_id):
    """Är platsens namn bekräftat nyligen nog att lita på?"""
    nar = _namn_tid.get(int(asset_id))
    return nar is not None and (time.time() - nar) <= _NAMN_TTL


def _bor_fragas(asset_id):
    """Är det dags att be Alphan bekräfta namnet igen?"""
    nar = _namn_tid.get(int(asset_id))
    return nar is None or (time.time() - nar) > _NAMN_FRAGA


def glom_namnen(anledning):
    """Kasta alla namnbekräftelser och be om nya.

    Namnen finns kvar i sync_buffer — de behövs för loggen — men de räknas
    inte längre som bekräftade, så inga positioner publiceras på dem förrän
    Alphan svarat. Hellre en hund som saknas i en halv minut än en hund som
    ritar sitt spår ovanpå en annans."""
    if not _namn_tid:
        return
    logger.info("Glömmer namnen och frågar om: %s", anledning)
    _namn_tid.clear()
    with _pending_lock:
        for plats in list(_aktiva_platser):
            sync_buffer.pop(str(plats) + "_name_done", None)
            sync_buffer.pop(str(plats) + "_name1", None)
            sync_buffer.pop(str(plats) + "_name2", None)
            _pending_names.add(plats)


def _se_plats(asset_id):
    """Bokför att en plats hörts av, och märk om listan ändrat form.

    Två saker avslöjar en omnumrering. Den ena är att en plats tillkommer.
    Den andra — den som faktiskt hände 12 sep — är att en plats TYSTNAR:
    tas en hund bort ur listan glider alla under henne ner ett steg, och de
    platserna var redan kända. Bara "ny plats" hade missat det helt."""
    nu = time.time()
    _plats_sedd[asset_id] = nu

    tystnade = [p for p, t in _plats_sedd.items()
                if p != asset_id and (nu - t) > _PLATS_TYST]
    for p in tystnade:
        _plats_sedd.pop(p, None)
        _aktiva_platser.discard(p)
    if tystnade:
        glom_namnen("plats %s tystnade" %
                    ", ".join(str(p) for p in sorted(tystnade)))

    if asset_id in _aktiva_platser:
        return
    _aktiva_platser.add(asset_id)
    if len(_aktiva_platser) > 1:
        glom_namnen("plats %d tillkom" % asset_id)


def _maybe_request_name(channel, idx):
    """Mark an asset as needing identification. Deliberately does not send.

    send_acknowledged_data() waits for a transfer event with no overall timeout,
    so calling it here — on the ANT+ dispatch thread — hangs all decoding
    indefinitely if the handheld goes out of range mid-transfer. The actual send
    happens on _name_request_loop instead.
    """
    if channel is None:
        return
    # name_done betyder "båda sidorna har kommit in", inte "namnet gäller för
    # alltid". Har bekräftelsen hunnit bli gammal frågar vi om igen — det var
    # den saknade förnyelsen som lät ett namn överleva en omnumrering.
    if sync_buffer.get(str(idx) + "_name_done") and not _bor_fragas(idx):
        return
    with _pending_lock:
        _pending_names.add(idx)


def _name_request_loop():
    while True:
        time.sleep(_NAME_REQUEST_INTERVAL)
        channel = _active_channel
        if channel is None:
            continue
        with _pending_lock:
            _pending_names.difference_update(
                {i for i in _pending_names
                 if sync_buffer.get(str(i) + "_name_done") and not _bor_fragas(i)}
            )
            pending = sorted(_pending_names)
        if not pending:
            continue
        try:
            channel.send_acknowledged_data(_REQUEST_ID_PAYLOAD)
            logger.debug("Requested identification, pending assets: %s", pending)
        except Exception as exc:
            logger.debug("Identification request failed: %s", exc)


def _ensure_name_thread():
    global _name_thread
    if _name_thread is None or not _name_thread.is_alive():
        _name_thread = threading.Thread(
            target=_name_request_loop, name="ant-name-request", daemon=True)
        _name_thread.start()


def _tystnadsvakt():
    """Bygger om kanalen när inget hörts på en stund.

    Alphan är avstängd mellan jakterna, så tystnad i sig är normalt — men en
    ombyggnad kostar ingenting när det inte finns något att ta emot, och är
    det enda som får tillbaka en kanal som slutat lyssna. Hellre en rad i
    loggen då och då än en jakt utan hundar.
    """
    while True:
        time.sleep(30)
        node = _active_node
        if node is None or _last_page == 0.0:
            continue
        if time.time() - _last_page < _SILENCE_LIMIT:
            continue
        logger.warning("Inga ANT+-sidor på %d minuter — bygger om kanalen "
                       "(normalt när Alphan är avstängd)", max(1, int(_SILENCE_LIMIT / 60)))
        try:
            node.stop()        # får node.start() att returnera i start()
        except Exception as exc:
            logger.warning("Kunde inte stoppa ANT+-noden: %s", exc)


_idtabell_thread = None


def _ensure_idtabell():
    global _idtabell_thread
    if not _ID_LOGG:
        return
    if _idtabell_thread is None or not _idtabell_thread.is_alive():
        _idtabell_thread = threading.Thread(
            target=_idtabell_loop, name="ant-idtabell", daemon=True)
        _idtabell_thread.start()


def _ensure_watchdog():
    global _watchdog_thread
    if _watchdog_thread is None or not _watchdog_thread.is_alive():
        _watchdog_thread = threading.Thread(
            target=_tystnadsvakt, name="ant-tystnadsvakt", daemon=True)
        _watchdog_thread.start()


def _logga_idsida(page, data, idx):
    """Skriver ut en identifikationssida rå. Tyst om ANT_IDLOG inte är satt."""
    if not _ID_LOGG:
        return
    hela = " ".join("%02X" % b for b in data[:8])
    logger.info("IDSIDA 0x%02X plats=%-2d byte1=0x%02X byte2=0x%02X(%3d) "
                "namnbytes=%s  text=%r  |  hela: %s",
                page, idx, data[1], data[2], data[2],
                " ".join("%02X" % b for b in data[3:8]),
                _avkoda_namn(bytes(data[3:8])), hela)


def _idtabell_loop():
    """Var 30:e sekund: vilka platser som är igång och vad de heter.

    Det är den här raden som visar själva omnumreringen. Tar du bort en hund
    mitt i en jakt ska tabellen ändra form på nästa rad — och då vet vi exakt
    när det hände och vad som flyttade sig."""
    while True:
        time.sleep(30)
        if not _aktiva_platser:
            continue
        rader = []
        for plats in sorted(_aktiva_platser):
            namn = sync_buffer.get(str(plats) + "_name", "?")
            rader.append("%d=%s%s" % (plats, namn,
                                      "" if namn_farskt(plats) else " (obekräftat)"))
        logger.info("IDTABELL  %s", "  |  ".join(rader))


def _on_data(data, on_position, channel=None):
    global _last_page
    _last_page = time.time()
    page = data[0]
    asset_id = int(data[1])
    # Only the low 5 bits are the asset index; the upper bits differ between
    # page types (location byte1=0x64→100, identification byte1=0xE4→228, same
    # dog). Name/identification state is keyed by this masked index so it joins
    # with the location stream. device_id keeps the raw location byte for a
    # stable Traccar id. See mikkosh/AntAssetTracker parseAssetIdx (&0x1F).
    idx = data[1] & 0x1F

    if page in (0x01, 0x02):
        _maybe_request_name(channel, idx)

    if page == 0x01:
        sync_buffer[asset_id] = data
        distance   = data[2] | (data[3] << 8)
        bearing    = (data[4] / 256.0) * 360.0
        status_raw = data[5] & 0x07
        low_bat    = bool((data[5] >> 3) & 1)
        gps_lost   = bool((data[5] >> 4) & 1)
        comm_lost  = bool((data[5] >> 5) & 1)

        # Råa bitarna, för att kunna avgöra hur handenheten faktiskt signalerar
        # tappad kontakt. Sätt ANT_DEBUG=1 för att få ut dem.
        logger.debug("Hund %d: statusbyte=0x%02x läge=%s(%d) dist=%dm "
                     "bäring=%.0f° låg_batt=%s gps_lost=%s comm_lost=%s",
                     asset_id, data[5], SITUATIONS.get(status_raw, "?"), status_raw,
                     distance, bearing, low_bat, gps_lost, comm_lost)

        # Tappad kontakt går fram på två sätt beroende på handenhet: egen bit
        # i statusbyten, eller lägeskod 7. Båda betyder att positionen nedan är
        # den senast kända, inte var hunden är nu — och då ska kartan visa ett
        # frågetecken i stället för att låta hunden stå kvar som "trädskällande"
        # i timmar. Positionen skickas fortfarande, så man ser var den sist
        # fanns; det är bara påståendet om vad den gör som dras tillbaka.
        # gps_lost hoppade tidigare över hela meta-uppdateringen. Positionen
        # skickades ändå — men med FÖRRA lägets meta, alltså gammalt läge på
        # en punkt som inte rörde sig. Det var så "Hundar 2" kunde stå på
        # samma koordinat i 4085 rapporter märkt "Moving". Meta skrivs nu
        # alltid; det är bara koordinaterna som är opålitliga utan fix.
        tappad = comm_lost or gps_lost or status_raw == 7
        sync_buffer[str(asset_id) + "_meta"] = {
            "distance": distance,
            "bearing": bearing,
            "situation": ("NotConnected" if tappad else
                          SITUATIONS.get(status_raw, "Code {}".format(status_raw))),
            "low_battery": low_bat,
            "comm_lost": tappad,
        }

    elif page == 0x02:
        # Consume the paired page 0x01 so a rebroadcast 0x02 can't re-emit with
        # a stale latitude low-half. Page 0x02 is broadcast more often than 0x01;
        # emitting on every 0x02 froze the latitude between 0x01 updates and drew
        # a right-angle staircase on the map. One position per matched 1:2 pair.
        p1 = sync_buffer.pop(asset_id, None)
        if p1 is not None:
            meta = sync_buffer.get(str(asset_id) + "_meta", {})

            lat_semi = p1[6] | (p1[7] << 8) | (data[2] << 16) | (data[3] << 24)
            lon_semi = data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)
            lat = _semi_to_deg(lat_semi)
            lon = _semi_to_deg(lon_semi)

            # 0x80000000 semicircles = ±180° — Garmin's "no GPS fix" sentinel
            if abs(lat) > 179.9 or abs(lon) > 179.9:
                logger.debug("Dog %d: invalid coords %.1f,%.1f — skipping", asset_id, lat, lon)
                return

            _se_plats(idx)
            on_position({
                "device_id": str(asset_id),
                "name": sync_buffer.get(str(idx) + "_name", "Dog {}".format(asset_id)),
                "namn_farskt": namn_farskt(idx),
                "lat": lat,
                "lon": lon,
                "situation": meta.get("situation", "Unknown"),
                "distance": meta.get("distance", 0),
                "bearing": meta.get("bearing", 0.0),
                "low_battery": meta.get("low_battery", False),
                "comm_lost": meta.get("comm_lost", False),
            })

    elif page == 0x10:
        # Asset Identifier page 1: color + first 5 chars of the name set in the
        # Alpha 100. Bytarna sparas råa och avkodas först när båda halvorna
        # finns — se _avkoda_namn. Ingen strippning här: ett mellanslag kan
        # vara ett riktigt tecken vid 5/6-gränsen ("Bella Boo").
        _logga_idsida(page, data, idx)
        # Första halvan börjar en ny omgång: kasta den gamla andra halvan så
        # de aldrig kan paras ihop. Alphan skickar 0x10 och 0x11 direkt efter
        # varandra, så den nya andra halvan är millisekunder bort.
        #
        # Utan detta blev namnbytet till "Doggy" först "Doggyr 6" — ny förhalva
        # mot gammal efterhalva. Det syntes 12 sep 14:19:00 och hann inte ut på
        # en position, men ett halvt namn är ett eget id: hund-doggyr-6 hade
        # blivit en riktig enhet i Traccar.
        ny_forhalva = bytes(data[3:8])
        if sync_buffer.get(str(idx) + "_name1") != ny_forhalva:
            # Namnet håller på att ändras. Då gäller inte den gamla
            # bekräftelsen längre — antingen har hunden döpts om, eller så är
            # det en annan hund på platsen. Båda ska tystas tills det nya
            # namnet är helt.
            _namn_tid.pop(idx, None)
        sync_buffer.pop(str(idx) + "_name2", None)
        sync_buffer.pop(str(idx) + "_name_done", None)
        sync_buffer[str(idx) + "_name1"] = ny_forhalva
        _update_name(idx)

    elif page == 0x11:
        # Asset Identifier page 2: type + last 5 chars of the name
        _logga_idsida(page, data, idx)
        sync_buffer[str(idx) + "_name2"] = bytes(data[3:8])
        _update_name(idx)

    elif page == 0x52:
        # Common page 82 = the *transmitting device's* battery, i.e. the Alpha 100
        # handheld — not a collar. The profile lists asset index as present only in
        # pages 1, 2, 16 and 17, so this page cannot describe an individual dog.
        # Not attached to any dog's position, but reported here because the
        # handheld going flat is the one failure that blacks out every dog at
        # once — a collar dying only loses that collar.
        coarse = data[7] & 0x0F
        fractional = data[6] / 256.0
        voltage = round(coarse + fractional, 2)
        status = BATTERY_STATUS.get((data[7] >> 4) & 0x07, "Unknown")
        previous = sync_buffer.get("handheld_battery_status")
        sync_buffer["handheld_battery_voltage"] = voltage
        sync_buffer["handheld_battery_status"] = status
        if status != previous:
            report = logger.warning if status in ("Low", "Critical") else logger.info
            report("Handheld (Alpha 100) battery: %.2fV (%s)", voltage, status)
        else:
            logger.debug("Handheld (Alpha 100) battery: %.2fV (%s)", voltage, status)


def handheld_battery():
    """Alpha 100 battery as (voltage, status); (None, None) until page 0x52 arrives.

    This is the handheld's own battery, never a collar's — see the 0x52 handler.
    """
    return (sync_buffer.get("handheld_battery_voltage"),
            sync_buffer.get("handheld_battery_status"))


def _open_channel(node, device_id, on_position):
    channel = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
    channel.on_broadcast_data = lambda data: _on_data(data, on_position, channel)
    channel.on_burst_data = lambda data: _on_data(data, on_position, channel)
    # on_close anropas aldrig av openant — hooken låg kvar och gav en falsk
    # känsla av att en stängd kanal skulle märkas. Tystnadsvakten gör jobbet.
    channel.set_search_timeout(255)   # 255 = sök för alltid
    # 2048 = 16 Hz, matches the Asset Tracker master's transmit rate. At 8192
    # (4 Hz) we downsampled the stream and phase-locked onto page 0x02, so page
    # 0x01 (latitude low bits) only slipped through ~every 2 min — that starved
    # the 1:2 pairing and froze the latitude. See mikkosh/AntAssetTracker.
    channel.set_period(2048)
    channel.set_rf_freq(57)
    channel.set_id(device_id, 41, 0)
    channel.open()
    global _active_channel, _last_page
    _active_channel = channel
    _last_page = time.time()      # räkna tystnaden från nu, inte från förra passet
    _ensure_name_thread()
    _ensure_watchdog()
    _ensure_idtabell()
    logger.info("ANT+ channel open — listening for Alpha 100")
    return channel


def start(device_id: int, on_position, reconnect_delay: int = 5):
    """Listen for Alpha 100 broadcasts and call on_position(dict) on each fix.

    Automatically reconnects if the ANT+ node drops.
    """
    global _active_channel, _active_node
    while True:
        node = None
        try:
            # Drop all per-asset state before reconnecting. A page 0x01 left over
            # from before the drop would pair with the first page 0x02 after it,
            # assembling a latitude from two different fixes — the exact
            # right-angle staircase the 1:2 pairing was added to remove.
            _active_channel = None
            _active_node = None
            sync_buffer.clear()
            with _pending_lock:
                _pending_names.clear()

            node = Node()
            node.set_network_key(0x00, NETWORK_KEY)
            _open_channel(node, device_id, on_position)
            _active_node = node
            node.start()
            # node.start() returns normally only on clean shutdown
            logger.warning("ANT+ node.start() returned — reconnecting in %ds", reconnect_delay)
        except KeyboardInterrupt:
            logger.info("Shutting down ANT+ listener")
            break
        except Exception as exc:
            logger.error("ANT+ error: %s — reconnecting in %ds", exc, reconnect_delay)
        finally:
            if node is not None:
                try:
                    node.stop()
                except Exception:
                    pass
        time.sleep(reconnect_delay)


def dump():
    """Raw page dump — prints all received pages for protocol analysis."""
    def on_data(data):
        page = data[0]
        if page != 0x03:
            print("P{:02x}: {}".format(page, " ".join("{:02x}".format(b) for b in data)))

    node = Node()
    node.set_network_key(0x00, NETWORK_KEY)
    channel = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
    channel.on_broadcast_data = on_data
    channel.set_period(2048)  # 16 Hz — match master rate (see _open_channel)
    channel.set_rf_freq(57)
    channel.set_id(0, 41, 0)

    print("Raw dump — Ctrl-C to stop")
    try:
        channel.open()
        node.start()
    except KeyboardInterrupt:
        pass
    finally:
        channel.close()
        node.stop()
