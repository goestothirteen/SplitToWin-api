from flask import Blueprint, request, jsonify
from .services.receipt_parser import parse_receipt_text

api = Blueprint("api", __name__)

@api.route("/parse-receipt", methods=["POST"])
def parse_receipt():
    data = request.json
    ocr_text = data.get("ocrText")

    if not ocr_text:
        return jsonify({"error": "Missing OCR text"}), 400

    result = parse_receipt_text(ocr_text)
    return jsonify(result)

@api.route("/healthcheck", methods=["GET"])
def healthcheck():
    return jsonify({"status": "healthy"})
