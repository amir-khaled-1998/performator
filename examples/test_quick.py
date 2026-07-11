# Test rapide de Performator : RAG + outils code (sans LLM)
from local_rag import LocalRAG
from code_agent import CodeTools

print("=== 1. RAG semantique (nomic-embed-text via Ollama) ===")
rag = LocalRAG(db_path="test_rag.db", embedding_dims=768)
rag.add_document(open("README.md", encoding="utf-8").read(), metadata={"doc": "README.md"})
print(f"Chunks indexes : {rag.count()}")
for r in rag.search("comment livrer une modification de code en toute securite ?", top_k=2):
    print(f"  score={r.score:.3f}  {r.text[:100]!r}")
rag.close()

print("\n=== 2. Outils code (ripgrep + ctags) ===")
ct = CodeTools(r"D:\apps\performator")
print(ct.build_index())
print("-- find_symbol('build_agent') --")
print(ct.find_symbol("build_agent"))
print("-- grep_code('def ask') --")
print(ct.grep_code("def ask"))
