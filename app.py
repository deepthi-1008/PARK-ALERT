import os
import re
import sys
import cv2
import time
import hmac
import hashlib
import secrets
import html as html_lib
import logging
import sqlite3
import threading
import subprocess
import atexit
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify, abort
from dotenv import load_dotenv

from core.tracker import ZoneTracker
from core.plate_reader import PlateReader, CONFUSION_PAIRS
from services.email_service import (
    send_warning_email,
    send_penalty_notice_email,
    build_upi_uri,
    _make_upi_qr_png,
    UPI_VPA,
)

try:
    import razorpay
except ImportError:  # app still starts; payment routes report the missing package
    razorpay = None

load_dotenv()

# --------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------- #
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("park_alert.app")

# --------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database", "no_parking.db")
EVIDENCE_DIR = os.path.join(BASE_DIR, "static", "evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

# Dwell time at which ANPR runs and the warning email is dispatched (unchanged).
DWELL_OCR_TRIGGER_SECONDS = 20.0

# Dwell time at which a violation row appears in the UI table (unchanged).
VIOLATION_THRESHOLD_SECONDS = 30

# Incremental fine: 500 at 30-39s, +200 per further 10-second block.
BASE_FINE = 500.0
FINE_STEP_PER_10S = 200.0

AUTO_EMAIL_ON_LOW_CONFIDENCE = True

# Razorpay test-mode credentials. Read from .env; the placeholders below are
# only defaults so the app starts. Real keys never belong in source code.
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_YourKeyHere")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "YourKeySecretHere")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
# Test-only switch for /pay_fine. Keep it off in production.
PAYMENT_SIMULATION = os.getenv("PAYMENT_SIMULATION", "0") == "1"

# Public access through ngrok (optional). With USE_NGROK=1 the server opens an
# HTTPS tunnel at startup and sets PUBLIC_BASE_URL to the tunnel address.
USE_NGROK = os.getenv("USE_NGROK", "0") == "1"
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "")
NGROK_DOMAIN = os.getenv("NGROK_DOMAIN", "")  # optional reserved domain from the ngrok dashboard
# Path to ngrok.exe / ngrok. Defaults to ngrok.exe next to app.py, then to PATH.
NGROK_PATH = os.getenv("NGROK_PATH", "") or (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ngrok.exe")
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ngrok.exe"))
    else "ngrok"
)
NGROK_WATCHDOG_SECONDS = int(os.getenv("NGROK_WATCHDOG_SECONDS", "20"))

# Signs per-challan payment links. Without it, a random secret is used and
# links from earlier emails stop working after a restart.
PAY_LINK_SECRET = os.getenv("PAY_LINK_SECRET", "") or secrets.token_hex(32)
if not os.getenv("PAY_LINK_SECRET"):
    logger.warning("PAY_LINK_SECRET is not set in .env. Payment links from earlier emails will stop working after this restart.")

# Simulation only. When True, requests that arrive through the public tunnel
# may skip the payment token too. Leave it False on any public URL: otherwise
# anyone on the internet can mark any challan PAID.
SIMULATION_ALLOW_UNSIGNED_PUBLIC = os.getenv("SIMULATION_ALLOW_UNSIGNED_PUBLIC", "0") == "1"

_rzp_client = None


