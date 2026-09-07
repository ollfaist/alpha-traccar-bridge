#!/bin/sh
# Gör systemd-journalen beständig på bryggan. Körs en gång, som root.
#
# Bakgrund: /var/log/journal fanns men var tom, så journald skrev till
# /run/log/journal — alltså RAM. Efter jakten 7 sep 2026, när hundarna föll
# bort ur kartan, gick det inte att felsöka i efterhand: allt hade raderats när
# Pi:n startades om. Traccars egen logg räddade den utredningen, men bryggans
# sida av historien var borta.
#
# Storleken hålls nere med flit: Pi:n loggar en rad per position och hund
# (~2400 rader per hund och timme) på ett SD-kort som inte tål att fyllas.

set -e

if [ "$(id -u)" != "0" ]; then
    echo "Kör som root: sudo $0" >&2
    exit 1
fi

install -d -g systemd-journal -m 2755 /var/log/journal
mkdir -p /etc/systemd/journald.conf.d

cat > /etc/systemd/journald.conf.d/persistent.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
MaxRetentionSec=1month
EOF

systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald

# En omstart av journald räcker inte: den fortsätter skriva i /run tills
# journalen flyttas över. Vid varje boot gör systemd-journal-flush.service
# detta automatiskt, men första gången får vi göra det själva.
journalctl --flush
journalctl --sync

echo "Klart. Journalen ligger nu i /var/log/journal och överlever omstart:"
journalctl --list-boots 2>/dev/null | tail -3
du -sh /var/log/journal
