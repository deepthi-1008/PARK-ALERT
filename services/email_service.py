import os
import socket
import ssl
import smtplib
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("PARK_ALERT_EMAIL")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
# Implicit TLS (SMTPS) on 465. The certificate is always checked against SMTP_SERVER's name.
SMTP_PORT = int(os.getenv("SMTP_PORT", "465").strip() or 465)
if SMTP_PORT != 465:
    logger.warning("SMTP_PORT=%s is set, but this sender uses SMTPS on 465. Set SMTP_PORT=465 in .env.", SMTP_PORT)
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "deepthi9626@gmail.com").strip()
# Gmail app passwords are shown with spaces ("abcd efgh ijkl mnop"). Spaces are removed here.
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "").replace(" ", "").strip()

# Timeouts and retries for transient network drops (DNS hiccups, resets, slow hotspots).
SMTP_TIMEOUT_SECONDS = 20
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 2  # waits 2s, then 4s between the 3 attempts


def _ipv4_address(host: str) -> str:
    """Resolve the host to an IPv4 address. Falls back to the hostname if resolution fails.
    The address is used only to open the TCP connection; TLS still checks the hostname."""
    try:
        infos = socket.getaddrinfo(host, SMTP_PORT, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0]
    except OSError:
        return host


class _IPv4SMTP_SSL(smtplib.SMTP_SSL):
    """SMTP_SSL that connects over IPv4 but verifies the certificate against the hostname.
    Passing a raw IP to smtplib makes certificate verification fail, because the certificate
    is issued for smtp.gmail.com, not for the IP address."""

    def _get_socket(self, host, port, timeout):
        ip = _ipv4_address(host)
        raw = socket.create_connection((ip, port), timeout)
        return self.context.wrap_socket(raw, server_hostname=host)


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
    """One SMTPS attempt: connect over IPv4, verify the certificate for SMTP_SERVER,
    log in, send. The context manager closes the socket even when a step fails.
    Raises on any failure; the retry loops decide whether to try again."""
    context = ssl.create_default_context()
    with _IPv4SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS, context=context) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [target_email], msg.as_string())


def send_warning_email(target_email, owner_name, plate_number, vehicle_type, duration_sec, evidence_path=None):
    if not target_email or "@" not in target_email or not SENDER_PASSWORD:
        logger.warning(f"Skipping dispatch: Invalid email ({target_email}) or missing password.")
        return False

    try:
        msg = _build_message(target_email, owner_name, plate_number, vehicle_type, duration_sec, evidence_path)
    except Exception as e:
        logger.error(f"[!] Failed to build email for {target_email} ({plate_number}): {e}")
        return False

    last_error = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            _send_once(msg, target_email)
            logger.info(f"[OK] Warning email sent to {target_email} ({plate_number}) on attempt {attempt}.")
            return True
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as e:
            logger.error(f"[!] Permanent SMTP rejection for {target_email} ({plate_number}), not retrying: {e!r}")
            return False
        except ssl.SSLCertVerificationError as e:
            # A certificate mismatch will fail the same way every time, so stop.
            logger.error(f"[!] TLS certificate check failed for {SMTP_SERVER}, not retrying: {e!r}")
            return False
        except (socket.gaierror, socket.timeout, TimeoutError, ConnectionError, OSError, smtplib.SMTPException) as e:
            last_error = e
            logger.warning(
                f"[!] Email attempt {attempt}/{MAX_SEND_ATTEMPTS} to {target_email} ({plate_number}) failed: {e!r}"
            )
            if attempt < MAX_SEND_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        except Exception as e:
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
    """Same retry policy as send_warning_email: permanent SMTP rejections and
    certificate errors stop immediately; transient network errors retry with 2s/4s backoff."""
    last_error = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            _send_once(msg, target_email)
            logger.info(f"[OK] {label} sent to {target_email} on attempt {attempt}.")
            return True
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as e:
            logger.error(f"[!] Permanent SMTP rejection for {label} to {target_email}, not retrying: {e!r}")
            return False
        except ssl.SSLCertVerificationError as e:
            logger.error(f"[!] TLS certificate check failed for {SMTP_SERVER}, not retrying: {e!r}")
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


