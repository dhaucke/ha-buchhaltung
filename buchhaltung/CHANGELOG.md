# Changelog

## 0.8.1

- Fehler behoben: automatisch erzeugte Dauerrechnungen haben bei aktiver
  Regelbesteuerung keine Umsatzsteuer berechnet, da weder das
  Dauerrechnungs-Formular noch die automatische Erzeugung einen
  USt-Satz kannten – wer eine Dauerrechnung mit automatischem Versand
  laufen hatte, hat Rechnungen ohne ausgewiesene USt verschickt
- neue Warnung auf dem Dashboard, wenn der Umsatz sich den
  Kleinunternehmergrenzen nach § 19 UStG nähert oder sie überschreitet
  (Vorjahresumsatz über 25.000 € bzw. laufender Umsatz über 100.000 €)
- beim Import von Eingangsrechnungen wird ein im Beleg genannter
  USt-Satz jetzt automatisch erkannt und bei aktiver Regelbesteuerung
  in den Entwurf übernommen; für Kleinunternehmer bleibt er weiterhin
  ohne Bedeutung, da der Vorsteuerabzug ohnehin entfällt
- neue Dashboard-Kacheln „Vereinnahmte USt“ und „Gezahlte Vorsteuer“ für
  das laufende Jahr, sichtbar nur bei aktiver Regelbesteuerung

## 0.8.0

- das Tool war bisher ausschließlich für Kleinunternehmer nach § 19 UStG
  ausgelegt (keine Umsatzsteuer wurde je berechnet); die bestehende
  Einstellung „Kleinunternehmerregelung verwenden“ ist jetzt ein
  echter Umschalter – deaktiviert, wird auf Ausgangsrechnungen pro
  Position ein USt-Satz (19 %/7 %/0 %) erfasst, Netto-/USt-/Bruttobeträge
  werden in Formular, PDF und ZUGFeRD-XML korrekt ausgewiesen
  (gruppiert nach Steuersatz) und eine USt-IdNr. kann hinterlegt werden
- Eingangsrechnungen erlauben in diesem Modus die Angabe des im Beleg
  ausgewiesenen USt-Satzes; die Vorsteuer wird automatisch aus
  Bruttobetrag, Steuersatz und betrieblichem Anteil berechnet und separat
  ausgewiesen
- die EÜR-Auswertung (Seite, CSV- und PDF-Export) zeigt die gezahlte
  Vorsteuer als eigene, informative Zeile – ersetzt keine
  Umsatzsteuervoranmeldung
- für Kleinunternehmer ändert sich nichts: ohne Umschalten bleiben
  Formulare, PDFs und ZUGFeRD-Dateien exakt wie zuvor

## 0.7.4

- zwei aus 0.7.3 stammende Karten (Datensicherung, Testmail-Bereich auf
  der Microsoft-Seite) hatten kein Innenabstand, wodurch Text und Buttons
  am Kartenrand klebten (`.card` allein bringt kein Padding mit, das kam
  bislang immer über die zusätzliche Klasse `form`) – behoben
- Testmail-Eingabefeld war ungebremst 100% breit statt wie das
  Zertifikat-Testen daneben ausgerichtet zu sein

## 0.7.3

- Karenzzeit vor der ersten und Mindestabstand zwischen
  Zahlungserinnerungen werden jetzt tatsächlich geprüft (die
  entsprechenden Einstellungen existierten bisher, wurden aber nirgends
  ausgewertet) und sind in den Rechnungswesen-Einstellungen sichtbar
- Kundenanlage warnt jetzt vor Duplikaten (gleicher Name oder gleiche
  E-Mail-Adresse) statt sie stillschweigend anzulegen
- Dashboard zeigt eine Karte „Alte Entwürfe“ für Angebote/Aufträge/
  Rechnungen, die seit über 14 Tagen unverändert im Entwurfsstatus
  hängen, mit direktem Lösch-/Öffnen-Link
- Microsoft-Einrichtungsseite zeigt jetzt das Ablaufdatum des
  Graph-Zertifikats an und warnt, wenn es abgelaufen ist oder in
  weniger als 60 Tagen abläuft
