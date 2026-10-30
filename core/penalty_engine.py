"""
Fine calculation based on vehicle type and how long it dwelled in the
No-Parking zone. Rate is per hour, prorated for partial durations.
e.g. Bus parked 3 hrs -> 150 * 3 = 450
"""

HOURLY_RATES = {
    "Scooter/Bike": 50,     # two-wheeler
    "Autorickshaw": 60,
    "Car": 100,
    "Van": 120,
    "Bus": 150,
    "Truck": 150,
}

DEFAULT_RATE = 50  # fallback if vehicle_type isn't in the table above


def calculate_fine(vehicle_type, dwell_seconds):
    rate_per_hour = HOURLY_RATES.get(vehicle_type, DEFAULT_RATE)
    hours = dwell_seconds / 3600.0
    fine = round(rate_per_hour * hours, 2)
    return fine