-- =====================================================================
-- Project: PARK-ALERT (Smart Traffic & Parking Enforcement System)
-- File: database/schema.sql
-- Description: DDL Reference Schema for Local Custom Dummy Database
-- =====================================================================

-- Enforce Foreign Key Constraints in SQLite
PRAGMA foreign_keys = ON;

-- 1. Vehicle Registration Registry Table (Local Mock Registry)
CREATE TABLE IF NOT EXISTS vehicles (
    plate_number TEXT PRIMARY KEY,
    owner_name TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    email TEXT NOT NULL
);

-- 2. Parking Violations Record Table (Offense Tracking)
CREATE TABLE IF NOT EXISTS violations (
    violation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_number TEXT NOT NULL,
    duration_sec INTEGER NOT NULL,
    fine_amount REAL NOT NULL,
    status TEXT DEFAULT 'UNPAID',
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plate_number) REFERENCES vehicles (plate_number) ON DELETE CASCADE
);

-- 3. B-Tree Index on Plate Number for Fast ANPR Query Matching (< 1 ms)
CREATE INDEX IF NOT EXISTS idx_vehicles_plate ON vehicles (plate_number);
CREATE INDEX IF NOT EXISTS idx_violations_plate_status ON violations (plate_number, status);