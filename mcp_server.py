"""
mcp_server.py — Expose les outils de LECTURE de code_agent comme serveur MCP.

Tout client MCP (OpenCode, Claude Code, Qwen Code, Cline...) peut alors récupérer
du contexte sur :
  - le code source (grep, symboles, lecture ciblée)
  - la documentation (RAG sémantique)
  - la base de données (schéma + SQL lecture seule)
  - les logs (recherche filtrée)

C'est la couche "COMPRENDRE". La couche "IMPLÉMENTER" reste OpenCode + Qwen, qui
consomme ce serveur. Aucun outil d'écriture n'est exposé ici : la frontière
read-only est volontaire (audit/permissions propres, contexte bancaire).

Transport : stdio (le défaut MCP). Configuration via variables d'environnement.

Lancement (référencé depuis opencode.json) :
    CODE_ROOT=/chemin/code DB_PATH=metier.db LOG_PATH=/var/log/app.log \
    DOCS_DB=rag.db python mcp_server.py

Dépendances : mcp (SDK officiel), + code_agent.py et local_rag.py à côté.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from code_agent import CodeTools, DbTools, LogTools

try:
    from local_rag import LocalRAG
    _HAS_RAG = True
except ImportError:
    _HAS_RAG = False


# --- Configuration depuis l'environnement ---------------------------------- #
CODE_ROOT = os.environ.get("CODE_ROOT", ".")
TAGS_FILE = os.environ.get("TAGS_FILE", "tags.json")
DB_PATH = os.environ.get("DB_PATH", "")
LOG_PATH = os.environ.get("LOG_PATH", "")
DOCS_DB = os.environ.get("DOCS_DB", "")

mcp = FastMCP("code-context")  # nom du serveur tel que vu par le client

# --- Instanciation des boîtes à outils ------------------------------------- #
_code = CodeTools(CODE_ROOT, tags_file=TAGS_FILE)
_db = DbTools(DB_PATH) if DB_PATH else None
_logs = LogTools(LOG_PATH) if LOG_PATH else None
_rag = LocalRAG(db_path=DOCS_DB) if (DOCS_DB and _HAS_RAG) else None


# --- Outils CODE ------------------------------------------------------------ #
@mcp.tool()
def grep_code(pattern: str, glob: str = "") -> str:
    """Recherche regex/texte dans le code source (ripgrep). Pour usages, chaînes, patterns."""
    return _code.grep_code(pattern, glob=glob or None)


@mcp.tool()
def find_symbol(name: str) -> str:
    """Localise la DÉFINITION d'un symbole (fonction, classe, méthode) via ctags."""
    return _code.find_symbol(name)


@mcp.tool()
def read_file(rel_path: str, start: int = 1, end: int = 0) -> str:
    """Lit une tranche d'un fichier source (chemin relatif à la racine du code)."""
    return _code.read_file(rel_path, start=start, end=end or None)


# --- Outils DATABASE -------------------------------------------------------- #
@mcp.tool()
def get_schema() -> str:
    """Retourne le schéma (DDL) de la base de données."""
    if _db is None:
        return "Aucune base configurée (DB_PATH absent)."
    return _db.get_schema()


@mcp.tool()
def run_sql(query: str) -> str:
    """Exécute une requête SELECT/WITH en LECTURE SEULE sur la base."""
    if _db is None:
        return "Aucune base configurée (DB_PATH absent)."
    return _db.run_sql(query)


# --- Outils LOGS ------------------------------------------------------------ #
@mcp.tool()
def search_logs(pattern: str = "", level: str = "", since: str = "", until: str = "") -> str:
    """Recherche dans les logs par pattern, niveau (ERROR/WARN/INFO) et plage de temps ISO."""
    if _logs is None:
        return "Aucun log configuré (LOG_PATH absent)."
    return _logs.search_logs(
        pattern=pattern, level=level or None,
        since=since or None, until=until or None,
    )


# --- Outils DOCS (RAG) ------------------------------------------------------ #
if _rag is not None:
    @mcp.tool()
    def search_docs(query: str, top_k: int = 5) -> str:
        """Recherche sémantique dans la DOCUMENTATION (RAG). Pour questions conceptuelles."""
        hits = _rag.search(query, top_k=top_k)
        if not hits:
            return "Aucun passage pertinent."
        return "\n\n".join(
            f"[doc {h.id} | {h.metadata.get('doc','?')} | {h.score:.2f}]\n{h.text}"
            for h in hits
        )


if __name__ == "__main__":
    mcp.run()  # transport stdio par défaut
