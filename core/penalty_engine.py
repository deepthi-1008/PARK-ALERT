import sqlite3
import os

DB_PATH = os.path.join("database", "no_parking.db")

class PenaltyEngine:
    def __init__(self, base_fine=500.0, per_min_rate=100.0, grace_period_sec=30):
        self.base_fine = base_fine
        self.per_min_rate = per_min_rate
        self.grace_period_sec = grace_period_sec

    def check_repeat_offender(self, plate_number: str) -> bool:
        """Queries database to check if the vehicle has past UNPAID violations."""
        if not os.path.exists(DB_PATH):
            return False

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT COUNT(*) FROM violations WHERE plate_number = ? AND status = 'UNPAID'",
            (plate_number,)
        )
        unpaid_count = cursor.fetchone()[0]
        conn.close()

        return unpaid_count > 0

    def calculate_fine(self, duration_sec: int, plate_number: str) -> dict:
        """
        Calculates total fine:
        - Free during grace period (<= 30s)
        - Base Fine (₹500) + Overstay Rate (+₹100/min)
        - 2x Multiplier for Repeat Unpaid Offenders
        """
        if duration_sec <= self.grace_period_sec:
            return {
                "duration_sec": duration_sec,
                "overstay_min": 0,
                "base_fine": 0.0,
                "overstay_fine": 0.0,
                "is_repeat_offender": False,
                "multiplier": 1.0,
                "total_fine": 0.0
            }

        # Calculate excess time beyond grace period (in minutes, rounded up)
        overstay_sec = duration_sec - self.grace_period_sec
        overstay_min = (overstay_sec + 59) // 60  # Ceil division

        # Incremental Overstay Calculation
        overstay_fine = overstay_min * self.per_min_rate
        subtotal = self.base_fine + overstay_fine

        # Repeat Offender Multiplier Assessment
        is_repeat = self.check_repeat_offender(plate_number)
        multiplier = 2.0 if is_repeat else 1.0
        final_fine = subtotal * multiplier

        return {
            "duration_sec": duration_sec,
            "overstay_min": overstay_min,
            "base_fine": self.base_fine,
            "overstay_fine": overstay_fine,
            "is_repeat_offender": is_repeat,
            "multiplier": multiplier,
            "total_fine": final_fine
        }