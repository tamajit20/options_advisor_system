import sys, json
sys.path.insert(0, '/app')
from database.connection import SQLServerConnection

db = SQLServerConnection()
rows = db.fetch_all(
    "SELECT TOP 3 suggestion_id, underlying, status, confidence_score, conditions_json "
    "FROM options_suggestions ORDER BY generated_on DESC"
)
for r in rows:
    sid   = r["suggestion_id"]
    sym   = r["underlying"]
    st    = r["status"]
    score = r["confidence_score"]
    print(f"--- {sid} {sym} status={st} score={score}")
    cj = r["conditions_json"]
    if cj:
        checks = json.loads(cj) if isinstance(cj, str) else cj
        if isinstance(checks, list):
            for c in checks:
                print(f"  [{c.get('status','?'):10}] {c.get('label','?')}")
        else:
            print("  (old dict format:", list(checks.keys()), ")")
db.close()
