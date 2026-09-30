# MCP Kondor+ & TIBCO BW pour OpenCode

Deux serveurs [MCP](https://modelcontextprotocol.io) en Python pour piloter un environnement **Kondor+** et rétro-spécifier les flux **TIBCO BW 5** qui l'alimentent, depuis un agent IA comme [OpenCode](https://opencode.ai).

| Serveur | Rôle | Où il agit |
|---|---|---|
| `mcp_kondor` | Exploitation : services, livraisons, import/export au format des tables Kondor | Serveur Linux, via SSH/SFTP |
| `bw_flows` | Rétro-spécification des flux BW 5 → Kondor+ à partir des fichiers d'échange et des EAR | En local, sur le poste |

Les deux serveurs tournent **sur le poste Windows** et sont lancés par OpenCode (stdio). Rien n'est installé sur les serveurs.

```
OpenCode (Windows) ──stdio──> mcp_kondor ──SSH/SFTP──> serveur Linux (Kondor+)
                   ──stdio──> bw_flows   ──(lecture)──> corpus local + EAR + SFTP optionnel
```

## mcp_kondor — exploitation

| Domaine | Outils |
|---|---|
| Services | `service_status`, `service_action` (start/stop/restart), `tail_log`, `server_info` |
| Livraisons | `deploy` (envoi SFTP + sauvegarde automatique), `list_backups`, `rollback` |
| Import Kondor | `validate_import` (contrôle local du format de table), `import_file` |
| Export / KPI | `run_export`, `list_exports`, `read_export`, `download_export` |

Garde-fous :
- Tout ce qui est autorisé est déclaré dans `config.yaml` (liste blanche).
- Toute action d'écriture renvoie d'abord un aperçu et ne s'exécute qu'avec `confirm=true`.
- Chaque action est tracée dans `audit.log`.
- `read_only: true` bloque toute écriture (conseillé en production).

## bw_flows — rétro-spécification BW 5 → Kondor+

Deux sources, combinées dans une spécification unique :

1. **Fichiers d'entrée/sortie (boîte noire)** : les enregistrements sont corrélés par clé métier, puis la règle de chaque champ Kondor est déduite. L'inférence couvre la copie, les formats de date, le signe, les constantes, les tables de correspondance et les règles conditionnelles. Les filtres et les cas particuliers sont aussi détectés.
2. **Groupe d'EAR (boîte blanche)** : extraction des process (`.ear` → `.par`/`.sar`/`.projlib` → `.process`) et résolution des variables globales (défauts de l'EAR + surcharges de déploiement). L'outil reconstitue la chaîne du flux **entre EAR** (JMS, RV, fichiers, sous-process) et extrait le mapping XPath de chaque champ Kondor.

| Étape | Outils |
|---|---|
| Corpus | `list_flows`, `sync_corpus` (SFTP, lecture seule), `corpus_stats` |
| Exploration | `profile`, `sample_pairs` |
| Inférence | `infer_mapping`, `lookup_table`, `find_edge_cases` |
| EAR | `index_ears`, `trace_flow`, `process_detail`, `kondor_mapping_from_ear`, `global_variables`, `search_ears` |
| Livrables | `generate_spec` → `SPEC_<flux>.md` + `golden_<flux>.jsonl` |
| Migration | `compare_outputs` : compare les sorties d'une nouvelle implémentation (ex. Apache Camel) aux sorties BW réelles |

Les champs déclarés dans `mask_fields` sont masqués (hash court) avant de remonter au modèle.

Voir [`examples/exemple_SPEC_deals_fo_to_kondor.md`](examples/exemple_SPEC_deals_fo_to_kondor.md) pour une spec générée sur des données de test.

## Installation (Windows)

```powershell
mkdir C:\mcp; cd C:\mcp
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt

# Mot de passe SSH dans le Gestionnaire d'identifiants Windows (jamais en clair)
.\venv\Scripts\python -c "import keyring,getpass; keyring.set_password('mcp_kondor','<USER>', getpass.getpass())"

# Première connexion pour enregistrer la clé du serveur
ssh <USER>@<serveur>
```

Adapter ensuite `mcp_kondor/config.yaml` (scripts Kondor+, chemins, formats de tables) et `bw_flows/flows.yaml` (flux, clés de corrélation, dossiers des EAR).

### opencode.json

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kondor": {
      "type": "local",
      "command": ["C:\\mcp\\venv\\Scripts\\python.exe", "C:\\mcp\\mcp_kondor\\mcp_kondor.py"],
      "environment": { "KDR_HOST": "<serveur>", "KDR_USER": "<USER>" },
      "timeout": 20000
    },
    "bw-flows": {
      "type": "local",
      "command": ["C:\\mcp\\venv\\Scripts\\python.exe", "C:\\mcp\\bw_flows\\mcp_bw_flows.py"],
      "environment": { "KDR_HOST": "<serveur>", "KDR_USER": "<USER>" }
    }
  }
}
```

Pour tester un serveur seul : `npx @modelcontextprotocol/inspector C:\mcp\venv\Scripts\python.exe C:\mcp\bw_flows\mcp_bw_flows.py`

## Essai rapide avec des données de test

```bash
python examples/generate_test_corpus.py ./demo   # corpus XML -> CSV Kondor fictif
python examples/generate_test_ears.py ./demo     # groupe de 2 EAR BW5 reliés par JMS
```

Puis pointer `corpus_dir`, `ears.dirs` et `ears.deployment_configs` de `flows.yaml` vers `./demo`.

## Sécurité

- Commencer en **recette**, en `read_only: true`.
- Utiliser un compte Linux dédié, à droits restreints. Si les scripts start/stop l'exigent, prévoir un `sudo NOPASSWD` limité à ces seuls scripts.
- Ne jamais committer de config réelle, de corpus, d'EAR ou de spec générée : le `.gitignore` les exclut.
- Sur des données de production, privilégier un modèle local (ex. Ollama) dans OpenCode.

## Limites connues

- Les fonctions custom BW (`tib:lookup`, activités Java) sont remontées telles quelles.
- Les liens entre EAR par HTTP/SOAP ou par base partagée ne sont pas encore reliés automatiquement (utiliser `search_ears`).
- Les règles combinant plusieurs champs d'entrée ressortent en confiance faible dans l'inférence par corpus.
