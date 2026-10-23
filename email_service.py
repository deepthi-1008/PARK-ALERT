import os
import socket
import smtplib
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("PARK_ALERT_EMAIL")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "deepthi9626@gmail.com")
# No hardcoded fallback password: credentials come only from .env / environment.
# If missing, dispatch is skipped with a warning instead of sending with a stale secret.
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")

# Resilience settings for requirement 3: transient DNS/network drops
# (e.g. "[Errno 11001] getaddrinfo failed") should be retried rather than
# treated as a permanent failure, since several violations may be
# dispatching concurrently and a brief network blip shouldn't drop one.
SMTP_TIMEOUT_SECONDS = 10
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 2  # 2s, 4s, 8s


def _build_message(target_email, owner_name, plate_number, vehicle_type, duration_sec, evidence_path=None):
    msg = MIMEMultipart("related")
    msg["Subject"] = f"URGENT: No-Parking Warning Notice - {plate_number}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = target_email

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 25px;">
            <p style="font-size: 16px;">Dear <strong>{owner_name}</strong>,</p>
            <p style="font-size: 15px; line-height: 1.5;">
                Your vehicle <strong>{plate_number}</strong> ({vehicle_type}) has been detected inside a <strong>Restricted No-Parking Zone</strong> for over <strong>{duration_sec} seconds</strong>.
            </p>
            <div style="background-color: #2b2510; color: #d4a373; padding: 15px; border-left: 4px solid #b5838d; border-radius: 4px; margin: 20px 0;">
                <p style="margin: 0; font-size: 14px; line-height: 1.4;">
                    <strong>Warning:</strong> Please move your vehicle immediately. Remaining parked beyond 30 seconds will result in a penalty fine of <strong>₹500.00</strong>.
                </p>
            </div>
            <p style="font-size: 14px; color: #666;">
                An evidence snapshot of your vehicle inside the restricted boundary is shown below:
            </p>
            <div style="text-align: center; margin: 20px 0;">
                <img src="cid:evidence_img" style="max-width: 100%; border-radius: 6px; border: 1px solid #ddd;" alt="Evidence Snapshot" />
            </div>
            <br>
            <p style="font-size: 12px; color: #888; border-top: 1px solid #eee; padding-top: 15px;">
                PARK-ALERT Automated Traffic Enforcement System
            </p>
        </div>
    </body>
    </html>
    """

    msg_alt = MIMEMultipart("alternative")
    msg.attach(msg_alt)
    msg_alt.attach(MIMEText(html_content, "html"))

    if evidence_path and os.path.exists(evidence_path):
        with open(evidence_path, "rb") as f:
            img_data = f.read()
        ev_mime = MIMEImage(img_data)
        ev_mime.add_header("Content-ID", "<evidence_img>")
        ev_mime.add_header("Content-Disposition", "inline", filename=os.path.basename(evidence_path))
        msg.attach(ev_mime)

    return msg


def _send_once(msg, target_email):
    """One SMTP connect+send attempt with an explicit socket timeout.
    Raises on any failure -- the retry loop in send_warning_email decides
    whether to retry or give up."""
    server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)
    try:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [target_email], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass  # connection may already be dead -- nothing more to do


def send_warning_email(target_email, owner_name, plate_number, vehicle_type, duration_sec, evidence_path=None):
    if not target_email or "@" not in target_email or not SENDER_PASSWORD:
        logger.warning(f"Skipping dispatch: Invalid email ({target_email}) or missing password.")
        return False

    try:
        msg = _build_message(target_email, owner_name, plate_number, vehicle_type, duration_sec, evidence_path)
    except Exception as e:
        logger.error(f"[!] Failed to build email for {target_email} ({plate_number}): {e}")
        return False

    # Transient failures worth retrying: DNS resolution drops (the
    # "[Errno 11001] getaddrinfo failed" case), connection resets, and
    # socket timeouts. A permanent failure (bad credentials, rejected
    # recipient) will just fail the same way on every attempt and still
    # exits cleanly after the last one.
    last_error = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            _send_once(msg, target_email)
            logger.info(f"[OK] Warning email sent to {target_email} ({plate_number}) on attempt {attempt}.")
            return True
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as e:
            # Permanent rejections: retrying with the same credentials or
            # recipient will fail identically, so stop immediately.
            logger.error(f"[!] Permanent SMTP rejection for {target_email} ({plate_number}), not retrying: {e!r}")
            return False
        except (socket.gaierror, socket.timeout, TimeoutError, ConnectionError, OSError, smtplib.SMTPException) as e:
            last_error = e
            logger.warning(
                f"[!] Email attempt {attempt}/{MAX_SEND_ATTEMPTS} to {target_email} ({plate_number}) "
                f"failed: {e!r}"
            )
            if attempt < MAX_SEND_ATTEMPTS:
                backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))  # 2s, 4s, 8s
                time.sleep(backoff)
        except Exception as e:
            # Not a recognized transient-network error -- no point retrying
            # (e.g. a malformed message or an auth failure).
            logger.error(f"[!] Non-retryable email dispatch failure for {target_email} ({plate_number}): {e}")
            return False

    logger.error(
        f"[!] Email dispatch to {target_email} ({plate_number}) failed after "
        f"{MAX_SEND_ATTEMPTS} attempts: {last_error!r}"
    )
    return False


# ---------------------------------------------------------------------
# Final penalty notice -- sent when the video stream ends. The warning
# email above (20s dwell) is unchanged and does not use anything below.
# ---------------------------------------------------------------------
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
UPI_VPA = os.getenv("UPI_VPA", "")  # e.g. yourname@okhdfcbank, or a Razorpay-linked VPA
UPI_PAYEE_NAME = "PARK_ALERT_TRAFFIC"


def build_upi_uri(fine_amount, violation_id):
    """Dynamic UPI deep link for this violation's amount."""
    return (
        f"upi://pay?pa={UPI_VPA}&pn={UPI_PAYEE_NAME}"
        f"&am={float(fine_amount):.2f}&cu=INR&tn=E-Challan_Violation_{violation_id}"
    )


