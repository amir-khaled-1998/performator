"""
performator_cli.py — Point d'entree unique pour l'executable client.

Sous-commandes :
    selfcheck                     verifie l'installation (imports, ollama, rg, ctags)
    index <code_root>             construit l'index de symboles (tags.json)
    ask "question" ...            agent Q&A lecture seule (code + db + logs + docs)
    rag-add <fichier> --db ...    indexe un document dans le RAG
    rag-ask "question" --db ...   interroge le RAG (recherche + generation)
    mcp                           lance le serveur MCP stdio (pour opencode.json)
    orchestrate ...               cycle de modification encadre (delegue a orchestrate.py)

Compile en .exe autonome via PyInstaller : le client n'a besoin ni de Python
ni des paquets pip — seulement d'Ollama (+ modeles) deja en place.
"""
from __future__ import annotations

import argparse
import os
import sys


def _base_dir() -> str:
    if getattr(sys, "frozen", False):          # execute depuis l'exe PyInstaller
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# rg.exe / ctags.exe livres a cote de l'exe : on les rend visibles de subprocess
os.environ["PATH"] = _base_dir() + os.pathsep + os.environ.get("PATH", "")

# Console Windows cp1252 : ne jamais planter sur un caractere unicode
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def cmd_selfcheck(_args) -> None:
    import shutil
    import urllib.request

    print("Performator selfcheck")
    print(f"  base        : {_base_dir()}")
    import numpy
    print(f"  numpy       : {numpy.__version__}")
    import mcp as mcp_pkg
    print(f"  mcp         : {getattr(mcp_pkg, '__version__', 'ok')}")
    for tool in ("rg", "ctags"):
        path = shutil.which(tool)
        print(f"  {tool:<11} : {path or 'INTROUVABLE'}")
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
            import json
            models = [m["name"] for m in json.loads(r.read())["models"]]
        print(f"  ollama      : OK ({url}) — modeles : {', '.join(models)}")
    except Exception as e:
        print(f"  ollama      : INACCESSIBLE ({url}) — {e}")


def cmd_index(args) -> None:
    from code_agent import CodeTools
    print(CodeTools(args.code_root, tags_file=args.tags).build_index())


def cmd_ask(args) -> None:
    from code_agent import build_agent
    agent = build_agent(
        code_root=args.code_root, db_path=args.db or "",
        log_path=args.log or "", docs_db=args.docs_db,
        model=args.model,
    )
    print(agent.ask(args.question))


def cmd_rag_add(args) -> None:
    from local_rag import LocalRAG
    rag = LocalRAG(db_path=args.db, embedding_dims=768, llm_model=args.model)
    text = open(args.fichier, encoding="utf-8", errors="ignore").read()
    ids = rag.add_document(text, metadata={"doc": os.path.basename(args.fichier)})
    print(f"{len(ids)} chunks indexes dans {args.db}")
    rag.close()


def cmd_rag_ask(args) -> None:
    from local_rag import LocalRAG
    rag = LocalRAG(db_path=args.db, embedding_dims=768, llm_model=args.model)
    rag.ollama.timeout = 600  # generation CPU : le defaut de 120 s est trop court
    res = rag.ask(args.question, top_k=args.top_k)
    print(res["answer"])
    print("\nSources :", [h.id for h in res["sources"]])
    rag.close()


def cmd_mcp(_args) -> None:
    # Configuration via variables d'environnement (cf. opencode.json) :
    # CODE_ROOT, TAGS_FILE, DB_PATH, LOG_PATH, DOCS_DB
    import mcp_server
    mcp_server.mcp.run()


def cmd_orchestrate(args) -> None:
    import orchestrate
    orchestrate.orchestrate(args.request, args.repo, args.model, args.agent,
                            args.test, args.config)


def main() -> None:
    p = argparse.ArgumentParser(prog="performator",
                                description="Agent local souverain : Q&A code/db/logs/docs + livraison encadree")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selfcheck", help="verifie l'installation").set_defaults(fn=cmd_selfcheck)

    s = sub.add_parser("index", help="construit l'index de symboles ctags")
    s.add_argument("code_root")
    s.add_argument("--tags", default="tags.json")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("ask", help="agent Q&A lecture seule")
    s.add_argument("question")
    s.add_argument("--code-root", required=True)
    s.add_argument("--db", default=None, help="base SQLite metier (lecture seule)")
    s.add_argument("--log", default=None, help="fichier de logs")
    s.add_argument("--docs-db", default=None, help="base RAG documentaire")
    s.add_argument("--model", default="qwen3:8b")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("rag-add", help="indexe un document dans le RAG")
    s.add_argument("fichier")
    s.add_argument("--db", default="rag.db")
    s.add_argument("--model", default="qwen3:8b")
    s.set_defaults(fn=cmd_rag_add)

    s = sub.add_parser("rag-ask", help="interroge le RAG documentaire")
    s.add_argument("question")
    s.add_argument("--db", default="rag.db")
    s.add_argument("--model", default="qwen3:8b")
    s.add_argument("--top-k", type=int, default=3)
    s.set_defaults(fn=cmd_rag_ask)

    sub.add_parser("mcp", help="serveur MCP stdio (reference par opencode.json)").set_defaults(fn=cmd_mcp)

    s = sub.add_parser("orchestrate", help="cycle de modification encadre via OpenCode")
    s.add_argument("request")
    s.add_argument("--repo", required=True)
    s.add_argument("--model", default="ollama/qwen3:8b")
    s.add_argument("--agent", default="deliver")
    s.add_argument("--test", default=None)
    s.add_argument("--config", default=None)
    s.set_defaults(fn=cmd_orchestrate)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
