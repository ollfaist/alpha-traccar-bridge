import argparse
import logging
import os
import time
import yaml
from ant_listener import start as ant_start, dump as ant_dump, glom_namnen
from traccar_client import send_position, hund_id, byt_namn

# ANT_DEBUG=1 ger de råa statusbyten från handenheten — används för att
# se hur den faktiskt signalerar tappad kontakt.
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("ANT_DEBUG") else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "config", "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


# systemd-timesyncd creates this once it has actually synchronised.
_TIMESYNC_MARKER = "/run/systemd/timesync/synchronized"
_clock_ok = False


def _clock_is_trustworthy():
    """Whether the system clock can be used to stamp positions.

    The Pi Zero 2W has no RTC: at boot it restores fake-hwclock's last-shutdown
    time, which can be hours or days in the past. Stamping positions with that
    writes the start of a hunt into the past, where Traccar may reorder or drop
    them. Until the clock is confirmed synced we send no timestamp at all and
    let Traccar stamp on arrival, which is at least monotonic.
    """
    global _clock_ok
    if not _clock_ok and os.path.exists(_TIMESYNC_MARKER):
        _clock_ok = True
        logger.info("System clock synchronised — timestamping positions")
    return _clock_ok


MAX_FART = 25.0        # m/s — 90 km/h, långt över vad en hund gör
MIN_HOPP = 500.0       # m — under detta kan det vara dålig GPS


def meter(a, b):
    """Grovt avstånd i meter. Noggrant nog på de här latituderna."""
    return (((a[0] - b[0]) * 111320.0) ** 2 +
            ((a[1] - b[1]) * 57000.0) ** 2) ** 0.5


def rimligt_steg(forra, koord, nu):
    """Går punkten ihop med hundens förra, fysiskt sett?

    forra är (lat, lon, när) eller None. Korta hopp släpps alltid igenom —
    en GPS-fix darrar, och under MIN_HOPP säger avståndet ingenting. Det är
    de långa hoppen på kort tid som avslöjar att platsen bär en annan hund.
    """
    if not forra:
        return True            # första punkten — inget att jämföra med
    dt = nu - forra[2]
    if dt <= 0:
        return True
    d = meter((forra[0], forra[1]), koord)
    return d <= MIN_HOPP or (d / dt) <= MAX_FART


