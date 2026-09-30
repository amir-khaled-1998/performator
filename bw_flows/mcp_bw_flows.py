"""
MCP "bw-flows" : rétro-spécification de flux TIBCO BW 5 -> Kondor+.

Deux sources complémentaires :
  1. Les fichiers d'entrée/sortie (boîte noire, spécification par l'exemple) :
     corpus local <corpus_dir>/<flux>/in et /out, rempli à la main ou via sync_corpus (SFTP, lecture seule).
  2. Les EAR BW5 (boîte blanche) : un groupe d'EAR est indexé ensemble (EAR -> .par/.sar/.projlib -> .process),
     les process sont reliés entre EAR par les files JMS, sujets RV, chemins de fichiers et appels de sous-process,
     avec résolution des variables globales (défauts de l'EAR + surcharges de déploiement).

  - Toute l'analyse tourne en local ; seules des synthèses remontent au modèle,
    avec masquage des champs sensibles (mask_fields dans flows.yaml).
  - Sorties écrites uniquement dans <spec_dir> : SPEC_<flux>.md, golden_<flux>.jsonl,
    ear_index.json et ear_cache/ (process extraits, lisibles directement par l'agent).

Variables d'environnement (uniquement pour sync_corpus) :
  KDR_HOST, KDR_USER, KDR_PORT (22), KDR_PWD (sinon keyring "mcp_kondor"), BWF_CONFIG

IMPORTANT : ne jamais écrire sur stdout (réservé au protocole MCP).
"""
import io
import os
import re
import csv
import json
import math
import fnmatch
import hashlib
import logging
import zipfile
import posixpath
import datetime as dt
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

