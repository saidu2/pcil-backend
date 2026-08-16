# ─────────────────────────────────────────────────────────────────────────────
# app/services/d365_service.py
#
# Microsoft Dynamics 365 Integration Service
#
# This service handles ALL communication with D365 (the ERP).
# It uses MSAL (Microsoft Authentication Library) for OAuth2 authentication
# and httpx for async REST API calls to the D365 Web API.
#
# ── How D365 Integration Works ───────────────────────────────────────────────
#
#  OUTBOUND (PCIL Backend → D365):
#    When a KYC is submitted or subscription is activated, this service
#    pushes a record to D365 via REST API so the compliance/operations
#    team can see and process it in their D365 interface.
#
#  INBOUND (D365 → PCIL Backend via Webhook):
#    When the D365 team approves/denies a KYC or subscription, D365
#    fires a webhook to POST /api/v1/webhooks/d365 (see webhooks router).
#    The webhook handler updates the database and notifies the client.
#
# ── Setup Steps (Do Once) ────────────────────────────────────────────────────
#  1. Go to Azure Portal → Azure Active Directory → App Registrations
#  2. Create "PCIL Backend" app registration
#  3. Add API permission: Dynamics CRM → user_impersonation
#  4. Create a client secret (set as AZURE_CLIENT_SECRET in .env)
#  5. In D365: Settings → Security → Application Users → New
#     Create an application user for the App Registration
#     Assign appropriate security role (e.g. "PCIL Integration User")
#  6. Set D365_BASE_URL in .env to your D365 org URL
# ─────────────────────────────────────────────────────────────────────────────

import logging
from typing import Optional, Dict, Any

import httpx
import msal

from app.core.config import settings

logger = logging.getLogger(__name__)


