"""
local_rag.py — RAG 100% local, autonome, à persistance disque.

Philosophie de conception
--------------------------
- Stockage durable  : SQLite (stdlib Python) — métadonnées, CRUD, transactions, backup = 1 fichier.
- Vecteurs          : stockés en BLOB (numpy float32), recherche par produit scalaire (numpy).
- Embeddings + LLM  : Ollama, appelé via urllib (stdlib). Aucune donnée ne sort de la machine.
- Dépendance        : numpy uniquement. Aucun serveur, aucun Docker, aucune API cloud.

Adapté à un environnement souverain (pas d'egress). Pour puller numpy via miroir
PyPI interne : pip install --index-url https://<artifactory>/pypi/simple numpy

Montée en charge
----------------
La recherche est en force brute (dot product sur toute la matrice). C'est instantané
jusqu'à ~50-100k chunks. Au-delà, remplacer _search_vectors() par un index FAISS
(faiss.IndexFlatIP ou IndexHNSWFlat) en gardant SQLite comme source de vérité.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# --------------------------------------------------------------------------- #
# Client Ollama minimal (stdlib uniquement)
# --------------------------------------------------------------------------- #
class OllamaClient:
    def __init__(self, base_url: str = "http://localhost:11434", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama injoignable sur {self.base_url} : {e}") from e

    def embed(self, model: str, text: str) -> np.ndarray:
        out = self._post("/api/embeddings", {"model": model, "prompt": text})
        vec = np.asarray(out["embedding"], dtype=np.float32)
        return _normalize(vec)

    def generate(self, model: str, prompt: str, system: str | None = None) -> str:
        payload = {"model": model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        return self._post("/api/generate", payload).get("response", "")


# --------------------------------------------------------------------------- #
# Utilitaires vecteurs
# --------------------------------------------------------------------------- #
def _normalize(vec: np.ndarray) -> np.ndarray:
    """Normalise L2 -> la similarité cosinus devient un simple produit scalaire."""
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# --------------------------------------------------------------------------- #
# Résultat de recherche
# --------------------------------------------------------------------------- #
@dataclass
class Hit:
    id: int
    text: str
    score: float
    metadata: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# RAG
# --------------------------------------------------------------------------- #
class LocalRAG:
    def __init__(
        self,
        db_path: str = "rag.db",
        ollama_url: str = "http://localhost:11434",
        embed_model: str = "nomic-embed-text",
        llm_model: str = "llama3.2",
        embedding_dims: int = 768,  # nomic-embed-text = 768. À ALIGNER avec ton modèle !
    ):
        self.db_path = db_path
        self.embed_model = embed_model
        self.llm_model = llm_model
        self.embedding_dims = embedding_dims
        self.ollama = OllamaClient(ollama_url)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                text      TEXT NOT NULL,
                metadata  TEXT NOT NULL DEFAULT '{}',
                vector    BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()

    # ---- Écriture --------------------------------------------------------- #
    def add(self, text: str, metadata: dict | None = None) -> int:
        """Ajoute un chunk unique. Retourne son id."""
        vec = self.ollama.embed(self.embed_model, text)
        if vec.shape[0] != self.embedding_dims:
            raise ValueError(
                f"Dimension {vec.shape[0]} != embedding_dims={self.embedding_dims}. "
                "Aligne le paramètre sur ton modèle d'embedding."
            )
        cur = self._conn.execute(
            "INSERT INTO chunks (text, metadata, vector) VALUES (?, ?, ?)",
            (text, json.dumps(metadata or {}), _to_blob(vec)),
        )
        self._conn.commit()
        return cur.lastrowid

    def add_document(
        self, text: str, metadata: dict | None = None,
        chunk_size: int = 800, overlap: int = 100,
    ) -> list[int]:
        """Découpe un document en chunks (par caractères, avec recouvrement) puis indexe."""
        chunks = _chunk_text(text, chunk_size, overlap)
        return [self.add(c, metadata) for c in chunks]

    def update(self, chunk_id: int, text: str | None = None,
               metadata: dict | None = None) -> None:
        row = self._conn.execute(
            "SELECT text, metadata FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Chunk {chunk_id} introuvable")
        new_text = text if text is not None else row["text"]
        new_meta = metadata if metadata is not None else json.loads(row["metadata"])
        vec = self.ollama.embed(self.embed_model, new_text)
        self._conn.execute(
            "UPDATE chunks SET text = ?, metadata = ?, vector = ? WHERE id = ?",
            (new_text, json.dumps(new_meta), _to_blob(vec), chunk_id),
        )
        self._conn.commit()

    def delete(self, chunk_id: int) -> None:
        self._conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
        self._conn.commit()

    # ---- Recherche -------------------------------------------------------- #
    def search(self, query: str, top_k: int = 5,
               where: dict | None = None) -> list[Hit]:
        """
        Recherche sémantique top-k.
        `where` : filtre d'égalité sur les métadonnées, ex. {"source": "RH"}.
        """
        qvec = self.ollama.embed(self.embed_model, query)
        return self._search_vectors(qvec, top_k, where)

    def _search_vectors(self, qvec: np.ndarray, top_k: int,
                        where: dict | None) -> list[Hit]:
        sql = "SELECT id, text, metadata, vector FROM chunks"
        params: list[Any] = []
        if where:
            clauses = []
            for k, v in where.items():
                clauses.append("json_extract(metadata, '$.' || ?) = ?")
                params.extend([k, v])
            sql += " WHERE " + " AND ".join(clauses)

        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return []

        matrix = np.vstack([_from_blob(r["vector"]) for r in rows])  # (N, d), déjà normalisé
        scores = matrix @ qvec                                       # cosinus = dot product
        top_idx = np.argsort(-scores)[:top_k]

        return [
            Hit(
                id=rows[i]["id"],
                text=rows[i]["text"],
                score=float(scores[i]),
                metadata=json.loads(rows[i]["metadata"]),
            )
            for i in top_idx
        ]

    # ---- RAG complet (retrieval + génération) ----------------------------- #
    def ask(self, question: str, top_k: int = 5, where: dict | None = None) -> dict:
        hits = self.search(question, top_k, where)
        context = "\n\n".join(f"[{h.id}] {h.text}" for h in hits)
        system = (
            "Tu réponds uniquement à partir du CONTEXTE fourni. "
            "Si l'information n'y figure pas, dis-le clairement. "
            "Cite les passages utilisés par leur numéro entre crochets."
        )
        prompt = f"CONTEXTE :\n{context}\n\nQUESTION : {question}\n\nRÉPONSE :"
        answer = self.ollama.generate(self.llm_model, prompt, system=system)
        return {"answer": answer, "sources": hits}

    # ---- Divers ----------------------------------------------------------- #
    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]

    def close(self) -> None:
        self._conn.close()


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    if chunk_size <= overlap:
        raise ValueError("chunk_size doit être > overlap")
    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return [c for c in chunks if c.strip()]


# --------------------------------------------------------------------------- #
# Exemple d'utilisation
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    rag = LocalRAG(
        db_path="rag.db",
        embed_model="nomic-embed-text",
        llm_model="llama3.2",
        embedding_dims=768,
    )

    # Indexation
    rag.add("La procédure de congé se demande 30 jours à l'avance.",
            metadata={"source": "RH", "doc": "conges.pdf"})
    rag.add("Le télétravail est autorisé jusqu'à 3 jours par semaine.",
            metadata={"source": "RH", "doc": "teletravail.pdf"})
    rag.add_document(open("un_document.txt").read(), metadata={"source": "technique"}) \
        if False else None  # exemple : indexer un fichier entier

    # Recherche filtrée
    for hit in rag.search("combien de jours de télétravail ?", top_k=3, where={"source": "RH"}):
        print(f"  [{hit.score:.3f}] {hit.text}")

    # RAG complet
    res = rag.ask("Quelles sont les règles de télétravail ?")
    print("\nRéponse :", res["answer"])

    rag.close()