- neuer Button „Datensicherung herunterladen“ in den Einstellungen
  (Datenbank, Logo, erzeugte und importierte PDFs als ZIP) – bewusst
  ohne den privaten Microsoft-Graph-Schlüssel, der dieses Gerät nie
  verlässt

## 0.7.2

- Rechnungs-/Angebots-/Auftrags-/Gutschriftlisten und die
  Eingangsrechnungen-Übersicht zeigen jetzt ebenfalls nur die letzten 20
  Einträge mit „Alle anzeigen“-Link statt unbegrenzt alles aufzulisten
- Kundenliste hat jetzt ein Suchfeld (Name, Kundennummer, Ansprechpartner,
  E-Mail)
- neue Seite „Protokoll“ unter den Einstellungen zeigt das bereits
  bestehende, bisher unsichtbare Änderungsprotokoll (wer/was/wann),
  filterbar nach Bereich
- Dauerrechnungen können jetzt gelöscht werden, nicht mehr nur pausiert
  werden – bereits erzeugte Rechnungen bleiben davon unberührt
- Rechnungs-/Gutschriftversand zeigt jetzt eine Vorschau mit editierbarem
  Betreff und Text vor dem Senden, analog zu den Zahlungserinnerungen,
  statt sofort ohne Vorschau zu versenden

## 0.7.1

- Dokumentenliste in der Kundenansicht zeigt standardmäßig nur die
  letzten 20 Einträge mit einem „Alle anzeigen“-Link, statt unbegrenzt
  alle Dokumente und Archiv-Rechnungen aufzulisten
- Dokumente und Archiv-Rechnungen werden dabei jetzt chronologisch
  gemischt sortiert statt Archiv-Rechnungen immer unabhängig vom Datum
  ans Ende zu hängen

## 0.7.0

- Dauerrechnungen können jetzt nachträglich bearbeitet werden (Preis,
  Menge, Beschreibung, Rechnungstag, Versandeinstellungen), nicht mehr
  nur anlegen/pausieren/aktivieren
- neues Versandformat wählbar – automatisch (ZUGFeRD bevorzugt), immer
  ZUGFeRD-PDF oder immer normales PDF – sowohl für Dauerrechnungen als
  auch beim manuellen Versand einer einzelnen Rechnung/Gutschrift
- bei automatisch versendeten Dauerrechnungen wird ZUGFeRD bei Bedarf
  jetzt selbstständig erzeugt, da dort niemand vorher manuell auf
  „ZUGFeRD erzeugen“ klicken kann

## 0.6.9

- Hauptmenü ist jetzt nach Verkauf, Einkauf und Auswertung gruppiert statt
  einer langen flachen Liste
- Einstellungen sind in vier Unterseiten mit Reiter-Navigation aufgeteilt:
  Unternehmen, Rechnungswesen, Nummernkreise, Microsoft – statt einer
  einzigen überladenen Seite

## 0.6.8

- E-Mail-Text beim Versenden von Rechnungen/Gutschriften ist jetzt in den
  Einstellungen frei anpassbar (Platzhalter: {typ}, {nummer}, {kunde},
  {firma}, {absender})
- Dokumenttyp im Standardtext wird korrekt großgeschrieben
  ("Rechnung 2026-07-0135" statt "rechnung 2026-07-0135")

## 0.6.7

- ZUGFeRD-Erzeugung für Rechnungen mit Leistungszeitraum (z. B.
  Dauerrechnungen) schlug mit „ApplicableHeaderTradeDelivery … is not
  nillable“ fehl; behoben, indem zusätzlich immer ein Lieferdatum (BT-72)
  mitgeschickt wird

## 0.6.6

- eine stornierte Monatsrechnung blockiert eine Dauerrechnung nicht mehr
  dauerhaft für diesen Monat – ein erneuter Lauf ist jetzt möglich, sobald
  die fehlerhafte Rechnung storniert wurde

## 0.6.5

- Nummernkreis-Zähler (Rechnungen, Kunden, Angebote, Aufträge) lassen sich
  jetzt auch nach der Ersteinrichtung in den Einstellungen einsehen und
  korrigieren – vorher gab es dafür keine Möglichkeit mehr

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