class D365Service:
    """
    Service class for all Dynamics 365 API operations.
    Uses OAuth 2.0 Client Credentials flow (service-to-service — no user login needed).
    Tokens are cached by MSAL and automatically refreshed before expiry.
    """

    def __init__(self):
        # ── MSAL Confidential Client ──────────────────────────────────────────
        # This authenticates the backend app (not a user) to Azure AD.
        # Uses the App Registration credentials from .env
        self._msal_app = msal.ConfidentialClientApplication(
            client_id=settings.AZURE_CLIENT_ID,
            client_credential=settings.AZURE_CLIENT_SECRET,
            authority=f"https://login.microsoftonline.com/{settings.AZURE_TENANT_ID}",
        )

        # D365 API scope — always this format for D365
        self._scope = [f"{settings.D365_BASE_URL}/.default"]

    # ── Token Acquisition ─────────────────────────────────────────────────────

    def _get_access_token(self) -> Optional[str]:
        """
        Get a valid Azure AD access token for D365 API calls.
        MSAL handles caching — tokens are reused until 5 minutes before expiry.

        Returns None if authentication fails (check .env credentials).
        """
        # Try to get token from MSAL cache first
        result = self._msal_app.acquire_token_silent(self._scope, account=None)

        if not result:
            # Cache miss — fetch new token from Azure AD
            result = self._msal_app.acquire_token_for_client(scopes=self._scope)

        if "access_token" in result:
            return result["access_token"]

        # If we reach here, authentication failed
        logger.error(
            f"D365 authentication failed: {result.get('error')} — "
            f"{result.get('error_description')}"
            "\nCheck AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET in .env"
        )
        return None

    def _get_headers(self) -> Dict[str, str]:
        """Build HTTP headers for D365 API calls"""
        token = self._get_access_token()
        if not token:
            raise ValueError("Could not obtain D365 access token. Check .env credentials.")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # OData-Version required by D365 Web API
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }

    # ── KYC Operations ────────────────────────────────────────────────────────

    async def push_kyc_to_d365(
        self,
        kyc_id: str,
        user_full_name: str,
        user_email: str,
        account_type: str,
        submitted_at: str,
        kyc_data: Dict[str, Any],
    ) -> Optional[str]:
        """
        Push a KYC submission to D365 when a client submits their KYC form.
        Creates a new record in the D365 KYC entity.

        ── D365 Customisation Required ──────────────────────────────────────────
        Your D365 developer needs to create a custom entity:
          Entity: pcil_kycsubmissions  (set as D365_KYC_ENTITY in .env)
          Fields (at minimum):
            pcil_kycid          — our internal KYC UUID
            pcil_clientname     — client full name
            pcil_clientemail    — client email
            pcil_accounttype    — Individual / Corporate / Joint / Minor
            pcil_submittedat    — submission datetime
            pcil_status         — pending (set by us), approved/denied (set by D365 team)
        The field names below (pcil_*) must match what's in your D365 entity.
        ────────────────────────────────────────────────────────────────────────

        Returns:
            The D365 record GUID if successful, None on failure.
        """
        if not settings.D365_BASE_URL:
            logger.warning("D365_BASE_URL not configured. Skipping D365 KYC push.")
            return None

        # ── TODO: Map our fields to your actual D365 entity fields ───────────
        # The field names (pcil_kycid, etc.) are examples.
        # Your D365 developer must confirm the exact field names in D365.
        d365_payload = {
            "pcil_kycid": kyc_id,
            "pcil_clientname": user_full_name,
            "pcil_clientemail": user_email,
            "pcil_accounttype": account_type,
            "pcil_submittedat": submitted_at,
            "pcil_status": "pending",
            # Add more fields as needed by the compliance team
        }

        entity_url = f"{settings.D365_BASE_URL}/{settings.D365_KYC_ENTITY}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    entity_url,
                    headers=self._get_headers(),
                    json=d365_payload,
                )

                if response.status_code == 204:
                    # D365 returns 204 on create — record ID is in the header
                    d365_record_id = response.headers.get("OData-EntityId", "")
                    # Extract GUID from URL like:
                    # https://org.crm.dynamics.com/api/data/v9.2/pcil_kycsubmissions(guid)
                    if "(" in d365_record_id:
                        d365_record_id = d365_record_id.split("(")[1].rstrip(")")
                    logger.info(f"KYC {kyc_id} pushed to D365 successfully. D365 ID: {d365_record_id}")
                    return d365_record_id
                else:
                    logger.error(
                        f"D365 KYC push failed: {response.status_code} — {response.text}"
                    )
                    return None

            except httpx.RequestError as e:
                logger.error(f"D365 connection error during KYC push: {e}")
                return None

    # ── Subscription Operations ───────────────────────────────────────────────

    async def push_subscription_to_d365(
        self,
        subscription_id: str,
        user_full_name: str,
        user_email: str,
        product_name: str,
        amount: float,
        currency: str,
        activated_at: str,
        maturity_date: Optional[str],
    ) -> Optional[str]:
        """
        Push an activated subscription to D365.
        Called after admin activates a subscription in the admin panel.

        ── D365 Customisation Required ──────────────────────────────────────────
        Entity: pcil_subscriptions  (set as D365_SUBSCRIPTION_ENTITY in .env)
        Your D365 developer must create this custom entity with appropriate fields.
        ────────────────────────────────────────────────────────────────────────

        Returns:
            D365 record GUID if successful, None on failure.
        """
        if not settings.D365_BASE_URL:
            logger.warning("D365_BASE_URL not configured. Skipping D365 subscription push.")
            return None

        # ── TODO: Map to your actual D365 entity fields ───────────────────────
        d365_payload = {
            "pcil_subscriptionid": subscription_id,
            "pcil_clientname": user_full_name,
            "pcil_clientemail": user_email,
            "pcil_productname": product_name,
            "pcil_amount": amount,
            "pcil_currency": currency,
            "pcil_activatedat": activated_at,
            "pcil_maturitydate": maturity_date,
            "pcil_status": "active",
        }

        entity_url = f"{settings.D365_BASE_URL}/{settings.D365_SUBSCRIPTION_ENTITY}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.post(
                    entity_url,
                    headers=self._get_headers(),
                    json=d365_payload,
                )
                if response.status_code == 204:
                    d365_record_id = response.headers.get("OData-EntityId", "")
                    if "(" in d365_record_id:
                        d365_record_id = d365_record_id.split("(")[1].rstrip(")")
                    logger.info(f"Subscription {subscription_id} pushed to D365. ID: {d365_record_id}")
                    return d365_record_id
                else:
                    logger.error(f"D365 subscription push failed: {response.status_code} — {response.text}")
                    return None
            except httpx.RequestError as e:
                logger.error(f"D365 connection error during subscription push: {e}")
                return None

    async def get_kyc_status_from_d365(self, d365_record_id: str) -> Optional[Dict]:
        """
        Fetch the current status of a KYC record directly from D365.
        Used to manually sync status if a webhook was missed.

        ── D365 Integration Point ────────────────────────────────────────────
        This is a fallback — normally D365 pushes status via webhook.
        Call this from an admin "Sync from D365" button if needed.
        ─────────────────────────────────────────────────────────────────────
        """
        if not settings.D365_BASE_URL:
            return None

        url = (
            f"{settings.D365_BASE_URL}/{settings.D365_KYC_ENTITY}({d365_record_id})"
            f"?$select=pcil_status,pcil_reviewedat,pcil_denialreason"
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                response = await client.get(url, headers=self._get_headers())
                if response.status_code == 200:
                    return response.json()
                logger.error(f"D365 fetch failed: {response.status_code}")
                return None
            except httpx.RequestError as e:
                logger.error(f"D365 connection error: {e}")
                return None


# ── Singleton instance ────────────────────────────────────────────────────────
# Import this anywhere: from app.services.d365_service import d365_service
d365_service = D365Service()