# ---------------------------------------------------------------------
# Reminder for an unpaid challan, sent from the Unpaid dashboard.
# ---------------------------------------------------------------------
def send_reminder_email(to_email, plate_number, owner_name, fine_amount, violation_id, evidence_path, pay_url):
    """Formal reminder with the 5-day deadline, the evidence image inline, and a green
    'Pay Fine Online' button. Uses the verified SMTPS (465) transport, retries and certificate checks."""
    if not to_email or "@" not in to_email or not SENDER_PASSWORD:
        logger.warning(f"Skipping reminder: invalid email ({to_email}) or missing password.")
        return False

    fine_text = f"{float(fine_amount):,.2f}"
    action_line = (
        f"Action Required: Please settle this outstanding fine of \u20b9{fine_text} on vehicle {plate_number} "
        "within 5 days of this notice issuance to avoid legal prosecution and court summons under the Motor Vehicles Act."
    )

    text_body = (
        f"Dear {owner_name},\n\n"
        f"{action_line}\n\n"
        f"Challan Number: KSP-BLR-2026-{violation_id}\n"
        f"Vehicle: {plate_number}\n"
        f"Fine payable: Rs {fine_text}\n\n"
        f"Pay online: {pay_url}\n\n"
        "PARK-ALERT Automated Traffic Enforcement System\n"
    )

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 25px;">
            <h2 style="color:#b02a37; margin-top:0;">URGENT REMINDER: Unpaid e-Challan #{violation_id}</h2>
            <p style="font-size: 16px;">Dear <strong>{owner_name}</strong>,</p>
            <div style="background-color: #fff3cd; color: #664d03; padding: 14px; border-left: 4px solid #dc3545; border-radius: 4px; margin: 20px 0;">
                <p style="margin: 0; font-size: 15px; line-height: 1.5;"><strong>{action_line}</strong></p>
            </div>
            <table style="width:100%; border-collapse: collapse; font-size: 15px; margin: 15px 0;">
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Challan No</td><td style="padding:8px; border-bottom:1px solid #eee;">KSP-BLR-2026-{violation_id}</td></tr>
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Vehicle</td><td style="padding:8px; border-bottom:1px solid #eee;">{plate_number}</td></tr>
                <tr><td style="padding:8px; color:#666;">Fine payable</td><td style="padding:8px; color:#dc3545; font-weight:bold;">&#8377;{fine_text}</td></tr>
            </table>
            <div style="text-align: center; margin: 25px 0;">
                <a href="{pay_url}" style="background-color:#198754; color:#ffffff; padding:12px 24px; border-radius:6px; text-decoration:none; font-weight:bold; display:inline-block;">Pay Fine Online</a>
            </div>
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
    msg["Subject"] = f"URGENT REMINDER: Unpaid Traffic e-Challan #{violation_id} for Vehicle {plate_number}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email
    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))

    if evidence_path and os.path.exists(evidence_path):
        with open(evidence_path, "rb") as f:
            ev = MIMEImage(f.read())
        ev.add_header("Content-ID", "<evidence_img>")
        ev.add_header("Content-Disposition", "inline", filename=os.path.basename(evidence_path))
        msg.attach(ev)
    else:
        logger.warning(f"Reminder for challan {violation_id}: no evidence image to attach.")

    return _send_with_retry(msg, to_email, f"reminder #{violation_id}")


# ---------------------------------------------------------------------
# Payment receipt and e-Challan settlement, sent from the Paid dashboard.
# ---------------------------------------------------------------------
PAID_SAFETY_TAGLINE = (
    "Safe Roads, Smooth Flow: We deeply appreciate your cooperation. Even brief parking in restricted zones "
    "can disrupt traffic and emergency access. We kindly request your support in parking only in designated "
    "zones to keep Karnataka's roadways smooth and safe for all."
)


