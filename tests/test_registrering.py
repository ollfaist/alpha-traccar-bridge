# -*- coding: utf-8 -*-
"""Provar registreringslogiken mot en påhittad Traccar — ingen hårdvara behövs.

Kör: python tests/test_registrering.py

Fallen kommer alla från verkliga fel: hunden som försvann när Alphas lista
numrerades om, platshållaren som aldrig fick sitt riktiga namn, registreringen
som tyst misslyckades när bryggan stod utanför hemmanätet, och enheten laget
själv döpt om i Traccar.
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
        krock = any(d["id"] != kropp["id"] and str(d["uniqueId"]) == str(kropp["uniqueId"])
                    for d in DEVICES)
        if krock:
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


def unikt(namn):
    return [d["uniqueId"] for d in DEVICES if d["name"] == namn]


# --- 1: känd hund som bytt platsnummer ------------------------------------
DEVICES[:] = [{"id": 1, "name": "Sampo", "uniqueId": "105", "attributes": {}},
              {"id": 2, "name": "Etsa", "uniqueId": "100", "attributes": {}}]
nollstall()
skicka("98", "Sampo")
visa("1) Sampo dyker upp som 98 (var 105)")
kolla("Sampo flyttad till 98", unikt("Sampo"), ["98"])
kolla("ingen ny enhet", len(DEVICES), 2)
kolla("reservadressen användes", tc._admin_ok_url, BAS)

# --- 2: helt ny hund ------------------------------------------------------
nollstall()
skicka("99", "Rex")
visa("2) Okänd hund 'Rex' som 99")
kolla("ny enhet skapad", unikt("Rex"), ["99"])
kolla("kopplingen satt direkt",
      (DEVICES[-1].get("attributes") or {}).get("garminName"), "Rex")

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
kolla("platshållare skapad", unikt("Ny hund 97"), ["97"])

# --- 5: Garmin-namnet kommer in efteråt -----------------------------------
nollstall()
innan = len(DEVICES)
skicka("97", "Bella")
visa("5) Garmin-namnet 'Bella' kommer in för 97")
kolla("platshållaren döptes om", unikt("Bella"), ["97"])
kolla("ingen dubblett", len(DEVICES), innan)

# --- 6: känt halsband — en koll, sedan tyst -------------------------------
nollstall()
skicka("98", "Sampo")
forsta = [l[0] for l in LOGG if l[0] in ("GET", "PUT", "POST")]
LOGG.clear()
for _ in range(5):
    skicka("98", "Sampo")
sedan = [l[0] for l in LOGG if l[0] in ("GET", "PUT", "POST")]
visa("6) Sampo skickar på sitt kända id 98")
# Kopplingen sattes redan i fall 1, så här räcker en uppslagning.
kolla("en uppslagning första positionen", forsta, ["GET"])
kolla("inga anrop för de fem följande", sedan, [])

# --- 7: två hundar byter plats med varandra -------------------------------
DEVICES[:] = [{"id": 1, "name": "Sampo", "uniqueId": "98", "attributes": {}},
              {"id": 2, "name": "Etsa", "uniqueId": "100", "attributes": {}}]
nollstall()
skicka("100", "Sampo")        # blockerad av Etsa
skicka("98", "Etsa")          # blockerad av Sampo
tc._registered_names.clear()
skicka("100", "Sampo")
visa("7) Sampo och Etsa har bytt plats i listan")
kolla("Sampo på 100", unikt("Sampo"), ["100"])
kolla("Etsa på 98", unikt("Etsa"), ["98"])
kolla("fortfarande två enheter", len(DEVICES), 2)

# --- 8: laget har döpt om enheten i Traccar -------------------------------
# Halsbandet heter "Hundar 8" i handenheten, men laget vill se "Sampo" på
# kartan. Namnet är deras — bryggan får bara komma ihåg kopplingen.
DEVICES[:] = [{"id": 1, "name": "Sampo", "uniqueId": "105", "attributes": {}}]
nollstall()
skicka("105", "Hundar 8")
visa("8a) Halsbandet 'Hundar 8' skickar på Sampos id")
kolla("namnet orört", DEVICES[0]["name"], "Sampo")
kolla("kopplingen sparad",
      (DEVICES[0].get("attributes") or {}).get("garminName"), "Hundar 8")

nollstall()
skicka("99", "Hundar 8")
visa("8b) Listan numreras om — halsbandet kommer in som 99")
kolla("samma enhet flyttad", unikt("Sampo"), ["99"])
kolla("ingen ny enhet", len(DEVICES), 1)

# --- 9: ingen admin-adress svarar — bryggan får inte proppa kön -----------
# I skogen kan varken LAN-adressen eller den publika nås. Uppslagningen görs
# numera även när positionen gick fram, så utan paus hade varje position
# kostat en timeout per adress på sändartråden och köat upp resten bakom sig.
DEVICES[:] = [{"id": 1, "name": "Sampo", "uniqueId": "105", "attributes": {}}]
nollstall()
forsok = []
riktig = tc._admin_request


def _dod_admin(admin, method, path, **kw):
    forsok.append(path)
    raise tc.requests.RequestException("nätet är nere")


tc._admin_request = _dod_admin
try:
    for _ in range(4):
        tc._deliver(BAS + "/", "105", 63.27, 13.34, {"dogName": "Hundar 8"}, admin=ADMIN)
finally:
    tc._admin_request = riktig
print("\n9) Ingen admin-adress svarar")
kolla("pausen är satt", tc._admin_nasta_forsok > time.time(), True)
kolla("bara ett försök på fyra positioner", len(forsok), 1)
nollstall()

print("\n%s" % ("ALLA TESTER OK" if fel == 0 else "%d FEL" % fel))
sys.exit(1 if fel else 0)
