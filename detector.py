import cv2
import numpy as np
from ultralytics import YOLO


class VehicleTracker:
    """
    YOLOv8 + ByteTrack Integration Module for PARK-ALERT.
    Detects target vehicle classes and tracks persistent IDs across frames.
    """
    def __init__(self, model_path="yolov8n.pt", confidence=0.3):
        # 1. Load YOLOv8 pre-trained model weights
        self.model = YOLO(model_path)
        self.confidence = confidence

        # COCO Class IDs for vehicles: car (2), motorcycle (3), bus (5), truck (7)
        self.target_classes = [2, 3, 5, 7]

    def process_frame(self, frame):
        """
        Runs YOLOv8 detection and ByteTrack tracking on a single frame.
        Returns annotated frame and list of tracked vehicle details.
        """
        if frame is None:
            return None, []

        # 2. Execute ByteTrack tracking via Ultralytics API
        results = self.model.track(
            source=frame,
            persist=True,               # Maintain tracking state across consecutive frames
            tracker="bytetrack.yaml",   # Use ByteTrack tracking algorithm
            classes=self.target_classes,# Filter target vehicle classes
            conf=self.confidence,
            verbose=False               # Suppress console logging output
        )

        tracked_vehicles = []

        if results and len(results) > 0 and results[0].boxes is not None:
            boxes = results[0].boxes

            # Verify that ByteTrack assigned tracking IDs
            if boxes.id is not None:
                track_ids = boxes.id.int().cpu().tolist()
                coords = boxes.xyxy.cpu().numpy()
                cls_ids = boxes.cls.int().cpu().tolist()
                confs = boxes.conf.cpu().tolist()

                for track_id, box, cls_id, conf in zip(track_ids, coords, cls_ids, confs):
                    x1, y1, x2, y2 = map(int, box)

                    # Calculate bounding box centroid (center point)
                    cx = int((x1 + x2) / 2)
                    cy = int((y1 + y2) / 2)

                    class_name = self.model.names[cls_id]

                    tracked_vehicles.append({
                        "track_id": track_id,
                        "class_name": class_name,
                        "bbox": (x1, y1, x2, y2),
                        "centroid": (cx, cy),
                        "confidence": float(conf)
                    })

        annotated_frame = results[0].plot() if results else frame
        return annotated_frame, tracked_vehicles


if __name__ == "__main__":
    # Self-test block when running detector module directly
    tracker = VehicleTracker(model_path="yolov8n.pt")
    
    # Open default camera stream or video file
    cap = cv2.VideoCapture(0)

    print("\n==================================================")
    print("   YOLOV8 + BYTETRACK VEHICLE TRACKER ACTIVE      ")
    print("==================================================")
    print(" Press 'q' in the display window to exit.\n")

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        annotated_frame, vehicles = tracker.process_frame(frame)

        # Render track ID and centroid overlay
        for v in vehicles:
            cv2.circle(annotated_frame, v["centroid"], 5, (0, 255, 0), -1)
            cv2.putText(
                annotated_frame, 
                f"ID: #{v['track_id']} ({v['class_name']})", 
                (v["bbox"][0], v["bbox"][1] - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2
            )

        cv2.imshow("PARK-ALERT: YOLOv8 + ByteTrack Tracker", annotated_frame)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()