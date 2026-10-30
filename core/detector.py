"""
core/detector.py
-----------------
YOLOv8-based vehicle detector for PARK-ALERT.

Responsibilities:
    * Load a YOLOv8 model once and reuse it across frames (no per-frame reload).
    * Detect only the target vehicle classes: car, motorcycle, bus, truck.
    * Optionally rotate incoming frames 90 deg clockwise before inference
      (to correct a sideways-mounted camera) and transparently map the
      resulting boxes back into the ORIGINAL frame's coordinate space, so
      every downstream consumer (ROI checks, tracker, drawing) can keep
      working in one consistent coordinate system.
    * Never raise on a bad/corrupt frame or a model hiccup -- log and
      return an empty detection list instead, so a single bad frame can
      never crash or hang the video-processing loop.

Output format:
    detect(frame) -> List[Tuple[int, int, int, int, float, int]]
        Each tuple is (x1, y1, x2, y2, confidence, class_id) in the
        ORIGINAL frame's pixel coordinates.
"""

from __future__ import annotations

import logging
import time
from typing import List, Tuple, Optional

import numpy as np

try:
    import cv2
except ImportError as exc:  # pragma: no cover - environment guard
    raise ImportError(
        "opencv-python is required for core/detector.py (pip install opencv-python)"
    ) from exc

try:
    from ultralytics import YOLO
except ImportError as exc:  # pragma: no cover - environment guard
    raise ImportError(
        "ultralytics is required for core/detector.py (pip install ultralytics)"
    ) from exc


logger = logging.getLogger("park_alert.detector")
if not logger.handlers:
    # Sensible default so the module logs even if the host app hasn't
    # configured logging yet -- this makes silent freezes much easier to
    # diagnose (you will see "detector: inference failed" in the console
    # instead of the app just appearing to hang).
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# COCO class ids for the vehicle types we care about.
# (COCO: 2=car, 3=motorcycle, 5=bus, 7=truck)
DEFAULT_TARGET_CLASSES = {2, 3, 5, 7}

# Human-friendly labels, used by app.py when drawing / logging.
CLASS_NAME_MAP = {
    2: "Car",
    3: "Scooter/Bike",
    5: "Bus",
    7: "Truck",
}

Detection = Tuple[int, int, int, int, float, int]


