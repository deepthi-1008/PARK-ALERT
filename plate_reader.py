"""
core/plate_reader.py
---------------------
ANPR (Automatic Number Plate Recognition) helper.

v4 changes:

  1. Structure-aware normalizer (`normalize_indian_plate`) now accepts BOTH
     common Indian shapes without breaking either:
         10 chars: [State 2][District 2][Series 2][Number 4]   e.g. KA37EC2109
          9 chars: [State 2][District 2][Series 1][Number 4]   e.g. KA28W5310
     and also 8 chars ([District 1][Series 1]) for rare single-digit RTO codes.
     Rather than trimming the string to a fixed length, every plausible
     window (with up to 2 leading characters and any trailing characters
     dropped) is parsed under every slot split, and the reading that needs
     the fewest character corrections -- and the least trimming -- wins.
     Full-length, no-trim readings are always preferred, so a trailing '9'
     is never silently dropped from 'KA37E02109'.

  2. Stacked-row assembly (`_assemble_candidates`) is rewritten:
       * duplicate detections of the same text on the same box are removed,
       * rows are clustered by vertical (Y) centre using a running row mean,
         so a short bottom row (e.g. "W 5310") is not chained into the top
         row by drift,
       * fragments within a row are ordered by their left (X) edge,
       * candidates are emitted per row, as the full top-to-bottom join, and
         as adjacent-row joins.

  3. Each OCR pass (enhanced, dark-background, binarized) is assembled on
     its OWN detections and the candidate lists are merged afterwards.
     Previously the detections from all passes were pooled before row
     grouping, so the same characters could be concatenated twice in a row.

  4. Everything from v3 is kept: CLAHE contrast enhancement, the
     dark/green-background pass with stroke-closing, confusion-pair variant
     generation, and transposition variants within the number segment.

This file only produces candidate readings. Every match is verified against
the database in app.py, and a candidate is only accepted if exactly one
registered vehicle qualifies.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import List, Optional

import cv2
import numpy as np

logger = logging.getLogger("park_alert.plate_reader")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

try:
    import easyocr
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False
    logger.warning("easyocr is not installed (pip install easyocr) -- plate OCR will be disabled.")

# Standard Indian plate shape. Series is 1 or 2 letters; number is exactly 4 digits.
# District is 1 or 2 digits.
_PLATE_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,2}[0-9]{4}$")

_OCR_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# --------------------------------------------------------------------- #
# Known OCR confusion pairs (bidirectional). Shared with app.py's fuzzy
# DB-match step, so "known confusion" means the same thing in both places.
# --------------------------------------------------------------------- #
CONFUSION_PAIRS = frozenset({
    frozenset(("0", "O")), frozenset(("0", "D")), frozenset(("0", "Q")), frozenset(("0", "C")),
    frozenset(("1", "I")), frozenset(("1", "L")),
    frozenset(("2", "Z")),
    frozenset(("5", "S")),
    frozenset(("8", "B")),
    frozenset(("6", "G")),
    frozenset(("4", "A")),
    frozenset(("K", "R")),
    # Digit-digit glyph confusions -- broken/faint strokes on worn or
    # green EV plates routinely close/open a loop and flip these pairs.
    frozenset(("6", "0")),
    frozenset(("8", "0")),
    frozenset(("3", "8")),
})

# Adjacency form of CONFUSION_PAIRS, built once, used to generate
# single-position substitution variants of a candidate string.
_CONFUSION_ADJACENCY: dict = {}
for _pair in CONFUSION_PAIRS:
    _a, _b = tuple(_pair)
    _CONFUSION_ADJACENCY.setdefault(_a, set()).add(_b)
    _CONFUSION_ADJACENCY.setdefault(_b, set()).add(_a)


def generate_confusion_variants(s: str, limit: int = 30) -> List[str]:
    """All single-position substitutions of `s` using the known OCR
    confusion graph (never more than one character changed per variant)."""
    if not s:
        return []
    variants = []
    for i, ch in enumerate(s):
        for alt in _CONFUSION_ADJACENCY.get(ch, ()):
            variants.append(s[:i] + alt + s[i + 1:])
            if len(variants) >= limit:
                return variants
    return variants


def generate_transposition_variants(s: str) -> List[str]:
    """Adjacent-character transposition variants within the trailing
    4-digit NUMBER segment only (e.g. 'KA29ES1836' -> 'KA29ES1863')."""
    if len(s) < 4:
        return []
    variants = []
    start = len(s) - 4
    for i in range(start, len(s) - 1):
        variants.append(s[:i] + s[i + 1] + s[i] + s[i + 2:])
    return variants


# Position-specific correction maps: each slot must contain a particular
# character class, so a character of the wrong class is corrected via the
# matching confusion table.
_DIGIT_TO_LETTER_STATE = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}
_LETTER_TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "C": "0"}
_DIGIT_TO_LETTER_SERIES = {"0": "C", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "8": "B"}

# Whole-block overrides for the 2-char STATE slot -- covers compound
# misreads (e.g. K misread as R *and* A misread as 4 at once -> "R4")
# that a single-character map can't fix on its own.
_STATE_OVERRIDES = {
    "R4": "KA", "CR": "KA", "GA": "KA", "K4": "KA", "RA": "KA", "RK": "KA",
}


def _parse_window(window: str, district_len: int, series_len: int):
    """Apply the slot-specific corrections to `window` under one fixed
    slot split. Returns (corrected_plate, hamming_cost) or None if the
    corrected result is not a valid plate shape."""
    state = window[0:2]
    district = window[2:2 + district_len]
    series = window[2 + district_len:2 + district_len + series_len]
    number = window[-4:]

    state_fixed = _STATE_OVERRIDES.get(state, "".join(_DIGIT_TO_LETTER_STATE.get(c, c) for c in state))
    district_fixed = "".join(_LETTER_TO_DIGIT.get(c, c) for c in district)
    series_fixed = "".join(_DIGIT_TO_LETTER_SERIES.get(c, c) for c in series)
    number_fixed = "".join(_LETTER_TO_DIGIT.get(c, c) for c in number)

    corrected = state_fixed + district_fixed + series_fixed + number_fixed
    if len(corrected) != len(window) or not _PLATE_PATTERN.match(corrected):
        return None
    hamming = sum(1 for a, b in zip(window, corrected) if a != b)
    return corrected, hamming


def normalize_indian_plate(raw: str) -> Optional[str]:
    """Reconstruct a standard Indian plate from a raw OCR string.

    Handles 10-char (2-letter series) and 9-char (1-letter series) plates.
    Each candidate window is parsed under every valid slot split, and the
    reading with the lowest cost wins, where:
        cost = (characters corrected) + 2 * (leading/trailing chars dropped)
    A full-length reading with no dropped characters is therefore always
    preferred over a shorter reading that drops a character. On an exact
    cost tie, a 2-digit district is preferred.

    Returns None if no window can be reshaped into a valid plate. It never
    fabricates a plate from nothing."""
    if not raw:
        return None
    s = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if len(s) < 8:
        return None

    best_key = None
    best_plate = None
    for total in (10, 9, 8):
        for start in range(0, min(2, len(s) - total) + 1):
            end = start + total
            if end > len(s):
                continue
            window = s[start:end]
            drop_penalty = 2 * (start + (len(s) - end))
            for district_len, series_len in ((2, 2), (2, 1), (1, 2), (1, 1)):
                if 2 + district_len + series_len + 4 != total:
                    continue
                parsed = _parse_window(window, district_len, series_len)
                if parsed is None:
                    continue
                plate, hamming = parsed
                key = (hamming + drop_penalty, 0 if district_len == 2 else 1)
                if best_key is None or key < best_key:
                    best_key = key
                    best_plate = plate
    return best_plate


class PlateReader:
    """Wraps EasyOCR for vehicle-plate text extraction."""

    def __init__(self, use_gpu: bool = False, min_confidence: float = 0.25):
        self.min_confidence = min_confidence
        self._lock = threading.Lock()
        self.reader = None

        if not _EASYOCR_AVAILABLE:
            return

        try:
            self.reader = easyocr.Reader(["en"], gpu=use_gpu, verbose=False)
            logger.info("EasyOCR initialized (gpu=%s).", use_gpu)
        except Exception:
            logger.exception("Failed to initialize EasyOCR -- plate OCR disabled.")
            self.reader = None

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", text.upper())

    # ------------------------------------------------------------------ #
    # Preprocessing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _enhance(gray: np.ndarray) -> np.ndarray:
        """CLAHE contrast enhancement for faint/low-contrast plates."""
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        enhanced = cv2.bilateralFilter(enhanced, 5, 40, 40)
        return enhanced

    @staticmethod
    def _binarize(enhanced: np.ndarray) -> np.ndarray:
        thresh = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 10
        )
        # Close small gaps in character strokes (a faint/broken '0' that
        # would otherwise look like an open '6', 'c', etc.).
        kernel = np.ones((2, 2), np.uint8)
        return cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)

    @staticmethod
    def _dark_background_mask(crop_bgr: np.ndarray, gray: np.ndarray) -> Optional[np.ndarray]:
        """Contrast pass for a dark/green plate background (EV plates: dark
        green background, white lettering). Triggers on a high green-channel
        ratio OR generally dark luminance. Returns None for an ordinary
        bright-background plate, so the pass is skipped."""
        try:
            hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
            lower_green = np.array([35, 40, 40])
            upper_green = np.array([85, 255, 255])
            green_mask = cv2.inRange(hsv, lower_green, upper_green)

            total_px = green_mask.shape[0] * green_mask.shape[1]
            green_ratio = (cv2.countNonZero(green_mask) / float(total_px)) if total_px else 0.0
            mean_luminance = float(np.mean(gray))

            is_green = green_ratio >= 0.15
            is_dark_inverted = mean_luminance < 100.0

            if not (is_green or is_dark_inverted):
                return None

            if is_green:
                ys, xs = np.where(green_mask > 0)
                if len(xs) and len(ys):
                    x1, x2, y1, y2 = xs.min(), xs.max(), ys.min(), ys.max()
                    region = hsv[:, :, 2][y1:y2 + 1, x1:x2 + 1] if x2 > x1 and y2 > y1 else gray
                else:
                    region = gray
            else:
                region = gray

            _, binary = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            kernel = np.ones((2, 2), np.uint8)
            binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
            return binary
        except Exception:
            logger.exception("Dark/green-background mask extraction failed; skipping that pass.")
            return None

    # ------------------------------------------------------------------ #
    # OCR
    # ------------------------------------------------------------------ #
    def _run_ocr(self, image: np.ndarray) -> List[dict]:
        """Run EasyOCR on one image and return detections with their
        bounding-box geometry: x0/x1 (left/right), y0/y1 (top/bottom),
        cx/cy (centre) and h (height)."""
        try:
            with self._lock:  # serialize EasyOCR calls across threads
                raw_results = self.reader.readtext(image, allowlist=_OCR_ALLOWLIST)
        except Exception:
            logger.exception("EasyOCR inference failed.")
            return []

        out = []
        for bbox, text, conf in raw_results:
            if conf < self.min_confidence:
                continue
            cleaned = self._clean(text)
            if not cleaned:
                continue
            xs = [float(p[0]) for p in bbox]
            ys = [float(p[1]) for p in bbox]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            out.append({
                "text": cleaned,
                "conf": float(conf),
                "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                "cx": (x0 + x1) / 2.0,
                "cy": (y0 + y1) / 2.0,
                "h": y1 - y0,
            })
        return out

    @staticmethod
    def _box_iou(a: dict, b: dict) -> float:
        ix0, iy0 = max(a["x0"], b["x0"]), max(a["y0"], b["y0"])
        ix1, iy1 = min(a["x1"], b["x1"]), min(a["y1"], b["y1"])
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        if inter <= 0:
            return 0.0
        area_a = (a["x1"] - a["x0"]) * (a["y1"] - a["y0"])
        area_b = (b["x1"] - b["x0"]) * (b["y1"] - b["y0"])
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0

    @classmethod
    def _dedupe_detections(cls, detections: List[dict]) -> List[dict]:
        """Drop a detection when a higher-confidence detection has the same
        text on an overlapping box (EasyOCR occasionally reports one glyph
        twice)."""
        kept: List[dict] = []
        for d in sorted(detections, key=lambda d: -d["conf"]):
            if any(k["text"] == d["text"] and cls._box_iou(k, d) > 0.5 for k in kept):
                continue
            kept.append(d)
        return kept

    @classmethod
    def _assemble_candidates(cls, detections: List[dict]) -> List[str]:
        """Assemble stacked-plate text from one pass's detections.

        1. Remove duplicate detections.
        2. Cluster detections into rows by vertical centre. A detection joins
           the current row when its centre is within half a median text
           height of the row's mean centre. Using the running mean (not the
           last item) stops a short bottom row from drifting into the top row.
        3. Within each row, order fragments left-to-right by their left edge.
        4. Emit each row alone, the full top-to-bottom join, and the
           adjacent-row joins (covers a stray extra row)."""
        dets = cls._dedupe_detections(detections)
        if not dets:
            return []

        heights = [d["h"] for d in dets if d["h"] > 0]
        row_gap = 0.5 * float(np.median(heights)) if heights else 8.0

        dets.sort(key=lambda d: d["cy"])
        rows: List[dict] = []
        for d in dets:
            if rows and abs(d["cy"] - rows[-1]["cy"]) <= row_gap:
                rows[-1]["items"].append(d)
                rows[-1]["cy"] = sum(i["cy"] for i in rows[-1]["items"]) / len(rows[-1]["items"])
            else:
                rows.append({"cy": d["cy"], "items": [d]})

        row_strings = [
            "".join(i["text"] for i in sorted(r["items"], key=lambda i: i["x0"]))
            for r in rows
        ]
        row_strings = [r for r in row_strings if r]

        candidates = list(row_strings)
        if len(row_strings) > 1:
            candidates.append("".join(row_strings))
            for i in range(len(row_strings) - 1):
                candidates.append(row_strings[i] + row_strings[i + 1])

        seen = set()
        unique = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def read_plate_candidates(self, native_crop: Optional[np.ndarray]) -> List[str]:
        """Run OCR on a vehicle crop (from a NATIVE-resolution frame) and
        return an ORDERED list (best first) of plausible plate strings:
        normalized (positionally corrected) readings first, then raw
        assemblies, then confusion/transposition variants of the
        well-formed ones. Callers verify these against the database."""
        if self.reader is None:
            return []
        if native_crop is None or not isinstance(native_crop, np.ndarray) or native_crop.size == 0:
            return []

        try:
            gray = cv2.cvtColor(native_crop, cv2.COLOR_BGR2GRAY)
            enhanced = self._enhance(gray)
        except Exception:
            logger.exception("Preprocessing failed on a vehicle crop.")
            return []

        # Each pass keeps its own detections so rows are assembled from
        # one image at a time (see v4 note 3).
        passes: List[List[dict]] = [self._run_ocr(enhanced)]

        dark_bin = self._dark_background_mask(native_crop, gray)
        if dark_bin is not None:
            passes.append(self._run_ocr(dark_bin))

        longest = max((len(d["text"]) for p in passes for d in p), default=0)
        if longest < 4:
            try:
                passes.append(self._run_ocr(self._binarize(enhanced)))
            except Exception:
                logger.exception("Binarization/second OCR pass failed.")

        raw_candidates: List[str] = []
        seen_raw = set()
        for dets in passes:
            for c in self._assemble_candidates(dets):
                if c not in seen_raw:
                    seen_raw.add(c)
                    raw_candidates.append(c)

        ordered: List[str] = []
        for c in raw_candidates:
            norm = normalize_indian_plate(c)
            if norm:
                ordered.append(norm)
        ordered.extend(raw_candidates)

        variant_pool: List[str] = []
        for c in list(ordered):
            if _PLATE_PATTERN.match(c):
                variant_pool.extend(generate_confusion_variants(c))
                variant_pool.extend(generate_transposition_variants(c))
        ordered.extend(variant_pool)

        seen = set()
        unique_ordered = []
        for c in ordered:
            if c and c not in seen:
                seen.add(c)
                unique_ordered.append(c)
        return unique_ordered

    def read_plate(self, native_crop: Optional[np.ndarray]) -> Optional[str]:
        """Convenience wrapper: best single candidate, or None."""
        candidates = self.read_plate_candidates(native_crop)
        return candidates[0] if candidates else None