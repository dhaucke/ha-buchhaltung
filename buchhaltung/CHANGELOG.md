# Changelog

## 0.6.4

- neuer Button „Testmail senden“ auf der Microsoft-Einrichtungsseite prüft
  den kompletten Mailversand über Graph, nicht nur die Zertifikatsanmeldung
- Netzwerk- und Anmeldefehler von Microsoft Graph (z. B. falsche Tenant-ID,
  nicht erreichbar) werden jetzt als verständliche Fehlermeldung angezeigt
  statt als unbehandelter Serverfehler

## 0.6.3

- Zertifikat für die Microsoft-Graph-Anmeldung kann jetzt direkt im Add-on
  erstellt werden, statt es extern (z. B. mit OpenSSL) selbst erzeugen zu
  müssen
- der private Schlüssel verlässt dabei nie das Gerät; nur die
  Zertifikatsdatei wird zum Hochladen bei Microsoft zum Download angeboten
- manueller Upload eines eigenen Zertifikats bleibt als Alternative möglich

## 0.6.2

- Microsoft-Graph-Einrichtung überarbeitet: alle Felder (Tenant-ID,
  Client-ID, Dienstprinzipal-Objekt-ID, Absenderadresse, Zertifikat,
  privater Schlüssel) sind jetzt gesammelt auf der Microsoft-
  Einrichtungsseite statt auf zwei Seiten verteilt
- direkter Link zur passenden Entra-App-Registrierung, sobald die
  Client-ID eingetragen ist
- Status zeigt konkret an, welche Angaben noch fehlen, statt nur
  „vollständig/unvollständig“

## 0.6.1

- Lieferantenerkennung bei Eingangsrechnungen unterstützt jetzt auch
  kommagetrennte Kopfzeilen ("Firma, c/o Adresse, PLZ Ort"), nicht nur das
  bisherige Postfach-Format – behebt falsche Erkennung nach einem
  Adressumzug eines Lieferanten

## 0.6.0

- ZUGFeRD-/Factur-X-Rechnungen werden zusätzlich zur XML-Schema-Prüfung
  gegen die EN-16931-Geschäftsregeln (Schematron) validiert – offline mit
  dem offiziellen Mustang-Validator, kein externer Server nötig
- dabei einen echten Validierungsfehler in der eigenen Rechnungserzeugung
  gefunden und behoben (fehlende Verkäufer-Identifikation nach BR-CO-26)
- das Add-on-Image enthält dafür jetzt eine schlanke Java-Laufzeitumgebung

## 0.5.11

- nach dem Speichern einer Eingangsrechnung geht es automatisch zum
  nächsten offenen Beleg im Archiv weiter, solange noch welche vorhanden
  sind – kein Umweg mehr über das Menü

## 0.5.10

- Standard-EÜR-Kategorie für neu erfasste Eingangsrechnungen ist jetzt
  „Fremdleistungen“ statt „Sonstige Betriebsausgaben“

## 0.5.9

- Lieferanten können ein eigenes Zahlungsziel in Tagen hinterlegen
  (z. B. 14 Tage)
- Zahlungsdatum und Fälligkeit werden bei Eingangsrechnungen automatisch
  vorgeschlagen (Rechnungsdatum + Zahlungsziel, Wochenenden auf Montag
  verschoben)
- Anlegen einer Eingangsrechnung aus einem Archiv-Beleg markiert diesen
  direkt als geprüft, kein separater Klick mehr nötig

## 0.5.8

- Lieferantenerkennung bei Eingangsrechnungen liest jetzt die
  Retouradress-Zeile aus statt fälschlich den eigenen Empfängerblock zu
  verwenden
- Rechnungsnummern werden auch erkannt, wenn sie nicht dem bisherigen
  Jahres-Bindestrich-Schema folgen (z. B. reine Ziffernfolgen oder das
  Label „Dokumentnummer“)
- fehlerhafte Worttrennung bei eng gesetztem PDF-Text wird korrigiert
- Erkennung des Rechnungsendbetrags übernimmt keine „Netto“-Zwischensummen
  mehr und unterstützt negative Beträge (Gutschriften)

## 0.5.7

- Ausgangsrechnungen werden beim Markieren als bezahlt automatisch einem
  Kunden zugeordnet, sofern die Erkennung erfolgreich war
- kein separater Klick auf „Kundendaten übernehmen“ mehr für den
  Regelfall nötig
- Eingangsrechnungen übernehmen erkannten Lieferantennamen und -anschrift
  direkt aus dem PDF-Import als Vorschlag
- Auswahlliste bekannter Lieferanten füllt Anschrift, Ansprechpartner und
  E-Mail automatisch aus, sobald der Firmenname übereinstimmt

## 0.5.6

- Archiv kann nach Kunden- bzw. Unternehmensname gefiltert werden
- zusätzlicher Teiltextfilter für Kundennummern
- Filter berücksichtigen erkannte PDF-Daten und verknüpfte Kundenstammdaten
- Filter bleiben beim fortlaufenden Prüfen und Verbuchen erhalten
- Anzahl der Treffer und der darin noch offenen Belege wird angezeigt

## 0.5.5

- Archivliste wird nach Rechnungsnummer absteigend sortiert
- die höchste und damit neueste Rechnungsnummer steht immer zuerst
- die fortlaufende Prüfliste verwendet dieselbe Reihenfolge
- Belege ohne erkannte Rechnungsnummer werden am Ende einsortiert
- importierte Rechnungen in der Kundenakte folgen ebenfalls dieser Sortierung

## 0.5.4

- Zahlungsdatum bei historischen Ausgangsrechnungen wird automatisch
  vorgeschlagen
