"""
orchestrate.py — Pont requête utilisateur -> livraison encadrée.

Flux :
  1. Crée une BRANCHE GIT ISOLÉE (jamais sur main, jamais en prod).
  2. Compose un brief cadré qui dit à OpenCode d'utiliser les outils MCP
     (grep_code / find_symbol / read_file / get_schema / run_sql / search_docs)
     pour son contexte, d'implémenter, et d'écrire des tests.
  3. Lance OpenCode en HEADLESS (`opencode run`) avec un agent auto-accept
     (sinon, en non-interactif, OpenCode refuse les écritures — cf. issue connue).
  4. Lance la suite de TESTS.
  5. S'ARRÊTE sur un GATE DE REVUE HUMAINE : diff résumé, branche laissée telle quelle.
     Aucun merge, aucun push prod automatique. C'est ton point de contrôle gouvernance.

Le LLM réfléchit, OpenCode conduit, l'humain valide. En contexte bancaire,
la livraison finale passe par ton pipeline CI/CD existant après revue.

Usage :
    python orchestrate.py "Ajoute une validation null sur interestService dans DealBooking" \
        --repo /chemin/code --model ollama/qwen3-coder --test "mvn -q test"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _run(cmd: list[str], cwd: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "task"


def ensure_clean_tree(repo: str) -> None:
    out = _run(["git", "status", "--porcelain"], repo).stdout.strip()
    if out:
        raise SystemExit(
            "Arbre de travail non propre. Commit/stash d'abord — "
            "l'agent travaille sur une base saine."
        )


def create_branch(repo: str, request: str) -> str:
    branch = f"agent/{_slug(request)}-{datetime.now():%Y%m%d-%H%M%S}"
    r = _run(["git", "switch", "-c", branch], repo)
    if r.returncode != 0:
        raise SystemExit(f"git switch a échoué : {r.stderr}")
    return branch


def build_brief(request: str) -> str:
    return f"""TÂCHE : {request}

CONTRAINTES :
- Utilise les outils MCP de contexte (find_symbol, grep_code, read_file, get_schema,
  run_sql, search_docs) pour COMPRENDRE le code avant de modifier. N'invente aucun
  chemin ni signature : vérifie.
- Modifie le minimum de fichiers nécessaire. Pas de refactor non demandé.
- ÉCRIS des tests couvrant le changement.
- Respecte le style et les conventions existants du dépôt.
- À la fin, résume en 3 lignes ce que tu as changé et pourquoi.
"""


def run_opencode(repo: str, brief: str, model: str, agent: str,
                 config_path: str | None) -> str:
    """Lance OpenCode en non-interactif. Surcharge cette fonction pour tester sans binaire."""
    cmd = ["opencode", "run", "-q", "--model", model, "--agent", agent, brief]
    env_note = f" (OPENCODE_CONFIG={config_path})" if config_path else ""
    print(f"→ opencode run --model {model} --agent {agent}{env_note}")
    import os
    env = dict(os.environ)
    if config_path:
        env["OPENCODE_CONFIG"] = config_path
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env, timeout=3600)
    if r.returncode != 0:
        print("⚠ OpenCode a renvoyé un code non nul :", r.stderr[:500], file=sys.stderr)
    return r.stdout


def run_tests(repo: str, test_cmd: str | None) -> tuple[bool, str]:
    if not test_cmd:
        return True, "(aucune commande de test fournie)"
    r = _run(test_cmd.split(), repo)
    ok = r.returncode == 0
    return ok, (r.stdout + r.stderr)[-2000:]


def summarize_diff(repo: str) -> str:
    stat = _run(["git", "diff", "--stat", "HEAD"], repo).stdout
    # git diff ignore les fichiers nouveaux non suivis : on les liste à part.
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard"], repo
    ).stdout.strip()
    parts = []
    if stat:
        parts.append(stat.rstrip())
    if untracked:
        parts.append("Fichiers nouveaux (non suivis) :\n  " + "\n  ".join(untracked.splitlines()))
    return "\n".join(parts) if parts else "(aucune modification détectée)"


def orchestrate(request: str, repo: str, model: str, agent: str,
                test_cmd: str | None, config_path: str | None,
                opencode_runner=run_opencode) -> dict:
    repo = str(Path(repo).resolve())
    ensure_clean_tree(repo)
    branch = create_branch(repo, request)
    print(f"✓ Branche isolée : {branch}")

    brief = build_brief(request)
    agent_output = opencode_runner(repo, brief, model, agent, config_path)
    print("✓ OpenCode a terminé son passage.")

    tests_ok, test_log = run_tests(repo, test_cmd)
    diff = summarize_diff(repo)

    # ----- GATE DE REVUE HUMAINE : on s'arrête ici volontairement -----
    print("\n" + "=" * 60)
    print("GATE DE REVUE HUMAINE — rien n'est mergé ni poussé")
    print("=" * 60)
    print(f"Branche      : {branch}")
    print(f"Tests        : {'PASS ✓' if tests_ok else 'ÉCHEC ✗'}")
    print(f"\nDiff :\n{diff}")
    print("\nProchaines étapes (humain) :")
    print(f"  git -C {repo} diff {branch}      # relire les changements")
    print(f"  # puis ouvrir une PR -> ton pipeline CI/CD existant prend le relais")

    return {"branch": branch, "tests_ok": tests_ok,
            "diff": diff, "agent_output": agent_output, "test_log": test_log}


def main():
    p = argparse.ArgumentParser(description="Orchestrateur agent -> OpenCode (livraison encadrée)")
    p.add_argument("request", help="Demande en langage naturel")
    p.add_argument("--repo", required=True, help="Racine du dépôt git du code")
    p.add_argument("--model", default="ollama/qwen3-coder", help="provider/model OpenCode")
    p.add_argument("--agent", default="deliver", help="Agent OpenCode (auto-accept des écritures)")
    p.add_argument("--test", default=None, help="Commande de test, ex. 'mvn -q test' ou 'pytest -q'")
    p.add_argument("--config", default=None, help="Chemin vers opencode.json")
    args = p.parse_args()
    orchestrate(args.request, args.repo, args.model, args.agent, args.test, args.config)


if __name__ == "__main__":
    main()
