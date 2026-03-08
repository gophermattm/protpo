"""Tests for QxoClient — token management, pricing, and caching."""

import time
from unittest.mock import MagicMock, patch

import pytest

from qxo_client import QxoClient


# ── Helpers ───────────────────────────────────────────────────────────

def _make_client():
    db = MagicMock()
    client = QxoClient(db=db, password="test-pass")
    return client, db


def _mock_doc(exists, data=None):
    snap = MagicMock()
    snap.exists = exists
    snap.to_dict.return_value = data or {}
    return snap


# ── Task 2: Token management ─────────────────────────────────────────

@patch("qxo_client.requests.post")
def test_login_fetches_new_token_when_no_cache(mock_post):
    client, db = _make_client()
    client._token_doc.get.return_value = _mock_doc(exists=False)

    mock_post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "token": "fresh-token",
            "expires_at": time.time() + 3600,
        }),
    )

    token = client.get_token()

    assert token == "fresh-token"
    client._token_doc.set.assert_called_once()
    stored = client._token_doc.set.call_args[0][0]
    assert stored["token"] == "fresh-token"


def test_login_returns_cached_token_when_valid():
    client, _ = _make_client()
    future = time.time() + 600  # 10 min left
    client._token_doc.get.return_value = _mock_doc(
        exists=True, data={"token": "cached-token", "expires_at": future}
    )

    token = client.get_token()
    assert token == "cached-token"


@patch("qxo_client.requests.post")
def test_login_refreshes_token_when_near_expiry(mock_post):
    client, _ = _make_client()
    almost_expired = time.time() + 30  # only 30s left, within 60s buffer
    client._token_doc.get.return_value = _mock_doc(
        exists=True, data={"token": "old-token", "expires_at": almost_expired}
    )

    mock_post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "token": "new-token",
            "expires_at": time.time() + 3600,
        }),
    )

    token = client.get_token()
    assert token == "new-token"
    mock_post.assert_called_once()


# ── Task 3: Pricing ──────────────────────────────────────────────────

@patch("qxo_client.requests.get")
@patch.object(QxoClient, "get_token", return_value="tok-123")
def test_get_pricing_calls_api_and_normalizes(mock_token, mock_get):
    client, _ = _make_client()

    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "prices": [
                {"skuId": "SKU-A", "uom": "SQ", "price": 42.50},
                {"skuId": "SKU-A", "uom": "EA", "price": 4.25},
                {"skuId": "SKU-B", "uom": "EA", "price": 10.00},
            ]
        }),
    )

    result = client.get_pricing(["SKU-A", "SKU-B"])

    # Verify correct URL
    call_url = mock_get.call_args[0][0]
    assert "skuIds=SKU-A,SKU-B" in call_url
    assert "accountId=314102" in call_url

    # Verify auth header
    headers = mock_get.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer tok-123"

    # Verify normalization
    assert result == {
        "SKU-A": {"SQ": 42.50, "EA": 4.25},
        "SKU-B": {"EA": 10.00},
    }


# ── Task 4: Price caching ────────────────────────────────────────────

@patch.object(QxoClient, "get_pricing")
def test_get_pricing_cached_returns_cached_result(mock_pricing):
    client, db = _make_client()
    skus = ["SKU-X"]
    cache_key = QxoClient._price_cache_key(skus)

    cache_doc = MagicMock()
    cache_doc.get.return_value = _mock_doc(
        exists=True,
        data={"prices": {"SKU-X": {"EA": 5.0}}, "cached_at": time.time() - 100},
    )
    db.collection.return_value.document.return_value = cache_doc

    result = client.get_pricing_cached(skus)

    assert result == {"SKU-X": {"EA": 5.0}}
    mock_pricing.assert_not_called()


@patch.object(QxoClient, "get_pricing", return_value={"SKU-Y": {"EA": 7.0}})
def test_get_pricing_cached_calls_api_when_expired(mock_pricing):
    client, db = _make_client()
    skus = ["SKU-Y"]

    cache_doc = MagicMock()
    cache_doc.get.return_value = _mock_doc(
        exists=True,
        data={
            "prices": {"SKU-Y": {"EA": 3.0}},
            "cached_at": time.time() - 90000,  # older than 24h
        },
    )
    db.collection.return_value.document.return_value = cache_doc

    result = client.get_pricing_cached(skus)

    assert result == {"SKU-Y": {"EA": 7.0}}
    mock_pricing.assert_called_once_with(skus)
    cache_doc.set.assert_called_once()


# ── Task 7: Catalog download ────────────────────────────────────────

@patch("qxo_client.requests.get")
@patch.object(QxoClient, "get_token", return_value="tok-123")
def test_download_catalog_filters_by_category(mock_token, mock_get):
    client, _ = _make_client()
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value=[
            {"itemNumber": "253729", "description": "TPO 60mil", "category": "Roofing", "unitOfMeasure": "RL", "productNumber": "P1"},
            {"itemNumber": "999999", "description": "Lumber 2x4", "category": "Lumber", "unitOfMeasure": "EA", "productNumber": "P2"},
            {"itemNumber": "413335", "description": "Polyiso 2.5in", "category": "Insulation", "unitOfMeasure": "BDL", "productNumber": "P3"},
        ]),
    )
    items = client.download_catalog(categories=["Roofing", "Insulation"])
    assert len(items) == 2
    assert items[0]["itemNumber"] == "253729"
    assert items[1]["itemNumber"] == "413335"


# ── Task 8: Catalog sync to Firestore ───────────────────────────────

def test_sync_catalog_writes_batches():
    client, db = _make_client()
    mock_batch = MagicMock()
    db.batch.return_value = mock_batch

    items = [
        {"itemNumber": "SKU1", "description": "Item 1", "unitOfMeasure": "EA", "category": "Roofing", "productNumber": "P1"},
        {"itemNumber": "SKU2", "description": "Item 2", "unitOfMeasure": "BDL", "category": "Insulation", "productNumber": "P2"},
    ]
    count = client.sync_catalog_to_firestore(items)
    assert count == 2
    assert mock_batch.set.call_count == 2
    mock_batch.commit.assert_called_once()


# ── Task 14: Quote submission ─────────────────────────────────────

@patch("qxo_client.requests.post")
@patch.object(QxoClient, "get_token", return_value="tok-123")
def test_submit_quote_sends_correct_payload(mock_token, mock_post):
    client, _ = _make_client()
    mock_post.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"quoteId": "Q-12345", "status": "submitted"}),
    )

    result = client.submit_quote(
        project_name="123 Main St Reroof",
        items=[
            {"skuId": "253729", "quantity": 15, "uom": "RL"},
            {"skuId": "413335", "quantity": 40, "uom": "BDL"},
        ],
    )

    assert result["quoteId"] == "Q-12345"
    call_kwargs = mock_post.call_args
    body = call_kwargs[1]["json"]
    assert body["accountId"] == "314102"
    assert body["jobName"] == "123 Main St Reroof"
    assert body["workType"] == "R"
    assert len(body["quoteItems"]) == 2
    assert body["quoteItems"][0]["itemId"] == "253729"
    assert body["quoteItems"][0]["quantity"] == 15
