# -*- coding: utf-8 -*-
"""Provar registreringslogiken mot en påhittad Traccar — ingen hårdvara behövs.

Kör: python tests/test_registrering.py

Två saker kan ändras i Alphan mellan två jakter, och de drar åt olika håll:

  * Lägger man till eller tar bort ett halsband numreras hundlistan om, och
    samma hund kommer in under ett nytt platsnummer. Då är namnet det stabila.
  * Döper man om en hund i handenheten byter den namn men behåller platsen.
    Då är platsnumret det stabila.

Därför söks hunden först på namnet och sedan på platsen. Undantaget är Alphans
egna autonamn ("Hundar 3"), som återanvänds till nästa hund man lägger till —
de identifierar ingenting och får bara matcha på plats.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import traccar_client as tc

DEVICES = []
LOGG = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _svara(self, kod, data=None):
        kropp = json.dumps(data if data is not None else {}).encode()
        self.send_response(kod)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(kropp)))
        self.end_headers()
        self.wfile.write(kropp)

    def do_GET(self):
        if self.path.startswith("/api/devices"):
            LOGG.append(("GET", self.path))
            return self._svara(200, DEVICES)
        # OsmAnd-porten: 400 för okänt id, annars 200
        LOGG.append(("OSMAND", self.path))
        kant = any(("id=" + str(d["uniqueId"]) + "&") in self.path + "&" for d in DEVICES)
        return self._svara(200 if kant else 400)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        kropp = json.loads(self.rfile.read(n) or b"{}")
        LOGG.append(("POST", kropp))
        if any(str(d["uniqueId"]) == str(kropp["uniqueId"]) for d in DEVICES):
            return self._svara(400, {"error": "duplicate uniqueId"})
        ny = {"id": len(DEVICES) + 1, "name": kropp["name"],
              "uniqueId": str(kropp["uniqueId"]),
              "attributes": kropp.get("attributes") or {}}
        DEVICES.append(ny)
        return self._svara(200, ny)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        kropp = json.loads(self.rfile.read(n) or b"{}")
        LOGG.append(("PUT", kropp))
        if any(d["id"] != kropp["id"] and str(d["uniqueId"]) == str(kropp["uniqueId"])
               for d in DEVICES):
            return self._svara(400, {"error": "duplicate uniqueId"})
        for d in DEVICES:
            if d["id"] == kropp["id"]:
                d.update({"name": kropp["name"], "uniqueId": str(kropp["uniqueId"]),
                          "attributes": kropp.get("attributes") or {}})
                return self._svara(200, d)
        return self._svara(404)


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BAS = "http://127.0.0.1:%d" % srv.server_address[1]

# En adress som garanterat inte svarar, först i listan — så reservadressen
# också provas.
ADMIN = {"urls": ["http://127.0.0.1:9", BAS], "auth": ("u", "p")}

fel = 0


def nollstall():
    tc._registered_names.clear()
    tc._first_seen.clear()
    tc._admin_ok_url = None
    tc._admin_nasta_forsok = 0.0
    LOGG.clear()


def enhet(nr, namn, uid, garmin=None):
    attrs = {"garminName": garmin} if garmin else {}
    return {"id": nr, "name": namn, "uniqueId": uid, "attributes": attrs}


def skicka(device_id, namn=None):
    extras = {"dogName": namn} if namn else {}
    tc._deliver(BAS + "/", device_id, 63.27, 13.34, extras, admin=ADMIN)


def visa(rubrik):
    print("\n%s" % rubrik)
    for d in DEVICES:
        print("   enhet %s: namn=%-12s uniqueId=%-8s garminName=%s"
              % (d["id"], d["name"], d["uniqueId"],
                 (d.get("attributes") or {}).get("garminName")))


def kolla(vad, faktiskt, forvantat):
    global fel
    if faktiskt != forvantat:
        fel += 1
        print("   [FEL] %s: %r  (väntade %r)" % (vad, faktiskt, forvantat))
    else:
        print("   [OK] %s: %r" % (vad, faktiskt))


def platsen(namn):
    return [d["uniqueId"] for d in DEVICES if d["name"] == namn]


def namnet(uid):
    return [d["name"] for d in DEVICES if d["uniqueId"] == uid]


# --- 1: namngiven hund som bytt platsnummer -------------------------------
DEVICES[:] = [enhet(1, "Sampo", "105"), enhet(2, "Etsa", "100")]
nollstall()
skicka("98", "Sampo")
visa("1) Sampo dyker upp som 98 (var 105)")
kolla("Sampo flyttad till 98", platsen("Sampo"), ["98"])
kolla("ingen ny enhet", len(DEVICES), 2)
kolla("reservadressen användes", tc._admin_ok_url, BAS)

# --- 2: helt ny hund ------------------------------------------------------
nollstall()
skicka("99", "Rex")
visa("2) Okänd hund 'Rex' som 99")
kolla("ny enhet skapad", platsen("Rex"), ["99"])

# --- 3: namnlöst halsband inom fristen ------------------------------------
nollstall()
innan = len(DEVICES)
skicka("97")
visa("3) Namnlöst halsband 97, inom namnfristen")
kolla("inget skapat än", len(DEVICES), innan)

# --- 4: namnlöst halsband när fristen gått ut -----------------------------
tc._first_seen["97"] = time.time() - tc._NAME_GRACE - 1
skicka("97")
visa("4) Samma halsband när fristen gått ut")
kolla("platshållare skapad", namnet("97"), ["Ny hund 97"])

# --- 5: Garmin-namnet kommer in efteråt -----------------------------------
nollstall()
innan = len(DEVICES)
skicka("97", "Bella")
visa("5) Garmin-namnet 'Bella' kommer in för 97")
kolla("platshållaren döptes om", namnet("97"), ["Bella"])
kolla("ingen dubblett", len(DEVICES), innan)

# --- 6: känt halsband — en uppslagning, sedan tyst ------------------------
nollstall()
skicka("98", "Sampo")
forsta = [l[0] for l in LOGG if l[0] in ("GET", "PUT", "POST")]
LOGG.clear()
for _ in range(5):
    skicka("98", "Sampo")
sedan = [l[0] for l in LOGG if l[0] in ("GET", "PUT", "POST")]
visa("6) Sampo skickar på sitt kända id 98")
kolla("en uppslagning första positionen", forsta, ["GET"])
kolla("inga anrop för de fem följande", sedan, [])

# --- 7: två hundar byter plats med varandra -------------------------------
DEVICES[:] = [enhet(1, "Sampo", "98", "Sampo"), enhet(2, "Etsa", "100", "Etsa")]
nollstall()
skicka("100", "Sampo")        # blockerad av Etsa
skicka("98", "Etsa")          # blockerad av Sampo
tc._registered_names.clear()
skicka("100", "Sampo")
visa("7) Sampo och Etsa har bytt plats i listan")
kolla("Sampo på 100", platsen("Sampo"), ["100"])
kolla("Etsa på 98", platsen("Etsa"), ["98"])
kolla("fortfarande två enheter", len(DEVICES), 2)

# --- 8: hunden har döpts om i Alphan --------------------------------------
# Samma plats, nytt namn. Traccar ska visa det Alphan visar.
DEVICES[:] = [enhet(1, "Sampo", "105", "Sampo")]
nollstall()
skicka("105", "Rocky")
visa("8) 'Sampo' har döpts om till 'Rocky' i handenheten")
kolla("namnet följer med", namnet("105"), ["Rocky"])
kolla("ingen ny enhet", len(DEVICES), 1)
kolla("kopplingen uppdaterad",
      (DEVICES[0].get("attributes") or {}).get("garminName"), "Rocky")

# --- 9: autonamn speglas också -------------------------------------------
# "Hundar 8" är Alphans egen uppräkning. Namnet ska ändå synas i Traccar —
# laget vill se samma sak på båda ställena.
DEVICES[:] = [enhet(1, "Sampo", "105")]
nollstall()
skicka("105", "Hundar 8")
visa("9) Halsbandet heter 'Hundar 8' i Alphan")
kolla("Traccar visar samma namn", namnet("105"), ["Hundar 8"])

# --- 10: autonamn identifierar INTE över platsbyte ------------------------
# Tar man bort en hund återanvänder Alphan "Hundar 8" till nästa. Matchade vi
# på det namnet hade den nya hunden ärvt den gamlas enhet och spår.
nollstall()
skicka("99", "Hundar 8")
visa("10) Ett annat halsband kommer in som 'Hundar 8' på plats 99")
kolla("den gamla enheten orörd", namnet("105"), ["Hundar 8"])
kolla("egen enhet för den nya", namnet("99"), ["Hundar 8"])
kolla("två enheter", len(DEVICES), 2)

# --- 11: en hund som heter samma sak som en jägare ------------------------
DEVICES[:] = [enhet(1, "Olle", "19890605"), enhet(2, "Joel", "jakt-joel")]
nollstall()
skicka("98", "Olle")
visa("11) Halsband som heter 'Olle' — samma som en jägare")
kolla("jägarens enhet orörd", [d["uniqueId"] for d in DEVICES if d["id"] == 1], ["19890605"])
kolla("eget halsband skapat", [d["uniqueId"] for d in DEVICES if d["id"] == 3], ["98"])

# --- 12: ingen admin-adress svarar — bryggan får inte proppa kön ----------
# I skogen kan varken LAN-adressen eller den publika nås. Uppslagningen görs
# även när positionen gick fram, så utan paus hade varje position kostat en
# timeout per adress på sändartråden och köat upp resten bakom sig.
DEVICES[:] = [enhet(1, "Sampo", "105", "Sampo")]
nollstall()
forsok = []
riktig = tc._admin_request


def _dod_admin(admin, method, path, **kw):
    forsok.append(path)
    raise tc.requests.RequestException("nätet är nere")


tc._admin_request = _dod_admin
try:
    for _ in range(4):
        skicka("105", "Rocky")
finally:
    tc._admin_request = riktig
print("\n12) Ingen admin-adress svarar")
kolla("pausen är satt", tc._admin_nasta_forsok > time.time(), True)
kolla("bara ett försök på fyra positioner", len(forsok), 1)
nollstall()

print("\n%s" % ("ALLA TESTER OK" if fel == 0 else "%d FEL" % fel))
sys.exit(1 if fel else 0)
