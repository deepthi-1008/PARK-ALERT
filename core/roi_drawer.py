import cv2
import json
import numpy as np
import os

# File paths
VIDEO_PATH = os.path.join("static", "videos", "VID-20260815-WA0009.mp4")
CONFIG_DIR = "config"
CONFIG_PATH = os.path.join(CONFIG_DIR, "zone_config.json")

# Global variables for mouse interaction
points = []


def mouse_callback(event, x, y, flags, param):
    """Mouse event listener to capture click coordinates."""
    global points
    
    if event == cv2.EVENT_LBUTTONDOWN:
        points.append([x, y])
        print(f"[+] Point captured: ({x}, {y})")


def save_zone_config(roi_points):
    """Saves polygon corner coordinates to JSON configuration file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    
    config_data = {
        "zone_name": "No-Parking Enforcement Zone 1",
        "polygon_points": roi_points
    }
    
    with open(CONFIG_PATH, "w") as f:
        json.dump(config_data, f, indent=4)
        
    print(f"\n[✓] ROI Polygon saved successfully to: {CONFIG_PATH}")


def run_roi_drawer(video_path=VIDEO_PATH):
    global points
    points = [] # Reset points

    # Ensure video file exists
    if not os.path.exists(video_path):
        print(f"[!] Error: Video file not found at '{video_path}'. Please check file path.")
        return

    # Open video feed and read the first frame
    cap = cv2.VideoCapture(video_path)
    ret, frame = cap.read()
    cap.release()

    if not ret:
        print("[!] Error: Failed to extract frame from video stream.")
        return

    window_name = "ROI Selection Utility - Click points | 'c': Clear | 's': Save | 'q': Quit"
    cv2.namedWindow(window_name)
    cv2.setMouseCallback(window_name, mouse_callback)

    while True:
        display_frame = frame.copy()

        # 1. Draw circles at all clicked points
        for pt in points:
            cv2.circle(display_frame, (pt[0], pt[1]), 5, (0, 255, 0), -1)

        # 2. Draw lines connecting points if 2 or more exist
        if len(points) >= 2:
            for i in range(len(points) - 1):
                cv2.line(display_frame, tuple(points[i]), tuple(points[i+1]), (0, 0, 255), 2)

        # 3. Draw closed polygon & translucent red overlay if 3 or more exist
        if len(points) >= 3:
            cv2.line(display_frame, tuple(points[-1]), tuple(points[0]), (0, 0, 255), 2)
            
            overlay = display_frame.copy()
            pts_np = np.array(points, np.int32).reshape((-1, 1, 2))
            cv2.fillPoly(overlay, [pts_np], (0, 0, 255))
            cv2.addWeighted(overlay, 0.3, display_frame, 0.7, 0, display_frame)

        # Render window
        cv2.imshow(window_name, display_frame)
        key = cv2.waitKey(20) & 0xFF

        # Keyboard controls
        if key == ord('c'):
            points = []
            print("[*] Polygon selection cleared.")
        elif key == ord('s'):
            if len(points) >= 3:
                save_zone_config(points)
                break
            else:
                print("[!] Need at least 3 points to define a valid polygon!")
        elif key == ord('q') or key == 27: # 'q' or Esc key
            print("[*] Exited without saving.")
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    print("--- ROI Selection Utility Initialized ---")
    print("Instructions:")
    print(" 1. Left-click anywhere on the image to place polygon corners.")
    print(" 2. Press 'c' to clear selected points.")
    print(" 3. Press 's' to save zone coordinates to config/zone_config.json.")
    print(" 4. Press 'q' or Esc to exit.\n")
    
    run_roi_drawer()