# -*- coding: utf-8 -*-
"""Provar teleporteringsvakten mot verkliga tal ur jakten 19 sep 2026.

Kör: python tests/test_hopp.py

Bakgrund: hundlistan i Alphan kan glida ett eller två steg utan förvarning.
Då bär en plats plötsligt en annan hunds position, medan bryggans namn ännu
inte hunnit bekräftas om. 19 sep fick Renhunden på så vis Hundar 2:s position
16,8 km bort, fem sekunder efter sin egen — direkt efter att föraren tappat
kontakten med henne. Handenheten visade hela tiden ? på rätt plats.

Namnet kan alltså ljuga. Fysiken kan inte: en hund gör inte 16,8 km på fem
sekunder. Det som provas här är att gränsen skiljer det omöjliga från det
som faktiskt händer på en jakt — en hund som varit ur kontakt en halvtimme
och hunnit långt under tiden ska släppas igenom.
"""
import os
import sys

HAR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HAR), "src"))
os.environ.setdefault("ANT_NETWORK_KEY", "0000000000000000")


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

    for bit in ("openant", "openant.easy", "openant.easy.node",
                "openant.easy.channel", "yaml", "requests"):
        m = types.ModuleType(bit)
        m.__path__ = []
        m.Node = Vadsomhelst
        m.Channel = Vadsomhelst
        m.safe_load = Vadsomhelst()
        m.RequestException = Exception
        sys.modules.setdefault(bit, m)


_attrapp("openant")

import main  # noqa: E402

fel = 0


def kolla(vad, faktiskt, forvantat):
    global fel
    if faktiskt != forvantat:
        fel += 1
        print(u"   [FEL] %s: %r  (väntade %r)" % (vad, faktiskt, forvantat))
    else:
        print(u"   [OK]  %s: %r" % (vad, faktiskt))


print(u"\nTeleporteringsvakten:\n")

# --- själva olyckan ------------------------------------------------------
# 10:02:59 Renhunden 63.24408, 13.35722   (NotConnected, dist 2013 m)
# 10:03:04 samma plats 63.39017, 13.43142 (Moving, dist 13723 m)
# Fem sekunder, 16,8 km. Det var Hundar 2:s position.
renhunden = (63.24408, 13.35722, 1000.0)
hundar2 = (63.39017, 13.43142)
kolla(u"16,8 km på 5 sekunder stoppas",
      main.rimligt_steg(renhunden, hundar2, 1005.0), False)
kolla(u"och avståndet är det vi tror",
      round(main.meter(renhunden[:2], hundar2) / 1000.0, 1), 16.8)

# --- det som faktiskt händer på en jakt ---------------------------------
# 08:06:48 tappades kontakten på 63.26969, 13.34576.
# 08:38:33 kom hon tillbaka 1339 m bort — efter 32 minuter. Hon hade sprungit.
tappad = (63.26969, 13.34576, 1000.0)
ater = (63.25776, 13.34873)
kolla(u"1,3 km efter en halvtimmes tystnad släpps igenom",
      main.rimligt_steg(tappad, ater, 1000.0 + 31 * 60), True)

# Samma hopp på fem sekunder vore däremot omöjligt.
kolla(u"men samma hopp på fem sekunder stoppas",
      main.rimligt_steg(tappad, ater, 1005.0), False)

# En hund i full fart: 10 m/s är en bra bit över vad en jakthund håller.
kolla(u"en springande hund stoppas aldrig",
      main.rimligt_steg((63.2500, 13.3500, 1000.0), (63.2509, 13.3500), 1010.0),
      True)

# --- småhopp ska aldrig stoppas -----------------------------------------
# En GPS-fix darrar, och efter en lucka i mottagningen kan en stillastående
# hund se ut att ha hoppat några hundra meter. Under gränsen tittar vi inte
# på farten alls — annars hade vakten börjat kasta riktiga positioner.
kolla(u"400 m på en sekund passerar (under gränsen)",
      main.rimligt_steg((63.2500, 13.3500, 1000.0), (63.2536, 13.3500), 1001.0),
      True)
kolla(u"600 m på en sekund stoppas (över gränsen)",
      main.rimligt_steg((63.2500, 13.3500, 1000.0), (63.2554, 13.3500), 1001.0),
      False)

# --- gränsfall som inte får krascha -------------------------------------
kolla(u"första punkten har inget att jämföra med",
      main.rimligt_steg(None, (63.25, 13.35), 1000.0), True)
kolla(u"samma tidsstämpel två gånger släpps igenom",
      main.rimligt_steg((63.2500, 13.3500, 1000.0), (63.3900, 13.4300), 1000.0),
      True)
kolla(u"bakåt i tiden släpps också igenom",
      main.rimligt_steg((63.2500, 13.3500, 1000.0), (63.3900, 13.4300), 999.0),
      True)

print(u"\n%s" % (u"ALLA KONTROLLER OK" if fel == 0 else u"%d FEL" % fel))
sys.exit(1 if fel else 0)
