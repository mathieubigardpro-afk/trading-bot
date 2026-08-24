# Rapport d'anomalies de données (tools/check_data_anomalies.py)

Détecteur d'anomalies de corporate actions — `docs/RESEARCH-BACKLOG.md` idée P0#11 (cas DHR/Fortive, audit 2026-07-27). Ce rapport est un JOURNAL pour revue humaine : aucune correction automatique n'a été appliquée aux données. Un faux positif sur un vrai krach idiosyncratique est attendu et normal — l'humain tranche.

- Fichiers scannés : **121**
- Anomalies détectées : **19**
- Paramètres : seuil de rendement = 0.4, trou de calendrier max = 10 jours, sous-dossiers = ['equities', 'etf']

## Sauts de rendement (|close-to-close| > seuil) (19)

| Symbole | Date | Valeur | Détail |
|---|---|---|---|
| AAPL | 2000-09-29 | -0.5187 | rendement close-to-close -51.9% (0.8001 -> 0.3851, veille 2000-09-28) |
| AIG | 2008-09-15 | -0.6079 | rendement close-to-close -60.8% (150.7139 -> 59.0937, veille 2008-09-12) |
| AIG | 2008-09-17 | -0.4533 | rendement close-to-close -45.3% (46.5550 -> 25.4500, veille 2008-09-16) |
| AIG | 2008-09-19 | 0.4312 | rendement close-to-close +43.1% (33.3954 -> 47.7964, veille 2008-09-18) |
| AIG | 2009-03-16 | 0.6600 | rendement close-to-close +66.0% (6.2073 -> 10.3042, veille 2009-03-13) |
| AIG | 2009-03-18 | 0.4375 | rendement close-to-close +43.8% (11.9181 -> 17.1322, veille 2009-03-17) |
| AIG | 2009-08-05 | 0.6272 | rendement close-to-close +62.7% (8.3923 -> 13.6561, veille 2009-08-04) |
| AMD | 2016-04-22 | 0.5229 | rendement close-to-close +52.3% (2.6200 -> 3.9900, veille 2016-04-21) |
| BKNG | 2000-09-27 | -0.4233 | rendement close-to-close -42.3% (4.3807 -> 2.5263, veille 2000-09-26) |
| C | 2008-11-24 | 0.5782 | rendement close-to-close +57.8% (27.4919 -> 43.3890, veille 2008-11-21) |
| DHR | 2016-07-05 | 0.6122 | rendement close-to-close +61.2% (42.0097 -> 67.7273, veille 2016-07-01) |
| LOW | 1983-04-29 | -0.4130 | rendement close-to-close -41.3% (0.7154 -> 0.4199, veille 1983-04-28) |
| MCD | 1968-05-21 | -0.5072 | rendement close-to-close -50.7% (0.3543 -> 0.1746, veille 1968-05-20) |
| MCD | 1969-06-13 | -0.5010 | rendement close-to-close -50.1% (0.2219 -> 0.1107, veille 1969-06-12) |
| MS | 2008-10-13 | 0.8698 | rendement close-to-close +87.0% (6.5199 -> 12.1912, veille 2008-10-10) |
| NFLX | 2004-10-15 | -0.4091 | rendement close-to-close -40.9% (0.2490 -> 0.1471, veille 2004-10-14) |
| NFLX | 2013-01-24 | 0.4222 | rendement close-to-close +42.2% (1.4751 -> 2.0980, veille 2013-01-23) |
| NVDA | 2000-03-07 | 0.4241 | rendement close-to-close +42.4% (0.1116 -> 0.1589, veille 2000-03-06) |
| ORCL | 1992-12-23 | 0.4387 | rendement close-to-close +43.9% (0.3798 -> 0.5465, veille 1992-12-22) |

