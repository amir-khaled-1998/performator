# Validation du RAG sur un document reel : article Wikipedia "Fonds de commerce"
import os
from local_rag import LocalRAG

if os.path.exists("demo_doc_rag.db"):
    os.remove("demo_doc_rag.db")

rag = LocalRAG(db_path="demo_doc_rag.db", embedding_dims=768, llm_model="qwen3:8b")

texte = open("docs_demo/fonds_de_commerce.txt", encoding="utf-8").read()
ids = rag.add_document(texte, metadata={"doc": "fonds_de_commerce.txt", "source": "wikipedia-fr"})
print(f"Document indexe : {len(ids)} chunks\n")

questions = [
    "Quels sont les elements incorporels d'un fonds de commerce ?",
    "Quelle est la difference entre le fonds de commerce et l'entreprise ?",
]

for q in questions:
    print(f"QUESTION : {q}")
    print("-- Recherche semantique (top 2) --")
    for h in rag.search(q, top_k=2):
        print(f"  score={h.score:.3f}  {h.text[:120]!r}")
    print("-- Reponse generee (qwen3:8b + contexte RAG) --")
    res = rag.ask(q, top_k=3)
    reponse = res["answer"] if isinstance(res, dict) else res
    print(reponse)
    print("=" * 70)

rag.close()
