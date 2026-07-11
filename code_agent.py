"""
code_agent.py — Agent tool-use 100% local pour interroger un système hétérogène :
  - 7 M lignes de code source   -> recherche agentique (ripgrep + ctags + read_file)
  - documentation               -> RAG sémantique (local_rag.py)
  - base de données             -> schéma + SQL en LECTURE SEULE
  - logs                        -> recherche filtrée (pattern / niveau / plage de temps)

Principe : on N'EMBARQUE PAS tout dans un RAG unique. Chaque source garde son mode
d'accès naturel, exposé comme un OUTIL. L'agent (la boucle tool-use) choisit l'outil
adapté à la question. Le RAG n'est qu'UN outil parmi quatre.

Souveraineté : LLM via Ollama local (aucune donnée ne sort). Backend enfichable :
remplacer OllamaBackend par un backend Anthropic est trivial si la politique l'autorise.

Dépendances : numpy (pour local_rag), + binaires système ripgrep et ctags.
  apt: ripgrep universal-ctags   |   pull souverain via miroir interne.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable

# Le RAG docs est optionnel : si local_rag.py est absent, l'outil docs est désactivé.
try:
    from local_rag import LocalRAG
    _HAS_RAG = True
except ImportError:
    _HAS_RAG = False


# ===========================================================================
#  BACKEND LLM (enfichable) — défaut : Ollama local
# ===========================================================================
class OllamaBackend:
    def __init__(self, base_url="http://localhost:11434", model="qwen3:14b", timeout=300):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode())["message"]


# ===========================================================================
#  BOÎTE À OUTILS 1 — CODE (recherche agentique, pas d'embedding massif)
# ===========================================================================
class CodeTools:
    """ripgrep pour le texte/regex, ctags pour les symboles, read_file à la demande.
    C'est l'approche de Claude Code / Cursor : on fouille le repo, on ne le pré-embarque pas."""

    def __init__(self, code_root: str, tags_file: str = "tags.json"):
        self.root = Path(code_root).resolve()
        self.tags_file = Path(tags_file).resolve()

    def build_index(self) -> str:
        """À lancer UNE fois (et après gros changements). Indexe les symboles en JSON-lines."""
        # ctags refuse d'écraser un fichier tags au format JSON : on le supprime d'abord.
        self.tags_file.unlink(missing_ok=True)
        subprocess.run(
            ["ctags", "-R", "--output-format=json", "--fields=+n",
             "-f", str(self.tags_file), str(self.root)],
            check=True, capture_output=True,
        )
        n = sum(1 for _ in open(self.tags_file, encoding="utf-8", errors="ignore"))
        return f"Index symboles construit : {n} symboles ({self.tags_file})"

    def _safe(self, rel_path: str) -> Path:
        """Garde-fou : empêche toute sortie de l'arbre du code (path traversal)."""
        p = (self.root / rel_path).resolve()
        if not str(p).startswith(str(self.root)):
            raise ValueError(f"Chemin hors périmètre : {rel_path}")
        return p

    def grep_code(self, pattern: str, glob: str | None = None, max_results: int = 40) -> str:
        cmd = ["rg", "-n", "--no-heading", "--color=never", "-m", str(max_results)]
        if glob:
            cmd += ["-g", glob]
        cmd += [pattern, str(self.root)]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout
        lines = out.splitlines()[:max_results]
        if not lines:
            return f"Aucun résultat pour /{pattern}/"
        # On rend les chemins relatifs à la racine pour limiter le bruit
        rel = [l.replace(str(self.root) + os.sep, "") for l in lines]
        return "\n".join(rel)

    def find_symbol(self, name: str, max_results: int = 20) -> str:
        if not self.tags_file.exists():
            return "Index symboles absent : appeler build_index() d'abord."
        out = subprocess.run(
            ["rg", "-N", "--color=never", f'"name": "{name}"', str(self.tags_file)],
            capture_output=True, text=True,
        ).stdout
        hits = []
        for line in out.splitlines()[:max_results]:
            try:
                t = json.loads(line)
                if t.get("name") == name:
                    rel = t.get("path", "").replace(str(self.root) + os.sep, "")
                    hits.append(f'{t.get("kind","?")}  {rel}:{t.get("line","?")}')
            except json.JSONDecodeError:
                continue
        return "\n".join(hits) if hits else f"Symbole '{name}' introuvable."

    def read_file(self, rel_path: str, start: int = 1, end: int | None = None) -> str:
        p = self._safe(rel_path)
        if not p.is_file():
            return f"Fichier introuvable : {rel_path}"
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        end = end or min(start + 120, len(lines))  # plafond pour ne pas noyer le contexte
        sel = lines[start - 1:end]
        return "\n".join(f"{start + i:6d}  {l}" for i, l in enumerate(sel))


