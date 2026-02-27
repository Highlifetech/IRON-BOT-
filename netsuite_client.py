"""
NetSuite REST API Client for Iron Bot
Handles shipping status, ship-to addresses, and customer balances via TBA OAuth 1.0a.
"""
import os
import logging
import json
import requests
from requests_oauthlib import OAuth1

logger = logging.getLogger(__name__)


class NetSuiteClient:
    """Client for NetSuite REST API — shipping, addresses, and balances."""

    def __init__(self):
        self.account_id = os.environ.get("NETSUITE_ACCOUNT_ID", "")
        self.configured = bool(
            os.environ.get("NETSUITE_ACCOUNT_ID") and
            os.environ.get("NETSUITE_CONSUMER_KEY") and
            os.environ.get("NETSUITE_CONSUMER_SECRET") and
            os.environ.get("NETSUITE_TOKEN_ID") and
            os.environ.get("NETSUITE_TOKEN_SECRET")
        )
        if self.configured:
            account_url = self.account_id.lower().replace("_", "-")
            self.suiteql_url = f"https://{account_url}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
        else:
            logger.warning("NetSuite not configured — missing env vars.")
            self.suiteql_url = ""

    def _auth(self):
        # Read credentials fresh each call so Railway variable updates are picked up
        consumer_key = os.environ.get("NETSUITE_CONSUMER_KEY", "")
        consumer_secret = os.environ.get("NETSUITE_CONSUMER_SECRET", "")
        token_id = os.environ.get("NETSUITE_TOKEN_ID", "")
        token_secret = os.environ.get("NETSUITE_TOKEN_SECRET", "")
        account_id = os.environ.get("NETSUITE_ACCOUNT_ID", "")
        # NetSuite TBA requires uppercase account ID as realm and auth_header signature type
        realm = account_id.upper().replace("-", "_")
        logger.info(f"NetSuite auth: account={account_id}, token_id={token_id[:8]}..., realm={realm}")
        return OAuth1(
            consumer_key,
            consumer_secret,
            token_id,
            token_secret,
            realm=realm,
            signature_method="HMAC-SHA256",
            signature_type="auth_header"
        )

    def _not_configured(self):
        return {
            "error": "NetSuite not configured",
            "hint": "Add NETSUITE_ACCOUNT_ID, NETSUITE_CONSUMER_KEY, NETSUITE_CONSUMER_SECRET, NETSUITE_TOKEN_ID, NETSUITE_TOKEN_SECRET to Railway."
        }

    def _suiteql(self, query: str) -> list:
        if not self.configured:
            return []
        headers = {
            "Content-Type": "application/json",
            "Prefer": "transient"
        }
        resp = requests.post(
            self.suiteql_url,
            auth=self._auth(),
            headers=headers,
            json={"q": query},
            timeout=30
        )
        if not resp.ok:
            logger.error(f"NetSuite SuiteQL {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        return resp.json().get("items", [])

    # -------------------------------------------------------------------------
    # SHIPPING — order status + tracking + carrier
    # -------------------------------------------------------------------------

    def get_shipment_by_order(self, order_ref: str) -> dict:
        """Get shipping status and tracking for a specific order."""
        if not self.configured:
            return self._not_configured()
        clean = order_ref.upper().replace("SO-", "").replace("SO", "").strip()
        query = f"""
            SELECT
                so.tranid            AS order_number,
                so.trandate          AS order_date,
                so.status            AS order_status,
                cust.companyname     AS customer,
                sm.name              AS carrier,
                fulfill.trandate     AS ship_date,
                fulfill.status       AS fulfillment_status,
                fulfill.trackingnumbers AS tracking,
                so.shipaddress       AS ship_to_address
            FROM transaction so
            LEFT JOIN transaction fulfill
                ON fulfill.createdfrom = so.id AND fulfill.type = 'ItemShip'
            LEFT JOIN customer cust ON cust.id = so.entity
            LEFT JOIN shipmethod sm ON sm.id = so.shipmethod
            WHERE so.type = 'SalesOrd'
              AND (UPPER(so.tranid) LIKE '%{clean}%'
                   OR CAST(so.id AS VARCHAR(20)) = '{clean}')
            ORDER BY so.trandate DESC
            LIMIT 10
        """
        try:
            rows = self._suiteql(query)
            if not rows:
                return {"message": f"No order found matching '{order_ref}'"}
            return {"order_ref": order_ref, "results": rows}
        except Exception as e:
            logger.error("NetSuite shipping error: " + str(e))
            return {"error": str(e)}

    def get_recent_shipments(self, days: int = 7) -> dict:
        """Get all shipments from the past N days."""
        if not self.configured:
            return self._not_configured()
        query = f"""
            SELECT
                so.tranid            AS order_number,
                cust.companyname     AS customer,
                sm.name              AS carrier,
                fulfill.trandate     AS ship_date,
                fulfill.status       AS fulfillment_status,
                fulfill.trackingnumbers AS tracking,
                so.shipaddress       AS ship_to_address
            FROM transaction so
            LEFT JOIN transaction fulfill
                ON fulfill.createdfrom = so.id AND fulfill.type = 'ItemShip'
            LEFT JOIN customer cust ON cust.id = so.entity
            LEFT JOIN shipmethod sm ON sm.id = so.shipmethod
            WHERE so.type = 'SalesOrd'
              AND fulfill.trandate >= TO_DATE(SYSDATE - {days})
            ORDER BY fulfill.trandate DESC
            LIMIT 25
        """
        try:
            rows = self._suiteql(query)
            return {"recent_shipments": rows, "days_back": days, "count": len(rows)}
        except Exception as e:
            logger.error("NetSuite recent shipments error: " + str(e))
            return {"error": str(e)}

    # -------------------------------------------------------------------------
    # ADDRESS — where to ship an order
    # -------------------------------------------------------------------------

    def get_ship_address(self, query_term: str) -> dict:
        """Get the ship-to address for an order or customer."""
        if not self.configured:
            return self._not_configured()
        clean = query_term.upper().replace("SO-", "").replace("SO", "").strip()
        query = f"""
            SELECT
                so.tranid            AS order_number,
                cust.companyname     AS customer,
                so.shipaddress       AS ship_to_address,
                so.status            AS order_status
            FROM transaction so
            LEFT JOIN customer cust ON cust.id = so.entity
            WHERE so.type = 'SalesOrd'
              AND (UPPER(so.tranid) LIKE '%{clean}%'
                   OR UPPER(cust.companyname) LIKE '%{clean}%')
            ORDER BY so.trandate DESC
            LIMIT 10
        """
        try:
            rows = self._suiteql(query)
            if not rows:
                return {"message": f"No address found for '{query_term}'"}
            return {"query": query_term, "results": rows}
        except Exception as e:
            logger.error("NetSuite address error: " + str(e))
            return {"error": str(e)}

    # -------------------------------------------------------------------------
    # BALANCES — what each client owes
    # -------------------------------------------------------------------------

    def get_customer_balance(self, customer_name: str = None) -> dict:
        """Get outstanding balance for a specific customer or all customers."""
        if not self.configured:
            return self._not_configured()
        where = ""
        if customer_name:
            clean = customer_name.replace("'", "''")
            where = f"AND UPPER(cust.companyname) LIKE '%{clean.upper()}%'"
        query = f"""
            SELECT
                cust.companyname     AS customer,
                cust.balance         AS outstanding_balance,
                cust.overduebalance  AS overdue_balance,
                cust.currency        AS currency
            FROM customer cust
            WHERE cust.isinactive = 'F'
              AND cust.balance > 0
              {where}
            ORDER BY cust.overduebalance DESC, cust.balance DESC
            LIMIT 50
        """
        try:
            rows = self._suiteql(query)
            if not rows:
                msg = f"No outstanding balance found for '{customer_name}'" if customer_name else "No outstanding balances found"
                return {"message": msg}
            return {"balances": rows, "count": len(rows)}
        except Exception as e:
            logger.error("NetSuite balance error: " + str(e))
            return {"error": str(e)}

    def get_aged_receivables(self) -> dict:
        """Get aged AR — 30/60/90 days overdue breakdown."""
        if not self.configured:
            return self._not_configured()
        query = """
            SELECT
                cust.companyname     AS customer,
                inv.tranid           AS invoice_number,
                inv.trandate         AS invoice_date,
                inv.duedate          AS due_date,
                inv.amountremaining  AS amount_due,
                inv.currency         AS currency,
                CASE
                    WHEN (SYSDATE - inv.duedate) > 90 THEN '90+ days'
                    WHEN (SYSDATE - inv.duedate) > 60 THEN '60-90 days'
                    WHEN (SYSDATE - inv.duedate) > 30 THEN '30-60 days'
                    WHEN (SYSDATE - inv.duedate) > 0  THEN '1-30 days'
                    ELSE 'not yet due'
                END AS aging_bucket
            FROM transaction inv
            LEFT JOIN customer cust ON cust.id = inv.entity
            WHERE inv.type = 'CustInvc'
              AND inv.amountremaining > 0
            ORDER BY inv.duedate ASC
            LIMIT 50
        """
        try:
            rows = self._suiteql(query)
            return {"aged_receivables": rows, "count": len(rows)}
        except Exception as e:
            logger.error("NetSuite AR error: " + str(e))
            return {"error": str(e)}
