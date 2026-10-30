# PARK-ALERT
automated no-parking zone detection

# PARK-ALERT 🚗🚨
> **Automated No-Parking Zone Detection, ANPR & Statutory e-Challan System**

An edge computer vision and web surveillance system that detects illegally parked vehicles in restricted areas, measures stationary dwell time, extracts license plates, and automates traffic penalty notices via email[cite: 8, 9].

---

## 📌 Key Features
- **Polygon ROI Geofencing:** Draw custom no-parking zones on any camera stream using OpenCV[cite: 9].
- **Vehicle Tracking & Dwell Timer:** Detects vehicles with **YOLOv8** and tracks stationary duration frame-by-frame with **ByteTrack**[cite: 8, 9].
- **Smart ANPR:** Extracts registration numbers using **EasyOCR** with built-in glyph confusion heuristics (fixes `8/B`, `0/O`, `1/I`)[cite: 9].
- **Two-Tier Enforcement:**
  - **At 20 seconds:** Sends an automated warning email giving a 10-second grace period.
  - **At 30 seconds:** Generates a statutory Karnataka State Police e-Challan with photographic proof and compounding fines under MVA Sections 122/177[cite: 8, 9].
- **Web Dashboard:** Single-tab operator interface with live camera HUD, unpaid violation lists, paid archives, and simulated payment clearance[cite: 8].

---

## 🛠️ Tech Stack
- **Languages & Frameworks:** Python, Flask, HTML5, CSS3, JavaScript[cite: 8, 9, 10]
- **Computer Vision & AI:** OpenCV, Ultralytics YOLOv8, ByteTrack, EasyOCR[cite: 8, 9, 10]
- **Database & Networking:** SQLite3, Gmail SMTP (SSL), Ngrok[cite: 8, 9]

---

## 🚀 Quick Setup

### 1. Clone the Repository
```bash
git clone [https://github.com/deepthi-1008/PARK-ALERT.git](https://github.com/deepthi-1008/PARK-ALERT.git)
cd PARK-ALERT