class VehicleDetector:
    """Thin, defensive wrapper around a YOLOv8 model.

    Parameters
    ----------
    model_path:
        Path or name of the YOLOv8 weights (e.g. "yolov8n.pt").
    target_classes:
        Set of COCO class ids to keep. Defaults to car/motorcycle/bus/truck.
    conf_threshold:
        Minimum confidence to keep a detection. Raised to 0.50 by default
        to cut down on car/truck <-> two-wheeler misclassification.
    iou_threshold:
        NMS IoU threshold passed straight to the model.
    device:
        "cpu", "cuda", "cuda:0", etc. Left as None to let ultralytics decide.
    rotate_clockwise_90:
        Set True when the physical camera is mounted sideways and the raw
        frames need a 90 deg clockwise rotation before they look "upright"
        to the model. Detections are automatically rotated back into the
        ORIGINAL frame's coordinate space before being returned, so callers
        never need to think about this.
    max_consecutive_failures:
        If inference fails this many times in a row, the detector logs a
        loud warning (possible GPU/driver issue) but keeps trying on
        subsequent frames rather than raising -- a video stream should
        degrade gracefully, never lock up the Flask thread.
    """

    def __init__(
        self,
        model_path: str = "yolov8n.pt",
        target_classes: Optional[set] = None,
        conf_threshold: float = 0.50,
        iou_threshold: float = 0.45,
        device: Optional[str] = None,
        rotate_clockwise_90: bool = False,
        max_consecutive_failures: int = 10,
    ) -> None:
        self.target_classes = target_classes or set(DEFAULT_TARGET_CLASSES)
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.device = device
        self.rotate_clockwise_90 = rotate_clockwise_90
        self.max_consecutive_failures = max_consecutive_failures
        self._consecutive_failures = 0

        try:
            logger.info("Loading YOLOv8 model from '%s' ...", model_path)
            self.model = YOLO(model_path)
            if device:
                self.model.to(device)
            logger.info("YOLOv8 model loaded successfully.")
        except Exception:
            logger.exception(
                "Failed to load YOLOv8 model '%s'. Detector will be unusable "
                "until this is fixed.",
                model_path,
            )
            raise  # A missing/broken model is a fatal startup error, not a per-frame one.

    # ------------------------------------------------------------------ #
    # Rotation helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rotate_frame_cw(frame: np.ndarray) -> np.ndarray:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

    @staticmethod
    def _map_box_rotated_to_original(
        box: Tuple[float, float, float, float], original_height: int
    ) -> Tuple[int, int, int, int]:
        """Map an (x1, y1, x2, y2) box from the ROTATED frame back into the
        ORIGINAL frame's coordinate space.

        For a 90 deg clockwise rotation, a point (x, y) in the original
        image maps to (new_x, new_y) = (H - 1 - y, x) in the rotated image,
        where H is the ORIGINAL frame height. Inverting that:
            x_original = new_y
            y_original = H - 1 - new_x
        We apply this to both corners and then re-derive an axis-aligned
        box, since the corner-to-corner mapping alone can flip min/max.
        """
        rx1, ry1, rx2, ry2 = box
        h = original_height

        # Map both corners of the rotated box back.
        ox_a, oy_a = ry1, h - 1 - rx1
        ox_b, oy_b = ry2, h - 1 - rx2

        x1 = int(min(ox_a, ox_b))
        x2 = int(max(ox_a, ox_b))
        y1 = int(min(oy_a, oy_b))
        y2 = int(max(oy_a, oy_b))
        return x1, y1, x2, y2

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def detect(self, frame: Optional[np.ndarray]) -> List[Detection]:
        """Run detection on a single BGR frame.

        Always returns a list (possibly empty). Never raises -- any
        internal failure is logged and treated as "no detections this
        frame" so the caller's video loop keeps running.
        """
        # --- Guard against corrupt / missing frames -------------------
        if frame is None:
            logger.debug("detect() called with frame=None, skipping.")
            return []
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            logger.warning("detect() received an empty/invalid frame, skipping.")
            return []
        if frame.ndim != 3 or frame.shape[2] not in (3, 4):
            logger.warning(
                "detect() received a frame with unexpected shape %s, skipping.",
                getattr(frame, "shape", None),
            )
            return []

        original_height = frame.shape[0]
        infer_frame = frame
        if self.rotate_clockwise_90:
            try:
                infer_frame = self._rotate_frame_cw(frame)
            except Exception:
                logger.exception("Frame rotation failed; using un-rotated frame.")
                infer_frame = frame

        # --- Run inference, defensively -------------------------------
        try:
            results = self.model.predict(
                source=infer_frame,
                conf=self.conf_threshold,
                iou=self.iou_threshold,
                verbose=False,
            )
            self._consecutive_failures = 0
        except Exception:
            self._consecutive_failures += 1
            logger.exception(
                "YOLO inference failed (%d consecutive failures).",
                self._consecutive_failures,
            )
            if self._consecutive_failures >= self.max_consecutive_failures:
                logger.error(
                    "Detector has failed %d times in a row -- check GPU/CUDA "
                    "drivers, model file integrity, or input frame format.",
                    self._consecutive_failures,
                )
            return []

        detections: List[Detection] = []
        try:
            if not results:
                return detections
            result = results[0]
            boxes = getattr(result, "boxes", None)
            if boxes is None or len(boxes) == 0:
                return detections

            xyxy = boxes.xyxy.cpu().numpy()
            confs = boxes.conf.cpu().numpy()
            classes = boxes.cls.cpu().numpy().astype(int)

            for (x1, y1, x2, y2), conf, cls_id in zip(xyxy, confs, classes):
                if cls_id not in self.target_classes:
                    continue
                if self.rotate_clockwise_90:
                    ox1, oy1, ox2, oy2 = self._map_box_rotated_to_original(
                        (x1, y1, x2, y2), original_height
                    )
                else:
                    ox1, oy1, ox2, oy2 = int(x1), int(y1), int(x2), int(y2)

                detections.append((ox1, oy1, ox2, oy2, float(conf), int(cls_id)))
        except Exception:
            logger.exception("Failed while parsing YOLO results; returning no detections.")
            return []

        return detections

    def label_for(self, class_id: int) -> str:
        return CLASS_NAME_MAP.get(class_id, f"class_{class_id}")


# ---------------------------------------------------------------------- #
# Quick manual smoke test: `python -m core.detector path/to/video.mp4`
# ---------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    video_path = sys.argv[1] if len(sys.argv) > 1 else 0
    cap = cv2.VideoCapture(video_path)
    detector = VehicleDetector(rotate_clockwise_90=False)

    frame_count = 0
    start = time.time()
    while True:
        ok, frame = cap.read()
        if not ok:
            print("End of stream / read failure, stopping smoke test.")
            break
        dets = detector.detect(frame)
        frame_count += 1
        if frame_count % 30 == 0:
            elapsed = time.time() - start
            print(f"Frame {frame_count}: {len(dets)} detections | {frame_count/elapsed:.1f} FPS")
        if frame_count > 300:
            break
    cap.release()