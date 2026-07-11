# Suite de la validation : question 2 avec timeout Ollama elargi (CPU lent)
from local_rag import LocalRAG

rag = LocalRAG(db_path="demo_doc_rag.db", embedding_dims=768, llm_model="qwen3:8b")
rag.ollama.timeout = 600  # le defaut de 120 s est trop court en inference CPU

q = "Quelle est la difference entre le fonds de commerce et l'entreprise ?"
print(f"QUESTION : {q}\n")
res = rag.ask(q, top_k=3)
print(res["answer"])
print("\nSources :", [h.id for h in res["sources"]])
rag.close()
