# -*- coding: utf-8 -*-
"""Provar id-härledningen och enhetsregistreringen mot en påhittad Traccar.

Kör: python tests/test_id.py

Hundens id i Traccar är "hund-<namn>", härlett ur Garmin-namnet. Det betyder
att samma hund får samma enhet oavsett vilken handenhet eller brygga som hör
den — och att två bryggor som hör samma hund fyller på samma spår i stället
för att slåss om en enhet. Testerna nedan täcker slug-formen, de namn som
inte duger som id, och att registreringen är idempotent.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("ANT_NETWORK_KEY", "0000000000000000")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import traccar_client as tc

fel = 0


def kolla(vad, faktiskt, forvantat):
    global fel
    if faktiskt != forvantat:
        fel += 1
        print("   [FEL] %-46s %r  (väntade %r)" % (vad, faktiskt, forvantat))
    else:
        print("   [OK]  %-46s %r" % (vad, faktiskt))


# --- id-härledningen ---------------------------------------------------------
print("\nId ur namnet:\n")
kolla("'Sampo'", tc.hund_id("Sampo"), "hund-sampo")
kolla("'Måns'", tc.hund_id("Måns"), "hund-mans")
kolla("'Bella Boo'", tc.hund_id("Bella Boo"), "hund-bella-boo")
kolla("'Räv 2'", tc.hund_id("Räv 2"), "hund-rav-2")
kolla("blanksteg trimmas", tc.hund_id("  Sixten  "), "hund-sixten")
kolla("versaler spelar ingen roll", tc.hund_id("SIXTEN"), "hund-sixten")

print("\nNamn som inte duger som id:\n")
kolla("namnlös platshållare 'Dog 98'", tc.hund_id("Dog 98"), None)
kolla("uppräkningsnamn 'Hundar'", tc.hund_id("Hundar"), None)
kolla("uppräkningsnamn 'Hundar 3'", tc.hund_id("Hundar 3"), None)
kolla("uppräkningsnamn 'DOG 5'", tc.hund_id("DOG 5"), None)
kolla("tomt namn", tc.hund_id(""), None)
kolla("bara skräptecken", tc.hund_id("!!!"), None)


# --- registrering mot en påhittad Traccar ----------------------------------
DEVICES = []
LOGG = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _svar(self, kod, data=None):
        kropp = json.dumps(data if data is not None else {}).encode()
        self.send_response(kod)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(kropp)))
        self.end_headers()
        self.wfile.write(kropp)

    def do_GET(self):
        if self.path.startswith("/api/devices"):
            LOGG.append("GET")
            return self._svar(200, DEVICES)
        LOGG.append("OSMAND")
        kant = any(("id=" + d["uniqueId"] + "&") in self.path + "&" for d in DEVICES)
        return self._svar(200 if kant else 400)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        b = json.loads(self.rfile.read(n) or b"{}")
        LOGG.append("POST")
        if any(d["uniqueId"] == b["uniqueId"] for d in DEVICES):
            return self._svar(400, {"error": "duplicate uniqueId"})
        DEVICES.append({"id": len(DEVICES) + 1, "name": b["name"],
                        "uniqueId": b["uniqueId"], "attributes": b.get("attributes") or {}})
        return self._svar(200, DEVICES[-1])

    def do_PUT(self):
        n = int(self.headers.get("Content-Length", 0))
        b = json.loads(self.rfile.read(n) or b"{}")
        LOGG.append("PUT")
        for d in DEVICES:
            if d["id"] == b["id"]:
                d.update({"name": b["name"], "uniqueId": b["uniqueId"]})
                return self._svar(200, d)
        return self._svar(404)


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BAS = "http://127.0.0.1:%d" % srv.server_address[1]
ADMIN = {"urls": ["http://127.0.0.1:9", BAS], "auth": ("u", "p")}   # död adress först


def nollstall():
    tc._registered.clear()
    tc._admin_ok_url = None
    tc._admin_nasta_forsok = 0.0
    LOGG.clear()


def skicka(unique_id, dog_name):
    tc._deliver(BAS + "/", unique_id, 63.27, 13.34, {"dogName": dog_name}, admin=ADMIN)


def namn(uid):
    return next((d["name"] for d in DEVICES if d["uniqueId"] == uid), None)


print("\nRegistrering:\n")

# ny hund → enhet skapas, reservadressen används
DEVICES[:] = []
nollstall()
skicka("hund-sampo", "Sampo")
kolla("ny hund skapas", namn("hund-sampo"), "Sampo")
kolla("reservadressen användes", tc._admin_ok_url, BAS)

# samma hund igen → inga fler admin-anrop
nollstall()
skicka("hund-sampo", "Sampo")
LOGG.clear()
for _ in range(5):
    skicka("hund-sampo", "Sampo")
kolla("inga admin-anrop för känd hund", [x for x in LOGG if x != "OSMAND"], [])

# två bryggor hör samma hund → samma enhet, ingen dubblett
nollstall()
skicka("hund-sampo", "Sampo")   # "andra bryggan"
kolla("ingen andra enhet för samma hund",
      [d for d in DEVICES if d["uniqueId"] == "hund-sampo"].__len__(), 1)

# någon har döpt om enheten i Traccar → bryggan rättar tillbaka
nollstall()
DEVICES[0]["name"] = "Fido"
skicka("hund-sampo", "Sampo")
kolla("manuell omdöpning rättas mot Garmin", namn("hund-sampo"), "Sampo")

# platshållare för ett odöpt halsband
nollstall()
skicka("hund-plats-98", "Odöpt hund 98")
kolla("odöpt halsband får en egen enhet", namn("hund-plats-98"), "Odöpt hund 98")

# ingen admin-adress svarar → paus, inga upprepade timeouts
nollstall()
forsok = []
riktig = tc._admin_request


def _dod(admin, metod, vag, **kw):
    forsok.append(vag)
    raise tc.requests.RequestException("nätet nere")


tc._admin_request = _dod
try:
    for _ in range(4):
        skicka("hund-ny", "Ny")
finally:
    tc._admin_request = riktig
kolla("pausen är satt", tc._admin_nasta_forsok > time.time(), True)
kolla("bara ett försök på fyra positioner", len(forsok), 1)
nollstall()

print("\n%s" % ("ALLA TESTER OK" if fel == 0 else "%d FEL" % fel))
sys.exit(1 if fel else 0)
