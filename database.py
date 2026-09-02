import sqlite3
import os

# Define relative path for database storage
DB_DIR = "database"
DB_PATH = os.path.join(DB_DIR, "no_parking.db")

# Create database folder if it doesn't exist
os.makedirs(DB_DIR, exist_ok=True)


def initialize_database():
    """Establishes database connection and creates required tables."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Enable foreign key enforcement in SQLite
    cursor.execute("PRAGMA foreign_keys = ON;")

    # Table 1: vehicles (Owner Info Lookup)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vehicles (
            plate_number TEXT PRIMARY KEY,
            owner_name TEXT NOT NULL,
            phone_number TEXT NOT NULL,
            email TEXT
        );
    """)

    # Table 2: violations (E-Challan History)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS violations (
            violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_number TEXT NOT NULL,
            duration_sec INTEGER NOT NULL,
            fine_amount REAL NOT NULL,
            status TEXT DEFAULT 'UNPAID',
            image_path TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (plate_number) REFERENCES vehicles (plate_number) ON DELETE CASCADE
        );
    """)

    conn.commit()
    conn.close()
    print(f"[✓] Database schema initialized successfully at: {DB_PATH}")


def seed_dummy_data():
    """Populates the vehicles table with initial testing records."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # NOTE: Replace '+919876543210' with YOUR personal mobile number
    # so you receive real SMS alerts on your phone during testing!
    sample_vehicles = [
        ("KA35EA8213", "Rajesh Kumar", "+919632646431", "rajesh@gmail.com"),
        ("KA35E00152", "Priya Sharma", "+916361178587", "priya@gmail.com"),
        ("KA35EZ7659", "Anil Patel", "+918147285105", "anil@gmail.com"),
        ("KA35EK6133", "Sunita Deshmukh", "+919886908384", "sunita@gmail.com"),
        ("KA35X2474", "Rohan Verma", "+917676898449", "rohan@gmail.com")
    ]

    # 'INSERT OR IGNORE' avoids crashing if you run this script multiple times
    cursor.executemany("""
        INSERT OR IGNORE INTO vehicles (plate_number, owner_name, phone_number, email)
        VALUES (?, ?, ?, ?);
    """, sample_vehicles)

    conn.commit()
    conn.close()
    print("[✓] Sample vehicle registration records seeded.")


def verify_database():
    """Queries and displays all stored records in the terminal."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM vehicles;")
    records = cursor.fetchall()

    print("\n--- Current Registered Vehicles in Local VAHAN Database ---")
    for row in records:
        print(f"Plate: {row[0]} | Owner: {row[1]} | Phone: {row[2]} | Email: {row[3]}")

    conn.close()


if __name__ == "__main__":
    initialize_database()
    seed_dummy_data()
    verify_database()