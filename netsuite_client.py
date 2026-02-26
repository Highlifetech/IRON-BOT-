"""
NetSuite REST API Client for HLT Production Bot
Fetches shipping/fulfillment data from NetSuite using OAuth 1.0a (TBA).

Required Railway environment variables:
  NETSUITE_ACCOUNT_ID   - Your NetSuite account ID (e.g. 1234567)
  NETSUITE_CONSUMER_KEY
  NETSUITE_CONSUMER_SECRET
  NETSUITE_TOKEN_ID
  NETSUITE_TOKEN_SECRET
"""

import os
import logging
import time
import json
import requests
from requests_oauthlib import OAuth1

logger = logging.getLogger(__name__)

NETSUITE_ACCOUNT_ID = os.environ.get("NETSUITE_ACCOUNT_ID", "")
NETSUITE_CONSUMER_KEY = os.environ.get("NETSUITE_CONSUMER_KEY", "")
NETSUITE_CONSUMER_SECRET = os.environ.get("NETSUITE_CONSUMER_SECRET", "")
NETSUITE_TOKEN_ID = os.environ.get("NETSUITE_TOKEN_ID", "")
NETSUITE_TOKEN_SECRET = os.environ.get("NETSUITE_TOKEN_SECRET", "")


class NetSuiteClient:
    """Client for NetSuite REST API — reads sales orders and fulfillment/shipping data."""

    def __init__(self):
        self.account_id = NETSUITE_ACCOUNT_ID
        self.configured = bool(
            NETSUITE_ACCOUNT_ID and
            NETSUITE_CONSUMER_KEY and
            NETSUITE_CONSUMER_SECRET and
            NETSUITE_TOKEN_ID and
            NETSUITE_TOKEN_SECRET
        )
        if self.configured:
            self.base_url = f"https://{NETSUITE_ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/record/v1"
            self.suiteql_url = f"https://{NETSUITE_ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
        else:
            logger.warning("NetSuite not configured — missing env vars. Shipping queries will return placeholder data.")
            self.base_url = ""
            self.suiteql_url = ""

    def _auth(self):
        return OAuth1(
            NETSUITE_CONSUMER_KEY,
            NETSUITE_CONSUMER_SECRET,
            NETSUITE_TOKEN_ID,
            NETSUITE_TOKEN_SECRET,
            realm=NETSUITE_ACCOUNT_ID,
            signature_method="HMAC-SHA256"
        )

    def _suiteql(self, query: str) -> list:
        if not self.configured:
            return []
        headers = {
            "Content-Type": "application/json",
            "Prefer": "transient"
        }
        payload = {"q": query}
        resp = requests.post(
            self.suiteql_url,
            auth=self._auth(),
            headers=headers,
            json=payload,
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])

    def get_shipment_by_order(self, order_ref: str) -> dict:
        """Look up shipping status for a specific sales order number or ID."""
        if not self.configured:
            return {
                "error": "NetSuite not configured",
                "hint": "Add NETSUITE_ACCOUNT_ID, NETSUITE_CONSUMER_KEY, NETSUITE_CONSUMER_SECRET, NETSUITE_TOKEN_ID, NETSUITE_TOKEN_SECRET to Railway environment variables."
            }

        clean_ref = order_ref.upper().replace("SO-", "").replace("SO", "").strip()
        query = f"""
            SELECT
                so.tranid AS order_number,
                so.trandate AS order_date,
                so.status AS order_status,
                so.shippingaddress AS ship_to,
                item.displayname AS carrier,
                fulfill.trandate AS ship_date,
                fulfill.status AS fulfillment_status,
                fulfill.trackingnumbers AS tracking
            FROM transaction so
            LEFT JOIN transaction fulfill ON fulfill.createdfrom = so.id
                AND fulfill.type = 'ItemShip'
            LEFT JOIN shipmethod item ON item.id = so.shipmethod
            WHERE so.type = 'SalesOrd'
              AND (so.tranid LIKE '%{clean_ref}%' OR CAST(so.id AS VARCHAR(20)) = '{clean_ref}')
            ORDER BY so.trandate DESC
            LIMIT 5
        """
        try:
            rows = self._suiteql(query)
            if not rows:
                return {"message": f"No order found matching '{order_ref}'", "searched_for": clean_ref}
            return {"order_ref": order_ref, "results": rows}
        except Exception as e:
            logger.error("NetSuite SuiteQL error: " + str(e))
            return {"error": str(e), "order_ref": order_ref}

    def get_recent_shipments(self, days: int = 7) -> dict:
        """Get all recent shipments from the past N days."""
        if not self.configured:
            return {
                "error": "NetSuite not configured",
                "hint": "Add NETSUITE_ACCOUNT_ID, NETSUITE_CONSUMER_KEY, NETSUITE_CONSUMER_SECRET, NETSUITE_TOKEN_ID, NETSUITE_TOKEN_SECRET to Railway environment variables."
            }
        query = f"""
            SELECT
                so.tranid AS order_number,
                so.trandate AS order_date,
                so.status AS order_status,
                fulfill.trandate AS ship_date,
                fulfill.status AS fulfillment_status,
                fulfill.trackingnumbers AS tracking,
                item.displayname AS carrier
            FROM transaction so
            LEFT JOIN transaction fulfill ON fulfill.createdfrom = so.id
                AND fulfill.type = 'ItemShip'
            LEFT JOIN shipmethod item ON item.id = so.shipmethod
            WHERE so.type = 'SalesOrd'
              AND fulfill.trandate >= TO_DATE(SYSDATE - {days})
            ORDER BY fulfill.trandate DESC
            LIMIT 20
        """
        try:
            rows = self._suiteql(query)
            return {"recent_shipments": rows, "days_back": days, "count": len(rows)}
        except Exception as e:
            logger.error("NetSuite recent shipments error: " + str(e))
            return {"error": str(e)}
