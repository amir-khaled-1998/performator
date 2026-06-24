# Performator

Système agentique **100% local et souverain** pour interroger et faire évoluer un système
hétérogène — gros codebase, documentation, base de données et logs — sans qu'aucune donnée
ne quitte la machine.

L'idée centrale : **on n'embarque pas tout dans un RAG unique.** Chaque source garde son
mode d'accès naturel, exposé comme un *outil*. Un agent choisit le bon outil selon la question.
Le RAG sémantique n'est qu'un outil parmi d'autres — celui qui sert la documentation.

## Architecture

```
                Utilisateur
                     │
            orchestrate.py  ──────────────►  OpenCode (run headless)
         (branche isolée + gate)                   │  pilote Qwen via Ollama
                                                    ▼
                                          mcp_server.py  (serveur MCP, lecture seule)
                                                    │
                        ┌───────────┬───────────────┼───────────────┐
                     code        docs (RAG)        db (SQL RO)      logs
                  ripgrep+ctags  local_rag.py    schéma+requête   filtre temps/niveau
```

Deux couches :
- **Comprendre** (lecture seule) : `code_agent.py` + `local_rag.py`, exposés en MCP par `mcp_server.py`.
- **Implémenter** (écriture) : OpenCode + un modèle Qwen local, qui consomme le serveur MCP.

## Composants

| Fichier | Rôle |
|---|---|
| `local_rag.py` | RAG sémantique autonome (SQLite + numpy + Ollama). Persistance disque, CRUD, filtrage métadonnées. Pour la **documentation**. |
| `code_agent.py` | Boîtes à outils de lecture : code (ripgrep/ctags/read_file), db (SQL lecture seule), logs (filtre), docs (RAG). Inclut une boucle agent tool-use autonome via Ollama. |
| `mcp_server.py` | Expose les outils de lecture comme **serveur MCP** (stdio) pour OpenCode ou tout client MCP. |
| `orchestrate.py` | Requête → branche git isolée → OpenCode headless → tests → **gate de revue humaine**. |
| `opencode.json.example` | Config OpenCode : provider Ollama, modèle Qwen, serveur MCP, agents `deliver`/`explore`. |

## Prérequis

**Python** : `pip install -r requirements.txt` (numpy, mcp)

**Système** :
- [Ollama](https://ollama.com) — LLM + embeddings locaux
- [ripgrep](https://github.com/BurntSushi/ripgrep) — `apt install ripgrep`
- [universal-ctags](https://github.com/universal-ctags/ctags) — `apt install universal-ctags`
- [OpenCode](https://opencode.ai) — `curl -fsSL https://opencode.ai/install | bash`

**Modèles Ollama** :
```bash
ollama pull nomic-embed-text     # embeddings (768 dims)
ollama pull qwen3-coder          # agent de code (tool-calling requis)
```

> ⚠️ Contexte Ollama par défaut = 4096 tokens, ce qui casse les boucles d'outils.
> Crée un Modelfile avec `num_ctx` ≥ 64000 pour l'usage agentique.

## Démarrage rapide

### 1. Indexer la documentation (RAG)
```python
from local_rag import LocalRAG
rag = LocalRAG(db_path="rag.db", embedding_dims=768)
rag.add_document(open("doc.txt").read(), metadata={"doc": "doc.txt"})
```

### 2. Indexer les symboles du code
```python
from code_agent import CodeTools
print(CodeTools("/chemin/code").build_index())   # génère tags.json
```

### 3. Lancer l'agent de lecture (Q&A, sans modification)
```python
from code_agent import build_agent
agent = build_agent(code_root="/chemin/code", db_path="metier.db",
                    log_path="/var/log/app.log", docs_db="rag.db")
print(agent.ask("Où est définie calculateInterest et que fait-elle ?"))
```

### 4. Brancher OpenCode sur le contexte (MCP)
- Copier `opencode.json.example` → `opencode.json`, remplacer les chemins absolus.
- OpenCode découvre alors les outils du serveur `code-context`.

### 5. Développer et livrer (encadré)
```bash
python orchestrate.py "Ajoute une validation null sur interestService dans DealBooking" \
    --repo /chemin/code --model ollama/qwen3-coder --test "mvn -q test" --config opencode.json
```
L'orchestrateur isole une branche, laisse OpenCode implémenter, lance les tests, puis
**s'arrête sur un gate de revue humaine**. Aucun merge ni push automatique : la livraison
finale passe par revue + pipeline CI/CD existant.

## Notes de sécurité / souveraineté

- Tout est local : Ollama pour le LLM et les embeddings, SQLite pour le stockage, MCP en stdio.
- Le serveur MCP n'expose **que des outils de lecture**. La frontière read-only est volontaire.
- SQL en lecture seule (driver `mode=ro` + garde applicatif). Pour PostgreSQL, ajouter aussi un rôle SQL en lecture seule.
- Lecture de fichiers confinée à la racine du code (anti path-traversal).
- Ne jamais committer `*.db`, `tags.json`, `opencode.json` (déjà dans `.gitignore`).

## Limites connues

- Les modèles locaux restent sous les modèles frontière sur les refactors multi-fichiers les plus complexes. Viser des tâches **cadrées**.
- OpenCode évolue vite : le schéma de `opencode.json` (bloc `mcp`, clés `permission`) peut varier selon la version installée.
- En non-interactif (`opencode run`), prévoir un agent avec écritures auto-acceptées (agent `deliver` fourni).

## Licence

MIT — voir [LICENSE](LICENSE).
