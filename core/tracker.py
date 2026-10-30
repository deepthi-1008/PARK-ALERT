import os
import json
import cv2
import numpy as np
from ultralytics import YOLO


class ZoneTracker:
    def __init__(
        self,
        model_path="yolov8n.pt",
        config_path=os.path.join("config", "zone_config.json"),
        on_dwell_trigger=None,       # NEW: callback(track_id, cls_id, vehicle_type, native_crop, elapsed_seconds, start_frame)
        ocr_trigger_seconds=20.0,    # NEW: fire on_dwell_trigger once dwell crosses this
        on_dwell_tick=None,          # NEW: callback(track_id, cls_id, vehicle_type, elapsed_seconds, start_frame) once per second past 30s
    ):
        try:
            self.model = YOLO(model_path)
        except Exception:
            self.model = YOLO("yolov8n.pt")

        self.config_path = config_path
        self.vehicle_classes = [2, 3, 5, 7]  # Cars, Motorcycles, Buses, Trucks
        self.confidence_threshold = 0.15

        # Dwell timing memory maps
        self.dwell_start_frames = {}
        self.bbox_history = {}
        self.last_seen_frames = {}

        # Occlusion Grace Period: 150 frames (~5 seconds of memory to handle rider interaction)
        self.max_lost_frames = 150

        # --- NEW: async ANPR dispatch bookkeeping ---------------------
        self.on_dwell_trigger = on_dwell_trigger
        self.on_dwell_tick = on_dwell_tick
        # (track_id, start_frame) -> last whole-second dwell value reported
        # to on_dwell_tick, so the callback fires at most once per second.
        self._last_tick_second = {}
        self.ocr_trigger_seconds = ocr_trigger_seconds
        # track_id -> start_frame for which we've already fired the ANPR
        # trigger, so each dwell episode only fires once.
        self._ocr_triggered_start_frame = {}
        # Native (raw, un-resized) frame + scale factors to map a bbox from
        # the (possibly resized) tracking frame back into native-resolution
        # pixel coordinates for cropping. Set once per frame via
        # set_frame_context() before calling track_zone_dwell().
        self._native_frame = None
        self._scale_x = 1.0
        self._scale_y = 1.0

    # --- NEW -----------------------------------------------------------
    def set_frame_context(self, native_frame, scale_x=1.0, scale_y=1.0):
        """Give the tracker access to the current NATIVE-resolution capture
        frame (pre-resize, pre-compression) plus the scale factors needed
        to map a bbox from the tracking frame back into that native frame.
        Call this once per frame, right before track_zone_dwell()."""
        self._native_frame = native_frame
        self._scale_x = scale_x
        self._scale_y = scale_y

    def load_roi_polygon(self, frame_w, frame_h):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r") as f:
                    data = json.load(f)

                if "roi_polygon_normalized" in data:
                    norm_pts = data["roi_polygon_normalized"]
                    pixel_pts = [[int(pt[0] * frame_w), int(pt[1] * frame_h)] for pt in norm_pts]
                    return np.array(pixel_pts, np.int32).reshape((-1, 1, 2))
                elif "roi_polygon" in data:
                    pts = data["roi_polygon"]
                    return np.array(pts, np.int32).reshape((-1, 1, 2))
            except Exception:
                pass

        default_pts = [[0.1, 0.2], [0.9, 0.2], [0.9, 0.9], [0.1, 0.9]]
        pixel_pts = [[int(x * frame_w), int(y * frame_h)] for x, y in default_pts]
        return np.array(pixel_pts, np.int32).reshape((-1, 1, 2))

    def compute_iou(self, boxA, boxB):
        """Calculate Bounding Box Intersection over Union (IoU)."""
        xA = max(boxA[0], boxB[0])
        yA = max(boxA[1], boxB[1])
        xB = min(boxA[2], boxB[2])
        yB = min(boxA[3], boxB[3])

        interArea = max(0, xB - xA) * max(0, yB - yA)
        if interArea == 0:
            return 0.0

        boxAArea = (boxA[2] - boxA[0]) * (boxA[3] - boxA[1])
        boxBArea = (boxB[2] - boxB[0]) * (boxB[3] - boxB[1])

        return interArea / float(boxAArea + boxBArea - interArea)

    # --- NEW -------------------------------------------------------------
    def _maybe_dispatch_anpr(self, track_id, cls_id, vehicle_type, x1, y1, x2, y2, elapsed_seconds, start_frame):
        """If this track has just crossed the ANPR trigger threshold for
        the CURRENT dwell episode, crop it out of the native-resolution
        frame and hand it to the registered callback. The callback is
        expected to do the actual (slow) work -- OCR, DB lookup, email --
        on a background thread (e.g. via ThreadPoolExecutor.submit), so
        this method only ever does a cheap numpy slice + copy and returns
        immediately. Never raises -- a failure here must not break tracking.
        """
        if self.on_dwell_trigger is None or self._native_frame is None:
            return
        if elapsed_seconds < self.ocr_trigger_seconds:
            return
        if self._ocr_triggered_start_frame.get(track_id) == start_frame:
            return  # already dispatched for this dwell episode

        self._ocr_triggered_start_frame[track_id] = start_frame

        try:
            nh, nw = self._native_frame.shape[:2]
            nx1 = int(x1 * self._scale_x)
            ny1 = int(y1 * self._scale_y)
            nx2 = int(x2 * self._scale_x)
            ny2 = int(y2 * self._scale_y)

            # Small padding so the full plate (front apron/cowl included)
            # stays inside the crop, without hardcoding where the plate is.
            pad_x = int((nx2 - nx1) * 0.08)
            pad_y = int((ny2 - ny1) * 0.08)
            nx1 = max(0, nx1 - pad_x)
            ny1 = max(0, ny1 - pad_y)
            nx2 = min(nw, nx2 + pad_x)
            ny2 = min(nh, ny2 + pad_y)

            if nx2 <= nx1 or ny2 <= ny1:
                return

            # Copy -- the underlying native frame buffer will be
            # overwritten on the next loop iteration.
            native_crop = self._native_frame[ny1:ny2, nx1:nx2].copy()
        except Exception:
            return

        try:
            self.on_dwell_trigger(track_id, cls_id, vehicle_type, native_crop, elapsed_seconds, start_frame)
        except Exception:
            pass  # a bad callback must never take down the tracker

    def _maybe_dwell_tick(self, track_id, cls_id, vehicle_type, elapsed_seconds, start_frame):
        """Once a vehicle has reached the 30s violation threshold, report its
        whole-second dwell time whenever that value changes, so the violation
        row can keep its duration and fine up to date. Runs every frame but only
        calls the callback about once per second per vehicle. Never raises."""
        if self.on_dwell_tick is None or elapsed_seconds < 30:
            return
        key = (track_id, start_frame)
        if self._last_tick_second.get(key) == elapsed_seconds:
            return
        self._last_tick_second[key] = elapsed_seconds
        try:
            self.on_dwell_tick(track_id, cls_id, vehicle_type, elapsed_seconds, start_frame)
        except Exception:
            pass  # a bad callback must never take down the tracker

    def track_zone_dwell(self, frame, frame_count=0, fps=30.0):
        if frame is None:
            return frame

        h, w, _ = frame.shape
        annotated_frame = frame.copy()

        # Draw No-Parking Boundary Polygon in BLACK
        roi_polygon = self.load_roi_polygon(w, h)
        if roi_polygon is not None:
            cv2.polylines(annotated_frame, [roi_polygon], isClosed=True, color=(0, 0, 0), thickness=3)
            cv2.putText(annotated_frame, "RESTRICTED NO-PARKING ZONE",
                        (roi_polygon[0][0][0], max(30, roi_polygon[0][0][1] - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

        effective_fps = max(1.0, float(fps))

        try:
            results = self.model.track(
                frame,
                persist=True,
                classes=self.vehicle_classes,
                conf=self.confidence_threshold,
                iou=0.45,
                verbose=False
            )

            if len(results) > 0 and results[0].boxes is not None:
                boxes = results[0].boxes
                for box in boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    track_id = int(box.id[0]) if box.id is not None else 1
                    cls_id = int(box.cls[0]) if box.cls is not None else 2

                    cx = int((x1 + x2) / 2)
                    cy = int((y1 + y2) / 2)

                    # Check if vehicle centroid is inside polygon boundary
                    is_inside = cv2.pointPolygonTest(roi_polygon, (cx, cy), False) >= 0 if roi_polygon is not None else False
                    if not is_inside:
                        continue

                    # Robust Spatial Overlap (IoU) & Proximity Memory Match
                    if track_id not in self.dwell_start_frames:
                        matched_old_id = None
                        best_score = 0.0

                        for old_id, old_box in list(self.bbox_history.items()):
                            last_seen = self.last_seen_frames.get(old_id, 0)
                            if (frame_count - last_seen) <= self.max_lost_frames:
                                # Calculate bounding box overlap
                                iou = self.compute_iou([x1, y1, x2, y2], old_box)
                                old_cx = int((old_box[0] + old_box[2]) / 2)
                                old_cy = int((old_box[1] + old_box[3]) / 2)
                                dist = np.hypot(cx - old_cx, cy - old_cy)

                                # Match if bounding boxes overlap OR centroids are within 250px
                                if iou > 0.15 or dist < 250:
                                    score = iou + (1.0 / (dist + 1.0))
                                    if score > best_score:
                                        best_score = score
                                        matched_old_id = old_id

                        if matched_old_id and matched_old_id in self.dwell_start_frames:
                            # Inherit original start frame from before rider interaction
                            self.dwell_start_frames[track_id] = self.dwell_start_frames[matched_old_id]
                        else:
                            # Genuine new vehicle arrival
                            self.dwell_start_frames[track_id] = frame_count

                    # Update bounding box & frame history
                    self.bbox_history[track_id] = [x1, y1, x2, y2]
                    self.last_seen_frames[track_id] = frame_count

                    start_frame = self.dwell_start_frames[track_id]

                    # Continuous Dwell Timer Calculation
                    elapsed_frames = frame_count - start_frame
                    elapsed_seconds = int(elapsed_frames / effective_fps)

                    class_names = {2: "Car", 3: "Scooter/Bike", 5: "Bus", 7: "Truck"}
                    vehicle_type = class_names.get(cls_id, "Scooter/Bike")

                    # Color Transition: Yellow (<30s) -> Red (>=30s)
                    if elapsed_seconds >= 30:
                        box_color = (0, 0, 255)  # Red (BGR)
                        status_str = f"Time: {elapsed_seconds}s | PARKING VIOLATION!"
                    else:
                        box_color = (0, 255, 255)  # Yellow (BGR)
                        status_str = f"Time: {elapsed_seconds}s / 30s"

                    # Visual Rendering
                    cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), box_color, 2)
                    cv2.circle(annotated_frame, (cx, cy), 5, (255, 0, 0), -1)
                    cv2.putText(annotated_frame, f"ID #{track_id} ({vehicle_type}) | {status_str}",
                                (x1, max(20, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, box_color, 2)

                    # --- NEW: fire async ANPR dispatch at the configured threshold ---
                    self._maybe_dispatch_anpr(
                        track_id, cls_id, vehicle_type, x1, y1, x2, y2, elapsed_seconds, start_frame
                    )
                    self._maybe_dwell_tick(track_id, cls_id, vehicle_type, elapsed_seconds, start_frame)

        except Exception:
            pass

        return annotated_frame