def calculate_fine(duration_sec) -> float:
    """fine_amount = 500.0 + (max(0, (duration_sec - 30) // 10) * 200.0)"""
    buckets = max(0, (int(duration_sec) - VIOLATION_THRESHOLD_SECONDS) // 10)
    return BASE_FINE + buckets * FINE_STEP_PER_10S


def _fine_to_paise(fine_amount) -> int:
    return int(round(float(fine_amount) * 100))


# --------------------------------------------------------------------- #
# ANPR pipeline -- runs OFF the video thread.
# --------------------------------------------------------------------- #
plate_reader = PlateReader(use_gpu=False)
anpr_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ANPR")

# One dwell episode per (track_id, start_frame). Guarded by _episodes_lock.
_episodes = {}
_episodes_lock = threading.Lock()
# Episodes whose annotated snapshot should be refreshed from the next frame.
_snapshot_requests = set()
# Bumped on every upload; stale background work checks it and stops.
_session_generation = 0


def _ensure_violations_schema():
    """Bring an older on-disk violations table up to the current columns."""
    required_columns = {
        "track_id": "INTEGER",
        "plate_number": "TEXT",
        "vehicle_type": "TEXT",
        "duration_sec": "INTEGER",
        "fine_amount": "REAL",
        "status": "TEXT DEFAULT 'UNPAID'",
        "sms_status": "TEXT DEFAULT 'pending'",
        "evidence_path": "TEXT",
        "timestamp": "TEXT",
        "penalty_notice_sent": "INTEGER DEFAULT 0",
        "payment_link_id": "TEXT",
        "razorpay_order_id": "TEXT",
        "razorpay_order_amount": "INTEGER",
        "razorpay_payment_id": "TEXT",
        "paid_at": "TEXT",
    }
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS violations (
                       violation_id INTEGER PRIMARY KEY AUTOINCREMENT
                   )"""
            )
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(violations)")}
            for col, col_type in required_columns.items():
                if col not in existing_cols:
                    logger.info("Migrating violations table: adding missing column '%s'.", col)
                    conn.execute(f"ALTER TABLE violations ADD COLUMN {col} {col_type}")
            conn.commit()
    except Exception:
        logger.exception("Failed to verify/upgrade the violations table schema.")


_ensure_violations_schema()


_PLATE_STRUCT_RE = re.compile(r"^([A-Z]{2})([0-9]{1,2})([A-Z]{1,2})([0-9]{4})$")


def _decompose_plate(plate: str):
    m = _PLATE_STRUCT_RE.match(plate)
    if not m:
        return None
    state, district, series, number = m.groups()
    return {"state": state, "district": district, "series": series, "number": number, "full": plate}


def _fetch_all_vehicles():
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM vehicles").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            clean = re.sub(r"[^A-Z0-9]", "", str(d.get("plate_number", "")).upper())
            d["_clean_plate"] = clean
            d["_struct"] = _decompose_plate(clean)
            out.append(d)
        return out


def _is_known_confusion(a: str, b: str) -> bool:
    return a == b or frozenset((a, b)) in CONFUSION_PAIRS


def _state_series(plate: str):
    parts = _decompose_plate(plate)
    if not parts:
        return None, None
    return parts["state"], parts["series"]


def _damerau_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == lb:
        diffs = [i for i in range(la) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        if len(diffs) == 2 and diffs[1] == diffs[0] + 1:
            i = diffs[0]
            if a[i] == b[i + 1] and a[i + 1] == b[i]:
                return True
        return False
    if abs(la - lb) == 1:
        longer, shorter = (a, b) if la > lb else (b, a)
        return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))
    return False


def _level1_exact(candidates: list, vehicles: list):
    plate_index = {v["_clean_plate"]: v for v in vehicles}
    for c in candidates:
        if c in plate_index:
            return plate_index[c], f"exact match on '{c}'"
    return None, None


def _level2_full_plate_fuzzy(candidates: list, vehicles: list):
    hits, reasons = {}, {}
    for c in candidates:
        c_state, c_series = _state_series(c)
        for v in vehicles:
            plate = v["_clean_plate"]
            if len(plate) == len(c):
                diffs = [(x, y) for x, y in zip(c, plate) if x != y]
                if len(diffs) == 1 and _is_known_confusion(*diffs[0]):
                    hits[v["vehicle_id"]] = v
                    reasons.setdefault(v["vehicle_id"], f"glyph-confusion vs candidate '{c}'")
                    continue
            if c_state is not None:
                v_state, v_series = _state_series(plate)
                if c_state == v_state and c_series == v_series and _damerau_le1(c, plate):
                    hits[v["vehicle_id"]] = v
                    reasons.setdefault(v["vehicle_id"], f"state+series-locked Damerau<=1 vs candidate '{c}'")
    if len(hits) == 1:
        vid = next(iter(hits))
        return hits[vid], reasons[vid]
    if len(hits) > 1:
        return "AMBIGUOUS", [v["_clean_plate"] for v in hits.values()]
    return None, None


def _digit_permutation_match(token: str, number: str, max_position_diffs: int = 2) -> bool:
    if len(token) != len(number) or not token.isdigit():
        return False
    if sorted(token) != sorted(number):
        return False
    diffs = sum(1 for a, b in zip(token, number) if a != b)
    return 0 < diffs <= max_position_diffs


def _level3_numeric_high(candidates: list, vehicles: list):
    hits, reasons = {}, {}
    for c in candidates:
        token = re.sub(r"[^0-9]", "", c)
        if len(token) < 3:
            continue
        for v in vehicles:
            struct = v["_struct"]
            if not struct:
                continue
            number = struct["number"]
            if len(token) != len(number):
                continue
            if token == number or _digit_permutation_match(token, number):
                hits[v["vehicle_id"]] = v
                reasons.setdefault(v["vehicle_id"], f"full numeric match/reorder: token '{token}' vs plate number {number}")
    if len(hits) == 1:
        vid = next(iter(hits))
        return hits[vid], reasons[vid]
    if len(hits) > 1:
        return "AMBIGUOUS", [v["_clean_plate"] for v in hits.values()]
    return None, None


def _level3_scrambled_compound(candidates: list, vehicles: list):
    hits, reasons = {}, {}
    for c in candidates:
        token = re.sub(r"[^A-Z0-9]", "", c.upper())
        if len(token) < 8:
            continue
        token_letters = Counter(ch for ch in token if ch.isalpha())
        for v in vehicles:
            struct = v["_struct"]
            if not struct:
                continue
            number, district = struct["number"], struct["district"]
            needed_letters = Counter(struct["state"] + struct["series"])
            if any(token_letters[ch] < n for ch, n in needed_letters.items()):
                continue
            idx = token.find(number)
            if idx < 0:
                continue
            remainder = token[:idx] + token[idx + len(number):]
            if district not in remainder:
                continue
            hits[v["vehicle_id"]] = v
            reasons.setdefault(
                v["vehicle_id"],
                f"scrambled compound: token '{token}' contains state/series {struct['state']}{struct['series']}, "
                f"district {district} and number {number}",
            )
    if len(hits) == 1:
        vid = next(iter(hits))
        return hits[vid], reasons[vid]
    if len(hits) > 1:
        return "AMBIGUOUS", [v["_clean_plate"] for v in hits.values()]
    return None, None


def _level3_numeric_low(candidates: list, vehicles: list):
    hits, reasons = {}, {}
    for c in candidates:
        token = re.sub(r"[^0-9]", "", c)
        if len(token) < 3:
            continue
        for v in vehicles:
            struct = v["_struct"]
            if not struct:
                continue
            number, district = struct["number"], struct["district"]

            if len(token) < len(number) and (number.endswith(token) or number.startswith(token)):
                hits[v["vehicle_id"]] = v
                reasons.setdefault(v["vehicle_id"], f"numeric fragment '{token}' vs plate number {number}")
                continue

            if (
                len(token) == len(number)
                and token.isdigit()
                and token != number
                and sorted(token) == sorted(number)
            ):
                hits[v["vehicle_id"]] = v
                reasons.setdefault(v["vehicle_id"], f"scrambled digit permutation '{token}' vs {number}")
                continue

            if district and token.startswith(district) and len(token) >= len(district) + 2:
                remainder = token[len(district):]
                if remainder in number:
                    hits[v["vehicle_id"]] = v
                    reasons.setdefault(v["vehicle_id"], f"district+number compound '{token}' vs {district}/{number}")
    if len(hits) == 1:
        vid = next(iter(hits))
        return hits[vid], reasons[vid]
    if len(hits) > 1:
        return "AMBIGUOUS", [v["_clean_plate"] for v in hits.values()]
    return None, None


def _lookup_vehicle(candidates: list, track_id=None):
    if not candidates:
        return None, "high"
    try:
        vehicles = _fetch_all_vehicles()
    except Exception:
        logger.exception("DB read failed during plate lookup.")
        return None, "high"

    for level_fn, level_name in (
        (_level1_exact, "level1-exact"),
        (_level2_full_plate_fuzzy, "level2-fuzzy"),
        (_level3_numeric_high, "level3-numeric-high"),
        (_level3_scrambled_compound, "level3-scrambled-compound"),
    ):
        match, reason = level_fn(candidates, vehicles)
        if match == "AMBIGUOUS":
            logger.warning("[ANPR] Track %s: ambiguous %s match (%s) -- skipping.", track_id, level_name, reason)
            return None, "high"
        if match is not None:
            logger.info(
                "[ANPR] Track %s: %s -> vehicle_id=%s plate=%s owner=%s (%s)",
                track_id, level_name, match["vehicle_id"], match["_clean_plate"], match.get("owner_name"), reason,
            )
            return match, "high"

    match, reason = _level3_numeric_low(candidates, vehicles)
    if match == "AMBIGUOUS":
        logger.warning("[ANPR] Track %s: ambiguous LOW-confidence match (%s) -- skipping.", track_id, reason)
        return None, "low"
    if match is not None:
        logger.info(
            "[ANPR] Track %s: LOW-CONFIDENCE match -> vehicle_id=%s plate=%s (%s). auto_email=%s",
            track_id, match["vehicle_id"], match["_clean_plate"], reason, AUTO_EMAIL_ON_LOW_CONFIDENCE,
        )
        return match, "low"

    return None, "high"


# --------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------- #
def _insert_violation_row(track_id, plate_number, vehicle_type, duration_sec, fine_amount, evidence_path, sms_status):
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            cur = conn.execute(
                """INSERT INTO violations
                   (track_id, plate_number, vehicle_type, duration_sec, fine_amount, status, sms_status, evidence_path, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    track_id, plate_number, vehicle_type, int(duration_sec), float(fine_amount), "UNPAID",
                    sms_status, evidence_path, time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
            return cur.lastrowid
    except Exception:
        logger.exception("Failed to insert violation row for track_id=%s", track_id)
        return None


def _update_violation_dwell(violation_id, duration_sec, fine_amount):
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute(
                "UPDATE violations SET duration_sec = ?, fine_amount = ? WHERE violation_id = ?",
                (int(duration_sec), float(fine_amount), violation_id),
            )
            conn.commit()
    except Exception:
        logger.exception("Failed to update dwell/fine for violation_id=%s", violation_id)


def _update_sms_status(violation_id, sms_status):
    try:
        with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
            conn.execute("UPDATE violations SET sms_status = ? WHERE violation_id = ?", (sms_status, violation_id))
            conn.commit()
    except Exception:
        logger.exception("Failed to update sms_status for violation_id=%s", violation_id)


def _get_violation(violation_id):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM violations WHERE violation_id = ?", (violation_id,)).fetchone()
    return dict(row) if row else None


def _get_violation_by_order(order_id):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM violations WHERE razorpay_order_id = ? ORDER BY violation_id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
    return dict(row) if row else None


def _mark_paid(violation_id, payment_id=None):
    """Set status to PAID. Only verified payment paths call this."""
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        cur = conn.execute(
            """UPDATE violations
               SET status = 'PAID', paid_at = ?, razorpay_payment_id = COALESCE(?, razorpay_payment_id)
               WHERE violation_id = ? AND status != 'PAID'""",
            (time.strftime("%Y-%m-%d %H:%M:%S"), payment_id, violation_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _reset_session_violations():
    global _session_generation
    with _episodes_lock:
        _session_generation += 1
        _episodes.clear()
        _snapshot_requests.clear()
        try:
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                # Old rows are removed, but IDs are NOT reused: an email link from an
                # earlier upload must never settle a new challan that happens to get the same ID.
                conn.execute("DELETE FROM violations")
                conn.commit()
            logger.info("New upload: cleared previous violation records (session %d).", _session_generation)
        except Exception:
            logger.exception("Failed to clear previous violation records.")


def _save_evidence(native_crop, filename_stub: str) -> "str | None":
    evidence_path = os.path.join(EVIDENCE_DIR, f"{filename_stub}_{int(time.time())}.jpg")
    try:
        cv2.imwrite(evidence_path, native_crop)
        return evidence_path
    except Exception:
        logger.exception("Failed to save evidence image (%s).", filename_stub)
        return None


# --------------------------------------------------------------------- #
# Dwell episode state machine
#   20s   -> OCR + lookup; warning email sent once (unchanged behaviour).
#   >=30s -> violation row inserted once, then duration/fine updated in place.
#            Annotated snapshot refreshed each second.
#   end   -> final penalty notice sent for each confirmed violation.
# --------------------------------------------------------------------- #
def _commit_locked(key, ep):
    if not ep["ocr_done"] or ep["latest_elapsed"] < VIOLATION_THRESHOLD_SECONDS:
        return

    duration = ep["latest_elapsed"]
    fine = calculate_fine(duration)

    if ep["row_id"] is None:
        row_id = _insert_violation_row(
            key[0], ep["plate_text"], ep["vehicle_type"], duration, fine, ep["evidence_path"], ep["sms_status"],
        )
        if row_id is None:
            return
        ep["row_id"] = row_id
        ep["duration_written"] = duration
        logger.info("[VIOLATION] Track %s: row %s written at %ss, fine Rs %.2f (%s).",
                    key[0], row_id, duration, fine, ep["sms_status"])
        return

    if duration != ep["duration_written"]:
        _update_violation_dwell(ep["row_id"], duration, fine)
        ep["duration_written"] = duration


def _process_violation_job(key, generation, track_id, vehicle_type, native_crop):
    """Background thread: OCR, lookup, evidence save, and the 20s warning email."""
    try:
        candidates = plate_reader.read_plate_candidates(native_crop)
    except Exception:
        logger.exception("Plate OCR raised unexpectedly for track_id=%s.", track_id)
        candidates = []

    logger.info("[ANPR] Track %s: OCR candidates = %s", track_id, candidates)

    vehicle, confidence = (None, "high")
    if candidates:
        vehicle, confidence = _lookup_vehicle(candidates, track_id=track_id)

    if vehicle is not None:
        plate_text = vehicle["_clean_plate"]
        owner = vehicle.get("owner_name") or "Vehicle Owner"
        email = vehicle.get("email")
        evidence_path = _save_evidence(native_crop, f"{plate_text}_{track_id}")
        if confidence == "high" or AUTO_EMAIL_ON_LOW_CONFIDENCE:
            sms_status = "pending"
        else:
            sms_status = "low_confidence_skipped"
    else:
        plate_text = ",".join(candidates)[:120] if candidates else "UNKNOWN"
        owner = email = None
        evidence_path = _save_evidence(native_crop, f"UNRESOLVED_track{track_id}")
        sms_status = "unresolved"
        logger.warning("[ANPR] Track %s: no registered plate verified from %s -- logged as unresolved, no email.",
                       track_id, candidates)

    with _episodes_lock:
        ep = _episodes.get(key)
        if ep is None or ep["generation"] != generation:
            return
        ep.update(
            ocr_done=True,
            vehicle=vehicle,
            confidence=confidence,
            plate_text=plate_text,
            owner=owner,
            email=email,
            evidence_path=evidence_path,
            sms_status=sms_status,
        )
        email_duration = ep["latest_elapsed"]
        _commit_locked(key, ep)

    if sms_status != "pending":
        return

    # Warning email at the 20s mark -- unchanged.
    try:
        sent = send_warning_email(
            email, owner, plate_text, vehicle_type, int(email_duration), evidence_path=evidence_path,
        )
    except Exception:
        logger.exception("Email dispatch raised unexpectedly for track_id=%s.", track_id)
        sent = False
    final_status = "sent" if sent else "failed"

    with _episodes_lock:
        ep = _episodes.get(key)
        if ep is None or ep["generation"] != generation:
            return
        ep["sms_status"] = final_status
        if ep["row_id"] is not None:
            _update_sms_status(ep["row_id"], final_status)


def _handle_dwell_trigger(track_id, cls_id, vehicle_type, native_crop, elapsed_seconds, start_frame):
    key = (track_id, start_frame)
    with _episodes_lock:
        if key in _episodes:
            return
        generation = _session_generation
        _episodes[key] = {
            "generation": generation,
            "vehicle_type": vehicle_type,
            "ocr_done": False,
            "vehicle": None,
            "confidence": "high",
            "plate_text": None,
            "owner": None,
            "email": None,
            "evidence_path": None,
            "annotated_path": None,
            "sms_status": "pending",
            "latest_elapsed": int(elapsed_seconds),
            "row_id": None,
            "duration_written": None,
        }
    anpr_executor.submit(_process_violation_job, key, generation, track_id, vehicle_type, native_crop)


def _handle_dwell_tick(track_id, cls_id, vehicle_type, elapsed_seconds, start_frame):
    """Runs inside tracker.track_zone_dwell on the video thread. The snapshot is
    taken later in _flush_annotated_snapshots, once the frame is fully annotated."""
    key = (track_id, start_frame)
    with _episodes_lock:
        ep = _episodes.get(key)
        if ep is None or ep["generation"] != _session_generation:
            return
        ep["latest_elapsed"] = int(elapsed_seconds)
        _commit_locked(key, ep)
        if ep["row_id"] is not None:
            _snapshot_requests.add(key)


def _flush_annotated_snapshots(annotated_frame):
    """Save the fully annotated frame (red box, track overlay, ROI polygon) for
    each violator flagged this frame. One file per episode, overwritten each second."""
    with _episodes_lock:
        if not _snapshot_requests:
            return
        requests_now = []
        for key in list(_snapshot_requests):
            ep = _episodes.get(key)
            if ep is not None and ep["generation"] == _session_generation:
                requests_now.append((key, ep))
        _snapshot_requests.clear()

    for key, ep in requests_now:
        path = os.path.join(EVIDENCE_DIR, f"annotated_track{key[0]}_{key[1]}.jpg")
        try:
            cv2.imwrite(path, annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        except Exception:
            logger.exception("Failed to save annotated snapshot for track %s.", key[0])
            continue
        with _episodes_lock:
            if _episodes.get(key) is ep:
                ep["annotated_path"] = path


def _finalize_session(generation):
    """Called when the video stream ends. Queues one penalty notice per confirmed
    violation (a registered vehicle that received a warning email)."""
    jobs = []
    with _episodes_lock:
        if generation != _session_generation:
            return
        for key, ep in _episodes.items():
            if ep["row_id"] is None or ep["vehicle"] is None:
                continue
            if ep["sms_status"] == "low_confidence_skipped":
                continue
            duration = ep["duration_written"] or 0
            jobs.append({
                "violation_id": ep["row_id"],
                "plate": ep["plate_text"],
                "owner": ep["owner"] or "Vehicle Owner",
                "email": ep["email"],
                "duration": duration,
                "fine": calculate_fine(duration),
                "image": ep["annotated_path"] or ep["evidence_path"],
            })
    logger.info("[PENALTY] Stream ended: %d penalty notice(s) to send.", len(jobs))
    for job in jobs:
        anpr_executor.submit(_send_penalty_job, job, generation)


def _send_penalty_job(job, generation):
    sent = False
    try:
        sent = send_penalty_notice_email(
            job["email"], job["plate"], job["owner"], job["duration"], job["fine"],
            job["image"], job["violation_id"], pay_url=_pay_link(job["violation_id"]),
        )
    except Exception:
        logger.exception("Penalty notice raised unexpectedly for violation %s.", job["violation_id"])

    if not sent:
        return
    with _episodes_lock:
        if generation != _session_generation:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
                conn.execute(
                    "UPDATE violations SET penalty_notice_sent = 1 WHERE violation_id = ?",
                    (job["violation_id"],),
                )
                conn.commit()
        except Exception:
            logger.exception("Could not record penalty notice for violation %s.", job["violation_id"])


# --------------------------------------------------------------------- #
# Flask app + video streaming
# --------------------------------------------------------------------- #
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

tracker = ZoneTracker(
    on_dwell_trigger=_handle_dwell_trigger,
    on_dwell_tick=_handle_dwell_tick,
    ocr_trigger_seconds=DWELL_OCR_TRIGGER_SECONDS,
)
current_video_path = None
is_streaming = False

output_frame = None
frame_lock = threading.Lock()


def video_processing_worker():
    global current_video_path, tracker, is_streaming, output_frame

    if not current_video_path or not os.path.exists(current_video_path):
        return

    with _episodes_lock:
        session_generation = _session_generation

    cap = cv2.VideoCapture(current_video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_delay = 1.0 / fps
    frame_count = 0

    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    target_width = 1280
    target_height = int(target_width * (orig_h / orig_w))

    scale_x = orig_w / float(target_width)
    scale_y = orig_h / float(target_height)

    while cap.isOpened() and is_streaming:
        start_time = time.time()
        ret, frame = cap.read()
        if not ret:
            break

        frame_count += 1

        tracker.set_frame_context(frame, scale_x=scale_x, scale_y=scale_y)

        frame_resized = cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_CUBIC)
        processed_frame = tracker.track_zone_dwell(frame_resized, frame_count, fps=fps)

        _flush_annotated_snapshots(processed_frame)

        _, buffer = cv2.imencode('.jpg', processed_frame, [cv2.IMWRITE_JPEG_QUALITY, 95])

        with frame_lock:
            output_frame = buffer.tobytes()

        elapsed = time.time() - start_time
        if frame_delay > elapsed:
            time.sleep(frame_delay - elapsed)

    cap.release()
    is_streaming = False

    # Stream finished (end of video, or interrupted by a new upload).
    _finalize_session(session_generation)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_video():
    global current_video_path, tracker, is_streaming, output_frame

    is_streaming = False
    time.sleep(0.3)

    with frame_lock:
        output_frame = None

    if 'video_file' not in request.files:
        return redirect(url_for('index'))

    file = request.files['video_file']
    if file.filename == '':
        return redirect(url_for('index'))

    _reset_session_violations()

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(filepath)
    current_video_path = filepath

    tracker = ZoneTracker(
        on_dwell_trigger=_handle_dwell_trigger,
        on_dwell_tick=_handle_dwell_tick,
        ocr_trigger_seconds=DWELL_OCR_TRIGGER_SECONDS,
    )

    drawer_script = os.path.join(os.path.dirname(__file__), "core", "roi_drawer.py")
    if os.path.exists(drawer_script):
        try:
            subprocess.run([sys.executable, drawer_script, filepath], check=True)
        except subprocess.CalledProcessError:
            pass

    is_streaming = True
    thread = threading.Thread(target=video_processing_worker, daemon=True)
    thread.start()

    return redirect(url_for('index'))


def generate_frames():
    global output_frame, is_streaming
    while is_streaming:
        with frame_lock:
            if output_frame is None:
                time.sleep(0.01)
                continue
            encoded_bytes = output_frame

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + encoded_bytes + b'\r\n')
        time.sleep(0.03)


@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


def _evidence_url(evidence_path):
    if not evidence_path:
        return None
    filename = os.path.basename(str(evidence_path))
    if not filename or not os.path.isfile(os.path.join(EVIDENCE_DIR, filename)):
        return None
    return url_for('static', filename=f'evidence/{filename}')


def _fetch_violations(limit=100):
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT violation_id, track_id, plate_number, vehicle_type, duration_sec, "
            "fine_amount, status, sms_status, evidence_path, timestamp "
            "FROM violations ORDER BY violation_id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    out = []
    for r in rows:
        evidence_path = r["evidence_path"]
        out.append({
            "violation_id": r["violation_id"],
            "track_id": r["track_id"],
            "plate_number": r["plate_number"] or "UNKNOWN",
            "vehicle_type": r["vehicle_type"] or "",
            "duration_sec": int(r["duration_sec"] or 0),
            "fine_amount": float(r["fine_amount"]) if r["fine_amount"] is not None else BASE_FINE,
            "status": r["status"] or "UNPAID",
            "sms_status": r["sms_status"] or "pending",
            "timestamp": r["timestamp"],
            "evidence_path": evidence_path,
            "evidence_url": _evidence_url(evidence_path),
            "pay_token": _pay_token(r["violation_id"]),
        })
    return out


@app.route('/violations_data')
@app.route('/api/violations')
def violations_data():
    try:
        return jsonify({"violations": _fetch_violations()})
    except Exception:
        logger.exception("Failed to read violations from DB.")
        return jsonify({"violations": []})


# --------------------------------------------------------------------- #
# Payments -- Razorpay Orders + Checkout (test mode)
# --------------------------------------------------------------------- #
def _pay_token(violation_id) -> str:
    """Per-challan token that must accompany every payment link."""
    return hmac.new(
        PAY_LINK_SECRET.encode("utf-8"), str(violation_id).encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def _is_tunnel_request() -> bool:
    """ngrok adds X-Forwarded-For to every request it forwards to this server."""
    return bool(request.headers.get("X-Forwarded-For"))


def _pay_token_ok(violation_id) -> bool:
    """Validate the ?t= token on payment links.

    A valid token always passes. Without one, access is allowed only when
    PAYMENT_SIMULATION=1 and either the request is local, or the public tunnel
    has been opened on purpose with SIMULATION_ALLOW_UNSIGNED_PUBLIC=1.
    WARNING: with that opt-in, anyone with the link can mark a challan PAID."""
    supplied = request.args.get("t", "")
    if supplied and hmac.compare_digest(supplied, _pay_token(violation_id)):
        return True

    if PAYMENT_SIMULATION and (not _is_tunnel_request() or SIMULATION_ALLOW_UNSIGNED_PUBLIC):
        logger.warning(
            "[SIMULATION] Payment link for violation %s has a missing or wrong token; "
            "allowed because PAYMENT_SIMULATION=1 (%s).",
            violation_id, "tunnel" if _is_tunnel_request() else "localhost",
        )
        return True
    return False


def _pay_link(violation_id) -> str:
    return f"{PUBLIC_BASE_URL}/pay_challan/{violation_id}?t={_pay_token(violation_id)}"


# Only the payment endpoints are reachable through a public tunnel.
PUBLIC_ENDPOINTS = {"pay_challan", "payment_qr", "pay_fine", "pay_status"}


@app.before_request
def _restrict_public_requests():
    """ngrok adds X-Forwarded-For to every request it forwards. Those requests
    may reach only the payment pages, so the dashboard and data API stay private."""
    if request.headers.get("X-Forwarded-For") and request.endpoint not in PUBLIC_ENDPOINTS:
        abort(404)


_ngrok_state = {"api": None, "port": 5000}


def _open_ngrok_tunnel(port: int) -> str:
    """(Re)open the ngrok tunnel on the reserved domain. Returns the https URL.
    Raises on failure so the caller can log it and retry later."""
    from pyngrok import ngrok, conf

    pyngrok_config = conf.PyngrokConfig(
        ngrok_path=NGROK_PATH,
        auth_token=NGROK_AUTHTOKEN or None,
    )
    options = {"domain": NGROK_DOMAIN} if NGROK_DOMAIN else {}
    tunnel = ngrok.connect(port, "http", pyngrok_config=pyngrok_config, **options)
    public = tunnel.public_url
    if public.startswith("http://"):
        public = "https://" + public[len("http://"):]
    _ngrok_state["api"] = ngrok
    return public.rstrip("/")


def _ngrok_watchdog(port: int) -> None:
    """Keep the tunnel alive. Every NGROK_WATCHDOG_SECONDS it checks for a live
    tunnel and reopens it if the ngrok process died or the tunnel vanished."""
    global PUBLIC_BASE_URL
    import time
    while True:
        time.sleep(NGROK_WATCHDOG_SECONDS)
        try:
            ngrok = _ngrok_state["api"]
            alive = bool(ngrok and ngrok.get_tunnels())
        except Exception:
            alive = False
        if alive:
            continue
        logger.warning("[TUNNEL] Tunnel is down. Reconnecting to %s ...", NGROK_DOMAIN or "a new address")
        try:
            try:
                if _ngrok_state["api"]:
                    _ngrok_state["api"].kill()
            except Exception:
                pass
            new_url = _open_ngrok_tunnel(port)
            if new_url != PUBLIC_BASE_URL:
                PUBLIC_BASE_URL = new_url
            logger.info("[TUNNEL] Reconnected. Public URL: %s", PUBLIC_BASE_URL)
        except Exception as exc:
            msg = str(exc)
            if "ERR_NGROK_334" in msg or "already online" in msg.lower():
                logger.error(
                    "[TUNNEL] Domain %s is still online in another ngrok session. "
                    "Close the other ngrok window (or wait ~1 minute), then the watchdog retries.",
                    NGROK_DOMAIN,
                )
            else:
                logger.error("[TUNNEL] Reconnect failed: %s. Will retry.", msg)


def _start_public_tunnel(port: int) -> None:
    """Start the ngrok tunnel, point PUBLIC_BASE_URL at it, and start a watchdog.
    On failure the server keeps running with the PUBLIC_BASE_URL from .env."""
    global PUBLIC_BASE_URL
    if not NGROK_AUTHTOKEN:
        logger.warning("USE_NGROK=1 but NGROK_AUTHTOKEN is empty. Links will use PUBLIC_BASE_URL=%s", PUBLIC_BASE_URL)
        return
    if not os.path.exists(NGROK_PATH) and NGROK_PATH != "ngrok":
        logger.error("[TUNNEL] ngrok not found at %s. Set NGROK_PATH in .env.", NGROK_PATH)
        return
    try:
        PUBLIC_BASE_URL = _open_ngrok_tunnel(port)
        logger.info("[TUNNEL] Public URL: %s  (emails sent from now on use this address)", PUBLIC_BASE_URL)
    except Exception as exc:
        logger.error("[TUNNEL] Could not start ngrok: %s. Links will use PUBLIC_BASE_URL=%s", exc, PUBLIC_BASE_URL)

    atexit.register(lambda: _ngrok_state["api"] and _ngrok_state["api"].kill())
    threading.Thread(target=_ngrok_watchdog, args=(port,), daemon=True, name="ngrok-watchdog").start()


def _razorpay_configured():
    """True only when the SDK is installed and real test keys are set,
    so placeholder values are never sent to Razorpay."""
    if razorpay is None:
        return False
    if not RAZORPAY_KEY_ID.startswith("rzp_") or "YourKey" in RAZORPAY_KEY_ID:
        return False
    return bool(RAZORPAY_KEY_SECRET) and "YourKey" not in RAZORPAY_KEY_SECRET


def _razorpay_client():
    global _rzp_client
    if razorpay is None:
        raise RuntimeError("The razorpay package is not installed. Run: pip install razorpay")
    if _rzp_client is None:
        _rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    return _rzp_client


def _owner_for_plate(plate):
    for v in _fetch_all_vehicles():
        if v["_clean_plate"] == plate:
            return v.get("owner_name") or "Vehicle Owner", v.get("email")
    return "Vehicle Owner", None


def _get_or_create_order(violation, amount_paise):
    """Reuse the stored order if the amount is unchanged; otherwise create a new one."""
    if violation.get("razorpay_order_id") and violation.get("razorpay_order_amount") == amount_paise:
        return violation["razorpay_order_id"]

    order = _razorpay_client().order.create({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": f"rcpt_violation_{violation['violation_id']}",
        "payment_capture": 1,
        "notes": {
            "violation_id": str(violation["violation_id"]),
            "plate_number": str(violation["plate_number"]),
        },
    })
    with sqlite3.connect(DB_PATH, timeout=30.0) as conn:
        conn.execute(
            "UPDATE violations SET razorpay_order_id = ?, razorpay_order_amount = ? WHERE violation_id = ?",
            (order["id"], amount_paise, violation["violation_id"]),
        )
        conn.commit()
    logger.info("[PAYMENT] Created Razorpay order %s for violation %s (%d paise).",
                order["id"], violation["violation_id"], amount_paise)
    return order["id"]


def _page(title, body_html, status=200):
    return (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        f"<title>{html_lib.escape(title)}</title>"
        "<style>body{font-family:Segoe UI,Tahoma,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#222}"
        "table{width:100%;border-collapse:collapse}td{padding:8px;border-bottom:1px solid #eee}"
        "td:first-child{color:#666;width:40%}</style></head><body>"
        f"{body_html}</body></html>",
        status,
    )


@app.route('/pay_challan/<int:violation_id>')
def pay_challan(violation_id):
    """Challan checkout page. Renders without any Razorpay keys, so the
    offline simulation works on a laptop with no merchant account."""
    v = _get_violation(violation_id)
    if v is None:
        abort(404)
    if not _pay_token_ok(violation_id):
        return _page(
            "Link not valid",
            "<h2>This payment link is not valid</h2>"
            "<p>Please open the link from your penalty email, or contact the traffic office.</p>",
            403,
        )

    fine = float(v["fine_amount"] or BASE_FINE)
    return render_template(
        "payment.html",
        violation={
            "violation_id": v["violation_id"],
            "plate_number": v["plate_number"] or "UNKNOWN",
            "vehicle_type": v["vehicle_type"] or "Vehicle",
            "duration_sec": int(v["duration_sec"] or 0),
            "timestamp": v["timestamp"] or "",
            "status": v["status"],
        },
        fine_text=f"{fine:,.2f}",
        pay_token=request.args.get("t", ""),
        is_paid=v["status"] == "PAID",
        payment_ref=v.get("razorpay_payment_id") or "",
        simulation_enabled=PAYMENT_SIMULATION,
        qr_enabled=bool(UPI_VPA),
        qr_url=url_for("payment_qr", violation_id=violation_id),
        dashboard_url=url_for("index"),
    )


@app.route('/payment_qr/<int:violation_id>')
def payment_qr(violation_id):
    """Dummy UPI QR preview for the checkout page. Generated locally, so no
    external service receives the UPI ID or amount."""
    v = _get_violation(violation_id)
    if v is None or not UPI_VPA:
        abort(404)
    fine = float(v["fine_amount"] or BASE_FINE)
    png = _make_upi_qr_png(build_upi_uri(fine, violation_id))
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route('/api/verify_payment', methods=['POST'])
def verify_payment():
    """Called by the Razorpay Checkout handler after a payment attempt.
    The signature is checked with the SDK, then the payment is fetched from
    Razorpay and its amount compared to the fine before the row is marked PAID."""
    data = request.get_json(silent=True) or request.form.to_dict()
    order_id = (data.get("razorpay_order_id") or "").strip()
    payment_id = (data.get("razorpay_payment_id") or "").strip()
    signature = (data.get("razorpay_signature") or "").strip()
    if not (order_id and payment_id and signature):
        return jsonify(status="error", message="Missing payment details."), 400

    v = _get_violation_by_order(order_id)
    if v is None:
        return jsonify(status="error", message="Unknown order."), 404
    if v["status"] == "PAID":
        return jsonify(status="success", violation_id=v["violation_id"], already_paid=True)

    try:
        _razorpay_client().utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
    except Exception:
        # The SDK raises SignatureVerificationError on a bad signature.
        logger.warning("[PAYMENT] Signature verification failed for order %s.", order_id)
        return jsonify(status="error", message="Payment signature could not be verified."), 400

    try:
        payment = _razorpay_client().payment.fetch(payment_id)
    except Exception:
        logger.exception("[PAYMENT] Could not fetch payment %s from Razorpay.", payment_id)
        return jsonify(status="error", message="Could not confirm the payment with Razorpay."), 502

    expected_paise = _fine_to_paise(v["fine_amount"])
    if (
        payment.get("order_id") != order_id
        or payment.get("status") != "captured"
        or int(payment.get("amount") or 0) != expected_paise
    ):
        logger.warning("[PAYMENT] Payment %s does not match violation %s.", payment_id, v["violation_id"])
        return jsonify(status="error", message="Payment does not match this challan."), 400

    _mark_paid(v["violation_id"], payment_id=payment_id)
    logger.info("[PAYMENT] Violation %s marked PAID (payment %s).", v["violation_id"], payment_id)
    return jsonify(status="success", violation_id=v["violation_id"])


@app.route('/pay_fine/<int:violation_id>', methods=['POST'])
def pay_fine(violation_id):
    """Offline simulated payment. Only available when PAYMENT_SIMULATION=1.
    Marks the challan PAID and returns a simulated transaction ID."""
    if not PAYMENT_SIMULATION:
        return jsonify({"status": "error", "message": "Simulated payments are disabled. Set PAYMENT_SIMULATION=1."}), 403
    v = _get_violation(violation_id)
    if v is None:
        return jsonify({"status": "error", "message": "Violation not found."}), 404
    if not _pay_token_ok(violation_id):
        return jsonify({"status": "error", "message": "This payment link is not valid."}), 403
    if v["status"] == "PAID":
        return jsonify({"status": "success", "already_paid": True,
                        "transaction_id": v.get("razorpay_payment_id") or ""})

    transaction_id = "SIM-" + secrets.token_hex(6).upper()
    try:
        _mark_paid(violation_id, payment_id=transaction_id)
    except Exception:
        logger.exception("[PAYMENT] Could not record simulated payment for violation %s.", violation_id)
        return jsonify({"status": "error", "message": "Could not record the payment. Please try again."}), 500
    logger.info("[PAYMENT] Violation %s settled by simulation (%s).", violation_id, transaction_id)
    return jsonify({"status": "success", "violation_id": violation_id, "transaction_id": transaction_id})


@app.route('/pay_status/<int:violation_id>')
def pay_status(violation_id):
    """Lets the payment page check whether a payment went through when the POST
    response was lost (mobile network drop, screen lock, slow tunnel)."""
    v = _get_violation(violation_id)
    if v is None or not _pay_token_ok(violation_id):
        return jsonify({"status": "error", "message": "Not found."}), 404
    response = jsonify({"status": v["status"], "transaction_id": v.get("razorpay_payment_id") or ""})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/razorpay/webhook', methods=['POST'])
def razorpay_webhook():
    """Backup confirmation channel. Verifies X-Razorpay-Signature with the
    webhook secret and only marks PAID when the amount matches the fine."""
    if not RAZORPAY_WEBHOOK_SECRET:
        return jsonify({"status": "error", "message": "Webhook secret not configured."}), 503

    body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logger.warning("Razorpay webhook rejected: bad signature.")
        return jsonify({"status": "error"}), 400

    event = request.get_json(silent=True) or {}
    if event.get("event") != "payment.captured":
        return jsonify({"status": "ignored"}), 200

    payment = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payment.get("order_id")
    v = _get_violation_by_order(order_id) if order_id else None
    if v is None:
        return jsonify({"status": "ignored"}), 200
    if int(payment.get("amount") or 0) != _fine_to_paise(v["fine_amount"]):
        logger.warning("Razorpay webhook: amount mismatch for violation %s -- not marking PAID.", v["violation_id"])
        return jsonify({"status": "amount_mismatch"}), 200

    if _mark_paid(v["violation_id"], payment_id=payment.get("id")):
        logger.info("[PAYMENT] Violation %s marked PAID via webhook.", v["violation_id"])
    return jsonify({"status": "ok"}), 200


if __name__ == '__main__':
    if USE_NGROK:
        _start_public_tunnel(5000)  # start before any penalty email is sent
        # Note: the watchdog keeps the reserved domain online while this process runs.
    # 0.0.0.0 lets devices on the same Wi-Fi reach the server (LAN testing).
    # Public access uses the ngrok tunnel above instead of port forwarding.
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)