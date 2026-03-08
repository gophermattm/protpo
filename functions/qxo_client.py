"""QXO API client with Firestore-backed token and price caching."""

import hashlib
import time

import requests


class QxoClient:
    USERNAME = "matt@gorillaexteriors.com"
    SITE_ID = "becnus"
    ACCOUNT_ID = "314102"
    TOKEN_REFRESH_BUFFER = 60  # seconds before expiry to trigger refresh
    PRICE_CACHE_TTL = 86400  # 24 hours

    ROOFING_CATEGORIES = ["Roofing", "Insulation", "Waterproofing", "Adhesives", "Fasteners"]

    def __init__(self, db, password, base_url="https://uat-api.qxo.com"):
        self.db = db
        self.password = password
        self.base_url = base_url
        self._token_doc = db.collection("qxo_auth").document("current_token")

    # ── Token management ──────────────────────────────────────────────

    def get_token(self) -> str:
        """Return a valid token, refreshing if needed."""
        snap = self._token_doc.get()
        if snap.exists:
            data = snap.to_dict()
            expires_at = data.get("expires_at", 0)
            if time.time() < expires_at - self.TOKEN_REFRESH_BUFFER:
                return data["token"]
        return self._login()

    def _login(self) -> str:
        """Authenticate against QXO and cache the token in Firestore."""
        url = f"{self.base_url}/v1/rest/com/becn/login"
        payload = {
            "username": self.USERNAME,
            "siteId": self.SITE_ID,
            "password": self.password,
        }
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()

        token = body["token"]
        expires_at = body["expires_at"]

        self._token_doc.set({"token": token, "expires_at": expires_at})
        return token

    # ── Pricing ───────────────────────────────────────────────────────

    def get_pricing(self, sku_ids: list[str]) -> dict:
        """Fetch live pricing from QXO and normalize into {SKU: {UOM: price}}."""
        token = self.get_token()
        sku_param = ",".join(sku_ids)
        url = (
            f"{self.base_url}/v2/rest/com/becn/pricing"
            f"?skuIds={sku_param}&accountId={self.ACCOUNT_ID}"
        )
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        return self._normalize_pricing(resp.json())

    @staticmethod
    def _normalize_pricing(raw: dict) -> dict:
        """Convert API response into {SKU: {UOM: price}} mapping."""
        result = {}
        for item in raw.get("prices", []):
            sku = item.get("skuId")
            uom = item.get("uom", "EA")
            price = item.get("price")
            if sku is not None:
                result.setdefault(sku, {})[uom] = price
        return result

    # ── Price caching ─────────────────────────────────────────────────

    @staticmethod
    def _price_cache_key(sku_ids: list[str]) -> str:
        """Deterministic cache key: SHA-256 of sorted SKUs + account ID."""
        payload = ",".join(sorted(sku_ids)) + "|" + QxoClient.ACCOUNT_ID
        return hashlib.sha256(payload.encode()).hexdigest()

    def get_pricing_cached(self, sku_ids: list[str]) -> dict:
        """Return cached pricing if fresh, otherwise fetch and cache."""
        cache_key = self._price_cache_key(sku_ids)
        doc_ref = self.db.collection("qxo_price_cache").document(cache_key)
        snap = doc_ref.get()

        if snap.exists:
            data = snap.to_dict()
            cached_at = data.get("cached_at", 0)
            if time.time() - cached_at < self.PRICE_CACHE_TTL:
                return data["prices"]

        prices = self.get_pricing(sku_ids)
        doc_ref.set({"prices": prices, "cached_at": time.time()})
        return prices

    # ── Catalog ────────────────────────────────────────────────────────

    def download_catalog(self, categories: list[str] | None = None) -> list[dict]:
        token = self.get_token()
        resp = requests.get(
            f"{self.base_url}/v3/ctUsCatalogDownloadJson",
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        resp.raise_for_status()
        all_items = resp.json()
        filter_cats = categories or self.ROOFING_CATEGORIES
        return [item for item in all_items if item.get("category") in filter_cats]

    def sync_catalog_to_firestore(self, items: list[dict]) -> int:
        batch = self.db.batch()
        catalog_col = self.db.collection("qxo_catalog")
        count = 0
        for i, item in enumerate(items):
            doc_ref = catalog_col.document(item["itemNumber"])
            batch.set(doc_ref, {
                "itemNumber": item.get("itemNumber"),
                "description": item.get("description"),
                "unitOfMeasure": item.get("unitOfMeasure"),
                "category": item.get("category"),
                "productNumber": item.get("productNumber"),
            })
            count += 1
            if (i + 1) % 500 == 0:
                batch.commit()
                batch = self.db.batch()
        if count % 500 != 0:
            batch.commit()
        return count
