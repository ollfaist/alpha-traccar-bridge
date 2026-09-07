# -*- coding: utf-8 -*-
"""Provar registreringslogiken mot en påhittad Traccar — ingen hårdvara behövs.

Kör: python tests/test_registrering.py

Fallen kommer alla från verkliga fel: hunden som försvann när Alphas lista
numrerades om, platshållaren som aldrig fick sitt riktiga namn, och
registreringen som tyst misslyckades när bryggan stod utanför hemmanätet.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import traccar_client as tc

# --- Låtsas-Traccar -------------------------------------------------------
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
        kant = any(("id=" + str(d["uniqueId"])) in self.path for d in DEVICES)
        return self._svara(200 if kant else 400)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        kropp = json.loads(self.rfile.read(n) or b"{}")
        LOGG.append(("POST", kropp))
        if any(str(d["uniqueId"]) == str(kropp["uniqueId"]) for d in DEVICES):
            return self._svara(400, {"error": "duplicate uniqueId"})
        ny = {"id": len(DEVICES) + 1, "name": kropp["name"], "uniqueId": str(kropp["uniqueId"])}
        DEVICES.append(ny)
        return self._svara(200, ny)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        kropp = json.loads(self.rfile.read(n) or b"{}")
        LOGG.append(("PUT", kropp))
        for d in DEVICES:
            if d["id"] == kropp["id"]:
                d.update({"name": kropp["name"], "uniqueId": str(kropp["uniqueId"])})
                return self._svara(200, d)
        return self._svara(404)


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BAS = "http://127.0.0.1:%d" % port

# En adress som garanterat inte svarar, först i listan — så vi också provar
# att reservadressen tas.
DOD = "http://127.0.0.1:9"
ADMIN = {"urls": [DOD, BAS], "auth": ("u", "p")}


def nollstall():
    tc._registered_names.clear()
    tc._first_seen.clear()
    tc._admin_ok_url = None
    LOGG.clear()


def visa(rubrik):
    print("\n%s" % rubrik)
    for d in DEVICES:
        print("   enhet %s: namn=%-12s uniqueId=%s" % (d["id"], d["name"], d["uniqueId"]))


fel = 0


def kolla(vad, faktiskt, forvantat):
    global fel
    ok = faktiskt == forvantat
    if not ok:
        fel += 1
    print("   [%s] %s: %r%s" % ("OK" if ok else "FEL", vad, faktiskt,
                                "" if ok else "  (väntade %r)" % (forvantat,)))


# --- 1: känd hund som bytt platsnummer ------------------------------------
DEVICES[:] = [{"id": 1, "name": "Sampo", "uniqueId": "105"},
              {"id": 2, "name": "Etsa", "uniqueId": "100"}]
nollstall()
tc._deliver(BAS + "/", "98", 63.27, 13.34, {"dogName": "Sampo"}, admin=ADMIN)
visa("1) Sampo dyker upp som 98 (var 105)")
kolla("Sampo har flyttats till 98", DEVICES[0]["uniqueId"], "98")
kolla("ingen ny enhet skapad", len(DEVICES), 2)
kolla("reservadressen användes", tc._admin_ok_url, BAS)

# --- 2: helt ny hund ------------------------------------------------------
nollstall()
tc._deliver(BAS + "/", "99", 63.27, 13.34, {"dogName": "Rex"}, admin=ADMIN)
visa("2) Okänd hund 'Rex' som 99")
kolla("ny enhet skapad", len(DEVICES), 3)
kolla("rätt uniqueId", DEVICES[-1]["uniqueId"], "99")

# --- 3: namnlös inom fristen ----------------------------------------------
nollstall()
antal_innan = len(DEVICES)
tc._deliver(BAS + "/", "97", 63.27, 13.34, {}, admin=ADMIN)
visa("3) Namnlöst halsband 97, inom namnfristen")
kolla("inget skapat än", len(DEVICES), antal_innan)

# --- 4: namnlös efter fristen ---------------------------------------------
tc._first_seen["97"] = time.time() - tc._NAME_GRACE - 1
tc._deliver(BAS + "/", "97", 63.27, 13.34, {}, admin=ADMIN)
visa("4) Samma halsband när fristen gått ut")
kolla("platshållare skapad", len(DEVICES), antal_innan + 1)
kolla("platshållarnamn", DEVICES[-1]["name"], "Ny hund 97")

# --- 5: namnet kommer in efteråt ------------------------------------------
nollstall()
antal_innan = len(DEVICES)
tc._deliver(BAS + "/", "97", 63.27, 13.34, {"dogName": "Bella"}, admin=ADMIN)
visa("5) Garmin-namnet 'Bella' kommer in för 97")
kolla("platshållaren döptes om", [d["name"] for d in DEVICES if d["uniqueId"] == "97"], ["Bella"])
kolla("ingen dubblett skapad", len(DEVICES), antal_innan)

# --- 6: känt halsband — en koll, sedan tyst -------------------------------
# Namnet kontrolleras en gång per halsband och körning (enheten kan bära
# platshållarens namn), men får inte kosta ett anrop per position.
nollstall()
tc._deliver(BAS + "/", "98", 63.27, 13.34, {"dogName": "Sampo"}, admin=ADMIN)
forsta = [l[0] for l in LOGG if l[0] in ("PUT", "POST", "GET")]
LOGG.clear()
for _ in range(5):
    tc._deliver(BAS + "/", "98", 63.27, 13.34, {"dogName": "Sampo"}, admin=ADMIN)
sedan = [l[0] for l in LOGG if l[0] in ("PUT", "POST", "GET")]
visa("6) Sampo skickar på sitt kända id 98")
kolla("en koll forsta positionen", forsta, ["GET"])
kolla("inga anrop for de fem foljande", sedan, [])

# --- 7: två hundar byter plats med varandra -------------------------------
DEVICES[:] = [{"id": 1, "name": "Sampo", "uniqueId": "98"},
              {"id": 2, "name": "Etsa", "uniqueId": "100"}]
nollstall()
tc._deliver(BAS + "/", "100", 63.27, 13.34, {"dogName": "Sampo"}, admin=ADMIN)   # krock
tc._deliver(BAS + "/", "98", 63.27, 13.34, {"dogName": "Etsa"}, admin=ADMIN)
tc._registered_names.clear()
tc._deliver(BAS + "/", "100", 63.27, 13.34, {"dogName": "Sampo"}, admin=ADMIN)
visa("7) Sampo och Etsa har bytt plats i listan")
kolla("Sampo pa 100", [d["uniqueId"] for d in DEVICES if d["name"] == "Sampo"], ["100"])
kolla("Etsa pa 98", [d["uniqueId"] for d in DEVICES if d["name"] == "Etsa"], ["98"])
kolla("fortfarande tva enheter", len(DEVICES), 2)

print("\n%s" % ("ALLA TESTER OK" if fel == 0 else "%d FEL" % fel))
sys.exit(1 if fel else 0)
