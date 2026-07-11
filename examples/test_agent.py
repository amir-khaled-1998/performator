# Test de l'agent complet : Q&A tool-use avec qwen3:8b via Ollama
import sqlite3, os

# Mini base metier de test
if os.path.exists("test_metier.db"):
    os.remove("test_metier.db")
con = sqlite3.connect("test_metier.db")
con.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, nom TEXT, ville TEXT)")
con.executemany("INSERT INTO clients (nom, ville) VALUES (?, ?)",
                [("Dupont", "Paris"), ("Martin", "Lyon"), ("Bernard", "Paris")])
con.commit()
con.close()

# Mini fichier de logs
with open("test_app.log", "w", encoding="utf-8") as f:
    f.write("2026-07-05 10:00:01 INFO demarrage du service\n"
            "2026-07-05 10:02:13 ERROR timeout sur interestService\n"
            "2026-07-05 10:05:44 INFO reprise normale\n")

from code_agent import build_agent
agent = build_agent(code_root=r"D:\apps\performator",
                    db_path="test_metier.db",
                    log_path="test_app.log",
                    docs_db="test_rag.db",
                    model="qwen3:8b")
print(agent.ask("Combien de clients sont a Paris dans la base ? Reponds brievement."))
