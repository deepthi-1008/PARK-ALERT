import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "database", "no_parking.db")

def initialize_database():
    """Initializes the database directory, creates tables, and populates initial vehicle records."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    cursor = conn.cursor()

    # Clear old table data so the updated rows are freshly written
    cursor.execute("DROP TABLE IF EXISTS vehicles;")

    # 1. Create registered vehicles table
    cursor.execute("""
    CREATE TABLE vehicles (
        vehicle_id INTEGER PRIMARY KEY AUTOINCREMENT,
        plate_number TEXT UNIQUE NOT NULL,
        owner_name TEXT NOT NULL,
        phone_number TEXT,
        email TEXT NOT NULL
    );
    """)

    # 2. Create violations log table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS violations (
        violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id INTEGER,
        plate_number TEXT,
        vehicle_type TEXT,
        duration_sec INTEGER,
        fine_amount REAL,
        status TEXT DEFAULT 'UNPAID',
        sms_status TEXT DEFAULT 'pending',
        evidence_path TEXT,
        timestamp TEXT
    );
    """)

    # 3. Your registered vehicle dataset
    dataset = [
        ("KA35X2474", "Deepthi H.A.", "+919632646431", "dhanushree.d1004@gmail.com"),
        ("KA31EE8156", "Rajesh Kumar", "+918147285105", "pattedapeksham@gmail.com"),
        ("KA35E00152", "Priya Sharma", "+916361178587", "kcprajwal40@gmail.com"),
        ("KA29EJ9484", "Anil Patel", "+918147285105", "kcprajwal007@gmail.com"),
        ("KA29ES1863", "Sunita Deshmukh", "+916361178587", "hadeepthibec@gmail.com"),
        ("KA37EC2109", "rahul", "+919964658870", "rahulsavanur1234@gmail.com"),
        ("KA29EL0375", "Rohan Verma", "+917676898449", "rohan@gmail.com"),
        ("KA29ER9505", "Deepthi", "+919632646431", "hadeepthibec@gmail.com"),
        ("KA28W 5310", "apeksha ", "+918147285105", "pattedapeksham@gmail.com"),
        ("KA71M0929","Deepu","+919632646431","hadeepthibec@gmail.com")

    ]

    # Fresh insert of clean records
    cursor.executemany("""
    INSERT INTO vehicles (plate_number, owner_name, phone_number, email)
    VALUES (?, ?, ?, ?);
    """, dataset)

    conn.commit()

    print("\n--- Verified Vehicles in DB ---")
    cursor.execute("SELECT vehicle_id, plate_number, owner_name, email FROM vehicles;")
    for row in cursor.fetchall():
        print(row)

    conn.close()
    print("\n[✓] Database refreshed and clean records populated successfully!")

if __name__ == "__main__":
    initialize_database()