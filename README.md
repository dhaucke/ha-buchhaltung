# Buchhaltung

Lokale, webbasierte Anwendung für Kunden, Angebote, Auftragsbestätigungen,
Ausgangs- und Eingangsrechnungen, monatliche Dauerrechnungen sowie eine
EÜR-Arbeitsunterlage. Sämtliche Firmendaten und das
Unternehmenslogo werden erst bei der Ersteinrichtung eingegeben und
ausschließlich im persistenten Datenverzeichnis gespeichert.

## Stand 0.5.0

- neutraler Ersteinrichtungs-Assistent ohne fest eingebaute Firmendaten
- eigenes Logo als PNG, JPG oder WebP
- frei einstellbare Firmen-, Steuer- und Bankdaten
- Kleinunternehmerregelung nach § 19 UStG ein- oder ausschaltbar
- frei wählbare Startwerte für Kunden-, Angebots-, Auftrags- und Rechnungsnummern
- Kundenverwaltung mit Rechnungs-E-Mail
- Angebote, Auftragsbestätigungen und Rechnungen
- mehrere Positionen je Dokument mit Live-Summen und bearbeitbaren Entwürfen
- Workflow Angebot → Auftrag → Rechnung
- Rechnungsnummern nach dem Schema `YYYY-MM-NNNN`
- PDF-Erzeugung mit eigener Absenderzeile und eigenem Logo
- Status Entwurf, fertiggestellt, versendet und bezahlt
- Teil- und Vollgutschriften mit Auszahlung oder Verrechnung
- dreistufige Zahlungserinnerungen mit Vorschau vor dem Versand
- ZUGFeRD-/Factur-X-Hybridrechnungen im Profil EN 16931 mit lokaler
  XML-Schema-Validierung
- unveränderter Import alter PDF-Rechnungen mit SHA-256-Prüfsumme
- Massenimport für bis zu 50 PDF-Belege (maximal 20 MB je Datei)
- getrennte Verarbeitung von Ausgangs- und Eingangsrechnungen
- Original-PDFs direkt im Archiv öffnen
- Rechnungsnummer, Datum, Betrag, Kundennummer und Kundenanschrift auslesen
- Lieferanten- und Eingangsrechnungsverwaltung
- EÜR-Kategorien, betrieblicher Anteil und frei wählbares Zahlungsdatum
- EÜR-Jahresauswertung als Bildschirmansicht, PDF und CSV
- Fehlimporte und ungebuchte Entwürfe endgültig löschen
- Storno statt Löschung für gebuchte oder fertiggestellte Belege
- monatliche Dauerrechnungen mit Schutz gegen doppelte Monatsläufe
- optionale automatische PDF-Erzeugung und Versand über Microsoft Graph
- SQLite-Datenbank, Audit-Protokoll und persistentes Datenverzeichnis
- Home-Assistant-Ingress und alternatives Docker Compose

## Home Assistant

Das öffentliche GitHub-Repository kann im App-Store als benutzerdefiniertes
Repository hinzugefügt werden:

```text
https://github.com/dhaucke/ha-buchhaltung
```

Danach:

1. Einstellungen → Apps → App-Store öffnen.
2. Menü → Repositorys öffnen und die URL hinzufügen.
3. „Buchhaltung“ installieren.
4. App starten und „In Seitenleiste anzeigen“ aktivieren.
5. Beim ersten Öffnen den Einrichtungs-Assistenten abschließen.

Alle Nutzdaten liegen im persistenten App-Datenverzeichnis `/data` und werden
von Home-Assistant-Backups erfasst. Im GitHub-Repository werden keine
eingegebenen Firmen-, Kunden- oder Rechnungsdaten gespeichert.

## Ersteinrichtung

Beim ersten Start fragt die Anwendung folgende Bereiche ab:

1. Unternehmensname, Inhaber, Anschrift, Kontaktdaten und Logo
2. Steuer- und Bankdaten, Zahlungsziel und zuletzt vergebene Nummern
3. optional Tenant-ID, Client-ID und Absenderadresse für Microsoft Graph

Die zuletzt vergebene Nummer ist der Zählerstand vor dem ersten neu erzeugten
Dokument. Beispiel: Wird bei Rechnungen `132` eingetragen, erhält die nächste
Rechnung die laufende Nummer `0133`.

