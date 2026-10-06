# PimpMyKernel

[English](README.md) · **Deutsch**

Ein Desktop-Assistent für Debian/Ubuntu auf Deutsch, Englisch und Spanisch: offizielle Linux-Kernel von
kernel.org auswählen, mit der vorhandenen `/boot/config-*` bauen, `.deb`-Pakete
speichern und anschließend auf Wunsch installieren. Die Anwendung selbst wird
ebenfalls als `.deb` ausgeliefert. Das Appicon stammt aus `icon/PimpMyKernel.png`.

## Installation der Anwendung

Ubuntu 24.04 oder neuer bzw. ein Debian-System mit Python **3.12+**, Qt **6.4+**
und grafischer Sitzung:

Das fertige App-Paket gibt es unter
[GitHub Releases](https://github.com/Massefehler/PimpMyKernel/releases/latest).
Die `.deb` herunterladen und im Downloadordner installieren:

```bash
sudo apt install ./pimpmykernel_0.1.8_all.deb
```

Danach **PimpMyKernel** im Anwendungsmenü öffnen oder `pimpmykernel` ausführen.
Die Oberfläche immer als normalen Benutzer starten. Eine grafische
Administratorabfrage erscheint nur bei Aktionen, die sie benötigen.

Für jede neue Version das App-Paket wieder erstellen:

```bash
./build-deb.sh
```

Das Skript verwendet nur Python und `dpkg-deb`, benötigt keine Root-Rechte und
schreibt nach `dist/`. Für einen anderen Zielordner:
`./build-deb.sh --output /pfad/zum/ordner`. `SOURCE_DATE_EPOCH` kann für
reproduzierbare Paket-Metadaten gesetzt werden.
Ein selbst erstelltes App-Paket wird mit
`sudo apt install ./dist/pimpmykernel_0.1.8_all.deb` installiert.

## Systemsprache und manuelle Sprachauswahl

Standardmäßig erkennt die Anwendung bei jedem Start die bevorzugten
Oberflächensprachen des Desktops über Qt und verwendet die erste unterstützte Sprache:
**Deutsch**, **Englisch** oder **Spanisch**. Regionale Varianten wie `de_AT`,
`de_CH`, `en_GB`, `en_US`, `es_ES`, `es_MX` und `es_AR` werden berücksichtigt.
Wird keine unterstützte Sprache gefunden,
erscheint die Oberfläche auf Englisch.

Unter **Systemcheck → Sprache / Language** kann zwischen **Automatisch
(Systemsprache)**, **Deutsch**, **English** und **Español** gewählt werden. Eine manuelle
Auswahl hat Vorrang vor der Systemsprache; damit lässt sich beispielsweise auf
einem spanischen System ausdrücklich Englisch verwenden. Die Auswahl wird
sofort gespeichert und gilt ab dem **nächsten App-Start**. Laufende Builds
arbeiten in ihrer bisherigen Sprache weiter. Mit **Automatisch** folgt die
Anwendung nach dem nächsten Start wieder den Systemeinstellungen. Ungültige
oder inzwischen nicht unterstützte gespeicherte Werte fallen auf Automatisch
zurück.

Auch Statusmeldungen, Tooltips, Hilfe, Fehlermeldungen und die Dialoge für
Kernel-Installation, Deinstallation und menuconfig werden übersetzt. Zahlen
und Datumsangaben folgen dem Systemformat. Qt-eigene Schaltflächen und
Dateidialoge nutzen `qt6-translations-l10n`, das als Paketabhängigkeit
mitinstalliert wird. Externe Ausgaben von make, APT und GPG bleiben für
zuverlässiges Auslesen im bisherigen C-Format; die Anwendung übersetzt ihre
eigenen Hinweise. Administrator- und Terminal-Helfer übernehmen diese Sprache
ausdrücklich, auch wenn ihre Ausführungsumgebung bereinigt wird.

Für weitere Sprachen ist die Übersetzung getrennt vom Anwendungscode:
`pimpmykernel/assets/i18n/en.json` und `es.json` bilden die deutschen Originaltexte
auf ihre englischen und spanischen Übersetzungen ab. Neue Kataloge erhalten
denselben Aufbau, werden in `SUPPORTED_LANGUAGES` in `pimpmykernel/i18n.py`
eingetragen und in der Sprachauswahl mit ihrem eigenen Sprachnamen ergänzt. Benannte
Platzhalter wie `{version}` müssen erhalten bleiben. Die Tests prüfen
Vollständigkeit und Platzhalter. Bei einer unvollständigen weiteren Übersetzung
greift die englische Fassung. Eine Änderung der Anwendung wird immer mit einem
neu gebauten `.deb` ausgeliefert.

## Ablauf in der Oberfläche

1. **Kernel auswählen:** Die aktuelle Liste kommt live aus
   [releases.json](https://www.kernel.org/releases.json). Stable ist vorausgewählt;
   gepflegte LTS-Versionen stehen ebenfalls zur Verfügung. RCs können angezeigt
   werden, sind ohne offizielle Release-Signatur aber nicht baubar. Über
   **Ältere Versionen laden** wird das offizielle Archiv aller Kernelreihen ab
   Linux 4.x ergänzt. Damit lassen sich auch frühere Versionen wie **7.2.8**
   oder **7.2.3** wieder auswählen, bauen und anschließend installieren.
   **Kernelreihe** und **Versionssuche** lassen sich kombinieren, etwa Linux 6.x
   und `6.1.`. Die Liste ist nach Version absteigend sortiert; das aktuelle
   Stable-Release bleibt vorausgewählt. Aktuelle Releases und Archiv werden
   ohne doppelte Einträge zusammengeführt. Das Archiv lässt sich bei Bedarf
   aktualisieren; die aktuelle Release-Abfrage erhält bereits geladene ältere
   Versionen und die Auswahl. Es werden nur Quellarchive mit zugehöriger
   Release-Signatur angeboten, die vor dem Build geprüft wird. Läuft das
   System bereits auf dem neuesten stabilen Kernel, erscheint ein deutlicher
   Hinweis. Zusätze wie `-pmk` und die Distributions-ABI werden beim Vergleich
   der zugrunde liegenden Kernel-Version berücksichtigt.
2. **Build vorbereiten:** Die nach Version neueste `/boot/config-*` wird
   vorausgewählt. Eine andere vorhandene Konfiguration kann gewählt werden.
   Die aktive Kernel-Konfiguration ist in der Liste markiert.
3. **Jobs wählen:** Getrennte Auswahl für `make` und `make bindeb-pkg`.
   Angeboten werden unter anderem `-j8`, `-j10`, `-j12`, `-j16`, sofern die
   CPU-Affinität des Prozesses genug Threads zulässt, sowie kleinere Werte.
4. **Ausgabeordner wählen und starten:** Der Assistent prüft Werkzeuge und
   freien Speicher, lädt das offizielle `.tar.xz` und die `.tar.sign`, entpackt
   das Tar-Archiv und prüft dessen OpenPGP-Signatur **vor** jeder Ausführung
   aus dem Quellbaum. Nur hinterlegte Herausgeber werden akzeptiert. Ein eigener
   Downloadbalken zeigt Prozent und übertragene MiB. Erst nach einem erfolgreich
   abgeschlossenen Download steht er auf **100 %** und bleibt während der
   weiteren Build-Schritte sichtbar. Bei unbekannter Gesamtgröße wird zunächst
   nur die übertragene Datenmenge angezeigt.
5. **Konfiguration:** Die ausgewählte Datei wird als `.config` kopiert.
   `make oldconfig` erhält leere Antworten für die Kernel-Standardwerte neuer
   Optionen. Sichtbare Build-Optionen werden danach angewendet;
   `make olddefconfig` löst ihre Abhängigkeiten auf. Auf Wunsch folgt für
   erfahrene Nutzer `make menuconfig` mit anschließender Änderungsprüfung.
6. **Build:** `make -jN LOCALVERSION=-pmk`, danach
   `make bindeb-pkg -jM LOCALVERSION=-pmk KDEB_PKGVERSION=<version>-1`.
7. **Export:** Alle erzeugten `.deb`-Dateien kommen in einen neuen, eindeutigen
   Unterordner. Vorhandene Pakete werden nicht überschrieben. Der Export enthält
   außerdem `kernel.config`, `original.config`, `config-changes.diff`,
   `manifest.json` mit SHA-256-Prüfsummen und das vollständige `build.log`.
8. **Aufräumen:** Bei aktivierter Option werden das Archiv und ausschließlich
   der eigene markierte Build-Job gelöscht. Erst nach geprüftem Export.
9. **Installation:** Paketliste prüfen und „Kernel installieren“ wählen. APT
   installiert die lokalen Pakete über `pkexec`. Bisherige Kernel werden nicht
   entfernt. Einen Neustart führt die Anwendung nicht selbst aus. **Alle
   exportierten Kernel-.deb-Dateien bleiben auch nach der Installation im
   gewählten Exportordner erhalten.** Das Aufräumen betrifft nur den eigenen
   Build-Arbeitsordner, niemals diese Exporte oder gespeicherte Protokolle.

## Optionale manuelle Kernel-Konfiguration

Unter **Build vorbereiten → Für Linux-Profis und Enthusiasten** kann
**Vor dem Kompilieren make menuconfig öffnen** eingeschaltet werden. Die
Funktion ist bei jedem App-Start ausgeschaltet. Der sichtbare Hinweis erklärt:
Man sollte wissen, welche Optionen man ändert. Fehlerhafte Einstellungen können
Treiber entfernen oder dazu führen, dass der Kernel nicht startet.

Nach Download, Signaturprüfung und Übernahme der vorhandenen Konfiguration
öffnet sich `make menuconfig` in einem eigenen **xterm**-Terminal. xterm wird
vom App-Paket empfohlen und bei einer üblichen APT-Installation mit installiert.
Fehlt es, meldet die App dies bereits vor dem Build; es lässt sich mit
`sudo apt install xterm` ergänzen. Die Konfigurationsbibliotheken gehören zu den
bereits angebotenen Build-Werkzeugen.

Im Menü die gewünschten Optionen ändern, in **`.config`** speichern und
menuconfig beenden. Anschließend zeigt PimpMyKernel die gespeicherten Änderungen
gegenüber der vorbereiteten Konfiguration. Erst **Build fortsetzen** startet
die Kompilierung. **Build abbrechen** beendet den Vorgang und bietet danach
das Aufräumen an. Ein vorzeitig geschlossenes Terminal oder ein Fehler führt
zu keinem automatischen Weiterbauen.

Die vorher angewendeten Build-Optionen lassen sich im Menü gezielt ändern;
manuelle Einstellungen haben Vorrang. `make olddefconfig` löst anschließend
deren Abhängigkeiten auf. Der eigene Paketsuffix **`-pmk`** bleibt erhalten.
Alle Befehle laufen als normaler Benutzer; die ursprüngliche Konfiguration
wird nur gelesen. Zusätzlich zu den üblichen Exportdateien werden
**`before-menuconfig.config`** und **`menuconfig-changes.diff`** gespeichert.
`kernel.config` enthält die endgültige Konfiguration, und `config-changes.diff`
zeigt weiterhin alle Änderungen gegenüber der ursprünglichen Datei.

## Installierte Kernel verwalten

Die Rubrik **Installierte Kernel** liest die tatsächlich installierten Pakete
aus der Debian-Paketdatenbank. Pro Kernel werden die Version, die zugehörigen
Pakete und deren installierte Größe angezeigt. Über die Auswahlkästchen können
mehrere ältere Kernel ausgewählt und mit **Ausgewählte Kernel deinstallieren**
entfernt werden.

Der laufende Kernel trägt die sichtbare Notiz **„Dieser Kernel läuft gerade und
kann nicht entfernt werden.“** und kann nicht zur Deinstallation ausgewählt
werden. Beim Bewegen des Mauszeigers über den Eintrag erklärt ein Tooltip den
Schutz und zeigt die zugehörigen Pakete. Auch der
letzte installierte Kernel bleibt geschützt, wenn der laufende Kernel nicht
in der Paketdatenbank erfasst ist. Zugehörige Header, Module, Debug- und
versionsgebundene Werkzeuge werden mit entfernt. Gemeinsam verwendete Header
und Werkzeuge bleiben für andere installierte Kernel erhalten; allgemeine Pakete wie
`linux-libc-dev` werden nicht entfernt.

Vor der Bestätigung prüft eine APT-Simulation die konkrete Paketliste. Würde
APT weitere Pakete entfernen, etwa die Meta-Pakete für automatische
Distributions-Kernel-Updates, wird die Deinstallation mit einer Erklärung
gestoppt. Im Bestätigungsdialog wird die genaue Liste gezeigt. Erst nach der
bewussten Auswahl und grafischen Administratorfreigabe führt APT die Entfernung
aus. Der Root-Helfer prüft den laufenden Kernel, die Paketliste und den
APT-Entfernungsplan erneut. Es erfolgt kein pauschales `autoremove`.

Die Liste wird danach neu geladen. Exportierte `.deb`-Dateien und
Build-Protokolle bleiben auch nach der Deinstallation erhalten.

## Build-Optionen und Voraussetzungen

- **Debugsymbole/BTF weglassen** ist vorausgewählt und spart erheblich Zeit,
  Speicher und Paketgröße. Für BTF-basiertes eBPF/Tracing diese Option abwählen.
- **Fehlende Zertifikatpfade automatisch bereinigen · Ubuntu/Canonical** ist
  vorausgewählt. Bei der ersten Übernahme einer Ubuntu-Konfiguration werden
  fehlende Einträge wie `debian/canonical-certs.pem` und
  `debian/canonical-revoked-certs.pem` in `CONFIG_SYSTEM_TRUSTED_KEYS` und
  `CONFIG_SYSTEM_REVOCATION_KEYS` auf `""` gesetzt. Nur das Wort `canonical`
  aus dem Dateinamen zu löschen würde wieder auf eine fehlende Datei verweisen.
  Der normale Modulschlüssel `certs/signing_key.pem` bleibt erhalten und wird
  von kbuild automatisch erzeugt. Vorhandene Zertifikatdateien bleiben erhalten;
  ein fehlender externer Modulschlüssel wird auf den kbuild-Schlüssel umgestellt.
  Angepasst wird nur die kopierte `.config`, die ursprüngliche `/boot/config-*`
  bleibt unverändert. Das Protokoll zeigt die alten und neuen Pfade.
- Der Suffix **`-pmk`** trennt den eigenen Kernel vom vorhandenen Kernel.
- Der Systemcheck bietet die Installation aller Build-Werkzeuge über APT an.
  Die Anwendung benötigt sie erst für einen Kernel-Build.
- Mindestens **30 GiB** freien Platz einplanen, mit Debugsymbolen eher **60 GiB
  oder mehr**. Der Umfang hängt von der übernommenen Konfiguration ab.
  Die Vorprüfung blockiert unter 10 GiB bzw. 25 GiB mit Debugsymbolen.
- Selbst gebaute Kernel sind nicht automatisch für **Secure Boot** signiert.
  Bei erkannt aktivem Secure Boot bleibt die integrierte Installation gesperrt;
  Export und ein eigener Signierungsablauf sind möglich. Ist der Status nicht
  erkennbar, fragt die Installation nach einer bewussten Bestätigung.
- Distributions- und Drittanbietertreiber wie NVIDIA/DKMS können zusätzliche
  Schritte benötigen. Die Übernahme einer alten Konfiguration garantiert
  nicht, dass ein neuer Kernel auf jeder Hardware startet.

## Fehler und Abbruch

Der Download, die Build-Prozessgruppe und das menuconfig-Terminal sind abbrechbar. Nach einem Klick auf
**Build abbrechen** werden zuerst die laufenden Prozesse beendet. Anschließend
erscheint die Auswahl **Arbeitsdateien löschen** oder **Zur Fehlersuche behalten**.
Löschen entfernt den Download, den Quellbaum, Kompilierdateien, noch nicht
exportierte Pakete und temporäre Hilfsdateien des eigenen markierten Jobs.
Das gespeicherte Build-Protokoll und bereits exportierte Pakete bleiben erhalten.
Aufgeräumt wird im Hintergrund, damit die Oberfläche bedienbar bleibt.

Bei Buildfehlern oder wenn Dateien behalten werden, bleibt der Arbeitsordner
zur Diagnose verfügbar. Er kann geöffnet und später über **Arbeitsordner
löschen** entfernt werden. Beim Schließen des Fensters während eines Builds
werden die Prozesse beendet und die Arbeitsdateien behalten. APT-Aktionen werden
zu Ende geführt. Das Fenster wartet vor dem Schließen auf aktive Worker.

Arbeitsdateien: `${XDG_CACHE_HOME:-~/.cache}/pimpmykernel/jobs/`.
Die Build-Befehle erhalten ein eigenes `tmp/` innerhalb ihres Arbeitsordners als
`TMPDIR`, `TMP` und `TEMP`. Diese Hilfsdateien werden beim Aufräumen mit entfernt;
das allgemeine `/tmp` wird nicht pauschal geleert.
Vollständige Build-Protokolle:
`${XDG_STATE_HOME:-~/.local/state}/pimpmykernel/logs/`.
Einstellungen werden über Qt in der Benutzerkonfiguration gespeichert.
Beim Start wird immer neu von kernel.org abgefragt; es gibt keine eingebauten
Release-Versionsnummern und keine automatischen Installationen.

## Aus dem Quellcode starten

```bash
sudo apt install git python3-pyqt6 libqt6svg6 qt6-translations-l10n pkexec ca-certificates gnupg xterm
git clone https://github.com/Massefehler/PimpMyKernel.git
cd PimpMyKernel
./start.sh
```

Optional mit virtueller Umgebung:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
./start.sh
```

`python3 install-desktop.py` legt für den Quellcode-Start einen lokalen
Anwendungsmenü-Eintrag an. Bei Installation des `.deb` ist das nicht nötig.

## Entwicklung und Prüfungen

```bash
./start.sh --check
./start.sh --list-releases
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
./build-deb.sh
dpkg-deb --info dist/pimpmykernel_0.1.8_all.deb
```

Die Tests prüfen den vollständigen Ablauf mit einem lokalen Test-Kernel und
echten, kleinen Debian-Testpaketen sowie Signaturfehler, Export-Prüfsummen,
Pfadschutz, Prozessabbruch, Download-Prozentanzeige und Qt-Interaktionen. Die
Archiv-Auswahl wird auf signierte Dateipaare, Versionssortierung, alle
Kernelreihen, kombinierte Filter, Abbruch und Wiederholung nach Ladefehlern geprüft. Die
Kernelverwaltung wird mit lokalen Paketdaten und simulierten APT-Plänen geprüft,
einschließlich Schutz des laufenden und letzten Kernels sowie gemeinsam
verwendeter Header. Die Tests installieren oder deinstallieren keinen Kernel.
Die menuconfig-Tests prüfen einen echten Terminalkanal mit Tastatureingabe,
Fehler und vorzeitiges Schließen des Terminals, den Abbruch einschließlich
untergeordneter Prozesse, die Änderungsprüfung vor der Kompilierung und den
Export der manuell gewählten Konfiguration.
Die Sprachtests prüfen Deutsch, Englisch und Spanisch auf vollständige
Übersetzungen, unveränderte Platzhalter, automatische Spracherkennung und
gespeicherte Auswahl nach einem Neustart. Sie prüfen auch die übersetzten
Kernel-Schutzmeldungen, Standarddialoge und Terminal-Hinweise.
Der optionale Test `python3 tests/live_prepare.py --config /boot/config-<version>`
lädt einen echten Kernel, prüft die Release-Signatur und führt die Übernahme der
gewählten Konfiguration einschließlich Zertifikatbereinigung aus. Er kompiliert
oder installiert keinen Kernel.

Offizielle Grundlagen:
[Kernel-Signaturen](https://www.kernel.org/signature.html),
[Kernel-Konfiguration](https://docs.kernel.org/kbuild/kconfig.html),
[Kernel-Quellcode und Build](https://docs.kernel.org/admin-guide/README.html),
[APT-Simulation und Paketentfernung](https://manpages.debian.org/trixie/apt/apt-get.8.en.html).

Code-Lizenz: GPL-3.0-or-later, siehe `LICENSE`.
