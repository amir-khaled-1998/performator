"""
MCP Kondor+ / Linux pour OpenCode
Architecture : OpenCode (Windows) --stdio--> ce serveur (Windows) --SSH/SFTP--> serveur Linux

Variables d'environnement :
  KDR_HOST, KDR_USER            obligatoires
  KDR_PORT                      optionnel (22)
  KDR_PWD                       optionnel : sinon lu dans le Gestionnaire d'identifiants Windows (keyring)
  KDR_CONFIG                    optionnel : chemin du config.yaml (par défaut à côté du script)

IMPORTANT : ne jamais écrire sur stdout (réservé au protocole MCP). Tout passe par audit.log.
"""
import os
import re
import csv
import shlex
import fnmatch
import logging
import tarfile
import zipfile
import posixpath
import datetime as dt
from pathlib import Path

import yaml
import paramiko
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------- config
BASE = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("KDR_CONFIG", BASE / "config.yaml"))
with open(CFG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

ENV = CFG.get("env", "RECETTE")
READ_ONLY = bool(CFG.get("read_only", False))
SAFE_NAME = re.compile(r"^[\w.\-]+$")
SAFE_GLOB = re.compile(r"^[\w.\-*?]+$")

logging.basicConfig(
    filename=BASE / "audit.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("mcp_kondor")

mcp = FastMCP("kondor")


# ---------------------------------------------------------------- SSH
def _password():
    pwd = os.environ.get("KDR_PWD")
    if pwd:
        return pwd
    try:
        import keyring
        return keyring.get_password("mcp_kondor", os.environ["KDR_USER"])
    except Exception:
        return None


_client = None


def ssh() -> paramiko.SSHClient:
    """Connexion SSH réutilisée, reconnectée si coupée."""
    global _client
    if _client and _client.get_transport() and _client.get_transport().is_active():
        return _client
    c = paramiko.SSHClient()
    c.load_system_host_keys()  # ~/.ssh/known_hosts de Windows
    if CFG.get("ssh", {}).get("accept_unknown_host", False):
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        c.set_missing_host_key_policy(paramiko.RejectPolicy())
    pwd = _password()
    c.connect(
        os.environ["KDR_HOST"],
        port=int(os.environ.get("KDR_PORT", 22)),
        username=os.environ["KDR_USER"],
        password=pwd,
        look_for_keys=pwd is None,
        allow_agent=pwd is None,
        timeout=15,
    )
    _client = c
    return c


def run(cmd: str, timeout: int = 300) -> tuple[int, str]:
    """Exécute une commande distante, renvoie (code retour, sortie tronquée)."""
    log.info("RUN %s", cmd)
    _, out, err = ssh().exec_command(cmd, timeout=timeout)
    txt = out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    return rc, txt[-8000:]


def fmt(rc: int, txt: str) -> str:
    return f"rc={rc}\n{txt}".strip()


# ---------------------------------------------------------------- garde-fous
def guard_write(confirm: bool, preview: str):
    """Renvoie un message si l'action doit être bloquée ou confirmée, sinon None."""
    if READ_ONLY:
        return f"[{ENV}] Refusé : ce serveur MCP est en lecture seule."
    if not confirm:
        return f"[{ENV}] {preview}\n→ Rappeler avec confirm=true pour exécuter."
    return None


def local_file(path: str, allowed_dir: str) -> Path:
    """Un fichier Windows, obligatoirement dans le dossier autorisé."""
    root = Path(allowed_dir).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if not p.is_relative_to(root):
        raise ValueError(f"Fichier hors du dossier autorisé {root}")
    if not p.is_file():
        raise ValueError(f"Fichier introuvable : {p}")
    return p


def safe_name(name: str) -> str:
    name = posixpath.basename(name)
    if not SAFE_NAME.match(name):
        raise ValueError(f"Nom de fichier refusé : {name!r}")
    return name


def q(s: str) -> str:
    return shlex.quote(str(s))


# ---------------------------------------------------------------- infos serveur
@mcp.tool()
def server_info() -> str:
    """Environnement, hostname, uptime et espace disque des dossiers Kondor."""
    dirs = " ".join(q(d) for d in CFG.get("disk_check", ["/"]))
    rc, txt = run(f"hostname; uptime; df -h {dirs}", 30)
    return f"Environnement MCP : {ENV} (lecture seule : {READ_ONLY})\n" + fmt(rc, txt)


# ---------------------------------------------------------------- services
@mcp.tool()
def service_status(name: str = "") -> dict:
    """Statut d'un service Kondor+ déclaré, ou de tous si name est vide."""
    services = CFG["services"]
    names = [name] if name else list(services)
    res = {}
    for n in names:
        if n not in services:
            res[n] = "inconnu"
            continue
        rc, txt = run(services[n]["status"], 30)
        res[n] = ("UP" if rc == 0 else "DOWN") + (f" | {txt.strip()[:300]}" if txt.strip() else "")
    return res


@mcp.tool()
def service_action(name: str, action: str, confirm: bool = False) -> str:
    """Démarre, arrête ou redémarre un service Kondor+ (action: start|stop|restart). Exige confirm=true."""
    svc = CFG["services"].get(name)
    if not svc or action not in ("start", "stop", "restart"):
        return f"Refusé : service ou action inconnus. Services : {', '.join(CFG['services'])}"
    blocked = guard_write(confirm, f"Action demandée : {action} {name}")
    if blocked:
        return blocked
    log.info("SERVICE %s %s", action, name)
    steps = ["stop", "start"] if action == "restart" else [action]
    out = []
    for s in steps:
        rc, txt = run(svc[s], svc.get("timeout", 300))
        out.append(f"--- {s} ---\n{fmt(rc, txt)}")
        if rc != 0:
            out.append("Arrêt : la commande a échoué.")
            break
    rc, txt = run(svc["status"], 30)
    out.append(f"--- statut final : {'UP' if rc == 0 else 'DOWN'} ---")
    return "\n".join(out)


@mcp.tool()
def tail_log(name: str, lines: int = 200, grep: str = "") -> str:
    """Dernières lignes d'un log déclaré dans config.yaml, avec filtre optionnel (texte simple)."""
    path = CFG.get("logs", {}).get(name)
    if not path:
        return f"Log inconnu. Logs : {', '.join(CFG.get('logs', {}))}"
    cmd = f"tail -n {min(int(lines), 2000)} {q(path)}"
    if grep:
        cmd += f" | grep -i -F -- {q(grep)}"
    return fmt(*run(cmd, 30))


# ---------------------------------------------------------------- livraisons
def _package_members(p: Path) -> list[str]:
    if p.name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(p) as t:
            return t.getnames()
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            return z.namelist()
    raise ValueError("Format accepté : .tar.gz, .tgz, .tar ou .zip")


@mcp.tool()
def deploy(local_path: str, confirm: bool = False) -> str:
    """Livre un paquet Windows sur le serveur : upload SFTP, sauvegarde de l'existant, extraction.
    Ne redémarre pas les services (utiliser service_action). Sans confirm : aperçu du contenu."""
    d = CFG["deliveries"]
    p = local_file(local_path, d["local_dir"])
    members = _package_members(p)
    bad = [m for m in members if m.startswith("/") or ".." in Path(m).parts]
    if bad:
        return f"Refusé : chemins dangereux dans le paquet : {bad[:10]}"
    preview = (f"Livraison de {p.name} ({p.stat().st_size // 1024} Ko, {len(members)} fichiers) "
               f"vers {d['target']}\n" + "\n".join(members[:80]))
    blocked = guard_write(confirm, preview)
    if blocked:
        return blocked

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_pkg = posixpath.join(d["incoming"], p.name)
    backup = posixpath.join(d["backups"], f"app_{stamp}.tar.gz")
    log.info("DEPLOY %s -> %s (backup %s)", p, d["target"], backup)

    with ssh().open_sftp() as sftp:
        sftp.put(str(p), remote_pkg)

    rc, txt = run(f"tar -czf {q(backup)} -C {q(d['target'])} .", 900)
    if rc != 0:
        return "Sauvegarde échouée, livraison annulée.\n" + fmt(rc, txt)

    if p.suffix == ".zip":
        cmd = f"unzip -o {q(remote_pkg)} -d {q(d['target'])}"
    else:
        cmd = f"tar -xf {q(remote_pkg)} -C {q(d['target'])}"
    rc, txt = run(cmd, 900)
    status = "Livraison OK" if rc == 0 else "Livraison en ERREUR (utiliser rollback)"
    return f"{status}\nSauvegarde : {posixpath.basename(backup)}\n{fmt(rc, txt)}"


@mcp.tool()
def list_backups() -> list:
    """Sauvegardes disponibles pour un rollback (les plus récentes en dernier)."""
    with ssh().open_sftp() as sftp:
        files = sftp.listdir_attr(CFG["deliveries"]["backups"])
    files.sort(key=lambda a: a.st_mtime)
    return [f.filename for f in files if f.filename.endswith(".tar.gz")][-30:]


@mcp.tool()
def rollback(backup_name: str, confirm: bool = False) -> str:
    """Restaure une sauvegarde (écrase les fichiers de la cible). Exige confirm=true."""
    d = CFG["deliveries"]
    name = safe_name(backup_name)
    blocked = guard_write(confirm, f"Rollback de {d['target']} avec {name}")
    if blocked:
        return blocked
    log.info("ROLLBACK %s", name)
    return fmt(*run(f"tar -xzf {q(posixpath.join(d['backups'], name))} -C {q(d['target'])}", 900))


# ---------------------------------------------------------------- import Kondor
def _validate(table: str, p: Path) -> list[str]:
    spec = CFG["formats"].get(table)
    if not spec:
        return [f"Format inconnu : {table}. Formats : {', '.join(CFG['formats'])}"]
    cols = spec["columns"]
    errs = []
    with open(p, newline="", encoding=spec.get("encoding", "latin-1")) as f:
        reader = csv.reader(f, delimiter=spec.get("sep", ";"))
        if spec.get("header", False):
            header = next(reader, [])
            expected = [c["name"] for c in cols]
            if [h.strip() for h in header] != expected:
                errs.append(f"En-tête attendu : {expected}, trouvé : {header}")
        for i, row in enumerate(reader, 1):
            if not row:
                continue
            if len(row) != len(cols):
                errs.append(f"L{i}: {len(row)} colonnes au lieu de {len(cols)}")
                continue
            for c, v in zip(cols, row):
                v = v.strip()
                try:
                    if not v:
                        if c.get("required"):
                            raise ValueError("obligatoire")
                        continue
                    t = c.get("type", "str")
                    if t == "int":
                        int(v)
                    elif t == "float":
                        float(v.replace(",", ".") if spec.get("decimal") == "," else v)
                    elif t == "date":
                        dt.datetime.strptime(v, c.get("fmt", "%Y%m%d"))
                    if c.get("max") and len(v) > c["max"]:
                        raise ValueError(f"longueur > {c['max']}")
                    if c.get("values") and v not in c["values"]:
                        raise ValueError(f"valeur hors liste {c['values']}")
                except ValueError as e:
                    errs.append(f"L{i} {c['name']}={v!r} : {e}")
            if len(errs) >= 50:
                errs.append("… (arrêt à 50 erreurs)")
                break
    return errs


@mcp.tool()
def validate_import(table: str, local_path: str) -> str:
    """Vérifie en local qu'un fichier Windows respecte le format de la table Kondor (sans rien envoyer)."""
    p = local_file(local_path, CFG["kondor_io"]["local_import_dir"])
    errs = _validate(table, p)
    return f"{p.name} : format {table} OK" if not errs else "\n".join(errs)


@mcp.tool()
def import_file(table: str, local_path: str, confirm: bool = False) -> str:
    """Valide un fichier Windows, l'envoie dans le dossier d'import Kondor+ puis lance l'import. Exige confirm=true."""
    io = CFG["kondor_io"]
    p = local_file(local_path, io["local_import_dir"])
    errs = _validate(table, p)
    if errs:
        return "Import bloqué, fichier invalide :\n" + "\n".join(errs)
    blocked = guard_write(confirm, f"Fichier {p.name} valide pour {table}. Import prêt.")
    if blocked:
        return blocked
    remote = posixpath.join(io["import_dir"], safe_name(p.name))
    log.info("IMPORT %s %s -> %s", table, p, remote)
    with ssh().open_sftp() as sftp:
        sftp.put(str(p), remote)
    cmd = io["import_cmd"].format(table=q(table), file=q(remote))
    return fmt(*run(cmd, io.get("import_timeout", 1800)))


# ---------------------------------------------------------------- export / KPI
@mcp.tool()
def run_export(table: str) -> str:
    """Lance le script d'export Kondor+ pour une table déclarée (génère un fichier dans export_dir)."""
    io = CFG["kondor_io"]
    if not io.get("export_cmd"):
        return "Aucun export_cmd configuré."
    if table not in io.get("exportable", []):
        return f"Table non exportable. Autorisées : {io.get('exportable', [])}"
    log.info("EXPORT %s", table)
    return fmt(*run(io["export_cmd"].format(table=q(table)), io.get("export_timeout", 1800)))


@mcp.tool()
def list_exports(pattern: str = "*") -> list:
    """Liste les fichiers d'export/KPI du serveur (motif type *KPI*.csv)."""
    if not SAFE_GLOB.match(pattern):
        raise ValueError("Motif refusé")
    with ssh().open_sftp() as sftp:
        files = sftp.listdir_attr(CFG["kondor_io"]["export_dir"])
    files = [f for f in files if fnmatch.fnmatch(f.filename, pattern)]
    files.sort(key=lambda a: a.st_mtime)
    return [
        f"{f.filename}  {f.st_size // 1024} Ko  {dt.datetime.fromtimestamp(f.st_mtime):%Y-%m-%d %H:%M}"
        for f in files[-100:]
    ]


@mcp.tool()
def read_export(filename: str, max_lines: int = 200) -> str:
    """Lit le début d'un fichier d'export/KPI directement sur le serveur."""
    io = CFG["kondor_io"]
    remote = posixpath.join(io["export_dir"], safe_name(filename))
    lines = []
    with ssh().open_sftp() as sftp, sftp.open(remote, "rb") as f:
        for i, line in enumerate(f):
            if i >= min(int(max_lines), 2000):
                break
            lines.append(line.decode(io.get("encoding", "latin-1"), "replace") if isinstance(line, bytes) else line)
    return "".join(lines)


@mcp.tool()
def download_export(filename: str) -> str:
    """Rapatrie un fichier d'export/KPI sur le poste Windows (dossier local_export_dir)."""
    io = CFG["kondor_io"]
    name = safe_name(filename)
    local_dir = Path(io["local_export_dir"])
    local_dir.mkdir(parents=True, exist_ok=True)
    dest = local_dir / name
    with ssh().open_sftp() as sftp:
        sftp.get(posixpath.join(io["export_dir"], name), str(dest))
    log.info("DOWNLOAD %s -> %s", name, dest)
    return f"Téléchargé : {dest}"


if __name__ == "__main__":
    mcp.run()