def notera_punkt(senaste, uid, lat, lon, nu):
    """Bokför hundens senaste punkt — men bara när den är NY.

    Tidsstämpeln ska säga när hunden senast hördes av, inte när vi senast
    skickade om samma värde. En hund utan kontakt upprepar sin frusna punkt var
    30:e sekund; nollställde varje upprepning klockan såg en hund som hördes av
    igen minuter senare ut att ha flyttat sig på några sekunder.

    Det hände 18 okt 12:43: Wikström låg NotConnected på 63.334959, 13.346479 och
    kom tillbaka 686 m därifrån. Klockan gick 19 s från senaste upprepningen i
    stället för de minuter hon varit borta — 36 m/s, över gränsen — så en riktig
    position stoppades och namnen frågades om i onödan.
    """
    forra = senaste.get(uid)
    if forra and forra[0] == lat and forra[1] == lon:
        return
    senaste[uid] = (lat, lon, nu)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sek", type=int, default=None,
                        help="Med --dump: fånga så här många sekunder och skriv en sammanfattning")
    parser.add_argument("--dump", action="store_true",
                        help="Raw page dump for protocol analysis, no Traccar forwarding")
    args = parser.parse_args()

    if args.dump:
        print("Raw dump mode — Ctrl-C to stop")
        ant_dump(args.sek)
        return

    config = load_config()
    traccar_url = config["traccar"]["url"]
    device_id = config["ant"].get("device_id", 0)

    # Admin-API:et (för att auto-registrera nya halsband) är separat från
    # OsmAnd-adressen ovan och kräver inloggning.
    admin_cfg = config["traccar"].get("admin") or {}
    # Flera adresser tillåts och provas i tur och ordning. Bryggan flyttar
    # mellan hemmanätet och en delad uppkoppling i skogen: LAN-adressen är
    # snabbast hemma men finns inte alls på väg, och en enda adress gjorde att
    # registreringen tyst misslyckades varje gång bryggan var borta.
    admin_urls = admin_cfg.get("urls") or ([admin_cfg["url"]] if admin_cfg.get("url") else [])
    admin = None
    if admin_urls and admin_cfg.get("user") and admin_cfg.get("password"):
        admin = {"urls": list(admin_urls),
                 "auth": (admin_cfg["user"], admin_cfg["password"])}
        logger.info("Admin-API för registrering: %s", ", ".join(admin_urls))
    else:
        logger.warning("Ingen traccar.admin i config.yaml — nya halsband måste "
                        "fortfarande läggas till manuellt i Traccar.")

    last_state = {}
    last_sent = {}
    # När vi först såg en slot utan att ha fått namnet. Identifikationssidorna
    # dröjer några sekunder efter att hunden dykt upp — under tiden skickar vi
    # inget, hellre det än en enhet som heter "Dog 98".
    first_seen = {}
    warned_namnlos = set()
    NAME_GRACE = 30.0

    # Vilken plats som äger ett Traccar-id just nu. Två platser får aldrig
    # skriva till samma enhet: då blir spåret en linje mellan två hundar.
    agare = {}             # unique_id -> plats

    # En hund kan inte teleportera. Hundlistan i Alphan kan däremot glida ett
    # steg utan förvarning, och då bär en plats plötsligt en annan hunds
    # position medan vårt namn ännu inte hunnit bekräftas om.
    #
    # Jakten 19 sep: 10:02:48 låg "Hundar 2" på plats 99 på 63.39017,
    # 13.43142 med dist=13723 m. 10:03:04 fick Renhunden på plats 98 exakt
    # den koordinaten och exakt det avståndet — listan hade glidit två steg.
    # På kartan såg det ut som att hunden flyttat sig 16,8 km på 5 sekunder,
    # direkt efter att föraren tappat kontakten med henne. Handenheten visade
    # hela tiden ? på rätt plats.
    #
    # Fysiken avgör: 16,8 km på 5 sekunder är 12 000 km/h. Ett sådant hopp är
    # aldrig hunden, oavsett vad namnet påstår.
    senaste_punkt = {}     # unique_id -> (lat, lon, när)
    plats_id = {}          # plats -> unique_id vi publicerade under sist

    tystade = set()         # platser vi redan klagat på, så loggen inte svämmar

    # Tappad kontakt syns inte i statusbyten: Alphan sätter varken comm_lost
    # eller lägeskod 7 (noll sådana rader i loggen någonsin) — den upprepar
    # bara sin sista kända position. Jakten 11 sep stod "Hundar 2" på exakt
    # 63.418155, 13.161052 i 4085 rapporter i rad, märkt "Moving". På kartan
    # såg det ut som en springande hund som stod stilla.
    #
    # En levande GPS-fix darrar alltid någon meter. Bitidentiska koordinater
    # minut efter minut betyder därför inte "hunden står still" utan "det här
    # är samma gamla värde om igen". Det är det vi letar efter.
    frozen = {}            # slot -> (koordinat, när den frös)
    STALE_AFTER = 180.0    # 3 min identiska koordinater = tappad kontakt
    STALE_INTERVAL = 30.0  # och då räcker en position var 30:e sekund
    # ANT+ delivers ~8 fixes/s; that's far more than Traccar needs and would
    # flood the WAN link. Throttle to one send per device per interval, but
    # never throttle a situation change (Treed/Pointed alarms must fire at once).
    MIN_SEND_INTERVAL = 1.5

    def on_position(data):
        dev = data["device_id"]
        prev = last_state.get(dev, {})
        now = time.time()

        # Har koordinaten inte rört sig på en bit är positionen inte längre
        # färsk, oavsett vad Alphan påstår om läget. Se frozen ovan.
        koord = (round(data["lat"], 6), round(data["lon"], 6))
        fryst = frozen.get(dev)
        if not fryst or fryst[0] != koord:
            frozen[dev] = (koord, now)
            tappad = False
        else:
            tappad = (now - fryst[1]) >= STALE_AFTER

        situation = "NotConnected" if tappad else data["situation"]
        # The dog's own low-battery bit (page 0x01) — the only per-asset battery
        # signal the profile has. Handheld battery (page 0x52) must not feed this:
        # it carries no asset index, so it would alarm on every dog at once.
        low = bool(data.get("low_battery"))

        # Alarm only on the rising edge so Traccar notifies once, not per fix.
        alarm = None
        if situation in ("Treed", "Pointed") and prev.get("situation") != situation:
            alarm = situation.lower()
        elif low and not prev.get("low"):
            alarm = "lowBattery"

        last_state[dev] = {"situation": situation, "low": low}

        # Only an alarm may skip the rate limit. Previously *any* situation
        # change did, so a dog hovering at the Sitting/Moving threshold flipped
        # state every fix and sent at the full ~8 Hz pair rate over mobile data.
        # En hund vi tappat kontakten med skickar samma punkt om och om igen —
        # den får en mycket glesare takt, det är ändå ingen ny information.
        takt = STALE_INTERVAL if tappad else MIN_SEND_INTERVAL
        if alarm is None and (now - last_sent.get(dev, 0)) < takt:
            return
        last_sent[dev] = now

        # Id:t härleds ur namnet Alphan gett hunden — även "Hundar 2" duger,
        # den blir bara en enhet som heter så. hund_id() ger None bara medan
        # bryggan ännu inte hört namnet; då väntar vi en kort stund hellre än
        # att skapa "Dog 98" i Traccar.
        slot = data["device_id"]

        # Namnet är hundens enda stabila identitet, och det gäller bara så
        # länge Alphan nyligen bekräftat det. Efter en omnumrering av listan
        # kan det namn vi har höra till en annan hund — då väntar vi hellre
        # några sekunder på ett nytt svar än publicerar under fel identitet.
        if data.get("namn_farskt") is False:
            if slot not in tystade:
                tystade.add(slot)
                logger.info("Plats %s väntar på att Alphan bekräftar namnet "
                            "— inga positioner så länge", slot)
            return
        tystade.discard(slot)

        unique_id = hund_id(data["name"])
        if unique_id is None:
            forst = first_seen.setdefault(slot, now)
            if (now - forst) < NAME_GRACE:
                return
            # Halsbandet skickar aldrig sitt namn. Sällsynt — men då får det
            # ett id på platsnumret så det åtminstone syns på kartan.
            unique_id = "hund-namnlos-" + slot
            if slot not in warned_namnlos:
                warned_namnlos.add(slot)
                logger.warning("Halsbandet på plats %s skickar inget namn ens "
                               "efter %d s — visas som '%s'. Kontrollera att "
                               "hunden finns med i handenhetens lista.",
                               slot, int(NAME_GRACE), unique_id)
        # Håller en annan plats redan id:t är något fel: antingen har listan
        # numrerats om utan att vi märkt det, eller så bär två platser samma
        # namn. Båda fallen slutar med två hundar på en enhet, så vi stannar
        # och ber om namnen igen i stället.
        tidigare = agare.get(unique_id)
        if tidigare is not None and tidigare != slot:
            logger.warning("Plats %s och plats %s gör båda anspråk på %s — "
                           "frågar om namnen och skickar ingen av dem",
                           tidigare, slot, unique_id)
            glom_namnen("krock på %s" % unique_id)
            agare.pop(unique_id, None)
            return
        agare[unique_id] = slot

        dog_name = data["name"] if unique_id != "hund-namnlos-" + slot else unique_id
        koord_nu = (data["lat"], data["lon"])

        # Har platsen bytt id sedan sist kan det vara två helt olika saker.
        #
        # Samma hund omdöpt i Alphan: positionen ligger kvar där den var, för
        # det är bara etiketten som ändrats. Då ska Traccar-enheten byta namn
        # och id, inte dubbleras — annars delas spåren i två och den gamla
        # enheten blir liggande tom. Det var det som hände med Renhunden
        # 19 sep: hon lades till som "Hundar 1", döptes om direkt, och fanns
        # sedan som två hundar.
        #
        # En annan hund på samma plats: positionen hoppar. Då ska den nya
        # hunden få en egen enhet, precis som förut.
        gammalt_id = plats_id.get(slot)
        if gammalt_id and gammalt_id != unique_id:
            if rimligt_steg(senaste_punkt.get(gammalt_id), koord_nu, now) and byt_namn(
                    admin, gammalt_id, unique_id, dog_name):
                senaste_punkt[unique_id] = senaste_punkt.pop(gammalt_id, None)
                agare.pop(gammalt_id, None)
            else:
                senaste_punkt.pop(gammalt_id, None)
        plats_id[slot] = unique_id

        # En hund kan inte teleportera. Hoppar punkten längre än fysiken
        # tillåter bär platsen en annan hund än namnet påstår — listan har
        # glidit och vårt namn är inte omfrågat än. Då skickas ingenting, och
        # namnen frågas om på en gång. Se senaste_punkt ovan.
        if not rimligt_steg(senaste_punkt.get(unique_id), koord_nu, now):
            forra = senaste_punkt[unique_id]
            logger.warning("Plats %s: '%s' skulle ha flyttat sig %.0f m på "
                           "%.0f s — omöjligt. Skickar inget och frågar om "
                           "namnen.", slot, dog_name,
                           meter((forra[0], forra[1]), koord_nu), now - forra[2])
            glom_namnen("omöjligt hopp på plats %s" % slot)
            senaste_punkt.pop(unique_id, None)
            return
        notera_punkt(senaste_punkt, unique_id, data["lat"], data["lon"], now)

        logger.info("Hund '%s' [%s -> %s]: %.6f, %.6f  %s  dist=%dm%s",
                    dog_name, slot, unique_id,
                    data["lat"], data["lon"], situation,
                    data["distance"], "  LÅGT BATT" if low else "")
        extras = {
            "bearing": round(data["bearing"]),
            "altitude": 0,
            # No "batt": page 0x52 reports the *handheld's* battery, not the
            # collar's, so sending it here labelled every dog with the Alpha's
            # charge. The collar exposes only the low_battery bit, sent as an alarm.
            "event": "{} dist={}m".format(situation, data["distance"]),
        }
        # Decode time, not fix time — pages 0x01/0x02 carry no GPS timestamp.
        # It still keeps the track in order, because a retried position would
        # otherwise be stamped on arrival and land after newer ones. Only sent
        # once the clock is trustworthy; see _clock_is_trustworthy().
        if _clock_is_trustworthy():
            extras["timestamp"] = int(time.time())
        extras["dogName"] = dog_name
        if alarm:
            extras["alarm"] = alarm

        send_position(traccar_url, unique_id, data["lat"], data["lon"], extras, admin=admin)

    logger.info("Starting — device_id=%s", device_id)
    ant_start(device_id, on_position)


if __name__ == "__main__":
    main()
