# Spécification du flux deals_fo_to_kondor

*Rétro-spécifiée le 2026-10-01 à partir du corpus d'échanges (TIBCO BW 5 → Kondor+). À valider par le métier et, si disponible, par la lecture de l'EAR BW.*

## 1. Contexte

- Description : Deals du front-office (XML) transformés par BW en fichier d'import Kondor Deals
- Entrée : `TRADES_*.xml` (format xml)
- Sortie Kondor : `Deals_*.csv` (format csv)
- Clé de corrélation : `@id` → `Deals_Id`

## 2. Corpus analysé

| Entrées | Sorties | Paires corrélées | Entrées sans sortie | Sorties sans entrée | Multi-sorties |
|---|---|---|---|---|---|
| 800 | 733 | 733 | 67 | 0 | 0 |

## 3. Sélection des enregistrements

Règles de filtrage probables (entrées sans sortie Kondor) :

- `Status` = `CANCEL` → non transmis (100% des rejets, 0% des entrées traitées)

## 4. Mapping des champs Kondor

| Champ Kondor | Type | Source (entrée) | Règle | Confirmé | Confiance |
|---|---|---|---|---|---|
| Deals_Id | entier | `@id` | sans zéros de tête | 100% | haute |
| TradeDate | date %Y%m%d | `TradeDate` | date reformatée (%Y-%m-%dT%H:%M:%S → %Y%m%d) | 100% | haute |
| ValueDate | date %Y%m%d | `ValueDate` | date reformatée (%Y-%m-%d → %Y%m%d) | 100% | haute |
| Amount | décimal | `Side` | conditionnelle (BUY → nombre (reformaté) de Notional ; SELL → signe inversé de Notional) | 100% | haute |
| Currencies_Id | texte | `Notional/@ccy` | majuscules | 100% | haute |
| BuySell | texte | `Side` | table de correspondance — voir annexe BuySell | 100% | haute |
| Cpty_Id | entier | `Counterparty/Code` | table de correspondance — voir annexe Cpty_Id | 100% | haute |
| Folders_Id | entier | `Book` | table de correspondance — voir annexe Folders_Id | 100% | haute |
| Source | texte | `—` | constante 'FO' | 100% | haute |

## 5. Points à valider

- Appels BW à des référentiels/bases non visibles dans les fichiers : à confirmer.
- Gestion d'erreurs (rejets, fichiers d'erreur, relances) : à décrire.

## Annexes : tables de correspondance déduites

### BuySell (depuis `Side`)

| Entrée | Kondor |
|---|---|
| BUY | B |
| SELL | S |

### Cpty_Id (depuis `Counterparty/Code`)

| Entrée | Kondor |
|---|---|
| BANK_A | 1001 |
| BANK_E | 1005 |
| BANK_C | 1003 |
| BANK_D | 1004 |
| BANK_B | 1002 |

### Folders_Id (depuis `Book`)

| Entrée | Kondor |
|---|---|
| FX-FWD | 13 |
| FX-SPOT | 12 |
| MM | 20 |

## Règles lues dans les EAR (boîte blanche)

- Écriture Kondor : `Kondor_Deals:/Processes/SendKondor.process`, activité Write Kondor File → /opt/kplus/import/Deals_*.csv
- En amont : FO_Trades:/Processes/ReadTrades.process via jms queue.fo.trades
- Filtre (transition) : `FO_Trades:/Processes/ReadTrades.process : Parse Trades → Send Trade si $current/Status != 'CANCEL'`
- Référentiel appelé : `/Common/Lookups/CptyLookup.process : Query REF_CPTY : select KONDOR_ID from REF_CPTY where CODE = ?`

| Champ Kondor | Expression BW (XPath) | Règle déduite du corpus | Confirmé |
|---|---|---|---|
| Deals_Id | `number($JMS-Queue-Receiver/ActivityOutput/Body/Trade/@id)` | sans zéros de tête de `@id` | 100% |
| TradeDate | `tib:format-date('yyyyMMdd', substring($JMS-Queue-Receiver/ActivityOutput/Body/Trade/TradeDate, 1, 10))` | date reformatée (%Y-%m-%dT%H:%M:%S → %Y%m%d) de `TradeDate` | 100% |
| ValueDate | `translate($JMS-Queue-Receiver/ActivityOutput/Body/Trade/ValueDate, '-', '')` | date reformatée (%Y-%m-%d → %Y%m%d) de `ValueDate` | 100% |
| Amount | `-1 * $JMS-Queue-Receiver/ActivityOutput/Body/Trade/Notional` si `$JMS-Queue-Receiver/ActivityOutput/Body/Trade/Side = 'SELL'`<br>`$JMS-Queue-Receiver/ActivityOutput/Body/Trade/Notional` si `sinon (pas $JMS-Queue-Receiver/ActivityOutput/Body/Trade/Side = 'SELL')` | conditionnelle (BUY → nombre (reformaté) de Notional ; SELL → signe inversé de Notional) de `Side` | 100% |
| Currencies_Id | `upper-case($JMS-Queue-Receiver/ActivityOutput/Body/Trade/Notional/@ccy)` | majuscules de `Notional/@ccy` | 100% |
| BuySell | `substring($JMS-Queue-Receiver/ActivityOutput/Body/Trade/Side, 1, 1)` | table de correspondance de `Side` | 100% |
| Cpty_Id | `$Lookup-Cpty/root/kondorId` | table de correspondance de `Counterparty/Code` | 100% |
| Folders_Id | `tib:lookup('BOOKS', $JMS-Queue-Receiver/ActivityOutput/Body/Trade/Book)` | table de correspondance de `Book` | 100% |
| Source | `'FO'` | constante 'FO' de `—` | 100% |

Relire chaque ligne : l'expression BW fait foi, le corpus confirme qu'elle est bien celle appliquée en production. Un écart signale une surcharge de déploiement, une version d'EAR différente ou une branche jamais rencontrée.

## Tests d'acceptation

`golden_deals_fo_to_kondor.jsonl` contient 500 paires réelles (entrée → sortie Kondor attendue). Toute nouvelle implémentation (ex. route Camel) doit les reproduire ; vérification avec l'outil `compare_outputs`.
