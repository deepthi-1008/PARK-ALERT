import cv2
import json
import os
import sys
import numpy as np

CONFIG_PATH = os.path.join("config", "zone_config.json")
os.makedirs("config", exist_ok=True)

clicked_points = []
window_width = 1280
window_height = 720

def mouse_callback(event, x, y, flags, param):
    global clicked_points
    if event == cv2.EVENT_LBUTTONDOWN:
        if len(clicked_points) < 4:
            norm_x = float(x) / window_width
            norm_y = float(y) / window_height
            clicked_points.append([norm_x, norm_y])
            print(f"[+] Point {len(clicked_points)} selected: ({norm_x:.4f}, {norm_y:.4f})")

def save_normalized_config(points):
    # Save under multiple keys so all legacy and new modules can read it
    data = {
        "roi_polygon_normalized": points,
        "roi_polygon": [[int(pt[0] * window_width), int(pt[1] * window_height)] for pt in points],
        "point_count": len(points)
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=4)
    print(f"[✓] SUCCESS: ROI Configuration saved to '{CONFIG_PATH}'")

def run_roi_drawer(video_source=0):
    global clicked_points, window_width, window_height
    clicked_points = []

    cap = cv2.VideoCapture(video_source)
    if not cap.isOpened():
        print(f"[✗] Error opening video source '{video_source}'")
        return

    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        print("[✗] Failed to capture initial video frame.")
        return

    display_frame = cv2.resize(frame, (window_width, window_height))
    window_name = "PARK-ALERT: Draw No-Parking Zone (Click 4 Points)"

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, window_width, window_height)
    cv2.setMouseCallback(window_name, mouse_callback)

    while True:
        temp_img = display_frame.copy()

        pixel_pts = []
        for norm_x, norm_y in clicked_points:
            px = int(norm_x * window_width)
            py = int(norm_y * window_height)
            pixel_pts.append((px, py))
            cv2.circle(temp_img, (px, py), 6, (0, 255, 0), -1)

        if len(pixel_pts) > 1:
            pts_arr = np.array(pixel_pts, np.int32).reshape((-1, 1, 2))
            is_closed = (len(pixel_pts) == 4)
            color = (0, 0, 255) if is_closed else (0, 255, 255)
            cv2.polylines(temp_img, [pts_arr], isClosed=is_closed, color=color, thickness=2)

        cv2.imshow(window_name, temp_img)
        key = cv2.waitKey(20) & 0xFF

        if key == ord('s'):
            if len(clicked_points) == 4:
                save_normalized_config(clicked_points)
                break
            else:
                print(f"[!] Select 4 points before saving (Current: {len(clicked_points)})")
        elif key == ord('r'):
            clicked_points = []
            print("[*] Reset points.")
        elif key == ord('q'):
            break

    cv2.destroyAllWindows()

if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else 0
    run_roi_drawer(src)