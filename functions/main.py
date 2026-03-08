from firebase_functions import https_fn, options
from firebase_admin import initialize_app, firestore
import vertexai
from vertexai.generative_models import GenerativeModel
from vertexai.language_models import TextEmbeddingModel
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure

# 1. Initialize once at top
initialize_app()
# If you used a specific database ID (like 'versicobase'), add it here: database="versicobase"
db = firestore.client() 

PROJECT_ID = "tpo-pro-245d1"
LOCATION = "us-central1"

@https_fn.on_call(
    timeout_sec=60,
    memory=options.MemoryOption.GB_1,
    region="us-central1"
)
def askVersico(req: https_fn.CallableRequest):
    import json as _json, anthropic as _anthropic
    _data = req.data or {}
    if _data.get("mode") in ("audit", "nl", "sow"):
        mode = _data.get("mode")
        prompt = _data.get("prompt", "")
        system = _data.get("system", "")
        if not prompt:
            raise https_fn.HttpsError("invalid-argument", "prompt required")
        try:
            _client = _anthropic.Anthropic()
            _kw = {"model": "claude-sonnet-4-20250514", "max_tokens": 1000 if mode == "audit" else 300, "messages": [{"role": "user", "content": prompt}]}
            if system: _kw["system"] = system
            _msg = _client.messages.create(**_kw)
            return {"result": {"result": _msg.content[0].text}}
        except Exception as _e:
            raise https_fn.HttpsError("internal", str(_e))
    # 2. Initialize Vertex AI
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    
    user_question = req.data.get("question")
    if not user_question:
        return {"answer": "Please ask a question!", "sources": []}

    try:
        # 3. Generate Embedding (Fixed at 768 dimensions)
        embed_model = TextEmbeddingModel.from_pretrained("text-embedding-004")
        embeddings = embed_model.get_embeddings(
            [user_question],
            output_dimensionality=768
        )
        query_vector = embeddings[0].values

        # 4. Search Firestore
        collection = db.collection("versico_technical_data")
        
        results = collection.find_nearest(
            vector_field="embedding",
            query_vector=Vector(query_vector), 
            distance_measure=DistanceMeasure.COSINE,
            limit=20
        ).get()

        # 5. Extract Content and Human-Readable Sources
        context_parts = []
        human_sources = []

        for d in results:
            data = d.to_dict()
            content = data.get("content", "No content available.")
            context_parts.append(content)
            
            # Extract metadata for cleaner source labels
            source_file = data.get("source_file", "Unknown Document")
            page_num = data.get("page_number", "N/A")
            source_label = f"{source_file} (Page {page_num})"
            
            if source_label not in human_sources:
                human_sources.append(source_label)

        context_text = "\n---\n".join(context_parts)

        # 6. Generate Answer with Gemini 2.0 Flash
        chat_model = GenerativeModel("gemini-2.0-flash-001")
        
        # Professional Engineering Persona Prompt
        prompt = (
            f"You are VersiBot, a senior roofing engineer. Your goal is to synthesize an answer "
            f"from the provided technical documentation snippets. Many questions require combining "
            f"details from different pages (e.g., insulation R-values + fastening patterns + wall details).\n\n"
            f"If the data contains R-value per inch, perform the math for the user. "
            f"If multiple documents provide different parts of the system (fasteners vs. drains), "
            f"combine them into a cohesive roofing assembly guide.\n\n"
            f"TECHNICAL CONTEXT:\n{context_text}\n\n"
            f"USER QUESTION: {user_question}"
        )
        
        response = chat_model.generate_content(prompt)
        
        return {
            "answer": response.text,
            "sources": human_sources # Sending back the clean labels!
        }

    except Exception as e:
        print(f"Error during execution: {str(e)}")
        return {
            "answer": f"VersiBot hit a snag: {str(e)}",
            "sources": []
        }


@https_fn.on_call(
    timeout_sec=30,
    memory=options.MemoryOption.MB_256,
    region="us-central1",
    secrets=["QXO_PASSWORD"],
)
def getQxoPricing(req: https_fn.CallableRequest):
    import os
    from qxo_client import QxoClient

    sku_ids = (req.data or {}).get("skuIds")
    if not sku_ids or not isinstance(sku_ids, list):
        raise https_fn.HttpsError("invalid-argument", "skuIds must be a non-empty list")

    password = os.environ.get("QXO_PASSWORD")
    if not password:
        raise https_fn.HttpsError("internal", "QXO_PASSWORD secret not configured")

    client = QxoClient(
        db=db,
        password=password,
        base_url=os.environ.get("QXO_BASE_URL", "https://uat-api.qxo.com"),
    )
    try:
        prices = client.get_pricing_cached(sku_ids)
        return {"prices": prices}
    except Exception as e:
        raise https_fn.HttpsError("internal", f"QXO pricing error: {str(e)}")


@https_fn.on_call(
    timeout_sec=300,
    memory=options.MemoryOption.GB_1,
    region="us-central1",
    secrets=["QXO_PASSWORD"],
)
def syncQxoCatalog(req: https_fn.CallableRequest):
    import os
    from qxo_client import QxoClient

    password = os.environ.get("QXO_PASSWORD")
    if not password:
        raise https_fn.HttpsError("internal", "QXO_PASSWORD secret not configured")

    client = QxoClient(
        db=db,
        password=password,
        base_url=os.environ.get("QXO_BASE_URL", "https://uat-api.qxo.com"),
    )
    try:
        categories = (req.data or {}).get("categories")
        items = client.download_catalog(categories=categories)
        count = client.sync_catalog_to_firestore(items)
        return {"synced": count, "message": f"Synced {count} catalog items to Firestore"}
    except Exception as e:
        raise https_fn.HttpsError("internal", f"Catalog sync error: {str(e)}")