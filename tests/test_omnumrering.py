# -*- coding: utf-8 -*-
"""Spelar upp jakten 12 sep 2026 mot namnhanteringen i ant_listener.

Kör: python tests/test_omnumrering.py

Bakgrund: ANT+-profilen pekar ut en hund med sin PLATS i handenhetens lista,
och Alphan numrerar om listan när en hund läggs till eller tas bort. Platsen
är alltså inte hunden. Bryggan cachade namnet per plats och satte name_done
en gång för alla — det fanns ingen förnyelse — så efter en omnumrering
rapporterade "Hundar 8" som "Hundar 6" och sedan som "Hundar". Tre Traccar-
enheter fick två hundar var, och en av dem hoppade 20,9 km mellan två
rapporter.

Det som provas här är att ett namn har en ålder, att en ändrad lista kastar
alla bekräftelser, och att en position aldrig går ut på ett obekräftat namn.
"""
import os
import sys
import time

# Nyckeln används aldrig här — ant_listener vägrar bara importeras utan den.
os.environ.setdefault("ANT_NETWORK_KEY", "0000000000000000")

HAR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HAR), "src"))


def _attrapp(namn):
    """openant finns bara på Pi:n med donglen. Namnhanteringen rör aldrig
    radion, så en attrapp räcker — och ett test man bara kan köra på Pi:n
    blir ett test man slutar köra."""
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

    # Paketen måste läggas in i föräldraordning, annars är "openant.easy"
    # inte ett paket när "openant.easy.channel" ska hämtas.
    for bit in ("openant", "openant.easy", "openant.easy.node",
                "openant.easy.channel"):
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


def nollstall():
    al.sync_buffer.clear()
    al._namn_tid.clear()
    al._aktiva_platser.clear()
    al._plats_sedd.clear()
    with al._pending_lock:
        al._pending_names.clear()


def namnsidor(plats, namn):
    """Härmar Alphans två identifikationssidor (0x10 och 0x11)."""
    rad = namn.encode("utf-8").ljust(10, b"\x00")
    al.sync_buffer[str(plats) + "_name1"] = rad[:5]
    al.sync_buffer[str(plats) + "_name2"] = rad[5:10]
    al._update_name(plats)


print(u"\nOmnumrering av hundlistan:\n")

# --- namnet har en ålder -------------------------------------------------
nollstall()
namnsidor(105, u"Hundar 8")
kolla(u"namnet läses in", al.sync_buffer["105_name"], u"Hundar 8")
kolla(u"och är färskt direkt", al.namn_farskt(105), True)

al._namn_tid[105] = time.time() - (al._NAMN_TTL + 1)
kolla(u"men inte för alltid", al.namn_farskt(105), False)

# Det var precis det här som saknades: name_done satt kvar och blockerade
# förnyelsen, så ett namn kunde överleva hur länge som helst.
al._maybe_request_name(object(), 105)
with al._pending_lock:
    kolla(u"en gammal bekräftelse frågas om igen", 105 in al._pending_names, True)

namnsidor(105, u"Hundar 8")
kolla(u"och blir färsk igen när Alphan svarar", al.namn_farskt(105), True)

# --- en ändrad lista kastar alla bekräftelser ---------------------------
nollstall()
namnsidor(104, u"Hundar 6")
namnsidor(105, u"Hundar 8")
al._se_plats(104)
kolla(u"första platsen ändrar inget", al.namn_farskt(104), True)

al._se_plats(105)
kolla(u"men en ny plats kastar bekräftelserna", al.namn_farskt(104), False)
kolla(u"för alla platser, inte bara den nya", al.namn_farskt(105), False)
with al._pending_lock:
    kolla(u"och båda frågas om", sorted(al._pending_names), [104, 105])

# Namnet finns kvar för loggens skull — det är tilliten vi kastat, inte texten.
kolla(u"namnet finns kvar att logga", al.sync_buffer.get("104_name"), u"Hundar 6")

# --- själva olyckan: samma koordinat, tre identiteter -------------------
# 09:35:46 rapporterade plats 105 som "Hundar 8" på 63.38689, 13.27719.
# 09:35:51 rapporterade plats 104 samma punkt — men med namnet "Hundar 6",
# för listan hade numrerats om och bryggan trodde fortfarande på sin cache.
nollstall()
namnsidor(104, u"Hundar 6")
namnsidor(105, u"Hundar 8")
al._se_plats(105)
al._se_plats(104)      # hunden gled ner en plats — listan ändrade form

kolla(u"ingen plats får längre ett namn på förtroende",
      (al.namn_farskt(104), al.namn_farskt(105)), (False, False))

# Efter att Alphan svarat igen bär plats 104 rätt hund.
namnsidor(104, u"Hundar 8")
kolla(u"och efter svaret står rätt hund på rätt plats",
      (al.sync_buffer["104_name"], al.namn_farskt(104)), (u"Hundar 8", True))

# --- en plats som TYSTNAR avslöjar också en omnumrering ----------------
# Det var så det gick till 12 sep: plats 105 slutade rapportera och hundarna
# under gled ner ett steg — in på platser som redan var kända. En kontroll
# som bara letar efter NYA platser hade inte märkt något alls.
nollstall()
for plats in (103, 104, 105):
    namnsidor(plats, u"Hund %d" % plats)
    al._se_plats(plats)
namnsidor(103, u"Hund 103")
namnsidor(104, u"Hund 104")
namnsidor(105, u"Hund 105")
kolla(u"alla tre är bekräftade när listan står stilla",
      all(al.namn_farskt(p) for p in (103, 104, 105)), True)

# Plats 105 tystnar: hunden togs bort ur listan.
al._plats_sedd[105] = time.time() - (al._PLATS_TYST + 1)
al._se_plats(104)
kolla(u"en tystnad plats kastar bekräftelserna", al.namn_farskt(104), False)
kolla(u"och platsen räknas inte längre som aktiv", 105 in al._aktiva_platser, False)

# Och den ska inte fortsätta larma varje varv efteråt.
namnsidor(104, u"Hund 104")
al._se_plats(104)
kolla(u"men larmar inte om igen på samma tystnad", al.namn_farskt(104), True)

# --- glom_namnen ska tåla att anropas när inget finns -------------------
nollstall()
al.glom_namnen(u"inget att glömma")
kolla(u"tom glömska kraschar inte", al._namn_tid, {})

print(u"\n%s" % (u"ALLA KONTROLLER OK" if fel == 0 else u"%d FEL" % fel))
sys.exit(1 if fel else 0)