- Grundlage sind Rechnungsdatum und das in den Einstellungen hinterlegte
  Zahlungsziel
- Vorschläge auf Samstag oder Sonntag werden auf den folgenden Montag
  verschoben
- vorgeschlagenes Datum bleibt für den tatsächlichen Zahlungseingang änderbar
- „Als bezahlt & nächster Beleg“ verbucht und öffnet mit einem Klick den
  nächsten offenen Beleg

## 0.5.3

- fortlaufende Prüfliste für importierte PDF-Belege
- Archiv kennzeichnet Belege als offen oder geprüft
- „Prüfung fortsetzen“ öffnet direkt den nächsten offenen Beleg
- „Geprüft & nächster Beleg“ speichert die erkannten Daten und wechselt weiter
- „Speichern & nächster Beleg“ verbucht den Zahlungsstatus und lädt unmittelbar
  den nächsten offenen Beleg
- nach dem letzten Beleg führt der Arbeitsablauf automatisch zurück ins Archiv

## 0.5.2

- PDF-Import erkennt weiterhin identische Dateien über ihre SHA-256-Prüfsumme
- zusätzliche inhaltliche Dublettenprüfung für technisch unterschiedliche PDFs
  derselben Rechnung
- Ausgangsrechnungen werden anhand der eindeutigen Rechnungsnummer geprüft
- bei Eingangsrechnungen werden Rechnungsnummer und erkannter Lieferant
  gemeinsam verglichen
- der Massenimport nennt Anzahl und Dateinamen der übersprungenen Dubletten

## 0.5.1

- bereits importierte Ausgangsrechnungen werden rückwirkend über die erkannte
  Kundennummer mit dem bestehenden Kunden verknüpft
- geänderte Kundenanschriften verhindern die Zuordnung historischer Belege nicht
- die alte Rechnungsanschrift bleibt unverändert am importierten PDF erhalten
- erneute PDF-Analyse und manuelle Korrektur der Kundennummer lösen die
  Verknüpfung unmittelbar aus

## 0.5.0

- beliebig viele Positionen in Angeboten, Aufträgen, Rechnungen und
  Gutschriften direkt in der Oberfläche
- Entwürfe inklusive aller Positionen nachträglich bearbeiten
- Live-Berechnung der Positions- und Dokumentensummen
- mehrzeilige Positionen werden vollständig in die PDF-Ausgabe übernommen
- Gutschriften direkt aus einer Rechnung mit nachvollziehbarer Belegreferenz
- Teil- und Vollgutschriften mit Schutz vor Überbuchung
- getrennte Verbuchung als Auszahlung oder Verrechnung
- ausgezahlte Gutschriften mindern die Einnahmen in der EÜR
- dreistufige Zahlungserinnerungen mit Überfälligkeitsliste und Vorschau
- ZUGFeRD-/Factur-X-Ausgabe im Profil EN 16931 für Rechnungen und Gutschriften
- lokale XSD-Validierung sowie erneute Prüfung des eingebetteten XML
- ZUGFeRD-PDFs werden nach ihrer Erzeugung automatisch beim Mailversand verwendet
- geführte Entra-/Exchange-Application-RBAC-Einrichtung für ein Absenderpostfach
- Zertifikat und privater Schlüssel können ohne Dateifreigabe in der Oberfläche
  hochgeladen und auf Zusammengehörigkeit geprüft werden
- Zertifikatsanmeldung kann ohne Mailversand getestet werden

## 0.4.1

- ungebuchte PDF-Fehlimporte können auch dann gelöscht werden, wenn bereits ein
  Kunde mit dem Archivbeleg verknüpft wurde
- Löschschutz greift weiterhin bei gebuchten Eingangsrechnungen und fest
  zugeordneten Dokumenten

## 0.4.0

- Massenimport für bis zu 50 PDF-Belege pro Durchlauf
- getrennte Kennzeichnung von Ausgangs- und Eingangsrechnungen
- Lieferanten- und Eingangsrechnungsverwaltung mit Zahlungsdatum, EÜR-Kategorie
  und betrieblichem Anteil
- zahlungsbasierte EÜR-Arbeitsunterlage mit Jahresübersicht, PDF und CSV
- historische Ausgangsrechnungen können mit ihrem Zahlungsdatum in die EÜR
  aufgenommen werden
- Fehlimporte und Entwürfe lassen sich endgültig löschen
- gebuchte bzw. fertiggestellte Belege werden aus Gründen der Nachvollziehbarkeit
  nur storniert und bleiben erhalten
- frei wählbares Zahlungsdatum beim Buchen einer Ausgangsrechnung

## 0.3.0

- neutraler Ersteinrichtungs-Assistent ohne fest eingebaute Firmendaten
- Logo-Upload und dynamisches Branding für Oberfläche und PDFs
- Startwerte für alle Nummernkreise bei der Ersteinrichtung
- Kleinunternehmerregelung ein- oder ausschaltbar
- PDF-Import erkennt die Absenderdaten aus den gespeicherten Einstellungen
- neutrale E-Mail-Betreffzeilen und Grußformeln

## 0.2.0

- Home-Assistant-Repository-Layout für Installation per GitHub-URL
- Kundenakte mit Bearbeitung von Anschrift, Ansprechpartner und Rechnungs-E-Mail
- importierte Altbelege werden in der Kundenakte angezeigt und mitgezählt
- monatliche Dauerrechnungen mit Schutz gegen doppelte Monatsläufe
- optionale automatische Fertigstellung, PDF-Erzeugung und Microsoft-Graph-Versand

## 0.1.7

- Kundennummer aus importierten PDFs erkennen
- Kundenimport unter Home-Assistant-Ingress korrigiert
