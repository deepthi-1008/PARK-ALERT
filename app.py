from flask import Flask, render_template, request, redirect, url_for, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.path.join("database", "no_parking.db")


def get_db_connection():
    """Establishes a connection to the SQLite database with dictionary rows."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/")
def index():
    """Main Dashboard Route: Displays statistics cards and live violation table."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Fetch total counts and metrics
    cursor.execute("SELECT COUNT(*) FROM violations")
    total_violations = cursor.fetchone()[0]

    cursor.execute("SELECT SUM(fine_amount) FROM violations WHERE status = 'UNPAID'")
    unpaid_sum = cursor.fetchone()[0] or 0.0

    cursor.execute("SELECT SUM(fine_amount) FROM violations WHERE status = 'PAID'")
    collected_sum = cursor.fetchone()[0] or 0.0

    # 2. Fetch all violation records joined with owner details
    cursor.execute("""
        SELECT 
            v.violation_id,
            v.plate_number,
            v.duration_sec,
            v.fine_amount,
            v.status,
            v.image_path,
            v.timestamp,
            u.owner_name,
            u.phone_number
        FROM violations v
        LEFT JOIN vehicles u ON v.plate_number = u.plate_number
        ORDER BY v.timestamp DESC
    """)
    violations = cursor.fetchall()
    conn.close()

    return render_template(
        "index.html",
        total_violations=total_violations,
        unpaid_sum=unpaid_sum,
        collected_sum=collected_sum,
        violations=violations
    )


@app.route("/challan/<int:violation_id>")
def view_challan(violation_id):
    """Printable e-Challan ticket view."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            v.violation_id,
            v.plate_number,
            v.duration_sec,
            v.fine_amount,
            v.status,
            v.image_path,
            v.timestamp,
            u.owner_name,
            u.phone_number,
            u.email
        FROM violations v
        LEFT JOIN vehicles u ON v.plate_number = u.plate_number
        WHERE v.violation_id = ?
    """, (violation_id,))
    
    violation = cursor.fetchone()
    conn.close()

    if not violation:
        return "Violation record not found", 444

    return render_template("challan.html", violation=violation)


@app.route("/mark_paid/<int:violation_id>", methods=["POST"])
def mark_paid(violation_id):
    """Updates violation ticket status from UNPAID to PAID."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("UPDATE violations SET status = 'PAID' WHERE violation_id = ?", (violation_id,))
    conn.commit()
    conn.close()

    return redirect(url_for("index"))


if __name__ == "__main__":
    print("[*] Starting Flask Web Admin Dashboard at http://127.0.0.1:5000")
    app.run(debug=True, port=5000)