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

# --- en hund som hörs av igen efter en lång tystnad ----------------------
# 18 okt 12:43: Wikström låg NotConnected på en frusen punkt och rapporterade den
# var 30:e sekund. Kom tillbaka 686 m därifrån. Vakten nollställde klockan vid
# varje upprepning, räknade 19 s i stället för minuterna hon varit borta (36 m/s)
# och stoppade en riktig position — plus att alla namn frågades om i onödan.
FRUSEN = (63.334959, 13.346479)
TILLBAKA = (63.336096, 13.334656)
kolla(u"avståndet är det loggen visade (686 m)",
      round(main.meter(FRUSEN, TILLBAKA)), 686)


def spela(nollstall_vid_upprepning):
    """Hunden hörs av sist vid t=0 och upprepar sedan punkten var 30:e sekund
    i nästan nio minuter. Returnerar om den nya positionen vid t=529 släpps igenom."""
    senaste = {}
    uid = "hund-wikstrom"
    main.notera_punkt(senaste, uid, FRUSEN[0], FRUSEN[1], 0.0)
    for t in range(30, 540, 30):
        if nollstall_vid_upprepning:
            senaste[uid] = (FRUSEN[0], FRUSEN[1], float(t))     # så som det var förut
        else:
            main.notera_punkt(senaste, uid, FRUSEN[0], FRUSEN[1], float(t))
    # Sista upprepningen kom vid t=510 och hunden hörs av vid t=529: 19 s senare,
    # precis som i loggen (12:43:27 -> 12:43:46).
    return main.rimligt_steg(senaste.get(uid), TILLBAKA, 529.0)


kolla(u"förut: den riktiga positionen stoppades (felet)", spela(True), False)
kolla(u"nu: den släpps igenom", spela(False), True)

# Klockan ska stå kvar på när punkten var NY, inte när den skickades om.
senaste = {}
main.notera_punkt(senaste, "x", 63.1, 13.1, 100.0)
main.notera_punkt(senaste, "x", 63.1, 13.1, 130.0)
main.notera_punkt(senaste, "x", 63.1, 13.1, 160.0)
kolla(u"upprepad punkt behåller tidsstämpeln", senaste["x"][2], 100.0)

main.notera_punkt(senaste, "x", 63.2, 13.1, 190.0)
kolla(u"en ny punkt tar över klockan", senaste["x"], (63.2, 13.1, 190.0))

# Skyddet mot det vakten är till för får inte försvagas: en hund som är i kontakt
# hela tiden och plötsligt "är" 16,8 km bort är fortfarande omöjlig.
levande = {}
main.notera_punkt(levande, "y", 63.24408, 13.35722, 1000.0)
main.notera_punkt(levande, "y", 63.24409, 13.35723, 1001.5)
main.notera_punkt(levande, "y", 63.24410, 13.35724, 1003.0)
kolla(u"levande hund som 'teleporterar' stoppas fortfarande",
      main.rimligt_steg(levande["y"], (63.39017, 13.43142), 1005.0), False)

# Tom post (efter ett namnbyte utan tidigare punkt) får inte krascha.
tomma = {"z": None}
main.notera_punkt(tomma, "z", 63.0, 13.0, 5.0)
kolla(u"saknad tidigare punkt kraschar inte", tomma["z"], (63.0, 13.0, 5.0))

print(u"\n%s" % (u"ALLA KONTROLLER OK" if fel == 0 else u"%d FEL" % fel))
sys.exit(1 if fel else 0)
