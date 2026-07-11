# Performator — exécutable client autonome (Windows x64)

Un seul binaire, **aucune installation** : décompresser le zip dans un dossier et lancer
`performator.exe` depuis une console. Ni Python ni paquet pip ne sont requis — tout est
embarqué dans l'exe. `rg.exe` et `ctags.exe` (portables) doivent rester à côté de l'exe.

## Prérequis (déjà en place chez vous)

- **Ollama** accessible sur `http://localhost:11434` (sinon définir la variable `OLLAMA_URL`)
- Modèles présents : `qwen3:8b` (LLM) et `nomic-embed-text` (embeddings, requis pour le RAG :
  `ollama pull nomic-embed-text` si absent — 274 MB)
- **OpenCode** (uniquement pour la partie modification de code / `orchestrate`)
- Recommandé : contexte Ollama ≥ 16k (`setx OLLAMA_CONTEXT_LENGTH 16384` puis redémarrer Ollama),
  car le défaut de 4096 tokens casse les boucles d'outils

## Vérifier l'installation

```
performator.exe selfcheck
```

Contrôle numpy/mcp embarqués, rg, ctags, et la connexion à Ollama avec la liste des modèles.

## Commandes

```
# 1. Indexer les symboles du code (une fois, et après gros changements)
performator.exe index C:\chemin\du\code --tags C:\chemin\tags.json

# 2. Poser des questions (lecture seule : code + base + logs + doc)
performator.exe ask "Ou est definie calculateInterest et que fait-elle ?" ^
    --code-root C:\chemin\du\code --db metier.db --log app.log --docs-db rag.db

# 3. RAG documentaire : indexer puis interroger
performator.exe rag-add C:\docs\specification.txt --db rag.db
performator.exe rag-ask "Que dit la spec sur la validation des taux ?" --db rag.db

# 4. Serveur MCP (consommé par OpenCode — voir opencode.json.template)
performator.exe mcp

# 5. Cycle de modification encadré (branche git isolée + tests + revue humaine)
performator.exe orchestrate "Ajoute une validation null sur X" ^
    --repo C:\chemin\du\code --model ollama/qwen3:8b --test "mvn -q test" --config opencode.json
```

## Brancher OpenCode (partie modification)

1. Copier `opencode.json.template` → `opencode.json`
2. Remplacer les chemins `C:\CHEMIN\VERS\...` (exe, code à analyser, tags.json)
3. OpenCode voit alors les outils `code-context` (grep_code, find_symbol, read_file,
   get_schema, run_sql, search_logs) en lecture seule

## Notes

- Si la racine du code contient `.venv`, `node_modules`… l'indexation ctags peut être longue :
  créer un dossier `.ctags.d` dans le répertoire courant avec un fichier `exclude.ctags`
  contenant des lignes `--exclude=.venv`.
- Sécurité : le serveur MCP n'expose que des outils de **lecture** ; le SQL est en lecture
  seule ; la lecture de fichiers est confinée à `CODE_ROOT`.
- Limite connue : avec un modèle 8B, la partie **écriture** (orchestrate) doit rester sur des
  tâches très cadrées, et le diff doit toujours être relu (c'est le rôle du gate de revue).
