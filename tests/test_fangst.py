# -*- coding: utf-8 -*-
"""Provar sammanfattningen i ANT+-fångsten.

Kör: python tests/test_fangst.py

Fångsten finns för att svara på "skickar handenheten något annat i det här läget?"
När två lägen ska jämföras är det två saker som inte får gå förlorade: sidor
bryggan inte känner igen, och de bitar i statusbyten som bryggan inte avkodar
(bit 6 och 7). Båda är precis sådant som annars kastas tyst.

Allt går mot påhittade ramar — ingen radio och ingen handenhet behövs.
"""
import os
import sys

os.environ.setdefault("ANT_NETWORK_KEY", "0000000000000000")
HAR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HAR), "src"))


def _attrapp(namn):
    try:
        __import__(namn)
        return
    except ImportError:
        pass
    import types

    class Vadsomhelst(object):
        def __init__(self, *a, **k): pass
        def __call__(self, *a, **k): return self
        def __getattr__(self, _): return Vadsomhelst()

    for bit in ("openant", "openant.easy", "openant.easy.node", "openant.easy.channel"):
        m = types.ModuleType(bit)
        m.__path__ = []
        m.Node = Vadsomhelst
        m.Channel = Vadsomhelst
        sys.modules.setdefault(bit, m)


_attrapp("openant")
import ant_listener as al  # noqa: E402

fel = 0


def kolla(vad, faktiskt, forvantat):
    global fel
    if faktiskt != forvantat:
        fel += 1
        print(u"   [FEL] %s: %r  (väntade %r)" % (vad, faktiskt, forvantat))
    else:
        print(u"   [OK]  %s: %r" % (vad, faktiskt))


print(u"\nSammanfattande fångst:\n")

# --- bara kända sidor ----------------------------------------------------
i = al.Insamling()
for _ in range(10):
    i.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x01, 0x00, 0x00])   # läge Moving, plats 5
for _ in range(4):
    i.ta_emot([0x02, 0x65, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
i.ta_emot([0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
kolla(u"sidorna räknas", i.antal, {0x01: 10, 0x02: 4, 0x03: 1})
kolla(u"inga okända sidor när allt är känt", i.okanda, {})
kolla(u"plats avläses ur byte 1 (låga fem bitarna)", 0x65 & 0x1F, 5)
kolla(u"statusbyten räknas", i.status, {0x01: 10})
kolla(u"rapporten säger inget om okända sidor", "OKÄND" in i.rapport(1.0), False)

# --- en sida som bryggan inte känner -------------------------------------
# Det här är själva frågan: skickar det här läget något vi tyst kastar?
j = al.Insamling()
j.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x01, 0x00, 0x00])
for _ in range(3):
    j.ta_emot([0x20, 0x65, 0xAA, 0xBB, 0xCC, 0x00, 0x00, 0x00])    # ingen känd sida
kolla(u"okänd sida upptäcks", j.okanda, {0x20: 3})
rap = j.rapport(2.0)
kolla(u"och flaggas tydligt i rapporten", "OKÄND" in rap and "0x20" in rap, True)
kolla(u"med ett exempel på hur ramen ser ut", "P20: 20 65 aa bb cc" in rap, True)

# --- bitar i statusbyten som bryggan inte avkodar -------------------------
# Bit 0-2 läge, 3 lågt batteri, 4 GPS borta, 5 kontakt borta. Bit 6-7 läses inte.
k = al.Insamling()
k.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x01, 0x00, 0x00])        # 0x01: bara läge
k.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x41, 0x00, 0x00])        # 0x41: bit 6 satt
k.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x81, 0x00, 0x00])        # 0x81: bit 7 satt
rap = k.rapport(1.0)
kolla(u"bit 6 och 7 räknas som en egen sak", "2 av 3 ramar har bit 6 eller 7 satt" in rap, True)

l = al.Insamling()
l.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x01, 0x00, 0x00])
kolla(u"inga höga bitar ger noll", "0 av 1 ramar har bit 6 eller 7 satt" in l.rapport(1.0), True)

# --- exempel ger inte tusen kopior av samma ram --------------------------
m = al.Insamling()
for _ in range(1000):
    m.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, 0x01, 0x00, 0x00])
kolla(u"identiska ramar ger ett exempel", len(m.exempel[0x01]), 1)

n = al.Insamling()
for b in range(6):
    n.ta_emot([0x01, 0x65, 0x10, 0x00, 0x40, b, 0x00, 0x00])
kolla(u"men olika ramar sparas, högst tre", len(n.exempel[0x01]), 3)

# --- tom fångst kraschar inte --------------------------------------------
kolla(u"tom fångst går att rapportera", "0 ramar" in al.Insamling().rapport(5.0), True)

# --- korta ramar kraschar inte -------------------------------------------
o = al.Insamling()
o.ta_emot([0x01])
o.ta_emot([0x01, 0x65, 0x10])
kolla(u"ofullständiga ramar tas emot utan krasch", o.antal, {0x01: 2})

print(u"\n%s" % (u"ALLA KONTROLLER OK" if fel == 0 else u"%d FEL" % fel))
sys.exit(1 if fel else 0)