def send_paid_challan_email(to_email, plate_number, owner_name, fine_amount, violation_id,
                            transaction_id, evidence_path=None):
    """Polite receipt for a settled challan: payment confirmation, a green safety advisory,
    the summary table, and the evidence snapshot inline when it exists on disk.
    Uses the verified SMTPS (465) transport, retries and certificate checks."""
    if not to_email or "@" not in to_email or not SENDER_PASSWORD:
        logger.warning(f"Skipping paid-challan email: invalid email ({to_email}) or missing password.")
        return False

    challan_no = f"KSP-BLR-2026-{violation_id}"
    fine_text = f"{float(fine_amount):,.2f}"
    txn = transaction_id or "-"
    subject = f"Payment Receipt & E-Challan Settlement: {plate_number} (Challan #{violation_id})"
    primary = (
        f"Thank you for promptly settling your traffic e-Challan for vehicle {plate_number}. "
        f"Your payment of \u20b9{fine_text} has been successfully received and cleared under Transaction ID {txn}."
    )

    text_body = (
        f"Dear {owner_name},\n\n"
        f"{primary}\n\n"
        f"{PAID_SAFETY_TAGLINE}\n\n"
        f"Challan Number: {challan_no}\n"
        f"Vehicle Plate: {plate_number}\n"
        f"Fine Amount Paid: \u20b9{fine_text}\n"
        f"Transaction Reference: {txn}\n"
        "Payment Status: PAID & CLOSED\n\n"
        "PARK-ALERT Automated Traffic Enforcement System\n"
    )

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; background-color: #f9f9f9; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 25px;">
            <h2 style="color:#198754; margin-top:0;">Payment Receipt &amp; E-Challan Settlement</h2>
            <p style="font-size: 16px;">Dear <strong>{owner_name}</strong>,</p>
            <p style="font-size: 15px; line-height: 1.5;">{primary}</p>
            <div style="background-color:#e8f5e9; color:#1b5e20; padding:14px 16px; border-left:4px solid #198754; border-radius:4px; margin:20px 0;">
                <p style="margin:0; font-size:14px; line-height:1.5;">{PAID_SAFETY_TAGLINE}</p>
            </div>
            <table style="width:100%; border-collapse: collapse; font-size: 15px; margin: 15px 0;">
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Challan Number</td><td style="padding:8px; border-bottom:1px solid #eee;">{challan_no}</td></tr>
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Vehicle Plate</td><td style="padding:8px; border-bottom:1px solid #eee;">{plate_number}</td></tr>
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Fine Amount Paid</td><td style="padding:8px; border-bottom:1px solid #eee; color:#198754; font-weight:bold;">&#8377;{fine_text}</td></tr>
                <tr><td style="padding:8px; border-bottom:1px solid #eee; color:#666;">Transaction Reference</td><td style="padding:8px; border-bottom:1px solid #eee; font-family: monospace;">{txn}</td></tr>
                <tr><td style="padding:8px; color:#666;">Payment Status</td><td style="padding:8px; font-weight:bold; color:#198754;">PAID &amp; CLOSED</td></tr>
            </table>
            <div style="text-align: center; margin: 20px 0;">
                <img src="cid:evidence_img" style="max-width: 100%; border-radius: 6px; border: 1px solid #ddd;" alt="Violation Snapshot" />
            </div>
            <p style="font-size: 12px; color: #888; border-top: 1px solid #eee; padding-top: 15px;">
                PARK-ALERT Automated Traffic Enforcement System
            </p>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_email
    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_content, "html", "utf-8"))

    if evidence_path and os.path.exists(evidence_path):
        with open(evidence_path, "rb") as f:
            ev = MIMEImage(f.read())
        ev.add_header("Content-ID", "<evidence_img>")
        ev.add_header("Content-Disposition", "inline", filename=os.path.basename(evidence_path))
        msg.attach(ev)
    else:
        logger.warning(f"Paid-challan email {violation_id}: no evidence image to attach.")

    return _send_with_retry(msg, to_email, f"paid challan #{violation_id}")