## Docker / Umbrel

Im Projektverzeichnis:

```bash
docker compose up -d --build
```

Anschließend ist die Anwendung unter `http://HOST:8099` erreichbar. Für externen
Zugriff muss ein authentifizierender Reverse Proxy mit HTTPS vorgeschaltet
werden. Port 8099 nicht ungefiltert ins Internet freigeben.

## Microsoft Graph und Exchange Application RBAC

Benötigt werden:

- Entra-App-Registrierung
- Exchange Online Application RBAC, begrenzt auf das gewünschte Absenderpostfach
- Zertifikat und privater Schlüssel im PEM-Format

Tenant-ID, Client-ID, Dienstprinzipal-Objekt-ID und Absenderadresse werden unter
„Einstellungen“ eingetragen. Zertifikat und privater Schlüssel können dort
direkt hochgeladen werden; ein SMB-Zugriff auf Home Assistant ist dafür nicht
nötig. Die Microsoft-Einrichtung zeigt die passenden Exchange-Online-
PowerShell-Befehle und bietet einen Verbindungstest ohne Mailversand.

Die enge Exchange-RBAC-Rolle `Application Mail.Send` ist der
organisationsweiten Graph-Anwendungsberechtigung vorzuziehen. Eine zusätzlich
in Entra erteilte, organisationsweite `Mail.Send`-Berechtigung wirkt additiv und
würde die Beschränkung auf das eine Postfach ausweiten.

## E-Rechnung

Für fertiggestellte Rechnungen und Gutschriften kann ein ZUGFeRD-/Factur-X-PDF
im Profil EN 16931 erzeugt werden. Die Anwendung:

1. erzeugt den strukturierten CII-XML-Datensatz,
2. validiert ihn lokal gegen das mitgelieferte XML-Schema,
3. bettet ihn in das PDF ein und
4. verwendet danach dieses Hybrid-PDF beim Mailversand.

Die XRechnung mit vollständiger KoSIT-Geschäftsregelvalidierung ist noch nicht
freigeschaltet. Eine bloß syntaktisch gültige XML-Datei wird bewusst nicht als
„XRechnung-validiert“ bezeichnet.

Kleinunternehmer sind aktuell von der Pflicht zur Ausstellung einer
E-Rechnung ausgenommen, müssen E-Rechnungen aber empfangen können. Die
ZUGFeRD-Ausgabe ist daher eine freiwillige, zukunftssichere Funktion.

## EÜR und Beleglöschung

Die Auswertung ordnet Einnahmen und Ausgaben dem erfassten Zahlungsdatum zu.
Historische Ausgangsrechnungen werden im Archiv als bezahlt markiert;
Eingangsrechnungen werden mit Lieferant, Kategorie, Betrag, betrieblichem Anteil
und Zahlungsdatum gebucht.

Der PDF- und CSV-Export ist eine Arbeitsunterlage, keine direkte
ELSTER-Übermittlung. Sonderfälle wie die Zehn-Tage-Regel für regelmäßig
wiederkehrende Zahlungen, AfA, Einlagen/Entnahmen, Bewirtungsanteile und private
Nutzungsanteile müssen fachlich geprüft werden.

Nur unzugeordnete Fehlimporte und Entwürfe können physisch gelöscht werden.
Bereits gebuchte, versendete oder anderweitig steuerlich relevante Belege werden
storniert und bleiben zusammen mit dem Audit-Eintrag erhalten.

## Datensicherung

Das komplette `/data`-Verzeichnis sichern. Es enthält:

- `buchhaltung.sqlite3`
- `company-logo.png`
- erzeugte PDF-Dokumente
- importierte Altbelege
- erzeugte EÜR-Berichte
- Graph-Zertifikatsdateien, falls dort abgelegt

Private Schlüssel müssen in verschlüsselten Backups geschützt werden.

## Entwicklungstest

```bash
HD_DATA_DIR="$(mktemp -d)" python3 buchhaltung/app/web.py
```

Tests:

```bash
python3 -m unittest discover -s tests -v
```

## Noch vorgesehen

- XRechnung-Ausgabe mit offizieller KoSIT-Geschäftsregelvalidierung
- Import und Visualisierung eingehender strukturierter E-Rechnungen
- automatische, optional aktivierbare Erinnerungsläufe
