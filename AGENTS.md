# Projektvorgaben

- Ziel und Ablauf stehen in `Projektziel.txt`. Die Anwendung ist ein deutscher
  Desktop-Assistent für Debian/Ubuntu, kein Webdienst.
- Die Anwendung **immer als installierbare `.deb`-Datei ausliefern**, auch nach
  späteren Änderungen. Dafür `./build-deb.sh` ausführen und das fertige Paket
  unter `dist/` prüfen. Ein Quellcode-Start allein erfüllt die Auslieferung nicht.
- Das bereitgestellte Appicon ist `icon/PimpMyKernel.png`; die Laufzeitkopie
  liegt unter `pimpmykernel/assets/icon.png`.
- Version in `pimpmykernel/__init__.py` und `pyproject.toml` konsistent halten.
- Kernel-Builds laufen als normaler Benutzer. Privilegierte Aktionen sind auf
  Konfigurationslesen, Build-Abhängigkeiten und die bewusst gewählte Installation
  beschränkt. Den eigenen Arbeitsbereich automatisch erst nach erfolgreichem
  Export löschen. Beim bewusst abgebrochenen Build das Aufräumen anbieten und
  erst nach beendeten Build-Prozessen sowie der Auswahl „Arbeitsdateien löschen“
  den zugehörigen markierten Job löschen. Protokolle und Exporte erhalten.
- Passende Prüfungen: `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v`,
  `./start.sh --check`, Paketinhalt und Desktop-Eintrag prüfen. Ein kompletter
  echter Kernel-Build oder eine Systeminstallation ist für die Entwicklung der
  Oberfläche nicht erforderlich; Ergebnisse entsprechend ehrlich beschreiben.
