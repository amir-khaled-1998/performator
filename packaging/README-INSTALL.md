# Performator — Package d'installation OFFLINE (Windows x64)

Bundle auto-suffisant : **aucun accès internet n'est nécessaire** sur la machine cible.

## Contenu

| Dossier / fichier | Rôle |
|---|---|
| `install.ps1` | Script d'installation tout-en-un (offline) |
| `app/` | Sources Performator (agent, RAG, serveur MCP, orchestrateur) + scripts d'exemple `test_*.py` |
| `wheels/` | Paquets Python (numpy, mcp et dépendances) pour `pip install --no-index` |
| `tools/` | Installeurs : Python 3.12.7, Ollama 0.31.1, OpenCode 1.17.13, ripgrep 15.1, ctags 6.1 |
| `models/` | Modèles Ollama pré-téléchargés : `qwen3:8b` (5,2 GB) + `nomic-embed-text` (274 MB) |
| `opencode-seed/` | Packages npm du provider + cache registre — sans eux OpenCode tente d'aller sur internet au 1er lancement |

## Prérequis machine cible

- Windows 10/11 x64, **16 GB de RAM minimum** (inférence CPU)
- ~15 GB d'espace disque libre
- Droits utilisateur suffisants pour installer des programmes (installation par-utilisateur, pas besoin d'admin sauf politique restrictive)

## Installation

1. Copier le zip sur la machine cible (clé USB…) et le décompresser
2. Ouvrir PowerShell dans le dossier décompressé :

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
# ou avec des chemins personnalisés :
powershell -ExecutionPolicy Bypass -File .\install.ps1 -InstallDir D:\performator -ModelsDir D:\ollama\models
```

Le script : installe Python si absent → crée le venv et installe les wheels → dépose `rg`/`ctags`/`opencode` dans `bin\` → installe Ollama → copie les modèles → configure les variables d'environnement (`OLLAMA_MODELS`, `OLLAMA_CONTEXT_LENGTH=16384`) → génère `opencode.json` → smoke test final.

3. **Ouvrir une nouvelle console** après l'installation (PATH mis à jour).

## Vérifier le fonctionnement

```powershell
cd $env:USERPROFILE\performator     # ou votre -InstallDir
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe test_agent.py     # agent Q&A complet (SQL + LLM)
```

## Configuration pour le système cible

Éditer `opencode.json` (généré dans le dossier d'installation) : remplacer les valeurs
`A_REMPLACER_*` par la racine du code à analyser, puis construire l'index de symboles :

```powershell
.\.venv\Scripts\python.exe -c "from code_agent import CodeTools; print(CodeTools(r'C:\chemin\du\code').build_index())"
```

⚠️ Si la racine du code contient des dossiers volumineux hors-sujet (`.venv`, `node_modules`…),
copier `app\.ctags.d\` à côté ou compléter ses exclusions — sinon l'indexation est très lente.

## Utilisation

```powershell
# Q&A lecture seule sur le code / la base / les logs / la doc
.\.venv\Scripts\python.exe -c "from code_agent import build_agent; a = build_agent(code_root=r'C:\code', db_path='metier.db', log_path='app.log', docs_db='rag.db', model='qwen3:8b'); print(a.ask('ta question'))"

# Cycle de modification encadré (branche isolée + tests + gate de revue humaine)
.\.venv\Scripts\python.exe orchestrate.py "ta demande" --repo C:\code --model "ollama/qwen3:8b" --test "python -m unittest -v" --config .\opencode.json
```

## Notes et limites connues

- Le timeout du client Ollama de `local_rag.py` (120 s) peut être court en CPU : faire `rag.ollama.timeout = 600` après construction.
- `qwen3:8b` est fiable en **lecture** (Q&A, RAG, SQL). En **écriture** (OpenCode), il faut des tâches très cadrées et toujours relire le diff — c'est le rôle du gate de revue.
- Versions figées dans ce bundle : Python 3.12.7, Ollama 0.31.1, OpenCode 1.17.13. Ne pas mettre OpenCode à jour sur la machine cible (il retélécharcherait des composants en ligne).
