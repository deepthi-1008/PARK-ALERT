import sqlite3
import os
import sys

# Append project root directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.penalty_engine import PenaltyEngine, DB_PATH

def setup_test_db():
    """Seeds a temporary record into database to test repeat offender logic."""
    os.makedirs("database", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            plate_number TEXT PRIMARY KEY, owner_name TEXT, phone_number TEXT, email TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS violations (
            violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_number TEXT, duration_sec INTEGER, fine_amount REAL, status TEXT DEFAULT 'UNPAID'
        );
    """)
    
    # Register test plates
    cursor.execute("INSERT OR IGNORE INTO vehicles VALUES ('KA35CLEAN0', 'Clean Driver', '+910000000000', 'clean@test.com');")
    cursor.execute("INSERT OR IGNORE INTO vehicles VALUES ('KA35REPEAT', 'Repeat Offender', '+911111111111', 'repeat@test.com');")
    
    # Seed an unpaid violation for KA35REPEAT
    cursor.execute("INSERT OR IGNORE INTO violations (violation_id, plate_number, duration_sec, fine_amount, status) VALUES (999, 'KA35REPEAT', 45, 600.0, 'UNPAID');")
    
    conn.commit()
    conn.close()

def run_unit_tests():
    setup_test_db()
    engine = PenaltyEngine(base_fine=500.0, per_min_rate=100.0, grace_period_sec=30)
    
    test_cases = [
        {"desc": "Grace Period Test", "plate": "KA35CLEAN0", "duration": 25, "expected_fine": 0.0},
        {"desc": "Base Violation Test", "plate": "KA35CLEAN0", "duration": 35, "expected_fine": 600.0},  # Base ₹500 + 1 min (₹100)
        {"desc": "Extended Overstay Test", "plate": "KA35CLEAN0", "duration": 150, "expected_fine": 700.0}, # Base ₹500 + 2 mins (₹200)
        {"desc": "Repeat Offender Multiplier Test", "plate": "KA35REPEAT", "duration": 35, "expected_fine": 1200.0} # (₹500 + ₹100) * 2x
    ]

    print("\n==================================================")
    print("      RUNNING ENGINE UNIT TEST SUITE             ")
    print("==================================================")
    
    passed_tests = 0
    for idx, test in enumerate(test_cases, 1):
        result = engine.calculate_fine(test["duration"], test["plate"])
        actual_fine = result["total_fine"]
        status = "PASSED [✓]" if actual_fine == test["expected_fine"] else "FAILED [✗]"
        
        if actual_fine == test["expected_fine"]:
            passed_tests += 1
            
        print(f"Test #{idx}: {test['desc']}")
        print(f"  └─ Plate: {test['plate']} | Overstay: {test['duration']}s")
        print(f"  └─ Repeat Offender: {result['is_repeat_offender']} (Multiplier: {result['multiplier']}x)")
        print(f"  └─ Expected: ₹{test['expected_fine']} | Actual: ₹{actual_fine} ---> {status}\n")

    print(f"Test Results: {passed_tests}/{len(test_cases)} Passed.")
    print("==================================================\n")

if __name__ == "__main__":
    run_unit_tests()