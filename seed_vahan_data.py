import sqlite3
import os
import csv

DB_PATH = os.path.join("database", "no_parking.db")
DATASET_PATH = os.path.join("config", "vehicles.csv")


def load_sample_vehicles():
    """Loads vehicle records from the CSV dataset."""
    sample_vehicles = []

    with open(DATASET_PATH, "r", newline="", encoding="utf-8") as file:
        reader = csv.reader(file)

        # Skip header
        next(reader)

        for row in reader:
            sample_vehicles.append(tuple(row))

    return sample_vehicles


def seed_dummy_data():
    """Populates the vehicles table using the CSV dataset."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    sample_vehicles = load_sample_vehicles()

    cursor.executemany("""
        INSERT OR IGNORE INTO vehicles
        (plate_number, owner_name, phone_number, email)
        VALUES (?, ?, ?, ?);
    """, sample_vehicles)

    conn.commit()
    conn.close()

    print("[✓] Sample vehicle records seeded successfully.")


if __name__ == "__main__":
    seed_dummy_data()