# ===========================================================================
#  BOÎTE À OUTILS 2 — DOCS (RAG sémantique : le seul vrai cas RAG)
# ===========================================================================
class DocsTools:
    def __init__(self, rag: "LocalRAG"):
        self.rag = rag

    def search_docs(self, query: str, top_k: int = 5) -> str:
        hits = self.rag.search(query, top_k=top_k)
        if not hits:
            return "Aucun passage pertinent dans la documentation."
        return "\n\n".join(
            f"[doc {h.id} | {h.metadata.get('doc','?')} | score {h.score:.2f}]\n{h.text}"
            for h in hits
        )


# ===========================================================================
#  BOÎTE À OUTILS 3 — DATABASE (schéma + SQL LECTURE SEULE)
# ===========================================================================
class DbTools:
    """On n'embarque jamais une base. On donne le schéma + un text-to-SQL read-only."""

    _FORBIDDEN = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|"
        r"PRAGMA|VACUUM|REINDEX|TRUNCATE)\b", re.IGNORECASE,
    )

    def __init__(self, db_path: str, max_rows: int = 100):
        self.db_path = db_path
        self.max_rows = max_rows
        # Ouverture en mode read-only au niveau du driver (double sécurité)
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def get_schema(self) -> str:
        rows = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table','view') AND sql NOT NULL"
        ).fetchall()
        return "\n\n".join(r["sql"] for r in rows) or "(schéma vide)"

    def run_sql(self, query: str) -> str:
        # Garde applicatif : on refuse tout ce qui n'est pas pure lecture.
        if self._FORBIDDEN.search(query) or ";" in query.strip().rstrip(";"):
            return "Refusé : seules les requêtes SELECT/WITH en lecture seule sont autorisées."
        if not re.match(r"^\s*(SELECT|WITH)\b", query, re.IGNORECASE):
            return "Refusé : la requête doit commencer par SELECT ou WITH."
        try:
            cur = self.conn.execute(query)
            rows = cur.fetchmany(self.max_rows)
            cols = [d[0] for d in cur.description]
            head = " | ".join(cols)
            body = "\n".join(" | ".join(str(v) for v in tuple(r)) for r in rows)
            note = f"\n... (tronqué à {self.max_rows} lignes)" if len(rows) == self.max_rows else ""
            return f"{head}\n{body}{note}" if body else f"{head}\n(0 ligne)"
        except sqlite3.Error as e:
            return f"Erreur SQL : {e}"


# ===========================================================================
#  BOÎTE À OUTILS 4 — LOGS (recherche filtrée, pas d'embedding)
# ===========================================================================
class LogTools:
    def __init__(self, log_path: str):
        self.log_path = log_path

    def search_logs(self, pattern: str = "", level: str | None = None,
                    since: str | None = None, until: str | None = None,
                    max_results: int = 50) -> str:
        """Filtre par pattern + niveau + plage de temps (best-effort sur timestamp ISO en début de ligne)."""
        # ripgrep (moteur Rust) ne gère pas les look-around : on filtre niveau + temps en Python.
        cmd = ["rg", "-N", "--color=never", "-m", str(max_results * 5),
               "-e", pattern or ".", self.log_path]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()

        lvl = level.upper() if level else None

        def keep(line: str) -> bool:
            if lvl and not re.search(rf"\b{re.escape(lvl)}\b", line):
                return False
            if not (since or until):
                return True
            m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", line)
            if not m:
                return True  # pas de timestamp parsable -> on garde
            ts = m.group(1).replace("T", " ")
            if since and ts < since:
                return False
            if until and ts > until:
                return False
            return True

        filtered = [l for l in out if keep(l)][:max_results]
        return "\n".join(filtered) if filtered else "Aucune entrée de log correspondante."


