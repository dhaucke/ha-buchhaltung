# Changelog

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
