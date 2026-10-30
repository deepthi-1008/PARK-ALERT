import cv2
import re
import easyocr
import numpy as np

class ANPRProcessor:
    def __init__(self, languages=['en'], gpu=False):
        """Initialize EasyOCR reader instance."""
        print("[*] Initializing EasyOCR Engine...")
        self.reader = easyocr.Reader(languages, gpu=gpu)
        
        # Indian License Plate Regex Pattern (e.g., KA35EA8213, DL01AB1234, 22BH1234A)
        self.plate_pattern = re.compile(
            r'([A-Z]{2}\s?[0-9]{2}\s?[A-Z]{1,2}\s?[0-9]{4})|([0-9]{2}\s?BH\s?[0-9]{4}\s?[A-Z]{1,2})'
        )

    def preprocess_image(self, cropped_vehicle_img):
        """Applies Grayscale conversion, Gaussian blurring, and Adaptive Thresholding."""
        if cropped_vehicle_img is None or cropped_vehicle_img.size == 0:
            return None

        # 1. Grayscale Conversion
        gray = cv2.cvtColor(cropped_vehicle_img, cv2.COLOR_BGR2GRAY)

        # 2. Gaussian Blurring to remove noise
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # 3. Adaptive Thresholding for binarization
        thresh = cv2.adaptiveThreshold(
            blurred, 255, 
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 11, 2
        )

        return thresh

    def clean_plate_text(self, raw_text):
        """Sanitizes raw OCR string and converts common OCR misread character swaps."""
        clean_text = raw_text.upper()
        clean_text = re.sub(r'[^A-Z0-9]', '', clean_text)  # Remove special symbols/spaces

        # Common character correction swaps based on plate position
        corrections = {
            'O': '0', 'I': '1', 'Z': '2', 'S': '5', 'B': '8'
        }

        # Match against Indian Plate Regex Pattern
        match = self.plate_pattern.search(clean_text)
        if match:
            return match.group(0)

        return clean_text if len(clean_text) >= 6 else None

    def extract_plate_number(self, vehicle_crop):
        """Runs pre-processing, EasyOCR extraction, and Regex validation pipeline."""
        if vehicle_crop is None or vehicle_crop.size == 0:
            return None

        processed_img = self.preprocess_image(vehicle_crop)

        # Run OCR on both grayscale pre-processed and raw vehicle crop for maximum accuracy
        for target_img in [processed_img, vehicle_crop]:
            if target_img is None:
                continue
            try:
                results = self.reader.readtext(target_img)
                for bbox, text, prob in results:
                    if prob > 0.15:  # Confidence filter
                        cleaned_plate = self.clean_plate_text(text)
                        if cleaned_plate and len(cleaned_plate) >= 6:
                            print(f"[✓] ANPR SUCCESS: Recognized Plate -> {cleaned_plate} (Conf: {prob:.2f})")
                            return cleaned_plate
            except Exception as e:
                print(f"[!] OCR Read Error: {e}")

        return None