# ===========================================================================
#  DÉFINITION DES OUTILS (schémas JSON exposés au LLM)
# ===========================================================================
def build_tool_specs(has_docs: bool) -> list[dict]:
    def fn(name, desc, props, required):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

    tools = [
        fn("grep_code", "Recherche regex/texte dans le code source (ripgrep). "
                        "Pour trouver des usages, chaînes, patterns.",
           {"pattern": {"type": "string", "description": "Regex ou texte à chercher"},
            "glob": {"type": "string", "description": "Filtre fichiers, ex. '*.java'"}},
           ["pattern"]),
        fn("find_symbol", "Localise la DÉFINITION d'un symbole (fonction, classe, méthode) via ctags.",
           {"name": {"type": "string", "description": "Nom exact du symbole"}}, ["name"]),
        fn("read_file", "Lit une tranche d'un fichier source (chemin relatif à la racine du code).",
           {"rel_path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
           ["rel_path"]),
        fn("get_schema", "Retourne le schéma (DDL) de la base de données.", {}, []),
        fn("run_sql", "Exécute une requête SELECT/WITH en LECTURE SEULE sur la base.",
           {"query": {"type": "string", "description": "Requête SQL (SELECT/WITH uniquement)"}},
           ["query"]),
        fn("search_logs", "Recherche dans les logs par pattern, niveau (ERROR/WARN/INFO) et plage de temps ISO.",
           {"pattern": {"type": "string"}, "level": {"type": "string"},
            "since": {"type": "string"}, "until": {"type": "string"}}, []),
    ]
    if has_docs:
        tools.insert(3, fn("search_docs", "Recherche sémantique dans la DOCUMENTATION (RAG). "
                                          "Pour des questions conceptuelles couvertes par les docs.",
                           {"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]))
    return tools


# ===========================================================================
#  AGENT — boucle tool-use
# ===========================================================================
SYSTEM_PROMPT = """Tu es un assistant d'ingénierie sur un système composé de 4 sources :
- CODE SOURCE : utilise grep_code (usages/patterns), find_symbol (définitions), read_file (lecture ciblée).
- DOCUMENTATION : utilise search_docs pour les questions conceptuelles.
- BASE DE DONNÉES : utilise get_schema puis run_sql (lecture seule) pour les questions sur les données.
- LOGS : utilise search_logs pour les incidents, erreurs, événements datés.

Règles :
- Choisis le bon outil selon la nature de la question. N'invente jamais de chemin, de symbole ou de schéma : vérifie avec les outils.
- Pour le code, préfère find_symbol pour localiser, puis read_file pour lire le contexte exact.
- Cite tes sources (fichier:ligne, id de doc, requête SQL) dans la réponse finale.
- Si l'info n'est pas trouvable avec les outils, dis-le."""


class CodeAgent:
    def __init__(self, backend, code: CodeTools, db: DbTools, logs: LogTools,
                 docs: DocsTools | None = None, max_steps: int = 12):
        self.backend = backend
        self.max_steps = max_steps
        self.docs = docs
        # Table de routage nom d'outil -> fonction
        self.dispatch: dict[str, Callable] = {
            "grep_code": code.grep_code,
            "find_symbol": code.find_symbol,
            "read_file": code.read_file,
            "get_schema": db.get_schema,
            "run_sql": db.run_sql,
            "search_logs": logs.search_logs,
        }
        if docs:
            self.dispatch["search_docs"] = docs.search_docs
        self.tools = build_tool_specs(has_docs=docs is not None)

    def _exec_tool(self, name: str, args: dict) -> str:
        if name not in self.dispatch:
            return f"Outil inconnu : {name}"
        try:
            return str(self.dispatch[name](**args))
        except Exception as e:  # on renvoie l'erreur au LLM plutôt que de planter la boucle
            return f"Erreur outil {name}: {e}"

    def ask(self, question: str, verbose: bool = True) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question}]
        for step in range(self.max_steps):
            msg = self.backend.chat(messages, self.tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                return msg.get("content", "")
            messages.append(msg)  # message assistant portant les tool_calls
            for call in calls:
                name = call["function"]["name"]
                args = call["function"]["arguments"]
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                if verbose:
                    print(f"  → {name}({args})")
                result = self._exec_tool(name, args)
                messages.append({"role": "tool", "name": name, "content": result})
        return "Limite d'étapes atteinte sans réponse finale."


# ===========================================================================
#  ASSEMBLAGE
# ===========================================================================
def build_agent(code_root: str, db_path: str, log_path: str,
                docs_db: str | None = None, model: str = "qwen3:14b") -> CodeAgent:
    code = CodeTools(code_root)
    db = DbTools(db_path)
    logs = LogTools(log_path)
    docs = None
    if docs_db and _HAS_RAG:
        docs = DocsTools(LocalRAG(db_path=docs_db))
    return CodeAgent(OllamaBackend(model=model), code, db, logs, docs)


if __name__ == "__main__":
    agent = build_agent(
        code_root="/chemin/vers/le/code",
        db_path="metier.db",
        log_path="/var/log/app/app.log",
        docs_db="rag.db",        # base produite par local_rag.py
        model="qwen3:14b",       # modèle Ollama avec tool-calling fiable
    )
    # À lancer une fois pour indexer les symboles du code :
    # print(CodeTools("/chemin/vers/le/code").build_index())

    print(agent.ask("Où est définie la fonction calculateInterest et que fait-elle ?"))
    print(agent.ask("Combien de deals ont été bookés hier dans la base ?"))
    print(agent.ask("Y a-t-il des erreurs dans les logs depuis ce matin 08:00 ?"))
