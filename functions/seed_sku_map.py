"""Run once to seed the qxo_sku_map collection with BOM category placeholders."""
import firebase_admin
from firebase_admin import firestore

firebase_admin.initialize_app()
db = firestore.client()

MAPPINGS = [
    {"bom_item": "60 mil TPO (10' roll)", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "Polyiso 2.5\"", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "#14 HP Fasteners", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "3\" Barbed Plates", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "TPO Membrane Adhesive", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "TPO Peel & Stick", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "Coping Cap", "beacon_skus": [], "notes": "Populate after catalog sync"},
    {"bom_item": "Drain Assembly", "beacon_skus": [], "notes": "Populate after catalog sync"},
]

col = db.collection("qxo_sku_map")
for m in MAPPINGS:
    doc_id = m["bom_item"].lower().replace(" ", "_").replace('"', "").replace("'", "")
    col.document(doc_id).set(m)
    print(f"  Seeded: {doc_id}")

print(f"Done. Seeded {len(MAPPINGS)} BOM mappings.")
