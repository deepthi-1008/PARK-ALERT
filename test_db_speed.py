import sqlite3
import time
import os

DB_PATH = os.path.join("database", "no_parking.db")

def benchmark_database_queries(test_plate="KA35X2474"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Measure execution time for owner lookup and unpaid count query
    start_time = time.perf_counter()

    cursor.execute("SELECT owner_name, phone_number FROM vehicles WHERE plate_number = ?", (test_plate,))
    owner = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM violations WHERE plate_number = ? AND status = 'UNPAID'", (test_plate,))
    unpaid_count = cursor.fetchone()[0]

    end_time = time.perf_counter()
    conn.close()

    execution_time_ms = (end_time - start_time) * 1000

    print("=== Database Query Verification Results ===")
    print(f"[✓] Target Plate: {test_plate}")
    print(f"[✓] Owner Found: {owner[0] if owner else 'Guest Driver'} ({owner[1] if owner else 'N/A'})")
    print(f"[✓] Unpaid Violations: {unpaid_count}")
    print(f"[⚡] Total Query Execution Time: {execution_time_ms:.3f} milliseconds\n")

if __name__ == "__main__":
    benchmark_database_queries()