def _make_upi_qr_png(upi_uri):
    from io import BytesIO
    import qrcode

    buf = BytesIO()
    qrcode.make(upi_uri).save(buf, format="PNG")
    return buf.getvalue()


def _send_with_retry(msg, target_email, label):
    """Same retry policy as send_warning_email: permanent SMTP rejections stop
    immediately, transient network errors retry with 2s/4s/8s backoff."""
    last_error = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            _send_once(msg, target_email)
            logger.info(f"[OK] {label} sent to {target_email} on attempt {attempt}.")
            return True
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as e:
            logger.error(f"[!] Permanent SMTP rejection for {label} to {target_email}, not retrying: {e!r}")
            return False
        except (socket.gaierror, socket.timeout, TimeoutError, ConnectionError, OSError, smtplib.SMTPException) as e:
            last_error = e
            logger.warning(f"[!] {label} attempt {attempt}/{MAX_SEND_ATTEMPTS} to {target_email} failed: {e!r}")
            if attempt < MAX_SEND_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        except Exception as e:
            logger.error(f"[!] Non-retryable failure sending {label} to {target_email}: {e}")
            return False
    logger.error(f"[!] {label} to {target_email} failed after {MAX_SEND_ATTEMPTS} attempts: {last_error!r}")
    return False


def send_penalty_notice_email(
    to_email,
    plate_number,
    owner_name,
    duration_sec,
    fine_amount,
    annotated_image_path,
    violation_id,
    pay_url=None,
):
    """Final penalty notice: dynamic fine, annotated evidence snapshot
    (cid:evidence_img), a UPI QR for this exact amount (cid:payment_qr), and a
    'Pay Fine Online' button that opens the checkout page."""
    if not to_email or "@" not in to_email or not SENDER_PASSWORD:
        logger.warning(f"Skipping penalty notice: invalid email ({to_email}) or missing password.")
        return False

    pay_url = pay_url or f"{PUBLIC_BASE_URL}/pay_challan/{violation_id}"
    fine_text = f"{float(fine_amount):,.2f}"

    qr_png = None
    if UPI_VPA:
        try:
            qr_png = _make_upi_qr_png(build_upi_uri(fine_amount, violation_id))
        except Exception:
            logger.exception(f"Could not generate UPI QR for violation {violation_id}.")

    qr_block = (
        '<div style="text-align:center;margin:20px 0;">'
        '<p style="font-size:14px;color:#444;">Or scan with any UPI app to pay '
        f"<strong>₹{fine_text}</strong>:</p>"
        '<img src="cid:payment_qr" width="180" height="180" alt="UPI payment QR" /></div>'
    ) if qr_png else ""

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 25px;">
            <h2 style="color:#b02a37; margin-top:0;">PARK-ALERT e-Challan #{violation_id}</h2>
            <p style="font-size: 16px;">Dear <strong>{owner_name}</strong>,</p>
            <p style="font-size: 15px; line-height: 1.5;">
                Your vehicle <strong>{plate_number}</strong> remained in a <strong>Restricted No-Parking Zone</strong>
                for <strong>{duration_sec} seconds</strong>. A penalty has been issued.
            </p>
            <table style="width:100%; border-collapse: collapse; font-size: 15px; margin: 15px 0;">
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Violation ID</td><td style="padding:8px; border-bottom:1px solid #eee;">#{violation_id}</td></tr>
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Parked duration</td><td style="padding:8px; border-bottom:1px solid #eee;">{duration_sec} seconds</td></tr>
                <tr><td style="padding:8px; color:#666;">Fine payable</td><td style="padding:8px; color:#dc3545; font-weight:bold;">₹{fine_text}</td></tr>
            </table>
            <div style="text-align: center; margin: 25px 0;">
                <a href="{pay_url}" style="background-color:#198754; color:#ffffff; padding:12px 24px; border-radius:6px; text-decoration:none; font-weight:bold; display:inline-block;">Pay Fine Online</a>
            </div>
            {qr_block}
            <p style="font-size: 14px; color: #666;">Evidence snapshot of the violation:</p>
            <div style="text-align: center; margin: 20px 0;">
                <img src="cid:evidence_img" style="max-width: 100%; border-radius: 6px; border: 1px solid #ddd;" alt="Evidence Snapshot" />
            </div>
            <p style="font-size: 12px; color: #888; border-top: 1px solid #eee; padding-top: 15px;">
                PARK-ALERT Automated Traffic Enforcement System
            </p>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("related")
    msg["Subject"] = f"PENALTY NOTICE #{violation_id} - {plate_number} - Rs {fine_text}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email
    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(html_content, "html"))

    if annotated_image_path and os.path.exists(annotated_image_path):
        with open(annotated_image_path, "rb") as f:
            ev = MIMEImage(f.read())
        ev.add_header("Content-ID", "<evidence_img>")
        ev.add_header("Content-Disposition", "inline", filename=os.path.basename(annotated_image_path))
        msg.attach(ev)
    else:
        logger.warning(f"Penalty notice {violation_id}: no evidence image to attach.")

    if qr_png:
        qr = MIMEImage(qr_png, _subtype="png")
        qr.add_header("Content-ID", "<payment_qr>")
        qr.add_header("Content-Disposition", "inline", filename="payment_qr.png")
        msg.attach(qr)

    return _send_with_retry(msg, to_email, f"penalty notice #{violation_id}")