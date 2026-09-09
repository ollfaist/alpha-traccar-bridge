# -*- coding: utf-8 -*-
"""Provar namnavkodningen mot påhittade identifikationssidor — ingen hårdvara.

Kör: python tests/test_hundnamn.py

Namnet kommer i två ANT+-sidor med fem bytes vardera (0x10 och 0x11). Tidigare
avkodades varje halva för sig som ASCII med errors="ignore", och då föll å, ä
och ö bort tyst: "Måns" blev "Mns". Fallen nedan täcker båda tänkbara
teckenuppsättningar, och det otäcka fallet där ett tecken ligger med en byte i
vardera halvan.
"""
import os
import sys
import types

os.environ.setdefault("ANT_NETWORK_KEY", "0000000000000000")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

# openant pratar med USB-stickan och finns bara på bryggan. Avkodningen av
# namnet rör aldrig radion, så en attrapp räcker — då kan testet köras var som
# helst, precis som det andra.
for modul, attribut in (("openant", {}),
                        ("openant.easy", {}),
                        ("openant.easy.node", {"Node": object}),
                        ("openant.easy.channel", {"Channel": object})):
    m = types.ModuleType(modul)
    m.__dict__.update(attribut)
    sys.modules.setdefault(modul, m)

import ant_listener as al

fel = 0


def kolla(vad, faktiskt, forvantat):
    global fel
    if faktiskt != forvantat:
        fel += 1
        print("   [FEL] %-46s %r  (väntade %r)" % (vad, faktiskt, forvantat))
    else:
        print("   [OK]  %-46s %r" % (vad, faktiskt))


def sidor(rador):
    """Delar tio bytes i de två sidornas nyttolast, nollpaddat som Alphan gör."""
    fyllt = rador + b"\x00" * (10 - len(rador))
    return fyllt[:5], fyllt[5:]


def namn_via_sidor(rador, idx=4):
    """Kör bytarna genom hela vägen: två sidor in, namn ut."""
    for nyckel in ("_name1", "_name2", "_name_done", "_name"):
        al.sync_buffer.pop(str(idx) + nyckel, None)
    d1, d2 = sidor(rador)
    # data[0]=sida, data[1]=hundnummer, data[2]=färg/typ, data[3:8]=namnbytes
    al._on_data(bytes([0x10, 0xE0 | idx, 0]) + d1, lambda *_: None)
    al._on_data(bytes([0x11, 0xE0 | idx, 0]) + d2, lambda *_: None)
    return al.sync_buffer.get(str(idx) + "_name")


print("\nNamnavkodning:\n")

# --- latin-1, det Garmin sannolikt skickar -------------------------------
kolla("'Måns' i latin-1", namn_via_sidor("Måns".encode("iso-8859-1")), "Måns")
kolla("'Räv' i latin-1", namn_via_sidor("Räv".encode("iso-8859-1")), "Räv")
kolla("'Sköldpadda'[:10] i latin-1",
      namn_via_sidor("Sköldpadda".encode("iso-8859-1")), "Sköldpadda")

# --- utf-8, om Alphan ändå skickar det -----------------------------------
kolla("'Måns' i utf-8", namn_via_sidor("Måns".encode("utf-8")), "Måns")
kolla("'Bäckis' i utf-8", namn_via_sidor("Bäckis".encode("utf-8")), "Bäckis")

# --- tecknet som ligger över 5/6-gränsen ---------------------------------
# "Bosseä" i utf-8: fem ascii-tecken, sedan ä som två bytes — den ena hamnar
# sist i första sidan, den andra först i andra. Avkodas halvorna var för sig
# blir det obegripligt oavsett teckenuppsättning.
delat = "Bosseä".encode("utf-8")
kolla("tecken delat mellan sidorna (utf-8)", namn_via_sidor(delat), "Bosseä")

# --- vanliga fall som inte får gå sönder ---------------------------------
kolla("rent ascii", namn_via_sidor(b"Sampo"), "Sampo")
kolla("mellanslag vid 5/6-gränsen", namn_via_sidor(b"Bella Boo"), "Bella Boo")
kolla("fulla tio tecken", namn_via_sidor(b"Blixtsnabb"), "Blixtsnabb")

# --- namnlöst halsband ---------------------------------------------------
tomt = namn_via_sidor(b"")
kolla("helt tomt namn ger inget namn", tomt, None)
kolla("men markeras som besvarat, så frågeloopen slutar",
      al.sync_buffer.get("4_name_done"), True)

print("\n%s" % ("ALLA TESTER OK" if fel == 0 else "%d FEL" % fel))
sys.exit(1 if fel else 0)