# ============================================================ configuration
BASE = Path(__file__).resolve().parent
CFG_PATH = Path(os.environ.get("BWF_CONFIG", BASE / "flows.yaml"))
with open(CFG_PATH, encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

CORPUS = Path(CFG.get("corpus_dir", BASE / "corpus"))
SPECS = Path(CFG.get("spec_dir", BASE / "specs"))
MAX_PAIRS = int(CFG.get("max_pairs", 1000))
LOOKUP_MAX = int(CFG.get("lookup_max_distinct", 200))

logging.basicConfig(filename=BASE / "bw_flows_audit.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("bw_flows")

mcp = FastMCP("bw-flows")


def _flow(name: str) -> dict:
    fl = CFG.get("flows", {}).get(name)
    if not fl:
        raise ValueError(f"Flux inconnu : {name}. Flux : {', '.join(CFG.get('flows', {}))}")
    return fl


def _fmt(flow: dict, side: str) -> dict:
    spec = flow[side]["format"]
    if isinstance(spec, str):
        spec = CFG.get("formats", {}).get(spec)
        if not spec:
            raise ValueError(f"Format {flow[side]['format']!r} absent de la section formats")
    return spec


# ============================================================ lecture des fichiers
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _flatten_xml(el, path: str, out: dict):
    for k, v in el.attrib.items():
        out[f"{path}/@{_strip_ns(k)}" if path else f"@{_strip_ns(k)}"] = v.strip()
    kids = list(el)
    if not kids:
        if path:
            out[path] = (el.text or "").strip()
        return
    seen = Counter()
    for c in kids:
        name = _strip_ns(c.tag)
        seen[name] += 1
        p = f"{path}/{name}" if path else name
        if seen[name] > 1:
            p += f"[{seen[name]}]"
        _flatten_xml(c, p, out)


def _flatten_json(obj, path: str, out: dict):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, f"{path}.{k}" if path else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj, 1):
            _flatten_json(v, f"{path}[{i}]", out)
    else:
        out[path] = "" if obj is None else str(obj).strip()


def _col_names(fmt: dict):
    cols = fmt.get("columns")
    if not cols:
        return None
    return [c["name"] if isinstance(c, dict) else str(c) for c in cols]


def _parse_file(p: Path, fmt: dict):
    kind = fmt.get("type", "csv")
    enc = fmt.get("encoding", "latin-1")
    if kind == "xml":
        root = ET.parse(p).getroot()
        rec_tag = fmt.get("record")
        elems = [e for e in root.iter() if _strip_ns(e.tag) == rec_tag] if rec_tag else [root]
        for e in elems:
            out = {}
            _flatten_xml(e, "", out)
            yield out
    elif kind == "json":
        data = json.loads(p.read_text(encoding=enc))
        for part in (fmt.get("record") or "").split("."):
            if part:
                data = data[part]
        for item in (data if isinstance(data, list) else [data]):
            out = {}
            _flatten_json(item, "", out)
            yield out
    elif kind == "fixed":
        lines = p.read_text(encoding=enc).splitlines()[int(fmt.get("skip_lines", 0)):]
        for line in lines:
            if not line.strip():
                continue
            yield {c["name"]: line[c["start"] - 1:c["start"] - 1 + c["len"]].strip()
                   for c in fmt["columns"]}
    else:  # csv / délimité
        with open(p, newline="", encoding=enc) as f:
            rows = [r for r in csv.reader(f, delimiter=fmt.get("sep", ";"))
                    if r and any(x.strip() for x in r)]
        names = None
        if fmt.get("header"):
            names, rows = [h.strip() for h in rows[0]], rows[1:]
        else:
            names = _col_names(fmt)
        for r in rows:
            yield {(names[i] if names and i < len(names) else f"col{i + 1}"): v.strip()
                   for i, v in enumerate(r)}


_cache = {}


def _files(flow_name: str, side: str) -> list[Path]:
    d = CORPUS / flow_name / side
    pattern = _flow(flow_name)[{"in": "input", "out": "output"}[side]].get("pattern", "*")
    return sorted(p for p in d.glob("*") if p.is_file() and fnmatch.fnmatch(p.name, pattern)) if d.exists() else []


def _records(flow_name: str, side: str) -> tuple[list[dict], list[str]]:
    """Enregistrements aplatis d'un côté du flux (+ erreurs de lecture). Mis en cache."""
    flow = _flow(flow_name)
    fmt = _fmt(flow, "input" if side == "in" else "output")
    files = _files(flow_name, side)
    sig = (flow_name, side, tuple((p.name, p.stat().st_size, p.stat().st_mtime) for p in files))
    if sig in _cache:
        return _cache[sig]
    recs, errors = [], []
    for p in files:
        try:
            for r in _parse_file(p, fmt):
                r["_file"] = p.name
                recs.append(r)
        except Exception as e:  # un fichier illisible ne bloque pas le reste
            errors.append(f"{p.name}: {e}")
    _cache[sig] = (recs, errors)
    return recs, errors


# ============================================================ corrélation
def _norm_key(v: str, ops) -> str:
    v = (v or "").strip()
    for o in ops or []:
        if o == "upper":
            v = v.upper()
        elif o == "lstrip0":
            v = v.lstrip("0") or "0"
    return v


def _pairs(flow_name: str) -> dict:
    flow = _flow(flow_name)
    key = flow["key"]
    ins, e_in = _records(flow_name, "in")
    outs, e_out = _records(flow_name, "out")
    idx_in, idx_out = defaultdict(list), defaultdict(list)
    for r in ins:
        k = _norm_key(r.get(key["input"], ""), key.get("normalize"))
        if k:
            idx_in[k].append(r)
    for r in outs:
        k = _norm_key(r.get(key["output"], ""), key.get("normalize"))
        if k:
            idx_out[k].append(r)
    common = [k for k in idx_in if k in idx_out]
    pairs = [(idx_in[k][0], idx_out[k][0], k) for k in common]
    return {
        "ins": ins, "outs": outs, "errors": e_in + e_out,
        "idx_in": idx_in, "idx_out": idx_out, "pairs": pairs,
        "in_only": [k for k in idx_in if k not in idx_out],
        "out_only": [k for k in idx_out if k not in idx_in],
        "multi_out": {k: len(v) for k, v in idx_out.items() if len(v) > 1 and k in idx_in},
        "multi_in": {k: len(v) for k, v in idx_in.items() if len(v) > 1},
    }


# ============================================================ masquage
def _masks(flow_name: str):
    return list(CFG.get("mask_fields", [])) + list(_flow(flow_name).get("mask_fields", []))


def _mask(flow_name: str, field: str, value: str) -> str:
    if value and any(fnmatch.fnmatch(field, pat) for pat in _masks(flow_name)):
        return f"<masqué:{hashlib.sha1(value.encode()).hexdigest()[:6]}>"
    return value


# ============================================================ typage des valeurs
DATE_FMTS = ["%Y%m%d", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S",
             "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%d%m%Y", "%d-%b-%Y"]
_TZ = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _as_date(v: str):
    s = (v or "").strip()
    if len(s) < 6 or len(s) > 30:
        return None
    s = _TZ.sub("", s)
    for f in DATE_FMTS:
        try:
            return dt.datetime.strptime(s, f).date().isoformat(), f
        except ValueError:
            pass
    return None


def _as_num(v: str):
    s = (v or "").strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        x = float(s)
    except ValueError:
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _r(x):
    return round(x, 6) + 0.0  # 0.0 évite -0.0


# Transformations candidates : (nom, famille, fonction). L'ordre = préférence en cas d'égalité.
TRANSFORMS = [
    ("copie", "txt", lambda v: v.strip()),
    ("date reformatée", "date", lambda v: (_as_date(v) or (None,))[0]),
    ("sans zéros de tête", "txt", lambda v: v.strip().lstrip("0") or ("0" if v.strip() else "")),
    ("majuscules", "txt", lambda v: v.strip().upper()),
    ("nombre (reformaté)", "num", lambda v: None if _as_num(v) is None else _r(_as_num(v))),
    ("signe inversé", "num", lambda v: None if _as_num(v) is None else _r(-_as_num(v))),
    ("valeur absolue", "num", lambda v: None if _as_num(v) is None else _r(abs(_as_num(v)))),
    ("× 100", "num", lambda v: None if _as_num(v) is None else _r(_as_num(v) * 100)),
    ("÷ 100", "num", lambda v: None if _as_num(v) is None else _r(_as_num(v) / 100)),
]
T_ORDER = {t[0]: i for i, t in enumerate(TRANSFORMS)}


def _out_canon(v: str) -> dict:
    d = _as_date(v)
    n = _as_num(v)
    return {"txt": v.strip() or None, "date": d[0] if d else None, "num": None if n is None else _r(n)}


def _field_type(values: list[str]) -> str:
    vals = [v for v in values if v][:500]
    if not vals:
        return "vide"
    fmts = Counter(d[1] for d in (_as_date(v) for v in vals) if d)
    if fmts and sum(fmts.values()) >= 0.9 * len(vals):
        return f"date {fmts.most_common(1)[0][0]}"
    if sum(_as_num(v) is not None for v in vals) >= 0.95 * len(vals):
        return "entier" if all(re.fullmatch(r"-?\d+", v) for v in vals) else "décimal"
    return "texte"


def _conf(s: float) -> str:
    return "haute" if s >= 0.99 else "moyenne" if s >= 0.9 else "faible" if s >= 0.6 else "inconnue"


# ============================================================ inférence du mapping
def _conditional(pairs, in_cols, hit_list, max_values: int = 10):
    """Cherche un champ d'entrée à peu de valeurs qui, pour chacune de ses valeurs, sélectionne
    une transformation différente (ex. Side=BUY -> montant ; Side=SELL -> montant signe inversé)."""
    best = None
    for col in in_cols:
        groups = defaultdict(list)
        for i, (inrec, _, _) in enumerate(pairs):
            if hit_list[i] is not None:
                groups[inrec.get(col, "")].append(hit_list[i])
        if not (2 <= len(groups) <= max_values):
            continue
        total, ok, branches = 0, 0, {}
        for val, hl in groups.items():
            c = Counter(h for hs in hl for h in hs)
            total += len(hl)
            if not c:
                continue
            (src, t), cnt = sorted(c.items(), key=lambda x: (-x[1], T_ORDER[x[0][1]], x[0][0]))[0]
            ok += cnt
            branches[val] = (src, t)
        if len(set(branches.values())) < 2:  # même règle partout : pas une vraie condition
            continue
        sup = ok / total if total else 0
        if not best or sup > best["support"]:
            best = {"field": col, "support": sup, "branches": dict(sorted(branches.items()))}
    return best


_mapping_cache = {}


def _infer(flow_name: str) -> dict:
    P = _pairs(flow_name)
    pairs = P["pairs"][:MAX_PAIRS]
    if not pairs:
        raise ValueError("Aucune paire entrée/sortie corrélée : vérifier la clé du flux (key) et le corpus.")
    sig = (flow_name,) + tuple((p.name, p.stat().st_size, p.stat().st_mtime)
                               for s in ("in", "out") for p in _files(flow_name, s))
    if sig in _mapping_cache:
        return _mapping_cache[sig]

    in_cols = sorted({c for i, _, _ in pairs for c in i if c != "_file"})
    out_cols = [c for c in dict.fromkeys(c for _, o, _ in pairs for c in o) if c != "_file"]
    n = len(pairs)

    # 1) index inversé par paire : valeur transformée -> (colonne source, transformation)
    counts = {oc: Counter() for oc in out_cols}
    hits = {oc: [None] * len(pairs) for oc in out_cols}   # correspondances par paire (None = sortie vide)
    nonempty = Counter()
    for pi, (inrec, outrec, _) in enumerate(pairs):
        idx = {"txt": defaultdict(set), "date": defaultdict(set), "num": defaultdict(set)}
        for col in in_cols:
            v = inrec.get(col, "")
            if not v:
                continue
            for tname, fam, fn in TRANSFORMS:
                try:
                    tv = fn(v)
                except Exception:
                    tv = None
                if tv is not None and tv != "":
                    idx[fam][tv].add((col, tname))
        for oc in out_cols:
            ov = outrec.get(oc, "")
            if not ov.strip():
                continue
            nonempty[oc] += 1
            canon = _out_canon(ov)
            hit = set()
            for fam in ("txt", "date", "num"):
                if canon[fam] is not None:
                    hit |= idx[fam].get(canon[fam], set())
            counts[oc].update(hit)
            hits[oc][pi] = hit

    result = {}
    for oc in out_cols:
        vals = [o.get(oc, "") for _, o, _ in pairs]
        ne = nonempty[oc]
        entry = {"field": oc, "type": _field_type(vals), "fill": ne / n, "candidates": []}
        if ne == 0:
            entry.update(rule="toujours vide", source="—", support=1.0)
            result[oc] = entry
            continue

        top_val, top_n = Counter(v for v in vals if v).most_common(1)[0]
        cands = []
        for (col, tname), c in counts[oc].items():
            src_vals = [i.get(col, "") for i, _, _ in pairs]
            src_distinct = len(set(v for v in src_vals if v))
            cands.append({"source": col, "rule": tname, "support": c / ne, "src_distinct": src_distinct})
        cands.sort(key=lambda x: (-x["support"], T_ORDER[x["rule"]], x["source"]))

        # 2) constante
        const_ok = top_n / ne >= 0.98 and ne >= 5
        best = cands[0] if cands else None
        if const_ok and not (best and best["support"] >= 0.999 and best["src_distinct"] > 1):
            entry.update(rule=f"constante {top_val!r}", source="—", support=top_n / ne)
        elif best and best["support"] >= 0.95:
            entry.update(rule=best["rule"], source=best["source"], support=best["support"])
        else:
            # 3) table de correspondance (lookup) déduite
            best_lk = None
            for col in in_cols:
                m = defaultdict(Counter)
                for i, o, _ in pairs:
                    iv, ov = i.get(col, ""), o.get(oc, "")
                    if ov:
                        m[iv][ov] += 1
                if not (2 <= len(m) <= LOOKUP_MAX) or ne / len(m) < 3:
                    continue
                if len({ov for c in m.values() for ov in c}) < 2:
                    continue
                cons = sum(c.most_common(1)[0][1] for c in m.values()) / ne
                if not best_lk or cons > best_lk["support"]:
                    best_lk = {"source": col, "support": cons,
                               "table": {k: c.most_common(1)[0][0] for k, c in m.items()}}
            if best_lk and best_lk["support"] >= 0.95 and best_lk["support"] > (best["support"] if best else 0):
                entry.update(rule="table de correspondance", source=best_lk["source"],
                             support=best_lk["support"] * 0.999, table=best_lk["table"])
            else:
                # 4) règle conditionnelle : la transformation dépend d'un champ à peu de valeurs (ex. signe selon Side)
                cond = _conditional(pairs, in_cols, hits[oc])
                floor = max(best["support"] if best else 0, best_lk["support"] if best_lk else 0)
                if cond and cond["support"] >= 0.95 and cond["support"] > floor:
                    entry.update(rule="conditionnelle", source=cond["field"], support=cond["support"] * 0.998,
                                 detail=" ; ".join(f"{v or '(vide)'} → {t} de {s}" for v, (s, t) in cond["branches"].items()))
                elif best:
                    entry.update(rule=best["rule"], source=best["source"], support=best["support"])
                else:
                    entry.update(rule="non trouvée", source="?", support=0.0)
        entry["confidence"] = _conf(entry["support"])
        entry["candidates"] = [c for c in cands[:4] if not (c["source"] == entry["source"] and c["rule"] == entry["rule"])][:3]

        # formats de date détectés (pour la spec)
        if entry["rule"] == "date reformatée":
            fi = Counter(d[1] for d in (_as_date(i.get(entry["source"], "")) for i, _, _ in pairs) if d)
            fo = Counter(d[1] for d in (_as_date(o.get(oc, "")) for _, o, _ in pairs) if d)
            entry["detail"] = f"{fi.most_common(1)[0][0] if fi else '?'} → {fo.most_common(1)[0][0] if fo else '?'}"
        result[oc] = entry

    res = {"pairs": n, "in_cols": len(in_cols), "fields": result}
    _mapping_cache.clear()
    _mapping_cache[sig] = res
    return res


# ============================================================ règles de filtrage (entrées sans sortie)
def _filters(flow_name: str, P: dict) -> list[dict]:
    matched = [P["idx_in"][k][0] for k in P["idx_in"] if k in P["idx_out"]]
    unmatched = [P["idx_in"][k][0] for k in P["in_only"]]
    if not unmatched or not matched:
        return []
    cols = {c for r in unmatched for c in r if c != "_file"}
    found = []
    for col in cols:
        vu = Counter(r.get(col, "") for r in unmatched)
        vm = Counter(r.get(col, "") for r in matched)
        if len(vu) > 20:
            continue
        for val, cu in vu.items():
            pu, pm = cu / len(unmatched), vm.get(val, 0) / len(matched)
            if pu >= 0.8 and pm <= 0.05:
                found.append({"field": col, "value": val or "(vide)", "in_rejects": pu, "in_matched": pm})
    return sorted(found, key=lambda x: (-x["in_rejects"], x["in_matched"]))[:10]


# ============================================================ outils MCP
@mcp.tool()
def list_flows() -> list:
    """Liste les flux BW -> Kondor déclarés, avec la taille du corpus local de chacun."""
    out = []
    for name, fl in CFG.get("flows", {}).items():
        fi, fo = _files(name, "in"), _files(name, "out")
        last = max((p.stat().st_mtime for p in fi + fo), default=None)
        out.append({
            "flux": name, "description": fl.get("description", ""),
            "fichiers_entree": len(fi), "fichiers_sortie": len(fo),
            "dernier_fichier": dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M") if last else None,
        })
    return out


@mcp.tool()
def sync_corpus(flow: str, max_files: int = 300, since_days: int = 0) -> str:
    """Rapatrie en lecture seule (SFTP) les fichiers d'entrée et de sortie d'un flux vers le corpus local.
    Ne télécharge pas les fichiers déjà présents avec la même taille."""
    import paramiko
    fl = _flow(flow)
    pwd = os.environ.get("KDR_PWD")
    if not pwd:
        try:
            import keyring
            pwd = keyring.get_password("mcp_kondor", os.environ["KDR_USER"])
        except Exception:
            pwd = None
    c = paramiko.SSHClient()
    c.load_system_host_keys()
    c.set_missing_host_key_policy(paramiko.RejectPolicy())
    c.connect(os.environ["KDR_HOST"], port=int(os.environ.get("KDR_PORT", 22)),
              username=os.environ["KDR_USER"], password=pwd,
              look_for_keys=pwd is None, allow_agent=pwd is None, timeout=15)
    report = []
    limit = dt.datetime.now().timestamp() - since_days * 86400 if since_days else 0
    try:
        with c.open_sftp() as sftp:
            for side, key in (("in", "input"), ("out", "output")):
                remote = fl[key].get("remote_dir")
                if not remote:
                    report.append(f"{side}: pas de remote_dir, corpus alimenté à la main")
                    continue
                dest = CORPUS / flow / side
                dest.mkdir(parents=True, exist_ok=True)
                attrs = [a for a in sftp.listdir_attr(remote)
                         if fnmatch.fnmatch(a.filename, fl[key].get("pattern", "*")) and a.st_mtime >= limit]
                attrs.sort(key=lambda a: a.st_mtime, reverse=True)
                got = 0
                for a in attrs[:max_files]:
                    local = dest / a.filename
                    if local.exists() and local.stat().st_size == a.st_size:
                        continue
                    sftp.get(posixpath.join(remote, a.filename), str(local))
                    got += 1
                report.append(f"{side}: {got} nouveaux fichiers ({len(attrs)} disponibles)")
    finally:
        c.close()
    log.info("SYNC %s %s", flow, report)
    return "\n".join(report)


@mcp.tool()
def corpus_stats(flow: str) -> dict:
    """Volumes et qualité de corrélation d'un flux : paires, entrées sans sortie, sorties sans entrée, multiplicités."""
    P = _pairs(flow)
    return {
        "enregistrements_entree": len(P["ins"]),
        "enregistrements_sortie": len(P["outs"]),
        "paires_correlees": len(P["pairs"]),
        "entrees_sans_sortie": len(P["in_only"]),
        "sorties_sans_entree": len(P["out_only"]),
        "cles_avec_plusieurs_sorties": len(P["multi_out"]),
        "cles_dupliquees_en_entree": len(P["multi_in"]),
        "erreurs_lecture": P["errors"][:20],
    }


@mcp.tool()
def profile(flow: str, side: str = "in", offset: int = 0, limit: int = 60) -> list:
    """Profil des champs d'un côté du flux (side: in|out) : remplissage, valeurs distinctes, type détecté, exemples."""
    if side not in ("in", "out"):
        raise ValueError("side doit valoir in ou out")
    recs, _ = _records(flow, side)
    if not recs:
        return []
    cols = list(dict.fromkeys(c for r in recs for c in r if c != "_file"))
    out = []
    for col in cols[offset:offset + limit]:
        vals = [r.get(col, "") for r in recs]
        filled = [v for v in vals if v]
        top = Counter(filled).most_common(3)
        out.append({
            "champ": col, "remplissage": f"{len(filled) / len(recs):.0%}",
            "distincts": len(set(filled)), "type": _field_type(vals),
            "long_max": max((len(v) for v in filled), default=0),
            "exemples": [f"{_mask(flow, col, v)} ({c})" for v, c in top],
        })
    return out


@mcp.tool()
def sample_pairs(flow: str, n: int = 3, key: str = "") -> list:
    """Exemples de paires entrée -> sortie corrélées (champs sensibles masqués). key : une clé précise (optionnel)."""
    P = _pairs(flow)
    if key:
        k = _norm_key(key, _flow(flow)["key"].get("normalize"))
        pairs = [(P["idx_in"][k][0], P["idx_out"][k][0], k)] if k in P["idx_in"] and k in P["idx_out"] else []
    else:
        step = max(1, len(P["pairs"]) // max(1, n))
        pairs = P["pairs"][::step][:min(n, 10)]
    return [{
        "cle": _mask(flow, _flow(flow)["key"]["input"], k),
        "entree": {c: _mask(flow, c, v) for c, v in i.items() if v},
        "sortie": {c: _mask(flow, c, v) for c, v in o.items()},
    } for i, o, k in pairs]


@mcp.tool()
def infer_mapping(flow: str) -> str:
    """Déduit, pour chaque champ Kondor en sortie, sa source et sa règle (copie, format de date, signe,
    constante, table de correspondance…) avec un taux de confirmation sur les paires du corpus."""
    res = _infer(flow)
    lines = [f"{res['pairs']} paires analysées, {res['in_cols']} champs d'entrée.",
             "champ sortie | règle | source | confirmé | confiance"]
    for e in res["fields"].values():
        rule = e["rule"] + (f" ({e['detail']})" if e.get("detail") else "")
        if e.get("table"):
            rule += f" [{len(e['table'])} valeurs]"
        lines.append(f"{e['field']} | {rule} | {e['source']} | {e['support']:.0%} | {e.get('confidence', 'haute')}")
        for c in e.get("candidates", []):
            if c["support"] >= 0.5:
                lines.append(f"   alt : {c['rule']} de {c['source']} ({c['support']:.0%})")
    return "\n".join(lines)


@mcp.tool()
def lookup_table(flow: str, output_field: str) -> dict:
    """Table de correspondance déduite pour un champ de sortie (valeur d'entrée -> valeur Kondor)."""
    e = _infer(flow)["fields"].get(output_field)
    if not e:
        raise ValueError("Champ de sortie inconnu")
    if not e.get("table"):
        return {"message": f"Pas de table pour {output_field} (règle : {e['rule']})"}
    return {"source": e["source"], "confirme": f"{e['support']:.0%}",
            "table": {_mask(flow, e["source"], k): _mask(flow, output_field, v) for k, v in sorted(e["table"].items())}}


@mcp.tool()
def find_edge_cases(flow: str) -> dict:
    """Cas particuliers : entrées sans sortie (rejets / filtres, avec règle de filtrage probable),
    sorties sans entrée, une entrée qui produit plusieurs sorties, valeurs rares."""
    P = _pairs(flow)
    fl = _flow(flow)
    res = {
        "entrees_sans_sortie": len(P["in_only"]),
        "filtres_probables": [
            f"{f['field']} = {_mask(flow, f['field'], f['value'])} : {f['in_rejects']:.0%} des rejets, "
            f"{f['in_matched']:.0%} des entrées traitées" for f in _filters(flow, P)],
        "exemples_cles_sans_sortie": [_mask(flow, fl["key"]["input"], k) for k in P["in_only"][:10]],
        "sorties_sans_entree": len(P["out_only"]),
        "exemples_cles_sortie_orphelines": [_mask(flow, fl["key"]["output"], k) for k in P["out_only"][:10]],
        "une_entree_plusieurs_sorties": dict(list(P["multi_out"].items())[:10]),
    }
    rare = []
    outs = P["outs"]
    for col in list(dict.fromkeys(c for r in outs for c in r if c != "_file")):
        cnt = Counter(r.get(col, "") for r in outs)
        if 2 <= len(cnt) <= 30:
            for v, c in cnt.items():
                if c / len(outs) < 0.01:
                    rare.append(f"{col} = {_mask(flow, col, v) or '(vide)'} ({c} fois)")
    res["valeurs_rares_en_sortie"] = rare[:20]
    return res


@mcp.tool()
def generate_spec(flow: str, golden_max: int = 500) -> str:
    """Écrit la spécification SPEC_<flux>.md et le jeu de tests golden_<flux>.jsonl (paires entrée -> sortie attendue)."""
    fl = _flow(flow)
    P = _pairs(flow)
    res = _infer(flow)
    filt = _filters(flow, P)
    SPECS.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    md = [f"# Spécification du flux {flow}", "",
          f"*Rétro-spécifiée le {today} à partir du corpus d'échanges (TIBCO BW 5 → Kondor+). "
          f"À valider par le métier et, si disponible, par la lecture de l'EAR BW.*", "",
          "## 1. Contexte", "",
          f"- Description : {fl.get('description', 'à compléter')}",
          f"- Entrée : `{fl['input'].get('pattern', '*')}` (format {_fmt(fl, 'input').get('type', 'csv')})",
          f"- Sortie Kondor : `{fl['output'].get('pattern', '*')}` (format {_fmt(fl, 'output').get('type', 'csv')})",
          f"- Clé de corrélation : `{fl['key']['input']}` → `{fl['key']['output']}`", "",
          "## 2. Corpus analysé", "",
          f"| Entrées | Sorties | Paires corrélées | Entrées sans sortie | Sorties sans entrée | Multi-sorties |",
          "|---|---|---|---|---|---|",
          f"| {len(P['ins'])} | {len(P['outs'])} | {len(P['pairs'])} | {len(P['in_only'])} | "
          f"{len(P['out_only'])} | {len(P['multi_out'])} |", "",
          "## 3. Sélection des enregistrements", ""]
    if filt:
        md.append("Règles de filtrage probables (entrées sans sortie Kondor) :")
        md.append("")
        for f in filt:
            md.append(f"- `{f['field']}` = `{f['value']}` → non transmis ({f['in_rejects']:.0%} des rejets, "
                      f"{f['in_matched']:.0%} des entrées traitées)")
    else:
        md.append("Aucune règle de filtrage évidente détectée.")
    md += ["", "## 4. Mapping des champs Kondor", "",
           "| Champ Kondor | Type | Source (entrée) | Règle | Confirmé | Confiance |",
           "|---|---|---|---|---|---|"]
    for e in res["fields"].values():
        rule = e["rule"] + (f" ({e['detail']})" if e.get("detail") else "")
        if e.get("table"):
            rule += f" — voir annexe {e['field']}"
        md.append(f"| {e['field']} | {e['type']} | `{e['source']}` | {rule} | {e['support']:.0%} | "
                  f"{e.get('confidence', 'haute')} |")

    questions = [e for e in res["fields"].values() if e.get("confidence") in ("faible", "inconnue")]
    md += ["", "## 5. Points à valider", ""]
    for e in questions:
        md.append(f"- **{e['field']}** : règle {e['rule']} confirmée à {e['support']:.0%} seulement. "
                  f"Règle réelle ? (calcul, référentiel, champ combiné…)")
    if P["multi_out"]:
        md.append(f"- {len(P['multi_out'])} entrées produisent plusieurs lignes Kondor : règle d'éclatement à préciser.")
    if P["out_only"]:
        md.append(f"- {len(P['out_only'])} lignes Kondor sans entrée correspondante : autre source ou clé transformée ?")
    md += ["- Appels BW à des référentiels/bases non visibles dans les fichiers : à confirmer.",
           "- Gestion d'erreurs (rejets, fichiers d'erreur, relances) : à décrire.", ""]

    tables = [e for e in res["fields"].values() if e.get("table")]
    if tables:
        md += ["## Annexes : tables de correspondance déduites", ""]
        for e in tables:
            md += [f"### {e['field']} (depuis `{e['source']}`)", "", "| Entrée | Kondor |", "|---|---|"]
            for k, v in sorted(e["table"].items())[:200]:
                md.append(f"| {_mask(flow, e['source'], k) or '(vide)'} | {_mask(flow, e['field'], v)} |")
            md.append("")
    if EAR_INDEX.exists():
        try:
            em = _ear_mapping(flow)
        except Exception as e:
            em = {"trouve": False, "message": str(e)}
        md += ["## Règles lues dans les EAR (boîte blanche)", ""]
        if not em.get("trouve"):
            md += [f"{em.get('message')}", ""]
        else:
            md += [f"- Écriture Kondor : `{em['ear']}:{em['process']}`, activité {em['ecriture']}"]
            md += [f"- En amont : {u}" for u in em["en_amont"]] or []
            md += [f"- Filtre (transition) : `{f}`" for f in em["filtres"]]
            md += [f"- Référentiel appelé : `{r}`" for r in em["referentiels_appeles"]]
            md += ["", "| Champ Kondor | Expression BW (XPath) | Règle déduite du corpus | Confirmé |",
                   "|---|---|---|---|"]
            for e in res["fields"].values():
                lines = em["champs_kondor"].get(e["field"], [])
                expr = "<br>".join(
                    f"`{l['source']}`" + (f" si `{' ET '.join(l['conditions'])}`" if l["conditions"] else "")
                    for l in lines) or "**non trouvé dans l'EAR**"
                corpus = e["rule"] + (f" ({e['detail']})" if e.get("detail") else "")
                md.append(f"| {e['field']} | {expr} | {corpus} de `{e['source']}` | {e['support']:.0%} |")
            md += ["", "Relire chaque ligne : l'expression BW fait foi, le corpus confirme qu'elle est bien "
                       "celle appliquée en production. Un écart signale une surcharge de déploiement, "
                       "une version d'EAR différente ou une branche jamais rencontrée.", ""]

    md += ["## Tests d'acceptation", "",
           f"`golden_{flow}.jsonl` contient {min(golden_max, len(P['pairs']))} paires réelles "
           "(entrée → sortie Kondor attendue). Toute nouvelle implémentation (ex. route Camel) doit les reproduire ; "
           "vérification avec l'outil `compare_outputs`.", ""]

    spec_path = SPECS / f"SPEC_{flow}.md"
    spec_path.write_text("\n".join(md), encoding="utf-8")
    gold_path = SPECS / f"golden_{flow}.jsonl"
    with open(gold_path, "w", encoding="utf-8") as g:
        for i, o, k in P["pairs"][:golden_max]:
            g.write(json.dumps({"key": k, "input": i, "expected": o}, ensure_ascii=False) + "\n")
    log.info("SPEC %s -> %s", flow, spec_path)
    return (f"Spec écrite : {spec_path}\nTests golden : {gold_path}\n"
            f"{len(res['fields'])} champs, {len(questions)} points à valider.")


@mcp.tool()
def compare_outputs(flow: str, candidate_subdir: str = "candidate") -> dict:
    """Compare les sorties d'une nouvelle implémentation (fichiers déposés dans <corpus>/<flux>/<candidate_subdir>)
    aux sorties BW réelles, champ par champ, via la clé de corrélation."""
    if not re.fullmatch(r"[\w\-]+", candidate_subdir):
        raise ValueError("Nom de dossier refusé")
    fl = _flow(flow)
    fmt = _fmt(fl, "output")
    cdir = CORPUS / flow / candidate_subdir
    if not cdir.exists():
        raise ValueError(f"Dossier absent : {cdir}")
    norm = fl["key"].get("normalize")
    cand = {}
    for p in sorted(cdir.glob("*")):
        if p.is_file():
            for r in _parse_file(p, fmt):
                cand.setdefault(_norm_key(r.get(fl["key"]["output"], ""), norm), r)
    P = _pairs(flow)
    gold = {k: v[0] for k, v in P["idx_out"].items()}
    common = [k for k in cand if k in gold]
    diffs, examples, identical = Counter(), defaultdict(list), 0
    fields = [c for c in dict.fromkeys(c for r in gold.values() for c in r) if c != "_file"]
    for k in common:
        ok = True
        for f in fields:
            a, b = gold[k].get(f, ""), cand[k].get(f, "")
            if a != b and not (_as_num(a) is not None and _as_num(b) is not None and _r(_as_num(a)) == _r(_as_num(b))):
                ok = False
                diffs[f] += 1
                if len(examples[f]) < 3:
                    examples[f].append(f"{_mask(flow, fl['key']['output'], k)} : attendu {_mask(flow, f, a)!r}, obtenu {_mask(flow, f, b)!r}")
        identical += ok
    return {
        "lignes_candidat": len(cand), "lignes_bw_comparees": len(common),
        "identiques": f"{identical}/{len(common)} ({identical / len(common):.1%})" if common else "0",
        "manquantes_dans_candidat": len([k for k in gold if k not in cand]),
        "en_trop_dans_candidat": len([k for k in cand if k not in gold]),
        "ecarts_par_champ": {f: {"lignes": c, "exemples": examples[f]} for f, c in diffs.most_common(20)},
    }


# ============================================================ EAR BW5 (groupe d'EAR)
XSL_NS = "http://www.w3.org/1999/XSL/Transform"
EAR_CACHE = SPECS / "ear_cache"
EAR_INDEX = SPECS / "ear_index.json"
NESTED = (".par", ".sar", ".aar", ".projlib", ".zip")
KEEP_EXT = (".process", ".xml", ".xsd", ".substvar", ".sharedjmscon", ".sharedjdbc", ".sharedjmsapp",
            ".sharedparse", ".sharedhttp", ".sharedrvtransport", ".wsdl", ".java", ".archive", ".dd")
CONFIG_KEYS = ("fileName", "destination", "destinationQueue", "destinationTopic", "subject", "processName",
               "statement", "ConnectionReference", "pollInterval", "connection", "SharedJmsConnection")
GV_XPATH = re.compile(r"\$_globalVariables/(?:[\w\-]+:)?GlobalVariables/((?:(?:[\w\-]+:)?[\w\-]+/?)+)")
GV_PCT = re.compile(r"%%([^%]+)%%")
REF = re.compile(r"\$([A-Za-z_][\w\-.]*)")


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def _nsof(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _walk_zip(zf: zipfile.ZipFile, prefix: str, sink: dict, depth: int = 0):
    """Parcourt un EAR et ses archives imbriquées (.par, .sar, .projlib…), garde les fichiers utiles."""
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        low = name.lower()
        if low.endswith(NESTED) and depth < 4:
            try:
                with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                    _walk_zip(inner, f"{prefix}{name}!/", sink, depth + 1)
            except zipfile.BadZipFile:
                pass
        elif low.endswith(KEEP_EXT) or posixpath.basename(low) == "tibco.xml":
            sink[f"{prefix}{name}"] = zf.read(info)


def _nvpairs(root) -> dict:
    """Paires NameValuePair (TIBCO.xml d'EAR, config exportée par AppManage, .substvar)."""
    out = {}
    for el in root.iter():
        if _local(el.tag) in ("NameValuePair", "NameValuePairPassword", "NameValuePairInteger",
                              "NameValuePairBoolean", "globalVariable"):
            name = value = None
            for c in el:
                ln = _local(c.tag)
                if ln == "name":
                    name = (c.text or "").strip()
                elif ln == "value":
                    value = (c.text or "").strip()
            if name and value is not None and "password" not in el.tag.lower():
                out[name] = value
    return out


def _resolve(text: str, gv: dict) -> str:
    if not text:
        return text
    text = GV_PCT.sub(lambda m: gv.get(m.group(1), m.group(0)), text)
    def xp(m):
        key = "/".join(_local(p) if ":" not in p else p.split(":", 1)[1] for p in m.group(1).strip("/").split("/"))
        return f"'{gv[key]}'" if key in gv else m.group(0)
    return GV_XPATH.sub(xp, text)


def _split_args(expr: str) -> list[str]:
    args, depth, cur, quote = [], 0, "", None
    for ch in expr:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def _glob_from_expr(expr: str) -> str:
    """Transforme une expression de nom de fichier BW (littéral, %%var%%, concat(...)) en motif glob."""
    e = (expr or "").strip()
    if not e:
        return ""
    m = re.fullmatch(r"(?:\w+:)?concat\((.*)\)", e, re.S)
    parts = _split_args(m.group(1)) if m else [e]
    out = ""
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "'\"":
            out += p[1:-1]
        elif re.fullmatch(r"[\w\-./\\*?%:]+", p) and not p.startswith("$") and "(" not in p:
            out += p  # littéral non quoté (config) éventuellement avec joker
        else:
            out += "*"
    return re.sub(r"\*+", "*", out.replace("\\", "/"))


def _file_match(a: str, b: str) -> bool:
    """Deux motifs de fichiers désignent-ils les mêmes fichiers ? (comparaison tolérante)"""
    if not a or not b:
        return False
    da, fa = posixpath.split(a)
    db, fb = posixpath.split(b)
    if da and db and "*" not in da and "*" not in db and "%" not in da + db and da.rstrip("/") != db.rstrip("/"):
        return False
    if fnmatch.fnmatch(fa, fb) or fnmatch.fnmatch(fb, fa):
        return True
    pa, pb = fa.split("*")[0], fb.split("*")[0]
    ea = fa.rsplit(".", 1)[-1] if "." in fa else ""
    eb = fb.rsplit(".", 1)[-1] if "." in fb else ""
    return (len(min(pa, pb, key=len)) >= 3 and (pa.startswith(pb) or pb.startswith(pa))
            and (not ea or not eb or "*" in ea + eb or ea.lower() == eb.lower()))


def _bindings(el, path=(), conds=(), out=None) -> list:
    """Aplatit un mapping XSLT BW : [(chemin cible, expression source, conditions)]."""
    out = [] if out is None else out
    for c in el:
        name, ns = _local(c.tag), _nsof(c.tag)
        if not name:
            continue
        if ns == XSL_NS:
            if name in ("value-of", "copy-of"):
                out.append(["/".join(path), c.get("select", ""), list(conds)])
            elif name == "if":
                _bindings(c, path, conds + (c.get("test", ""),), out)
            elif name == "choose":
                prev = []
                for w in c:
                    wn = _local(w.tag)
                    if wn == "when":
                        _bindings(w, path, conds + (w.get("test", ""),), out)
                        prev.append(w.get("test", ""))
                    elif wn == "otherwise":
                        _bindings(w, path, conds + ((f"sinon (pas {' / '.join(prev)})",) if prev else ()), out)
            elif name == "for-each":
                _bindings(c, path, conds + (f"pour chaque {c.get('select', '')}",), out)
            elif name == "attribute":
                _bindings(c, path + ("@" + c.get("name", ""),), conds, out)
            elif name in ("variable", "param"):
                out.append(["$" + c.get("name", ""), c.get("select", "(contenu)"), list(conds)])
                _bindings(c, path, conds, out)
            elif name == "text":
                out.append(["/".join(path), f"'{(c.text or '').strip()}'", list(conds)])
            else:
                _bindings(c, path, conds, out)
        else:
            if len(c) == 0 and (c.text or "").strip():
                out.append(["/".join(path + (name,)), f"'{c.text.strip()}'", list(conds)])
            _bindings(c, path + (name,), conds, out)
    return out


def _kind(typ: str) -> str:
    t = typ.lower()
    if "callprocess" in t:
        return "call"
    if "file" in t and "eventsource" in t:
        return "file_in"
    if "file" in t and ("write" in t or "create" in t or "copy" in t or "rename" in t):
        return "file_out"
    if "file" in t and "read" in t:
        return "file_read"
    if "jms" in t and ("send" in t or "publish" in t or "reply" in t):
        return "jms_out"
    if "jms" in t and ("eventsource" in t or "get" in t or "receive" in t or "wait" in t):
        return "jms_in"
    if ("tibrv" in t or ".rv" in t) and ("pub" in t or "send" in t or "reply" in t):
        return "rv_out"
    if ("tibrv" in t or ".rv" in t) and ("eventsource" in t or "subscri" in t):
        return "rv_in"
    if "jdbc" in t:
        return "jdbc"
    if "java" in t:
        return "java"
    if "http" in t and "eventsource" in t:
        return "http_in"
    if "timer" in t or "eventsource" in t and "timer" in t:
        return "timer"
    if "render" in t or "mapper" in t or "parse" in t or "xml" in t:
        return "transform"
    return "autre"


def _endpoint(kind: str, cfg: dict, binds: list, gv: dict) -> str:
    bind = {p.rsplit("/", 1)[-1]: s for p, s, _ in binds}
    if kind in ("file_in", "file_out", "file_read"):
        raw = bind.get("fileName") or cfg.get("fileName", "")
        return _glob_from_expr(_resolve(raw, gv))
    if kind in ("jms_in", "jms_out"):
        raw = bind.get("destinationQueue") or bind.get("destinationTopic") or bind.get("destination") \
            or cfg.get("destination") or cfg.get("destinationQueue") or cfg.get("destinationTopic", "")
        return _glob_from_expr(_resolve(raw, gv)) if raw.startswith(("concat", "'", '"')) else _resolve(raw, gv)
    if kind in ("rv_in", "rv_out"):
        return _resolve(bind.get("subject") or cfg.get("subject", ""), gv).strip("'\"")
    if kind == "call":
        return "/" + _resolve(cfg.get("processName", ""), gv).lstrip("/")
    if kind == "jdbc":
        return re.sub(r"\s+", " ", cfg.get("statement", ""))[:300]
    return ""


def _parse_process(xml_bytes: bytes, ear: str, path: str, gv: dict) -> dict | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    if _local(root.tag) != "ProcessDefinition":
        return None
    name = next((el.text for el in root if _local(el.tag) == "name"), None) or path.split("!/")[-1]
    acts, trans = [], []
    for el in root.iter():
        ln = _local(el.tag)
        if ln in ("starter", "activity"):
            typ = next((c.text or "" for c in el if _local(c.tag) == "type"), "")
            cfg_el = next((c for c in el if _local(c.tag) == "config"), None)
            cfg = {}
            if cfg_el is not None:
                for c in cfg_el.iter():
                    if _local(c.tag) in CONFIG_KEYS and (c.text or "").strip() and _local(c.tag) not in cfg:
                        cfg[_local(c.tag)] = c.text.strip()
                if "java" in typ.lower():
                    cfg["java"] = "code Java présent (voir ear_cache)"
            ib = next((c for c in el if _local(c.tag) == "inputBindings"), None)
            binds = _bindings(ib) if ib is not None else []
            kind = _kind(typ)
            acts.append({"name": el.get("name", ""), "starter": ln == "starter", "type": typ.rsplit(".", 1)[-1],
                         "kind": kind, "config": cfg, "bindings": binds,
                         "endpoint": _endpoint(kind, cfg, binds, gv)})
        elif ln == "transition":
            t = {_local(c.tag): (c.text or "").strip() for c in el}
            trans.append({"from": t.get("from", ""), "to": t.get("to", ""),
                          "type": t.get("conditionType", "always"), "xpath": t.get("xpath", "")})
    starter = next((a for a in acts if a["starter"]), None)
    return {"ear": ear, "path": path.split("!/")[-1], "name": "/" + name.lstrip("/"),
            "starter": starter["name"] if starter else None, "activities": acts, "transitions": trans}


def _deploy_overrides() -> tuple[dict, list]:
    """Surcharges de variables globales issues des configs de déploiement (AppManage -export)."""
    res, files = {}, []
    for d in CFG.get("ears", {}).get("deployment_configs", []):
        for p in Path(d).glob("**/*.xml"):
            try:
                root = ET.parse(p).getroot()
            except ET.ParseError:
                continue
            app = next((el.text for el in root if _local(el.tag) == "name"), "") or p.stem
            res[p.stem.lower()] = {"app": app, "vars": _nvpairs(root)}
            files.append(p.name)
    return res, files


@mcp.tool()
def index_ears() -> dict:
    """Indexe tout le groupe d'EAR (dossiers ears.dirs de flows.yaml) : extrait les process, résout les
    variables globales (défauts EAR + surcharges de déploiement) et relie les process entre EAR."""
    dirs = CFG.get("ears", {}).get("dirs", [])
    ears = sorted({p for d in dirs for p in Path(d).glob("**/*.ear")})
    if not ears:
        raise ValueError(f"Aucun .ear trouvé dans {dirs}")
    overrides, dep_files = _deploy_overrides()
    EAR_CACHE.mkdir(parents=True, exist_ok=True)
    processes, gvars, used_overrides, unresolved = [], {}, set(), Counter()
    for ear in ears:
        sink = {}
        with zipfile.ZipFile(ear) as zf:
            _walk_zip(zf, "", sink)
        stem = ear.stem
        gv = {}
        for k, data in sink.items():  # défauts : TIBCO.xml de l'EAR et .substvar
            if k.lower().endswith(("tibco.xml", ".substvar")):
                try:
                    gv.update(_nvpairs(ET.fromstring(data)))
                except ET.ParseError:
                    pass
        defaults = dict(gv)
        for key, ov in overrides.items():  # surcharges de déploiement rattachées à cet EAR
            if stem.lower() in key or stem.lower() in (ov["app"] or "").lower():
                gv.update(ov["vars"])
                used_overrides.add(key)
        gvars[stem] = {k: {"valeur": v, "source": "déploiement" if defaults.get(k) != v else "EAR"}
                       for k, v in sorted(gv.items())}
        for k, data in sink.items():
            dest = EAR_CACHE / stem / k.replace("!/", "__/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if k.lower().endswith(".process"):
                pr = _parse_process(data, stem, k, gv)
                if pr:
                    processes.append(pr)
                    for m in GV_PCT.finditer(data.decode("utf-8", "replace")):
                        if m.group(1) not in gv:
                            unresolved[m.group(1)] += 1

    # liens entre process (y compris entre EAR)
    by_name = defaultdict(list)
    for i, p in enumerate(processes):
        by_name[p["name"].lower()].append(i)
    edges = []
    for i, p in enumerate(processes):
        for a in p["activities"]:
            ep, k = a["endpoint"], a["kind"]
            if not ep:
                continue
            if k == "call":
                cands = by_name.get(ep.lower(), [])
                same = [j for j in cands if processes[j]["ear"] == p["ear"]]
                for j in (same or cands)[:1]:
                    edges.append({"from": i, "to": j, "via": "appel", "value": ep, "activity": a["name"]})
            elif k in ("jms_out", "rv_out", "file_out"):
                want = {"jms_out": "jms_in", "rv_out": "rv_in", "file_out": "file_in"}[k]
                for j, q in enumerate(processes):
                    for b in q["activities"]:
                        if b["kind"] != want or not b["endpoint"]:
                            continue
                        ok = (_file_match(ep, b["endpoint"]) if k == "file_out"
                              else fnmatch.fnmatch(ep, b["endpoint"].replace(">", "*")) or ep == b["endpoint"])
                        if ok:
                            edges.append({"from": i, "to": j, "via": k[:-4], "value": ep, "activity": a["name"]})
    index = {"built": dt.datetime.now().isoformat(timespec="seconds"), "ears": [e.name for e in ears],
             "processes": processes, "edges": edges, "globals": gvars,
             "unresolved_vars": dict(unresolved.most_common(50))}
    EAR_INDEX.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    _ear_cache.clear()
    kinds = Counter(a["kind"] for p in processes for a in p["activities"] if a["starter"])
    log.info("INDEX_EARS %d EAR, %d process, %d liens", len(ears), len(processes), len(edges))
    return {
        "ears": len(ears), "process": len(processes),
        "declencheurs": dict(kinds), "liens_entre_process": len(edges),
        "liens_entre_ear": sum(processes[e["from"]]["ear"] != processes[e["to"]]["ear"] for e in edges),
        "configs_deploiement_utilisees": sorted(used_overrides),
        "configs_deploiement_non_rattachees": sorted(set(overrides) - used_overrides),
        "variables_non_resolues": dict(unresolved.most_common(15)),
        "fichiers_extraits": str(EAR_CACHE),
    }


_ear_cache = {}


def _ear() -> dict:
    if "idx" not in _ear_cache:
        if not EAR_INDEX.exists():
            raise ValueError("Index absent : lancer d'abord index_ears.")
        _ear_cache["idx"] = json.loads(EAR_INDEX.read_text(encoding="utf-8"))
    return _ear_cache["idx"]


def _find_process(idx: dict, query: str) -> int:
    q = query.lower().strip()
    hits = [i for i, p in enumerate(idx["processes"])
            if q == p["name"].lower() or q == f"{p['ear']}:{p['name']}".lower()]
    if not hits:
        hits = [i for i, p in enumerate(idx["processes"]) if q in f"{p['ear']}:{p['name']}".lower()]
    if len(hits) != 1:
        names = [f"{idx['processes'][i]['ear']}:{idx['processes'][i]['name']}" for i in hits[:15]]
        raise ValueError(f"{len(hits)} process correspondent à {query!r}" + (f" : {names}" if names else ""))
    return hits[0]


def _order(p: dict) -> list[dict]:
    """Activités dans l'ordre d'exécution (parcours des transitions depuis le déclencheur)."""
    acts = {a["name"]: a for a in p["activities"]}
    nxt = defaultdict(list)
    for t in p["transitions"]:
        nxt[t["from"]].append(t["to"])
    seen, order, dq = set(), [], deque([p["starter"]] if p["starter"] else [])
    while dq:
        n = dq.popleft()
        if n in seen:
            continue
        seen.add(n)
        if n in acts:
            order.append(acts[n])
        dq.extend(nxt.get(n, []))
    return order + [a for a in p["activities"] if a["name"] not in seen]


def _step_summary(idx: dict, i: int) -> dict:
    p = idx["processes"][i]
    st = next((a for a in p["activities"] if a["starter"]), None)
    return {
        "ear": p["ear"], "process": p["name"],
        "declencheur": f"{st['type']} {st['endpoint']}".strip() if st else "(sous-process)",
        "sorties": [f"{a['kind']} {a['endpoint']}" for a in p["activities"]
                    if a["kind"] in ("file_out", "jms_out", "rv_out") and a["endpoint"]],
        "filtres": [f"{t['from']} → {t['to']} si {t['xpath']}" for t in p["transitions"]
                    if t["type"].lower() == "xpath" and t["xpath"]],
        "referentiels_jdbc": [f"{a['name']} : {a['endpoint']}" for a in p["activities"] if a["kind"] == "jdbc"],
        "code_java": [a["name"] for a in p["activities"] if a["kind"] == "java"],
    }


@mcp.tool()
def trace_flow(start: str) -> dict:
    """Reconstitue la chaîne complète d'un flux à travers les EAR, à partir d'un nom de process,
    d'un motif de fichier d'entrée (ex. TRADES_*.xml) ou d'une file JMS / sujet RV."""
    idx = _ear()
    s = start.strip()
    roots = []
    for i, p in enumerate(idx["processes"]):
        st = next((a for a in p["activities"] if a["starter"]), None)
        if st and st["endpoint"] and (
                _file_match(s, st["endpoint"]) if st["kind"] == "file_in"
                else s.lower() in st["endpoint"].lower()):
            roots.append(i)
    if not roots:
        roots = [_find_process(idx, s)]
    out_edges = defaultdict(list)
    for e in idx["edges"]:
        out_edges[e["from"]].append(e)
    steps, links, seen, dq = [], [], set(), deque(roots)
    while dq:
        i = dq.popleft()
        if i in seen:
            continue
        seen.add(i)
        steps.append(_step_summary(idx, i))
        for e in out_edges[i]:
            q = idx["processes"][e["to"]]
            links.append(f"{idx['processes'][i]['ear']}:{idx['processes'][i]['name']} --{e['via']} "
                         f"{e['value']}--> {q['ear']}:{q['name']}")
            dq.append(e["to"])
    return {"etapes": steps, "liens": links,
            "attention": "Chaîne limitée aux liens détectables (JMS, RV, fichiers, appels). "
                         "Vérifier les variables non résolues dans index_ears."}


@mcp.tool()
def process_detail(process: str, max_mappings: int = 150) -> dict:
    """Détail d'un process : activités dans l'ordre, conditions des transitions (filtres),
    et mappings XPath cible <- source de chaque activité."""
    idx = _ear()
    p = idx["processes"][_find_process(idx, process)]
    acts, mappings = [], []
    for a in _order(p):
        acts.append(f"{a['name']} [{a['type']}]" + (f" → {a['endpoint']}" if a["endpoint"] else ""))
        for tgt, src, conds in a["bindings"]:
            if len(mappings) < max_mappings:
                mappings.append(f"{a['name']} :: {tgt} ← {src}" + (f"   [si {' ET '.join(conds)}]" if conds else ""))
    return {
        "ear": p["ear"], "process": p["name"], "activites": acts,
        "transitions": [f"{t['from']} → {t['to']}" + (f" [{t['type']}: {t['xpath']}]" if t["type"] != "always" else "")
                        for t in p["transitions"]],
        "mappings": mappings,
        "fichier_source": str(EAR_CACHE / p["ear"]),
    }


@mcp.tool()
def search_ears(text: str, max_results: int = 30) -> list:
    """Recherche un texte (nom de champ, file JMS, table, variable…) dans tous les fichiers extraits des EAR."""
    if not EAR_CACHE.exists():
        raise ValueError("Lancer d'abord index_ears.")
    t = text.lower()
    res = []
    for p in EAR_CACHE.rglob("*"):
        if not p.is_file():
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if t in line.lower():
                res.append(f"{p.relative_to(EAR_CACHE)}:{n}: {line.strip()[:200]}")
                if len(res) >= max_results:
                    return res
    return res


@mcp.tool()
def global_variables(ear: str = "", filter: str = "") -> dict:
    """Variables globales résolues par EAR (valeur et origine : EAR par défaut ou surcharge de déploiement)."""
    idx = _ear()
    out = {}
    for e, vs in idx["globals"].items():
        if ear and ear.lower() not in e.lower():
            continue
        sel = {k: v for k, v in vs.items() if filter.lower() in k.lower()}
        if sel:
            out[e] = sel
    return {"variables": out, "non_resolues": idx.get("unresolved_vars", {})}


def _ear_mapping(flow_name: str) -> dict:
    """Retrouve dans les EAR l'écriture du fichier Kondor du flux et le mapping de chaque champ."""
    idx = _ear()
    fl = _flow(flow_name)
    target = fl["output"].get("pattern", "*")
    kcols = {c.lower(): c for c in (_col_names(_fmt(fl, "output")) or [])}
    writers = [(i, a) for i, p in enumerate(idx["processes"]) for a in p["activities"]
               if a["kind"] == "file_out" and _file_match(a["endpoint"], target)]
    if not writers:
        return {"trouve": False, "message": f"Aucune écriture de fichier {target} trouvée dans les EAR."}
    i, w = writers[0]
    p = idx["processes"][i]
    acts = {a["name"].replace(" ", "-"): a for a in p["activities"]}
    # remonte les activités référencées par l'écriture ($Render-Deals/…), sur 4 niveaux
    chain, todo = [], [w]
    while todo and len(chain) < 12:
        a = todo.pop(0)
        if a in chain:
            continue
        chain.append(a)
        for _, src, conds in a["bindings"]:
            for ref in REF.findall(src + " " + " ".join(conds)):
                ref = ref.split("/")[0]
                if ref in acts and acts[ref] not in chain:
                    todo.append(acts[ref])
    fields, other = {}, []
    for a in chain:
        for tgt, src, conds in a["bindings"]:
            leaf = tgt.rsplit("/", 1)[-1].lstrip("@").lower()
            line = {"activite": a["name"], "cible": tgt, "source": src, "conditions": conds}
            if leaf in kcols:
                fields.setdefault(kcols[leaf], []).append(line)
            else:
                other.append(line)
    upstream = [e for e in idx["edges"] if e["to"] == i]
    # filtres de toute la chaîne amont (un filtre peut être dans un autre EAR)
    chain_up, seen_up, dq = [], {i}, deque([i])
    while dq:
        j = dq.popleft()
        chain_up.append(j)
        for e in idx["edges"]:
            if e["to"] == j and e["from"] not in seen_up:
                seen_up.add(e["from"])
                dq.append(e["from"])
    filters = [f"{idx['processes'][j]['ear']}:{idx['processes'][j]['name']} : {f}"
               for j in chain_up for f in _step_summary(idx, j)["filtres"]]
    lookups = [f"{idx['processes'][e['to']]['name']} : {r}" for e in idx["edges"]
               if e["from"] == i and e["via"] == "appel" for r in _step_summary(idx, e["to"])["referentiels_jdbc"]]
    return {
        "trouve": True, "ear": p["ear"], "process": p["name"], "ecriture": f"{w['name']} → {w['endpoint']}",
        "autres_ecritures_candidates": [f"{idx['processes'][j]['ear']}:{idx['processes'][j]['name']}"
                                        for j, _ in writers[1:6]],
        "champs_kondor": fields,
        "champs_non_rattaches": len(kcols) and [c for c in kcols.values() if c not in fields],
        "autres_mappings": other[:60],
        "en_amont": [f"{idx['processes'][e['from']]['ear']}:{idx['processes'][e['from']]['name']} via {e['via']} {e['value']}"
                     for e in upstream],
        "filtres": filters,
        "referentiels_appeles": lookups,
    }


@mcp.tool()
def kondor_mapping_from_ear(flow: str) -> dict:
    """Mapping de chaque champ Kondor lu dans les EAR : trouve le process qui écrit le fichier Kondor du flux,
    remonte les activités de transformation (Render, Mapper…) et rattache les expressions aux colonnes Kondor."""
    res = _ear_mapping(flow)
    if not res.get("trouve"):
        return res
    fmt_line = lambda l: f"{l['source']}" + (f"  [si {' ET '.join(l['conditions'])}]" if l["conditions"] else "")
    res["champs_kondor"] = {k: [fmt_line(l) for l in v] for k, v in res["champs_kondor"].items()}
    res["autres_mappings"] = [f"{l['activite']} :: {l['cible']} ← {fmt_line(l)}" for l in res["autres_mappings"]]
    return res


if __name__ == "__main__":
    mcp.run()
