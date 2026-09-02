import cv2
import os
import json
import numpy as np
from ultralytics import YOLO

# File paths
VIDEO_PATH = os.path.join("static", "videos", "VID-20260815-WA0009.mp4")
CONFIG_PATH = os.path.join("config", "zone_config.json")


def load_zone_polygon():
    """Loads polygon points from config/zone_config.json."""
    if not os.path.exists(CONFIG_PATH):
        print(f"[!] Config file not found at {CONFIG_PATH}. Please run core/roi_drawer.py first.")
        return None
    
    with open(CONFIG_PATH, "r") as f:
        data = json.load(f)
        
    pts = np.array(data["polygon_points"], np.int32).reshape((-1, 1, 2))
    return pts


def run_tracker(video_path=VIDEO_PATH):
    """Processes video stream using tuned YOLOv8 + ByteTrack parameters."""
    if not os.path.exists(video_path):
        print(f"[!] Video file not found: {video_path}")
        return

    print("[*] Loading YOLOv8 model...")
    model = YOLO("yolov8n.pt")  # Binary weights file loaded directly by PyTorch

    zone_pts = load_zone_polygon()
    cap = cv2.VideoCapture(video_path)
    window_name = "Vehicle Detection & Tracking (Tuned for Stability)"

    # Target classes: car(2), motorcycle/scooter(3), bus(5), truck(7)
    VEHICLE_CLASSES = [2, 3, 5, 7]

    print("[✓] Tracker running with lowered confidence threshold (0.15) for smooth two-wheeler tracking.")

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            print("[*] End of video stream.")
            break

        # TUNED YOLOv8 PARAMETERS TO STOP FLICKERING:
        # conf=0.15 -> Lower confidence catches smaller scooters consistently
        # iou=0.5   -> Smooths bounding box overlaps
        results = model.track(
            source=frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=VEHICLE_CLASSES,
            conf=0.15,      # FIXES FLICKERING on small/moving scooters
            iou=0.5,        # Helps hold tracking ID across frames
            verbose=False
        )

        # Draw the saved No-Parking zone polygon
        if zone_pts is not None:
            cv2.polylines(frame, [zone_pts], isClosed=True, color=(0, 0, 255), thickness=2)

        # Extract tracking bounding boxes and IDs
        if results[0].boxes is not None and results[0].boxes.id is not None:
            boxes = results[0].boxes.xyxy.cpu().numpy()
            track_ids = results[0].boxes.id.cpu().numpy().astype(int)
            class_ids = results[0].boxes.cls.cpu().numpy().astype(int)

            for box, track_id, cls_id in zip(boxes, track_ids, class_ids):
                x1, y1, x2, y2 = map(int, box)
                
                # Bottom-center coordinate (representing tire position)
                bottom_center = (int((x1 + x2) / 2), int(y2))

                # Draw bounding box & tracking ID label
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.circle(frame, bottom_center, 5, (255, 0, 0), -1)
                
                label = f"ID: #{track_id} ({model.names[cls_id]})"
                cv2.putText(frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

        # Render display window
        cv2.imshow(window_name, frame)

        # Exit on 'q' key
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    run_tracker()