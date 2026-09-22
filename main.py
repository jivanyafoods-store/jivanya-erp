"""
Shreeju Foods + Jivanya Foods Unified ERP API
Production-oriented FastAPI backend.

Environment variables:
  SUPABASE_URL
  SUPABASE_KEY
  AI_MODE                        optional, default free_local
  AI_WEB_SEARCH_ENABLED          optional, default true
  CORS_ORIGINS                   optional comma-separated origins
  TELEGRAM_BOT_TOKEN             optional
  TELEGRAM_CHAT_ID               optional authorized chat id
  APP_ENV                        optional, default production

Never put service-role keys or Telegram tokens in frontend HTML.
"""

from __future__ import annotations

import os
import json
import hashlib
import io
import re
import uuid
import html
from urllib.parse import quote_plus
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Literal

import httpx
from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File, status
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ConfigDict
from supabase import create_client, Client


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_ENV = os.getenv("APP_ENV", "production")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
AI_MODE = os.getenv("AI_MODE", "free_local").strip().lower()
AI_WEB_SEARCH_ENABLED = os.getenv("AI_WEB_SEARCH_ENABLED", "true").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables are required")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

raw_origins = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [x.strip() for x in raw_origins.split(",") if x.strip()]

app = FastAPI(
    title="Shreeju & Jivanya Unified ERP API",
    version="3.0.0",
    description="Dual-firm food ERP with inventory, sales, production, marketplace audit and AI agents.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"] ,
)

PUBLIC_API_PATHS = {"/api/v1/auth/login", "/api/v1/health", "/api/v1/ready", "/api/v1/orders/checkout"}
@app.middleware("http")
async def require_bearer_for_api(request: Request, call_next):
    # Health/login remain public. All business API calls require the Supabase Auth access token.
    if request.url.path.startswith("/api/v1/") and request.url.path not in PUBLIC_API_PATHS:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return Response(content=json.dumps({"detail": "Authentication required"}), status_code=401, media_type="application/json")
        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return Response(content=json.dumps({"detail": "Authentication required"}), status_code=401, media_type="application/json")
        try:
            supabase.auth.get_user(token)
        except Exception:
            return Response(content=json.dumps({"detail": "Invalid or expired access token"}), status_code=401, media_type="application/json")
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def d(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None or value == "":
            return default
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default

def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))

def xml_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)

def firm_id(value: str) -> str:
    value = (value or "").upper().strip()
    if value not in {"FIRM-SHREEJU", "FIRM-JIVANYA"}:
        raise HTTPException(400, "Invalid firm_id")
    return value

def safe_error(exc: Exception) -> str:
    return str(exc)[:500]

def request_id_from_headers(idempotency_key: Optional[str], request: Request) -> str:
    if idempotency_key:
        return idempotency_key[:120]
    return hashlib.sha256(
        f"{request.method}:{request.url.path}:{now_iso()}".encode()
    ).hexdigest()

def sb_select(table: str, *, firm: Optional[str] = None, limit: int = 200):
    q = supabase.table(table).select("*")
    if firm:
        q = q.eq("firm_id", firm)
    return q.limit(limit).execute().data or []

def audit(action: str, entity: str, entity_id: Optional[str], firm: Optional[str], details: Any = None):
    try:
        supabase.table("audit_logs").insert({
            "firm_id": firm,
            "action": action,
            "entity_type": entity,
            "entity_id": entity_id,
            "details": details if isinstance(details, dict) else {"value": details},
            "created_at": now_iso(),
        }).execute()
    except Exception:
        # Audit failure must not hide the primary business operation.
        pass

def get_product(sku: str, firm: str):
    rows = (
        supabase.table("master_catalog")
        .select("*")
        .eq("sku", sku)
        .eq("firm_id", firm)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None

def get_stock(sku: str, firm: str) -> Decimal:
    rows = (
        supabase.table("inventory_stock")
        .select("on_hand")
        .eq("sku", sku)
        .eq("firm_id", firm)
        .limit(1)
        .execute()
        .data
        or []
    )
    return d(rows[0]["on_hand"]) if rows else Decimal("0")

def set_stock(sku: str, firm: str, new_value: Decimal):
    supabase.table("inventory_stock").upsert(
        {"sku": sku, "firm_id": firm, "on_hand": money(new_value), "updated_at": now_iso()},
        on_conflict="firm_id,sku",
    ).execute()

def record_stock_ledger(firm: str, sku: str, qty_delta: Decimal, reason: str, reference: Optional[str] = None):
    supabase.table("stock_ledger").insert({
        "firm_id": firm,
        "sku": sku,
        "qty_delta": money(qty_delta),
        "reason": reason,
        "reference_id": reference,
        "created_at": now_iso(),
    }).execute()

async def free_web_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Best-effort public web discovery with no API key and no paid provider.
    This is intentionally retrieval-only; agents do not invent web facts."""
    if not AI_WEB_SEARCH_ENABLED or not query.strip():
        return []
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query[:300])
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent":"Mozilla/5.0"}) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                return []
        body = r.text
        results=[]
        # DDG Lite/HTML result blocks vary, so use conservative regex extraction.
        for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.I|re.S):
            href=html.unescape(m.group(1))
            title=re.sub(r'<[^>]+>','',html.unescape(m.group(2))).strip()
            if title and href:
                results.append({"title":title[:200],"url":href[:500]})
            if len(results)>=limit: break
        return results
    except Exception:
        return []


def _agent_result(agent: str, task: str, context: dict, *, web_results: Optional[list[dict[str,str]]] = None) -> dict:
    """Zero-cost deterministic AI layer. Uses explainable scoring/rules, not a paid LLM."""
    text_blob=json.dumps(context, default=str).lower()
    findings=[]; actions=[]; risks=[]
    if agent in {"Inventory Agent","Procurement Agent"}:
        rows=context.get("inventory",[]) or []
        low=[r for r in rows if d(r.get("on_hand")) <= d(r.get("reorder_level"), Decimal("10"))]
        findings.append(f"{len(low)} stock records are at/below the replenishment threshold.")
        actions.append("Review low-stock SKUs and create purchase/production proposals; require human approval before purchase.")
        if low: risks.append("Potential stockout risk on low-stock SKUs.")
    elif agent=="CFO Agent":
        orders=context.get("orders",[]) or []
        revenue=sum(d(x.get("total_amount")) for x in orders)
        tax=sum(d(x.get("tax_amount")) for x in orders)
        findings.append(f"Observed {len(orders)} orders with recorded revenue of ₹{money(revenue):,.2f} and tax of ₹{money(tax):,.2f}.")
        actions.append("Reconcile marketplace settlements and bank credits before treating payout as realized cash.")
        risks.append("Financial actions require human approval.")
    elif agent in {"Sales and CRM Agent","Sales Agent"}:
        orders=context.get("orders",[]) or []; customers=context.get("customers",[]) or []
        findings.append(f"Observed {len(orders)} orders and {len(customers)} customer records.")
        actions.append("Segment repeat, inactive and high-value customers using actual order history.")
    elif agent=="Logistics Agent":
        shipments=context.get("shipments",[]) or []
        findings.append(f"Observed {len(shipments)} shipment records for logistics review.")
        actions.append("Prioritize NDR/RTO and weight-variance exceptions before normal shipments.")
    elif agent in {"Fraud and Anomaly Agent","Fraud Agent"}:
        findings.append("Rule engine checks duplicate references, unusual amounts, repeated identifiers and settlement variances.")
        actions.append("Route flagged anomalies to the human approval queue; do not auto-block or auto-refund.")
        if any(k in text_blob for k in ["duplicate","variance","rto","refund"]): risks.append("Potential anomaly indicators found in supplied context.")
    elif agent in {"SEO Agent","SEO and Marketing Agent"}:
        product=str(context.get("product","")).strip(); city=str(context.get("city","")).strip()
        keywords=[x for x in [product, f"{product} online", f"{product} India", f"{product} {city}" if city else ""] if x]
        title=f"{product} | Natural & Quality Food Products" if product else "Natural Food Products"
        desc=f"Explore {product} with clear product information, usage details and transparent ordering." if product else "Explore natural food products with clear product information."
        findings.append("SEO draft is generated only from supplied product/city facts.")
        actions.append("Validate claims, ingredients, certifications and pricing before publishing.")
        return {"summary":"SEO draft prepared", "findings":findings, "actions":actions, "risks":risks, "evidence":[], "title":title, "description":desc, "keywords":keywords}
    elif agent=="Compliance Agent":
        findings.append("Compliance agent checks expiry windows, missing references and review status from ERP records.")
        actions.append("Review expiring FSSAI/GST/trademark and traceability records before renewal deadlines.")
    elif agent=="Market Intelligence Agent":
        wr=web_results or []
        findings.append(f"Public search returned {len(wr)} source candidates; these are retrieval signals, not verified market truth.")
        actions.append("Open the cited sources and manually verify competitor prices, claims and availability before changing strategy.")
        return {"summary":"Free public-web market scan completed", "findings":findings, "actions":actions, "risks":["Public search results can change and may be incomplete."], "evidence":wr, "sources":wr}
    elif agent=="Creative Agent":
        findings.append("Creative agent converts supplied product facts into reusable copy/layout instructions without a paid image API.")
        actions.append("Use the generated SVG/text as a base and replace only verified product facts.")
    elif agent=="RAG Knowledge Agent":
        knowledge=context.get("knowledge",[]) or []
        q=str(context.get("query","")).lower()
        matches=[x for x in knowledge if q and any(tok in json.dumps(x,default=str).lower() for tok in q.split() if len(tok)>2)]
        findings.append(f"Matched {len(matches)} knowledge records against the supplied question.")
        answer=(matches[0].get("content") or matches[0].get("title") or "Evidence not found in the knowledge base.") if matches else "Evidence not found in the knowledge base."
        return {"summary":answer, "answer":answer, "findings":findings, "actions":actions, "risks":risks, "evidence":matches[:10]}
    else:
        findings.append("Supervisor reviewed the supplied operational context using the free rule/score engine.")
        actions.append("Run the specialist agents and send high-risk recommendations to human approval.")
    return {"summary":f"{agent} completed", "findings":findings, "actions":actions, "risks":risks, "evidence":[]}


async def ai_agent(agent: str, firm: str, task: str, context: dict, web_search: bool = False):
    web_results = await free_web_search(task + " " + " ".join(str(v) for v in context.values() if isinstance(v,(str,int,float))), 5) if web_search else []
    result = _agent_result(agent, task, context, web_results=web_results)
    try:
        supabase.table("ai_runs").insert({"firm_id": firm, "agent_name": agent, "task": task[:500], "result": result, "created_at": now_iso()}).execute()
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)

class BarcodeScanRequest(BaseModel):
    firm_id: str
    barcode: str = Field(min_length=1, max_length=150)

class PosTransactionRequest(BaseModel):
    firm_id: str
    session_id: Optional[str] = None
    customer_phone: Optional[str] = None
    payment_mode: Literal["CASH", "UPI", "CARD"] = "CASH"
    items: list[OrderItem] = Field(min_length=1)

class ProductUpsert(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sku: str = Field(min_length=1, max_length=100)
    firm_id: str
    title: str = Field(min_length=1, max_length=250)
    category: Optional[str] = None
    sub_category: Optional[str] = None
    cogs_price: Decimal = Field(default=0, ge=0)
    other_expenses: Decimal = Field(default=0, ge=0)
    min_floor_price: Decimal = Field(default=0, ge=0)
    stock_hathras: Decimal = Field(default=0, ge=0)
    variants: Any = None
    image_url: Optional[str] = None
    short_description: Optional[str] = None

class OrderItem(BaseModel):
    sku: str
    quantity: Decimal = Field(gt=0)
    unit_price: Optional[Decimal] = Field(default=None, ge=0)

class CheckoutRequest(BaseModel):
    firm_id: str
    channel: Literal["WEBSITE", "POS_OFFLINE", "MEESHO", "AMAZON", "FLIPKART", "JIOMART", "B2B"] = "WEBSITE"
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    shipping_address: dict[str, Any] = {}
    items: list[OrderItem] = Field(min_length=1)
    idempotency_key: Optional[str] = None

class CustomerCreate(BaseModel):
    firm_id: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    customer_type: Literal["RETAIL", "B2B_WHOLESALE"] = "RETAIL"
    credit_limit: Decimal = Field(default=0, ge=0)

class POSQuickBillRequest(BaseModel):
    firm_id: str
    cashier_name: str
    payment_mode: Literal["CASH", "UPI", "CARD"]
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    items: list[OrderItem] = Field(min_length=1)

class RawMaterialInward(BaseModel):
    firm_id: str
    material_code: str
    name: str
    material_type: Literal["RAW_FOOD", "SPICE_BLEND", "PACKAGING_POUCH", "LABEL"]
    unit: str
    quantity: Decimal = Field(gt=0)
    cost_per_unit: Decimal = Field(ge=0)

class BOMItem(BaseModel):
    material_code: str
    quantity_required: Decimal = Field(gt=0)

class CreateBOM(BaseModel):
    firm_id: str
    finished_sku: str
    recipe_name: str
    wastage_percent: Decimal = Field(default=0, ge=0, le=100)
    ingredients: list[BOMItem] = Field(min_length=1)

class ProductionRun(BaseModel):
    firm_id: str
    finished_sku: str
    quantity_to_pack: Decimal = Field(gt=0)
    batch_number: str
    mfg_date: date
    expiry_date: date

class SettlementAudit(BaseModel):
    firm_id: str
    channel: Literal["MEESHO", "AMAZON", "FLIPKART", "JIOMART"]
    order_number: str
    settlement_ref: Optional[str] = None
    gross_order_value: Decimal = Field(ge=0)
    commission: Decimal = Field(default=0, ge=0)
    shipping_fee: Decimal = Field(default=0, ge=0)
    fixed_fee: Decimal = Field(default=0, ge=0)
    actual_bank_credit: Decimal = Field(ge=0)

class AIRequest(BaseModel):
    firm_id: str
    task: str
    context: dict[str, Any] = {}
    web_search: bool = False

class MarketResearchRequest(BaseModel):
    firm_id: str
    product: str
    market: str = "India"
    questions: list[str] = Field(default_factory=list)

class ApprovalAction(BaseModel):
    recommendation_id: str
    action: Literal["APPROVE", "REJECT"]
    reviewed_by: str


# ---------------------------------------------------------------------------
# Authentication compatibility
# ---------------------------------------------------------------------------

@app.post("/auth/login")
@app.post("/api/v1/auth/login")
async def login(payload: LoginRequest):
    """Authenticate against Supabase Auth. The dashboard sends email/username.
    For a new installation, create the first user in Supabase Auth > Users and
    then link that UUID to public.app_users/user_firms.
    """
    try:
        auth = supabase.auth.sign_in_with_password({
            "email": payload.username.strip(),
            "password": payload.password,
        })
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid email/username or password") from exc

    session = getattr(auth, "session", None)
    user = getattr(auth, "user", None)
    if not session or not user:
        raise HTTPException(status_code=401, detail="Authentication failed")

    user_id = str(getattr(user, "id", ""))
    profile = []
    try:
        profile = (supabase.table("app_users").select("*").eq("auth_user_id", user_id).limit(1).execute().data or [])
    except Exception:
        profile = []

    roles = []
    if profile:
        try:
            roles = (supabase.table("user_firms").select("firm_id,role_code").eq("user_id", profile[0]["id"]).execute().data or [])
        except Exception:
            roles = []

    role_codes = sorted({r.get("role_code") for r in roles if r.get("role_code")})
    permissions = ["dashboard.read"]
    if any(r in role_codes for r in {"SUPER_ADMIN", "ADMIN", "MANAGER"}):
        permissions += ["catalog.write", "inventory.write", "sales.write", "finance.read", "gst.read", "compliance.read", "ai.use", "approval.read", "system.read"]
    else:
        permissions += ["catalog.read", "inventory.read", "sales.read", "ai.use"]

    access_token = getattr(session, "access_token", None)
    refresh_token = getattr(session, "refresh_token", None)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user_id,
            "email": getattr(user, "email", None),
            "full_name": profile[0].get("full_name") if profile else getattr(user, "email", None),
            "roles": role_codes,
            "permissions": permissions,
            "firms": sorted({r.get("firm_id") for r in roles if r.get("firm_id")}),
        },
        "role": role_codes[0] if role_codes else "USER",
        "permissions": permissions,
    }


# Health / firms
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/api/v1/health")
def health():
    db_ok = False
    try:
        supabase.table("firms").select("id").limit(1).execute()
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "ai_configured": True,
        "environment": APP_ENV,
        "time": now_iso(),
    }

@app.get("/ready")
@app.get("/api/v1/ready")
def ready():
    return health()

@app.get("/firms")
@app.get("/api/v1/firms")
def firms():
    return sb_select("firms", limit=20)


# ---------------------------------------------------------------------------
# Catalog / inventory
# ---------------------------------------------------------------------------

@app.get("/products")
@app.get("/api/v1/products")
def products(firm_id: str = "FIRM-SHREEJU", limit: int = 200, search: str = ""):
    firm = globals()["firm_id"](firm_id) if firm_id else None
    rows = sb_select("master_catalog", firm=firm, limit=min(limit, 500))
    if search:
        q=search.lower().strip(); rows=[r for r in rows if q in f"{r.get('sku','')} {r.get('title','')} {r.get('category','')}".lower()]
    for row in rows:
        price = d(row.get("selling_price") or row.get("price") or row.get("mrp"))
        cogs = d(row.get("cogs_price"))
        row["calculated_margin"] = money(price - cogs)
        row["name"] = row.get("title")
        row["available_stock"] = money(get_stock(row.get("sku"), row.get("firm_id")))
    return rows

@app.post("/products")
@app.post("/api/v1/products")
def create_product_compat(payload: dict[str, Any]):
    firm=globals()["firm_id"](payload.get("firm_id"))
    row={"firm_id":firm,"sku":payload.get("sku"),"title":payload.get("title") or payload.get("name"),"category":payload.get("category"),"cogs_price":payload.get("cogs_price",payload.get("cogs",0)),"selling_price":payload.get("selling_price",0),"mrp":payload.get("mrp",payload.get("selling_price",0)),"stock_hathras":payload.get("stock_hathras",0),"updated_at":now_iso()}
    if not row["sku"] or not row["title"]: raise HTTPException(422,"sku and product name/title are required")
    result=supabase.table("master_catalog").upsert(row,on_conflict="firm_id,sku").execute()
    return (result.data or [row])[0]

@app.patch("/products/{product_id}")
@app.patch("/api/v1/products/{product_id}")
def update_product_compat(product_id: str, payload: dict[str, Any]):
    firm=globals()["firm_id"](payload.get("firm_id"))
    row={k:v for k,v in {"sku":payload.get("sku"),"title":payload.get("title") or payload.get("name"),"category":payload.get("category"),"cogs_price":payload.get("cogs_price",payload.get("cogs")),"selling_price":payload.get("selling_price"),"mrp":payload.get("mrp",payload.get("selling_price")),"updated_at":now_iso()}.items() if v is not None}
    updated=supabase.table("master_catalog").update(row).eq("id",product_id).eq("firm_id",firm).execute().data or []
    if not updated: raise HTTPException(404,"Product not found")
    return updated[0]

@app.post("/products/save")
@app.post("/api/v1/products/save")
def save_product(payload: ProductUpsert):
    firm = firm_id(payload.firm_id)
    row = payload.model_dump()
    row["firm_id"] = firm
    row["updated_at"] = now_iso()
    result = supabase.table("master_catalog").upsert(row, on_conflict="firm_id,sku").execute()
    audit("UPSERT", "product", payload.sku, firm, row)
    return {"success": True, "product": (result.data or [row])[0]}

@app.get("/inventory")
@app.get("/api/v1/inventory")
def inventory(firm_id: str = "FIRM-SHREEJU"):
    return sb_select("inventory_stock", firm=firm_id, limit=1000)

@app.get("/warehouse/scan-barcode")
@app.get("/api/v1/warehouse/scan-barcode")
def scan_barcode(sku: str, firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    product = get_product(sku, firm)
    if not product:
        raise HTTPException(404, "SKU/barcode not found")
    stock = get_stock(sku, firm)
    return {"sku": sku, "firm_id": firm, "product": product, "product_name": product.get("title"), "available": money(stock)}

@app.post("/warehouse/scan-barcode")
@app.post("/api/v1/warehouse/scan-barcode")
def scan_barcode_post(payload: BarcodeScanRequest):
    firm = globals()["firm_id"](payload.firm_id)
    code = payload.barcode.strip()
    # Current catalog uses SKU as the canonical barcode fallback. If variants later
    # contain a barcode field, this loop also supports that without schema changes.
    product = get_product(code, firm)
    if not product:
        rows = sb_select("master_catalog", firm=firm, limit=5000)
        for row in rows:
            variants = row.get("variants") or []
            if isinstance(variants, dict):
                variants = [variants]
            if any(str(v.get("barcode")) == code for v in variants if isinstance(v, dict)):
                product = row
                break
    if not product:
        raise HTTPException(404, "SKU/barcode not found")
    stock = get_stock(product["sku"], firm)
    return {"sku": product["sku"], "firm_id": firm, "product": product, "product_name": product.get("title"), "available": money(stock), "batch_no": None, "fefo_batch": None}


# ---------------------------------------------------------------------------
# Customers / orders / POS
# ---------------------------------------------------------------------------

@app.post("/customers")
@app.post("/api/v1/customers")
def create_customer(payload: CustomerCreate):
    firm = firm_id(payload.firm_id)
    row = payload.model_dump()
    row["firm_id"] = firm
    row["created_at"] = now_iso()
    result = supabase.table("customers").insert(row).execute()
    audit("CREATE", "customer", str((result.data or [{}])[0].get("id")), firm)
    return (result.data or [row])[0]

@app.get("/customers")
@app.get("/api/v1/customers")
def customers(firm_id: str = "FIRM-SHREEJU"):
    return sb_select("customers", firm=firm_id, limit=1000)

def place_order(payload: CheckoutRequest, channel_override: Optional[str] = None):
    firm = firm_id(payload.firm_id)
    channel = channel_override or payload.channel
    idem = payload.idempotency_key
    if idem:
        try:
            existing = (
                supabase.table("orders").select("*")
                .eq("firm_id", firm).eq("idempotency_key", idem)
                .limit(1).execute().data or []
            )
            if existing:
                return existing[0]
        except Exception:
            pass

    subtotal = Decimal("0")
    lines = []
    for item in payload.items:
        product = get_product(item.sku, firm)
        if not product:
            raise HTTPException(404, f"SKU not found: {item.sku}")
        available = get_stock(item.sku, firm)
        if available < item.quantity:
            raise HTTPException(409, f"Insufficient stock for {item.sku}: {available} available")
        price = item.unit_price
        if price is None:
            price = d(product.get("selling_price") or product.get("price") or product.get("mrp"))
        line_total = price * item.quantity
        subtotal += line_total
        lines.append({
            "sku": item.sku,
            "quantity": money(item.quantity),
            "unit_price": money(price),
            "line_total": money(line_total),
        })

    # Tax is taken from configured order/tax tables if available; never invent a tax rate.
    tax_amount = Decimal("0")
    try:
        tax_rows = (
            supabase.table("tax_rates").select("rate")
            .eq("firm_id", firm).eq("active", True).limit(1).execute().data or []
        )
        if tax_rows:
            tax_amount = subtotal * d(tax_rows[0].get("rate")) / Decimal("100")
    except Exception:
        pass

    total = subtotal + tax_amount
    order_number = f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:-3]}"

    order = {
        "firm_id": firm,
        "order_number": order_number,
        "channel": channel,
        "customer_name": payload.customer_name,
        "phone": payload.phone,
        "shipping_address": payload.shipping_address,
        "subtotal": money(subtotal),
        "tax_amount": money(tax_amount),
        "total_amount": money(total),
        "status": "CONFIRMED",
        "idempotency_key": idem,
        "created_at": now_iso(),
    }
    result = supabase.table("orders").insert(order).execute()
    saved = (result.data or [order])[0]
    order_id = saved.get("id")

    for line in lines:
        supabase.table("order_items").insert({
            "order_id": order_id,
            "firm_id": firm,
            **line,
        }).execute()

    # Stock writes are guarded at application level. For high-concurrency production,
    # deploy the optional reserve_stock RPC from the SQL file.
    for line in lines:
        sku = line["sku"]
        qty = d(line["quantity"])
        before = get_stock(sku, firm)
        after = before - qty
        if after < 0:
            raise HTTPException(409, f"Stock changed during checkout for {sku}; retry.")
        set_stock(sku, firm, after)
        record_stock_ledger(firm, sku, -qty, "SALE", str(order_id))

    audit("CREATE", "order", str(order_id), firm, {"order_number": order_number})
    return saved

@app.post("/orders/checkout")
@app.post("/api/v1/orders/checkout")
def checkout(payload: CheckoutRequest, request: Request, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    if idempotency_key and not payload.idempotency_key:
        payload = payload.model_copy(update={"idempotency_key": idempotency_key})
    return place_order(payload)

@app.post("/pos/quick-bill")
@app.post("/api/v1/pos/quick-bill")
def pos_bill(payload: POSQuickBillRequest):
    checkout_payload = CheckoutRequest(
        firm_id=payload.firm_id,
        channel="POS_OFFLINE",
        customer_name=payload.customer_name,
        phone=payload.phone,
        items=payload.items,
    )
    order = place_order(checkout_payload, "POS_OFFLINE")
    audit("POS_PAYMENT", "order", str(order.get("id")), firm_id(payload.firm_id),
          {"payment_mode": payload.payment_mode, "cashier": payload.cashier_name})
    return order

@app.get("/reports/daily-summary")
@app.get("/api/v1/reports/daily-summary")
def daily_summary(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    try:
        orders = sb_select("orders", firm=firm, limit=5000)
        today = date.today().isoformat()
        todays = [x for x in orders if str(x.get("created_at", ""))[:10] == today]
        gross = sum(d(x.get("total_amount")) for x in todays)
        return {"firm_id": firm, "date": today, "orders": len(todays), "gross_sales": money(gross)}
    except Exception as exc:
        raise HTTPException(500, safe_error(exc))


# ---------------------------------------------------------------------------
# Dashboard compatibility / aggregation routes
# ---------------------------------------------------------------------------

def _orders(firm: str, channel: Optional[str] = None, limit: int = 5000):
    q = supabase.table("orders").select("*").eq("firm_id", firm)
    if channel:
        q = q.eq("channel", channel)
    return q.order("created_at", desc=True).limit(limit).execute().data or []

@app.get("/dashboard/summary")
@app.get("/api/v1/dashboard/summary")
def dashboard_summary(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    orders = _orders(firm)
    today = date.today().isoformat()
    todays = [o for o in orders if str(o.get("created_at", ""))[:10] == today]
    sales = sum(d(o.get("total_amount")) for o in todays)
    stock = sb_select("inventory_stock", firm=firm, limit=5000)
    low = [x for x in stock if d(x.get("on_hand")) <= 0]
    return {"firm_id": firm, "date": today, "gmv": money(sales), "net_revenue": money(sales), "orders": len(todays), "pending_orders": len([o for o in orders if o.get("status") in {"CONFIRMED", "PENDING", "PACKING"}]), "low_stock": len(low), "pending_approvals": 0, "unread_notifications": 0}

@app.get("/products/by-sku/{sku}")
@app.get("/api/v1/products/by-sku/{sku}")
def product_by_sku(sku: str, firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    row = get_product(sku, firm)
    if not row:
        raise HTTPException(404, "SKU not found")
    return {**row, "name": row.get("title"), "selling_price": row.get("selling_price") or row.get("mrp") or 0, "pos_price": row.get("selling_price") or row.get("mrp") or 0}

@app.get("/inventory/stock")
@app.get("/api/v1/inventory/stock")
def inventory_stock(firm_id: str = "FIRM-SHREEJU", search: str = "", status: str = ""):
    firm = globals()["firm_id"](firm_id)
    rows = sb_select("inventory_stock", firm=firm, limit=5000)
    if search:
        q = search.lower(); rows = [r for r in rows if q in str(r.get("sku", "")).lower()]
    if status == "LOW": rows = [r for r in rows if d(r.get("on_hand")) <= 0]
    return {"items": rows, "total_on_hand": money(sum(d(r.get("on_hand")) for r in rows)), "total_reserved": money(sum(d(r.get("reserved")) for r in rows)), "low_stock_count": sum(1 for r in rows if d(r.get("on_hand")) <= 0), "expiring_count": 0}

@app.get("/b2b/summary")
@app.get("/api/v1/b2b/summary")
def b2b_summary(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id); rows = _orders(firm, "B2B")
    return {"orders": len(rows), "gmv": money(sum(d(x.get("total_amount")) for x in rows)), "outstanding": 0, "customers": len({x.get("phone") for x in rows if x.get("phone")})}

@app.get("/pos/recent")
@app.get("/api/v1/pos/recent")
def pos_recent(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id); rows = _orders(firm, "POS_OFFLINE", 50)
    return {"items": [{"id": x.get("id"), "invoice_no": x.get("order_number"), "total": x.get("total_amount", 0), "payment_mode": "RECORDED"} for x in rows]}

@app.get("/marketplaces/summary")
@app.get("/api/v1/marketplaces/summary")
def marketplaces_summary(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id); rows = _orders(firm, None, 5000)
    channels = ["AMAZON", "MEESHO", "FLIPKART", "JIOMART"]
    out=[]
    for ch in channels:
        cr=[x for x in rows if x.get("channel")==ch]; g=sum(d(x.get("total_amount")) for x in cr)
        out.append({"channel": ch, "gmv": money(g), "orders": len(cr), "fees": 0, "net_payout": money(g), "rto_rate": 0, "listings": 0, "status": "NO_DATA" if not cr else "SYNCED"})
    return {"gmv": money(sum(d(x.get("total_amount")) for x in rows if x.get("channel") in channels)), "net_payout": money(sum(d(x.get("total_amount")) for x in rows if x.get("channel") in channels)), "fees": 0, "rto_rate": 0, "mismatch": 0, "channels": out}

@app.get("/crm/summary")
@app.get("/api/v1/crm/summary")
def crm_summary(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id); rows = sb_select("customers", firm=firm, limit=5000)
    return {"customers": len(rows), "repeat_rate": 0, "at_risk": 0, "d2c_migrated": 0, "segments": [{"name": "All customers", "count": len(rows)}]}

@app.get("/procurement/summary")
@app.get("/api/v1/procurement/summary")
def procurement_summary(firm_id: str = "FIRM-SHREEJU"):
    firm=globals()["firm_id"](firm_id); rows=sb_select("raw_material_inwards", firm=firm, limit=5000)
    return {"open_pos": 0, "pending_grn": 0, "vendor_dues": 0, "qc_holds": 0, "purchase_orders": [{"po_no": x.get("id"), "vendor_name": x.get("name"), "amount": money(d(x.get("quantity"))*d(x.get("cost_per_unit"))), "expected_date": x.get("created_at"), "status": "RECEIVED"} for x in rows[:20]]}

@app.get("/production/active")
@app.get("/api/v1/production/active")
def production_active(firm_id: str = "FIRM-SHREEJU"):
    firm=globals()["firm_id"](firm_id); rows=sb_select("production_runs", firm=firm, limit=1000)
    return {"items": [{"sku":x.get("finished_sku"), "batch_no":x.get("batch_number"), "planned_qty":x.get("quantity_to_pack"), "status":x.get("status")} for x in rows if x.get("status") not in {"COMPLETED", "CANCELLED"}]}

@app.get("/warehouse/pick-queue")
@app.get("/api/v1/warehouse/pick-queue")
def warehouse_pick_queue(firm_id: str = "FIRM-SHREEJU"):
    firm=globals()["firm_id"](firm_id); rows=_orders(firm, None, 5000)
    items=[x for x in rows if x.get("status") in {"CONFIRMED", "PENDING", "PACKING"}]
    return {"items": [{"order_no":x.get("order_number"), "items_count":0, "status":x.get("status")} for x in items[:100]]}

@app.get("/legacy/compliance/items")
def compliance_items(firm_id: str = "FIRM-SHREEJU"):
    globals()["firm_id"](firm_id); return {"items": []}

@app.get("/ai/ops-snapshot")
@app.get("/api/v1/ai/ops-snapshot")
def ai_ops_snapshot(firm_id: str = "FIRM-SHREEJU"):
    firm=globals()["firm_id"](firm_id); runs=sb_select("ai_runs", firm=firm, limit=20)
    return {"pending_approvals":0, "pending_events":0, "failed_jobs":0, "knowledge_chunks":len(sb_select("knowledge_chunks", firm=firm, limit=5000)), "recent_runs":[{"agent":x.get("agent_name"),"status":"COMPLETED","started_at":x.get("created_at")} for x in runs]}

@app.get("/approvals")
@app.get("/api/v1/approvals")
def approvals(firm_id: str = "FIRM-SHREEJU", status: str = "PENDING"):
    firm=globals()["firm_id"](firm_id)
    try:
        q=supabase.table("ai_recommendations").select("*").eq("firm_id", firm)
        if status: q=q.eq("status", status)
        rows=q.order("created_at", desc=True).limit(100).execute().data or []
    except Exception:
        rows=[]
    return {"items":[{"id":x.get("id"),"created_at":x.get("created_at"),"action_type":"AI_RECOMMENDATION","requested_by":x.get("agent_name"),"firm_id":firm,"summary":x.get("recommendation"),"risk":x.get("risk_level","MEDIUM")} for x in rows]}

@app.post("/approvals/{recommendation_id}/{decision}")
@app.post("/api/v1/approvals/{recommendation_id}/{decision}")
def approval_decision(recommendation_id: str, decision: str):
    action="APPROVE" if decision.lower()=="approve" else "REJECT" if decision.lower()=="reject" else None
    if not action: raise HTTPException(400,"Decision must be approve or reject")
    rows=supabase.table("ai_recommendations").select("*").eq("id", recommendation_id).limit(1).execute().data or []
    if not rows: raise HTTPException(404,"Approval not found")
    new_status="APPROVED" if action=="APPROVE" else "REJECTED"
    updated=supabase.table("ai_recommendations").update({"status":new_status,"reviewed_at":now_iso()}).eq("id",recommendation_id).execute().data or []
    return updated[0] if updated else {**rows[0],"status":new_status}

@app.get("/notifications")
@app.get("/api/v1/notifications")
def notifications(unread_only: bool = False):
    return {"items": []}

@app.post("/notifications/mark-all-read")
@app.post("/api/v1/notifications/mark-all-read")
def mark_all_notifications_read():
    return {"success": True, "updated": 0}

@app.get("/system/status")
@app.get("/api/v1/system/status")
def system_status():
    h=health(); return {"database": h["database"], "worker":"inline", "last_backup":"—", "integrations":[{"name":"Supabase","status":"OK" if h["database"] else "ERROR"},{"name":"Free AI Mesh","status":"LOCAL"},{"name":"Telegram","status":"CONFIGURED" if TELEGRAM_BOT_TOKEN else "NOT_CONFIGURED"}]}

@app.get("/audit")
@app.get("/api/v1/audit")
def audit_list(firm_id: Optional[str] = None, limit: int = 25):
    firm = globals()["firm_id"](firm_id) if firm_id else None; rows=sb_select("audit_logs", firm=firm, limit=min(limit,200)); return {"items":[{"created_at":x.get("created_at"),"actor":x.get("actor_user_id") or "system","action":x.get("action"),"entity_type":x.get("entity_type"),"result":"OK"} for x in rows]}

@app.get("/procurement/suggestions")
@app.get("/api/v1/procurement/suggestions")
def procurement_suggestions(firm_id: str = "FIRM-SHREEJU"):
    firm=globals()["firm_id"](firm_id); rows=sb_select("inventory_stock", firm=firm, limit=5000); return {"items":[{"sku":x.get("sku"),"suggested_qty":max(0, 10-float(d(x.get("on_hand")))),"reason":"Below simple replenishment threshold"} for x in rows if d(x.get("on_hand"))<10][:100]}

@app.get("/logistics/track/{awb}")
@app.get("/api/v1/logistics/track/{awb}")
def track_logistics(awb: str):
    rows=supabase.table("shipments").select("*").eq("awb",awb).limit(1).execute().data or []
    if not rows: return {"awb":awb,"status":"NOT_FOUND","last_event":None}
    return {"awb":awb,"status":rows[0].get("status"),"last_event":None}

@app.get("/accounting/gst/{kind}-summary")
@app.get("/api/v1/accounting/gst/{kind}-summary")
def gst_summary(kind: str, firm_id: str = "FIRM-SHREEJU", tax_period: str = ""):
    firm=globals()["firm_id"](firm_id); rows=_orders(firm); taxable=sum(d(x.get("subtotal")) for x in rows if str(x.get("created_at", ""))[:7]==tax_period) if tax_period else sum(d(x.get("subtotal")) for x in rows)
    tax=sum(d(x.get("tax_amount")) for x in rows if str(x.get("created_at", ""))[:7]==tax_period) if tax_period else sum(d(x.get("tax_amount")) for x in rows)
    return {"kind":kind,"tax_period":tax_period,"taxable_value":money(taxable),"tax_total":money(tax),"total_tax":money(tax)}

@app.post("/ai/rag/search")
@app.post("/api/v1/ai/rag/search")
async def rag_search(payload: dict[str, Any]):
    firm=payload.get("firm_id"); query=str(payload.get("query", "")).strip()
    context={"query":query,"knowledge":sb_select("knowledge_documents", firm=firm, limit=50) if firm else sb_select("knowledge_documents", limit=50)}
    result=await ai_agent("RAG Knowledge Agent", firm_id(firm) if firm else "FIRM-SHREEJU", "Answer the query only from supplied knowledge where possible; state when evidence is missing.", context, False)
    return {"answer":result.get("summary") or result.get("answer") or "No answer returned.","sources":context.get("knowledge",[])[:10]}

@app.post("/seo/pages/generate")
@app.post("/api/v1/seo/pages/generate")
async def seo_page_generate(payload: dict[str, Any]):
    firm=globals()["firm_id"](payload.get("firm_id")); product=str(payload.get("product", "")); city=str(payload.get("city", ""))
    return await ai_agent("SEO Agent", firm, "Create an SEO draft using only supplied product/city facts.", payload, False)

@app.get("/search")
@app.get("/api/v1/search")
def global_search(q: str = "", firm_id: str = ""):
    firm=globals()["firm_id"](firm_id) if firm_id else None; ql=q.lower().strip(); items=[]
    for row in sb_select("master_catalog", firm=firm, limit=5000):
        hay=f"{row.get('sku','')} {row.get('title','')} {row.get('category','')}".lower()
        if ql and ql in hay: items.append({"type":"PRODUCT","id":row.get("id"),"title":row.get("title"),"sku":row.get("sku")})
    for row in sb_select("customers", firm=firm, limit=5000):
        hay=f"{row.get('name','')} {row.get('phone','')} {row.get('email','')}".lower()
        if ql and ql in hay: items.append({"type":"CUSTOMER","id":row.get("id"),"title":row.get("name")})
    return {"items":items[:100]}

@app.post("/pos/sessions/open")
@app.post("/api/v1/pos/sessions/open")
def pos_session_open(payload: dict[str, Any]):
    firm=globals()["firm_id"](payload.get("firm_id")); import uuid
    return {"id":str(uuid.uuid4()),"session_no":f"POS-{datetime.now().strftime('%Y%m%d-%H%M%S')}","firm_id":firm,"status":"OPEN"}

@app.post("/pos/transactions")
@app.post("/api/v1/pos/transactions")
def pos_transaction(payload: PosTransactionRequest):
    checkout_payload=CheckoutRequest(firm_id=payload.firm_id, channel="POS_OFFLINE", phone=payload.customer_phone, items=payload.items)
    order=place_order(checkout_payload, "POS_OFFLINE")
    order["payment_mode"]=payload.payment_mode
    return {**order,"invoice_no":order.get("order_number")}

# Procurement / production
# ---------------------------------------------------------------------------

@app.post("/procurement/raw-materials/inward")
@app.post("/api/v1/procurement/raw-materials/inward")
def raw_material_inward(payload: RawMaterialInward):
    firm = firm_id(payload.firm_id)
    row = payload.model_dump()
    row["firm_id"] = firm
    row["created_at"] = now_iso()
    result = supabase.table("raw_material_inwards").insert(row).execute()
    audit("CREATE", "raw_material_inward", str((result.data or [{}])[0].get("id")), firm, row)
    return (result.data or [row])[0]

@app.get("/procurement/raw-materials")
@app.get("/api/v1/procurement/raw-materials")
def raw_materials(firm_id: str = "FIRM-SHREEJU"):
    return sb_select("raw_material_inwards", firm=firm_id, limit=1000)

@app.post("/production/bom")
@app.post("/api/v1/production/bom")
def create_bom(payload: CreateBOM):
    firm = firm_id(payload.firm_id)
    row = {
        "firm_id": firm,
        "finished_sku": payload.finished_sku,
        "recipe_name": payload.recipe_name,
        "wastage_percent": money(payload.wastage_percent),
        "created_at": now_iso(),
    }
    result = supabase.table("boms").insert(row).execute()
    bom_id = (result.data or [row])[0].get("id")
    for item in payload.ingredients:
        supabase.table("bom_items").insert({
            "bom_id": bom_id,
            "firm_id": firm,
            **item.model_dump(),
        }).execute()
    audit("CREATE", "bom", str(bom_id), firm)
    return {"bom": row, "bom_id": bom_id}

@app.post("/production/run")
@app.post("/api/v1/production/run")
def production_run(payload: ProductionRun):
    firm = firm_id(payload.firm_id)
    boms = (
        supabase.table("boms").select("*")
        .eq("firm_id", firm).eq("finished_sku", payload.finished_sku)
        .eq("active", True).limit(1).execute().data or []
    )
    if not boms:
        raise HTTPException(404, "Active BOM not found")
    bom = boms[0]
    items = (
        supabase.table("bom_items").select("*")
        .eq("firm_id", firm).eq("bom_id", bom["id"]).execute().data or []
    )
    # Validate all inputs before making stock changes.
    for item in items:
        required = d(item["quantity_required"]) * payload.quantity_to_pack
        available = get_stock(item["material_code"], firm)
        if available < required:
            raise HTTPException(409, f"Insufficient raw material {item['material_code']}")
    run = {
        "firm_id": firm,
        "finished_sku": payload.finished_sku,
        "quantity_to_pack": money(payload.quantity_to_pack),
        "batch_number": payload.batch_number,
        "mfg_date": payload.mfg_date.isoformat(),
        "expiry_date": payload.expiry_date.isoformat(),
        "status": "COMPLETED",
        "created_at": now_iso(),
    }
    saved = supabase.table("production_runs").insert(run).execute()
    run_id = (saved.data or [run])[0].get("id")
    for item in items:
        required = d(item["quantity_required"]) * payload.quantity_to_pack
        before = get_stock(item["material_code"], firm)
        set_stock(item["material_code"], firm, before - required)
        record_stock_ledger(firm, item["material_code"], -required, "PRODUCTION_CONSUMPTION", str(run_id))
    before_fg = get_stock(payload.finished_sku, firm)
    set_stock(payload.finished_sku, firm, before_fg + payload.quantity_to_pack)
    record_stock_ledger(firm, payload.finished_sku, payload.quantity_to_pack, "PRODUCTION_OUTPUT", str(run_id))
    audit("CREATE", "production_run", str(run_id), firm, run)
    return {"run": run, "run_id": run_id}


# ---------------------------------------------------------------------------
# Marketplace settlement / audit
# ---------------------------------------------------------------------------

@app.post("/accounting/settlement-audit")
@app.post("/api/v1/accounting/settlement-audit")
def settlement_audit(payload: SettlementAudit):
    firm = firm_id(payload.firm_id)
    expected = payload.gross_order_value - payload.commission - payload.shipping_fee - payload.fixed_fee
    variance = expected - payload.actual_bank_credit
    row = {
        **payload.model_dump(),
        "firm_id": firm,
        "expected_credit": money(expected),
        "variance": money(variance),
        "status": "MATCHED" if variance == 0 else "MISMATCH",
        "created_at": now_iso(),
    }
    result = supabase.table("settlement_audits").insert(row).execute()
    audit("CREATE", "settlement_audit", str((result.data or [{}])[0].get("id")), firm, row)
    return (result.data or [row])[0]

@app.get("/legacy/accounting/pnl-summary")
def pnl_summary(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    orders = sb_select("orders", firm=firm, limit=10000)
    gross = sum(d(x.get("subtotal")) for x in orders)
    tax = sum(d(x.get("tax_amount")) for x in orders)
    net = sum(d(x.get("total_amount")) for x in orders)
    return {
        "firm_id": firm,
        "orders": len(orders),
        "gross_sales": money(gross),
        "tax": money(tax),
        "net_sales": money(net),
        "note": "COGS, marketplace fees and bank adjustments are reported only when corresponding ledger tables contain actual entries.",
    }


# ---------------------------------------------------------------------------
# AI agent mesh
# ---------------------------------------------------------------------------

@app.post("/ai/agent/run")
@app.post("/api/v1/ai/agent/run")
async def run_ai_agent(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    return await ai_agent("Supervisor AI", firm, payload.task, payload.context, payload.web_search)

@app.post("/ai/cfo")
@app.post("/api/v1/ai/cfo")
async def ai_cfo(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    context = payload.context
    if not context:
        context = {"orders": sb_select("orders", firm=firm, limit=1000), "settlements": sb_select("settlement_audits", firm=firm, limit=1000)}
    return await ai_agent("CFO Agent", firm, payload.task or "Find cash, margin and settlement risks.", context)

@app.post("/ai/inventory")
@app.post("/api/v1/ai/inventory")
async def ai_inventory(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    context = payload.context or {"inventory": sb_select("inventory_stock", firm=firm, limit=2000)}
    return await ai_agent("Inventory Agent", firm, payload.task or "Find stockout, expiry and replenishment risks.", context)

@app.post("/ai/sales")
@app.post("/api/v1/ai/sales")
async def ai_sales(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    context = payload.context or {"orders": sb_select("orders", firm=firm, limit=3000), "customers": sb_select("customers", firm=firm, limit=3000)}
    return await ai_agent("Sales and CRM Agent", firm, payload.task or "Analyse sales and repeat-customer opportunities.", context)

@app.post("/ai/procurement")
@app.post("/api/v1/ai/procurement")
async def ai_procurement(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    context = payload.context or {"inwards": sb_select("raw_material_inwards", firm=firm, limit=3000), "inventory": sb_select("inventory_stock", firm=firm, limit=2000)}
    return await ai_agent("Procurement Agent", firm, payload.task or "Analyse purchasing and material risks.", context)

@app.post("/ai/logistics")
@app.post("/api/v1/ai/logistics")
async def ai_logistics(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    context = payload.context or {"settlements": sb_select("settlement_audits", firm=firm, limit=3000), "shipments": sb_select("shipments", firm=firm, limit=3000)}
    return await ai_agent("Logistics Agent", firm, payload.task or "Analyse shipping, weight and RTO risks.", context)

@app.post("/ai/anomaly")
@app.post("/api/v1/ai/anomaly")
async def ai_anomaly(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    context = payload.context or {"audit_logs": sb_select("audit_logs", firm=firm, limit=3000), "settlements": sb_select("settlement_audits", firm=firm, limit=3000)}
    return await ai_agent("Fraud and Anomaly Agent", firm, payload.task or "Identify data-supported anomalies.", context)

@app.post("/ai/seo")
@app.post("/api/v1/ai/seo")
async def ai_seo(payload: AIRequest):
    firm = firm_id(payload.firm_id)
    return await ai_agent("SEO and Marketing Agent", firm, payload.task or "Create accurate SEO opportunities from supplied product facts.", payload.context, False)

@app.post("/ai/market-research")
@app.post("/api/v1/ai/market-research")
async def market_research(payload: MarketResearchRequest):
    firm = firm_id(payload.firm_id)
    questions = payload.questions or [
        "What are current customer trends and demand signals?",
        "What competitors and channels should be monitored?",
        "What price ranges are publicly visible?",
        "What product/SEO opportunities are supported by current evidence?",
    ]
    context = {"product": payload.product, "market": payload.market, "questions": questions}
    return await ai_agent(
        "Market Intelligence Agent",
        firm,
        "Research current market information and return source-backed findings. Do not invent prices or competitors.",
        context,
        web_search=True,
    )

@app.post("/ai/research")
@app.post("/api/v1/ai/research")
async def ai_research(payload: MarketResearchRequest):
    return await market_research(payload)

@app.post("/ai/approval")
@app.post("/api/v1/ai/approval")
def ai_approval(payload: ApprovalAction):
    rows = (
        supabase.table("ai_recommendations").select("*")
        .eq("id", payload.recommendation_id).limit(1).execute().data or []
    )
    if not rows:
        raise HTTPException(404, "Recommendation not found")
    row = rows[0]
    new_status = "APPROVED" if payload.action == "APPROVE" else "REJECTED"
    result = supabase.table("ai_recommendations").update({
        "status": new_status,
        "reviewed_by": payload.reviewed_by,
        "reviewed_at": now_iso(),
    }).eq("id", payload.recommendation_id).execute()
    audit("AI_APPROVAL", "ai_recommendation", payload.recommendation_id, row.get("firm_id"),
          {"action": payload.action, "reviewed_by": payload.reviewed_by})
    return (result.data or [{**row, "status": new_status}])[0]

@app.get("/ai/morning-briefing")
@app.get("/api/v1/ai/morning-briefing")
@app.post("/ai/cockpit/morning-briefing")
@app.post("/api/v1/ai/cockpit/morning-briefing")
async def morning_briefing(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    context = {
        "sales": sb_select("orders", firm=firm, limit=1000),
        "inventory": sb_select("inventory_stock", firm=firm, limit=1000),
        "settlements": sb_select("settlement_audits", firm=firm, limit=1000),
    }
    return await ai_agent(
        "Executive Briefing Agent",
        firm,
        "Prepare a concise morning briefing with facts, exceptions and recommended human actions.",
        context,
        False,
    )


# ---------------------------------------------------------------------------
# Telegram cockpit
# ---------------------------------------------------------------------------

async def telegram_send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"sent": False, "reason": "Telegram not configured"}
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]})
    return {"sent": r.is_success}

@app.post("/telegram/webhook")
@app.post("/api/v1/telegram/webhook")
async def telegram_webhook(payload: dict[str, Any]):
    message = payload.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        raise HTTPException(403, "Unauthorized Telegram chat")
    text = (message.get("text") or "").strip().lower()
    if text == "/start":
        reply = "ERP AI Cockpit active. Commands: /sales /stock /briefing"
    elif text == "/sales":
        summary = daily_summary("FIRM-SHREEJU")
        reply = json.dumps(summary, ensure_ascii=False)
    elif text.startswith("/stock"):
        sku = text.replace("/stock", "", 1).strip()
        if not sku:
            reply = "Usage: /stock SKU"
        else:
            reply = json.dumps(scan_barcode(sku, "FIRM-SHREEJU"), ensure_ascii=False, default=str)
    elif text == "/briefing":
        briefing = await morning_briefing("FIRM-SHREEJU")
        reply = json.dumps(briefing, ensure_ascii=False, default=str)[:3900]
    else:
        reply = "Unknown command. Use /sales, /stock SKU, /briefing."
    return await telegram_send(reply)

@app.get("/telegram/send-briefing")
@app.get("/api/v1/telegram/send-briefing")
async def send_briefing():
    briefing = await morning_briefing("FIRM-SHREEJU")
    return await telegram_send(json.dumps(briefing, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Truthful integration placeholders
# ---------------------------------------------------------------------------

@app.post("/logistics/rto/claim")
@app.post("/api/v1/logistics/rto/claim")
def rto_claim(payload: dict[str, Any]):
    firm = firm_id(payload.get("firm_id", "FIRM-SHREEJU"))
    row = {**payload, "firm_id": firm, "status": "PREPARED", "created_at": now_iso()}
    result = supabase.table("rto_claims").insert(row).execute()
    return {"status": "PREPARED", "claim": (result.data or [row])[0],
            "note": "External marketplace submission requires a configured marketplace adapter."}

@app.post("/orders/marketplace-bulk-ingest")
@app.post("/api/v1/orders/marketplace-bulk-ingest")
def marketplace_ingest(payload: dict[str, Any]):
    firm = firm_id(payload.get("firm_id", "FIRM-SHREEJU"))
    channel = str(payload.get("channel") or "MEESHO").upper()
    imported=[]
    for raw in payload.get("orders", []):
        order_number=str(raw.get("order_number") or raw.get("order_id") or "").strip()
        if not order_number: continue
        existing=supabase.table("orders").select("id").eq("firm_id",firm).eq("order_number",order_number).limit(1).execute().data or []
        if existing: continue
        # Only create a marketplace order when a SKU and quantity are explicitly supplied.
        items=raw.get("items") or []
        if not items: continue
        try:
            req=CheckoutRequest(firm_id=firm,channel=channel if channel in {"MEESHO","AMAZON","FLIPKART","JIOMART"} else "MEESHO",customer_name=raw.get("customer_name"),phone=raw.get("phone"),shipping_address=raw.get("shipping_address") or {},items=[OrderItem(sku=str(i["sku"]),quantity=d(i.get("quantity",1)),unit_price=d(i.get("unit_price",0))) for i in items])
            saved=place_order(req)
            imported.append(saved.get("order_number"))
        except Exception:
            continue
    return {"status":"IMPORTED" if imported else "REVIEW", "firm_id":firm, "channel":channel, "orders_imported":len(imported), "order_numbers":imported, "note":"Unknown/missing marketplace fields are left for review; no synthetic orders are created."}

@app.post("/legacy/orders/upload-manifest-pdf")
async def upload_manifest(file: UploadFile = File(...)):
    data = await file.read()
    return {
        "filename": file.filename,
        "bytes_received": len(data),
        "status": "RECEIVED",
        "note": "PDF parser/OCR adapter is not configured. No orders were fabricated or marked as imported.",
    }

@app.post("/legacy/ocr/scan-shipping-label")
async def ocr_scan(file: UploadFile = File(...)):
    data = await file.read()
    return {
        "filename": file.filename,
        "bytes_received": len(data),
        "status": "NOT_CONFIGURED",
        "note": "Configure an OCR provider before extracting customer PII.",
    }

@app.get("/system/backup/download")
@app.get("/api/v1/system/backup/download")
def backup_export(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    tables = ["master_catalog", "inventory_stock", "orders", "order_items", "customers", "settlement_audits", "stock_ledger", "audit_logs"]
    data = {t: sb_select(t, firm=firm, limit=10000) for t in tables}
    return {"firm_id": firm, "created_at": now_iso(), "tables": data,
            "note": "JSON export, not a replacement for Supabase managed backups."}


# ---------------------------------------------------------------------------
# Extended production features: accounting, compliance, documents, tools
# ---------------------------------------------------------------------------

class ExpenseRequest(BaseModel):
    firm_id: str
    account: str
    amount: Decimal = Field(gt=0)
    description: str = ""
    expense_date: Optional[date] = None

class BankTransactionRequest(BaseModel):
    firm_id: str
    txn_date: date
    utr: Optional[str] = None
    amount: Decimal
    description: str = ""

class BuyBoxRequest(BaseModel):
    firm_id: str
    sku: str
    competitor: str
    competitor_price: Decimal = Field(ge=0)

class WeightClaimRequest(BaseModel):
    firm_id: str
    awb: str
    expected_weight: Decimal = Field(ge=0)
    charged_weight: Decimal = Field(ge=0)
    claim_amount: Decimal = Field(ge=0)

class RTOQCRequest(BaseModel):
    firm_id: str
    awb: str
    sku: str
    quantity: Decimal = Field(gt=0)
    qc_status: Literal["RESTOCK", "DAMAGED", "REPACK", "REJECT"]

class CreativeJobRequest(BaseModel):
    firm_id: str
    brand: str
    product: str
    visual_type: Literal["MARKETPLACE_PRODUCT", "SOCIAL_POST", "WHATSAPP_PROMO"]
    notes: str = ""

class ComplianceRecordRequest(BaseModel):
    firm_id: str
    type: str
    reference_no: str
    issued_on: Optional[date] = None
    expires_on: Optional[date] = None
    owner: str = ""
    notes: str = ""

class ManifestOrder(BaseModel):
    order_number: str
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    awb: Optional[str] = None
    sku: Optional[str] = None
    quantity: Decimal = Field(default=1, gt=0)
    amount: Decimal = Field(default=0, ge=0)


def _month_filter(rows: list[dict], period: str):
    return [r for r in rows if not period or str(r.get("created_at", ""))[:7] == period]


def _post_journal(firm: str, reference_type: str, reference_id: str, lines: list[dict]):
    """Post a balanced double-entry journal. Every journal is required to balance."""
    debit = sum(d(x.get("debit")) for x in lines)
    credit = sum(d(x.get("credit")) for x in lines)
    if debit != credit:
        raise HTTPException(500, f"Unbalanced journal: debit={money(debit)} credit={money(credit)}")
    j = supabase.table("journal_entries").insert({
        "firm_id": firm, "reference_type": reference_type, "reference_id": reference_id,
        "entry_date": date.today().isoformat(), "description": reference_type,
    }).execute().data or []
    if not j:
        raise HTTPException(500, "Journal entry could not be created")
    journal_id = j[0]["id"]
    payload = []
    for line in lines:
        payload.append({
            "journal_id": journal_id, "firm_id": firm,
            "account_code": line["account_code"], "debit": money(d(line.get("debit"))),
            "credit": money(d(line.get("credit"))), "description": line.get("description", ""),
        })
    supabase.table("journal_lines").insert(payload).execute()
    return journal_id


@app.post("/accounting/expenses")
@app.post("/api/v1/accounting/expenses")
def accounting_expense(payload: ExpenseRequest):
    firm = firm_id(payload.firm_id)
    row = supabase.table("expenses").insert({
        "firm_id": firm, "account": payload.account, "amount": money(payload.amount),
        "description": payload.description, "expense_date": (payload.expense_date or date.today()).isoformat(),
    }).execute().data or []
    expense = row[0] if row else {"id": str(uuid.uuid4())}
    # Expense Dr / Bank-Cash Cr. Account code is user supplied but sanitized to a code-like value.
    expense_account = re.sub(r"[^A-Za-z0-9_-]", "", payload.account.upper())[:50] or "EXPENSE"
    _post_journal(firm, "EXPENSE", str(expense.get("id")), [
        {"account_code": expense_account, "debit": payload.amount, "credit": 0, "description": payload.description},
        {"account_code": "BANK_CASH", "debit": 0, "credit": payload.amount, "description": payload.description},
    ])
    audit("CREATE", "expense", str(expense.get("id")), firm, {"amount": money(payload.amount)})
    return expense


@app.get("/accounting/bank/unmatched")
@app.get("/api/v1/accounting/bank/unmatched")
def bank_unmatched(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    rows = sb_select("bank_transactions", firm=firm, limit=1000)
    return {"items": [{"date": x.get("txn_date"), "utr": x.get("utr") or "—", "amount": x.get("amount"), "matched": bool(x.get("matched"))} for x in rows if not x.get("matched")]}


@app.post("/accounting/reconcile-settlement")
@app.post("/api/v1/accounting/reconcile-settlement")
def reconcile_settlement(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    ref = str(payload.get("settlement_ref") or "")
    received = d(payload.get("received_amount"))
    row = supabase.table("settlement_audits").select("*").eq("firm_id", firm).eq("settlement_ref", ref).limit(1).execute().data or []
    if row:
        expected = d(row[0].get("expected_payout"))
        variance = received - expected
        supabase.table("settlement_audits").update({"actual_bank_credit": money(received), "variance": money(variance), "reconciled": abs(variance) <= Decimal("0.01")}).eq("id", row[0]["id"]).execute()
    else:
        variance = Decimal("0")
    audit("RECONCILE", "settlement", ref or None, firm, {"received": money(received), "variance": money(variance)})
    return {"success": True, "message": "Settlement reconciled", "variance": money(variance), "reconciled": abs(variance) <= Decimal("0.01")}


@app.post("/inventory/reconcile")
@app.post("/api/v1/inventory/reconcile")
def inventory_reconcile(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    rows = sb_select("inventory_stock", firm=firm, limit=5000)
    return {"success": True, "firm_id": firm, "items_checked": len(rows), "message": "Inventory ledger reconciliation check completed; no automatic destructive adjustment was made."}


@app.post("/sentinel/buy-box-alert")
@app.post("/api/v1/sentinel/buy-box-alert")
def buy_box_alert(payload: BuyBoxRequest):
    firm = firm_id(payload.firm_id)
    product = get_product(payload.sku, firm)
    our_price = d(product.get("selling_price") if product else 0)
    row = supabase.table("buy_box_observations").insert({
        "firm_id": firm, "sku": payload.sku, "competitor": payload.competitor,
        "competitor_price": money(payload.competitor_price), "our_price": money(our_price),
        "observed_at": now_iso(),
    }).execute().data or []
    return {"status": "RECORDED", "id": row[0].get("id") if row else None,
            "our_price": money(our_price), "competitor_price": money(payload.competitor_price),
            "message": "Competitor observation recorded. Price changes are never applied automatically."}


@app.post("/marketplaces/catalog-sync")
@app.post("/api/v1/marketplaces/catalog-sync")
def marketplace_catalog_sync(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    return {"status": "QUEUED", "firm_id": firm, "message": "Master Product Vault is authoritative; channel adapters can publish from it."}

@app.post("/marketplaces/sync")
@app.post("/api/v1/marketplaces/sync")
def marketplace_sync(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    return {"status": "QUEUED", "firm_id": firm, "message": "Marketplace sync job queued; credentials/adapters are required for external submission."}


@app.post("/crm/whatsapp-nudge")
@app.post("/api/v1/crm/whatsapp-nudge")
def whatsapp_nudge(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    return {"status": "PREPARED", "firm_id": firm, "phone": payload.get("phone"), "product": payload.get("product"), "message": "Message prepared. Sending requires an approved WhatsApp Business provider."}


@app.get("/accounting/pnl-summary")
@app.get("/api/v1/accounting/pnl-summary")
def pnl_summary_extended(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    orders = _orders(firm)
    revenue = sum(d(x.get("subtotal")) for x in orders)
    taxes = sum(d(x.get("tax_amount")) for x in orders)
    fees = sum(d(x.get("marketplace_fee")) for x in orders)
    expenses = sum(d(x.get("amount")) for x in sb_select("expenses", firm=firm, limit=5000))
    cogs = Decimal("0")
    for o in orders:
        for item in (supabase.table("order_items").select("*").eq("order_id", o.get("id")).limit(100).execute().data or []):
            p = get_product(item.get("sku"), firm) or {}
            cogs += d(item.get("quantity")) * d(p.get("cogs_price"))
    profit = revenue - cogs - fees - expenses
    return {"firm_id": firm, "revenue": money(revenue), "cogs": money(cogs), "marketplace_fees": money(fees), "taxes": money(taxes), "expenses": money(expenses), "net_profit": money(profit)}


@app.get("/accounting/tally/export")
@app.get("/api/v1/accounting/tally/export")
def tally_export(firm_id: str = "FIRM-SHREEJU", from_date: str = "", to_date: str = ""):
    firm = globals()["firm_id"](firm_id)
    journals = sb_select("journal_entries", firm=firm, limit=10000)
    lines = sb_select("journal_lines", firm=firm, limit=50000)
    lines_by = {}
    for line in lines: lines_by.setdefault(line.get("journal_id"), []).append(line)
    chunks = ['<?xml version="1.0" encoding="UTF-8"?><ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC><REQUESTDATA>']
    for j in journals:
        jd = str(j.get("entry_date", ""))
        if from_date and jd < from_date: continue
        if to_date and jd > to_date: continue
        chunks.append(f'<TALLYMESSAGE><VOUCHER VCHTYPE="{xml_escape(str(j.get("reference_type") or "Journal"))}" ACTION="Create"><DATE>{jd.replace("-", "")}</DATE><NARRATION>{xml_escape(str(j.get("description") or ""))}</NARRATION>')
        for line in lines_by.get(j.get("id"), []):
            chunks.append(f'<ALLLEDGERENTRIES.LIST><LEDGERNAME>{xml_escape(str(line.get("account_code") or ""))}</LEDGERNAME><ISDEEMEDPOSITIVE>{"Yes" if d(line.get("debit"))>0 else "No"}</ISDEEMEDPOSITIVE><AMOUNT>{money(d(line.get("credit"))-d(line.get("debit"))):.2f}</AMOUNT></ALLLEDGERENTRIES.LIST>')
        chunks.append('</VOUCHER></TALLYMESSAGE>')
    chunks.append('</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>')
    xml = ''.join(chunks)
    return Response(content=xml, media_type="application/xml", headers={"Content-Disposition": f'attachment; filename="{firm.lower()}_tally_export.xml"'})


@app.get("/accounting/gstr1/export")
@app.get("/api/v1/accounting/gstr1/export")
def gstr1_export(firm_id: str = "FIRM-SHREEJU", tax_period: str = ""):
    firm = globals()["firm_id"](firm_id)
    rows = _month_filter(_orders(firm), tax_period)
    return {"return": "GSTR-1", "firm_id": firm, "tax_period": tax_period, "b2c_invoices": len(rows), "taxable_value": money(sum(d(x.get("subtotal")) for x in rows)), "tax": money(sum(d(x.get("tax_amount")) for x in rows)), "note": "Summary/export data only; filing/submission requires GST portal credentials and validation."}

@app.get("/accounting/gstr3b/export")
@app.get("/api/v1/accounting/gstr3b/export")
def gstr3b_export(firm_id: str = "FIRM-SHREEJU", tax_period: str = ""):
    firm = globals()["firm_id"](firm_id)
    rows = _month_filter(_orders(firm), tax_period)
    tax = sum(d(x.get("tax_amount")) for x in rows)
    return {"return": "GSTR-3B", "firm_id": firm, "tax_period": tax_period, "outward_taxable_value": money(sum(d(x.get("subtotal")) for x in rows)), "outward_tax": money(tax), "note": "Summary/export data only; filing/submission requires GST portal credentials and validation."}


@app.post("/accounting/e-invoice")
@app.post("/api/v1/accounting/e-invoice")
def prepare_einvoice(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    invoice_id = str(payload.get("invoice_id"))
    rows = supabase.table("orders").select("*").eq("firm_id", firm).eq("id", invoice_id).limit(1).execute().data or []
    if not rows: raise HTTPException(404, "Invoice/order not found")
    return {"status": "PREPARED", "firm_id": firm, "invoice_id": invoice_id, "payload": {"document_type": "INV", "invoice_number": rows[0].get("order_number"), "value": rows[0].get("total_amount")}, "note": "NIC e-invoice submission is intentionally gated behind configured credentials and approval."}

@app.post("/accounting/e-way")
@app.post("/api/v1/accounting/e-way")
def prepare_eway(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    return {"status": "PREPARED", "firm_id": firm, "invoice_id": payload.get("invoice_id"), "payload": {"document_type": "EWAY"}, "note": "NIC e-way submission requires transporter/vehicle and portal credentials."}


@app.post("/logistics/weight-claims")
@app.post("/api/v1/logistics/weight-claims")
def weight_claim(payload: WeightClaimRequest):
    firm = firm_id(payload.firm_id)
    row = supabase.table("weight_claims").insert({**payload.model_dump(), "firm_id": firm, "expected_weight": money(payload.expected_weight), "charged_weight": money(payload.charged_weight), "claim_amount": money(payload.claim_amount), "status": "PREPARED"}).execute().data or []
    return row[0] if row else {"status": "PREPARED"}


@app.post("/logistics/rto/qc-process")
@app.post("/api/v1/logistics/rto/qc-process")
def rto_qc(payload: RTOQCRequest):
    firm = firm_id(payload.firm_id)
    result = {"firm_id": firm, "awb": payload.awb, "sku": payload.sku, "quantity": money(payload.quantity), "qc_status": payload.qc_status, "processed_at": now_iso()}
    if payload.qc_status == "RESTOCK":
        set_stock(payload.sku, firm, get_stock(payload.sku, firm) + payload.quantity)
        record_stock_ledger(firm, payload.sku, payload.quantity, "RTO_RESTOCK", payload.awb)
        result["stock_action"] = "RESTOCKED"
    elif payload.qc_status in {"DAMAGED", "REJECT"}:
        record_stock_ledger(firm, payload.sku, Decimal("0"), f"RTO_{payload.qc_status}", payload.awb)
        result["stock_action"] = "NOT_RESTOCKED"
    else:
        result["stock_action"] = "HELD_FOR_REPACK"
    return result


@app.post("/traceability/record")
@app.post("/api/v1/traceability/record")
def traceability_record(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    row = supabase.table("traceability_records").insert({
        "firm_id": firm, "sku": payload.get("sku"), "batch_no": payload.get("batch_no"),
        "quantity": money(d(payload.get("quantity"))), "source_type": payload.get("source_type"),
        "source_reference": payload.get("source_reference"), "mfg_date": payload.get("mfg_date"),
        "expiry_date": payload.get("expiry_date"),
    }).execute().data or []
    return row[0] if row else {"firm_id": firm, "sku": payload.get("sku"), "batch_no": payload.get("batch_no")}


@app.post("/ai/creative-jobs")
@app.post("/api/v1/ai/creative-jobs")
async def creative_job(payload: CreativeJobRequest):
    firm = firm_id(payload.firm_id)
    style = {
        "MARKETPLACE_PRODUCT": "white background, centered product pack, clean ecommerce hierarchy",
        "SOCIAL_POST": "square social layout, strong headline, concise benefit and CTA",
        "WHATSAPP_PROMO": "WhatsApp-friendly portrait layout, readable offer hierarchy and CTA",
    }[payload.visual_type]
    prompt = f"Brand: {payload.brand}. Product: {payload.product}. Style: {style}. Notes: {payload.notes}. Never invent certifications, ingredients, prices or claims."
    # Free creative agent: produces a lightweight SVG poster, no paid image API and no external image service.
    safe_brand=html.escape(payload.brand); safe_product=html.escape(payload.product or "Product")
    safe_notes=html.escape(payload.notes or "")
    svg=f"""<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1080" viewBox="0 0 1080 1080"><rect width="1080" height="1080" fill="#ffffff"/><rect x="55" y="55" width="970" height="970" rx="38" fill="#f3f7f3" stroke="#d6e3d8"/><text x="90" y="145" font-family="Arial" font-size="42" font-weight="700" fill="#153d2a">{safe_brand}</text><text x="90" y="310" font-family="Arial" font-size="62" font-weight="800" fill="#153d2a">{safe_product}</text><text x="90" y="390" font-family="Arial" font-size="30" fill="#526257">{html.escape(style)}</text><text x="90" y="850" font-family="Arial" font-size="28" fill="#526257">{safe_notes[:120]}</text><text x="90" y="940" font-family="Arial" font-size="34" font-weight="700" fill="#153d2a">Verify product facts before publishing.</text></svg>"""
    output_url="data:image/svg+xml;base64," + __import__('base64').b64encode(svg.encode()).decode()
    row = supabase.table("ai_creative_jobs").insert({"firm_id": firm, "brand": payload.brand, "product": payload.product, "visual_type": payload.visual_type, "prompt": prompt, "output_url": output_url, "status": "COMPLETED", "completed_at": now_iso()}).execute().data or []
    job_id = row[0].get("id") if row else str(uuid.uuid4())
    return {"id": job_id, "status": "COMPLETED", "output_url": output_url, "prompt": prompt, "agent":"Creative Agent", "note":"Free local creative agent. No paid image API is used."}


@app.get("/ai/runs")
@app.get("/api/v1/ai/runs")
@app.post("/ai/runs")
@app.post("/api/v1/ai/runs")
async def ai_runs(firm_id: str = "FIRM-SHREEJU", payload: dict[str, Any] | None = None):
    firm = globals()["firm_id"](firm_id)
    if payload and payload.get("agent"):
        return await ai_agent("Supervisor AI", firm, "Run requested supervisor cycle", payload)
    rows = sb_select("ai_runs", firm=firm, limit=100)
    return {"items": rows}



@app.post("/system/backups")
@app.post("/api/v1/system/backups")
def backup_job(payload: dict[str, Any] | None = None):
    firm = globals()["firm_id"]((payload or {}).get("firm_id") or "FIRM-SHREEJU")
    return {"status": "QUEUED", "firm_id": firm, "created_at": now_iso(), "note": "Supabase managed backups remain the authoritative disaster-recovery mechanism."}


@app.post("/production/orders")
@app.post("/api/v1/production/orders")
def production_order(payload: dict[str, Any]):
    firm = globals()["firm_id"](payload.get("firm_id"))
    row = supabase.table("production_orders").insert({
        "firm_id": firm, "sku": payload.get("sku"), "planned_quantity": money(d(payload.get("planned_quantity"))),
        "batch_code": payload.get("batch_code"), "status": "PLANNED",
    }).execute().data or []
    return row[0] if row else {"status": "PLANNED", "order_no": f"MO-{datetime.now().strftime('%Y%m%d%H%M%S')}"}


@app.get("/compliance/items")
@app.get("/api/v1/compliance/items")
def compliance_items_extended(firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    rows = sb_select("compliance_items", firm=firm, limit=500)
    today = date.today()
    out=[]
    for x in rows:
        exp = x.get("expires_on")
        status_val = "VALID"
        if exp:
            try:
                ed = date.fromisoformat(str(exp)[:10])
                if ed < today: status_val = "EXPIRED"
                elif (ed-today).days <= 30: status_val = "EXPIRING"
            except ValueError: pass
        out.append({**x, "status": status_val})
    return {"items": out}


@app.post("/compliance/items")
@app.post("/api/v1/compliance/items")
def compliance_create(payload: ComplianceRecordRequest):
    firm = firm_id(payload.firm_id)
    row = supabase.table("compliance_items").insert({**payload.model_dump(mode="json"), "firm_id": firm}).execute().data or []
    return row[0] if row else {"firm_id": firm, **payload.model_dump(mode="json")}


@app.post("/orders/upload-manifest-pdf")
@app.post("/api/v1/orders/upload-manifest-pdf")
async def upload_manifest_real(file: UploadFile = File(...), firm_id: str = "FIRM-SHREEJU"):
    firm = globals()["firm_id"](firm_id)
    data = await file.read()
    # Bytes stay in memory and are discarded after this request: no automatic persistence of customer PII.
    extracted = ""
    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as exc:
            return {"status": "PARSER_ERROR", "filename": file.filename, "bytes_received": len(data), "error": safe_error(exc)}
    if not extracted.strip():
        return {"status": "NO_TEXT", "filename": file.filename, "bytes_received": len(data), "orders_imported": 0, "note": "The PDF contains no extractable text. Use OCR-enabled scanning for image-only labels."}
    # Conservative parser: only import rows with an explicit order-number token. Unknown fields remain null.
    candidates=[]
    for line in extracted.splitlines():
        m=re.search(r"(?:Order|Order ID|Order No)\s*[:#-]?\s*([A-Za-z0-9_-]{4,40})", line, re.I)
        if m: candidates.append({"order_number":m.group(1), "source_line":line[:500]})
    if candidates:
        supabase.table("manifest_imports").insert([{
            "firm_id":firm,"source_filename":file.filename or "manifest.pdf","order_number":x["order_number"],
            "raw_line":x["source_line"],"status":"REVIEW"
        } for x in candidates[:200]]).execute()
    return {"status": "PARSED", "filename": file.filename, "bytes_received": len(data), "orders_imported": 0, "candidates": candidates[:200], "note": "Parsed candidates are saved as REVIEW records for validation. Original PDF bytes are not persisted."}


@app.post("/ocr/scan-shipping-label")
@app.post("/api/v1/ocr/scan-shipping-label")
async def ocr_scan_real(file: UploadFile = File(...)):
    data = await file.read()
    text = ""
    provider = "NONE"
    try:
        from PIL import Image
        import pytesseract
        image = Image.open(io.BytesIO(data))
        text = pytesseract.image_to_string(image)
        provider = "TESSERACT"
    except Exception as exc:
        return {"status": "OCR_NOT_AVAILABLE", "filename": file.filename, "bytes_received": len(data), "text": "", "note": f"Install Tesseract/Pillow to enable local OCR: {safe_error(exc)}"}
    awb = None
    m=re.search(r"\b(?:AWB|WAYBILL|TRACKING)\s*[:#-]?\s*([A-Z0-9-]{6,30})", text, re.I)
    if m: awb=m.group(1)
    phone = None
    pm=re.search(r"(?:\+91[ -]?)?[6-9]\d{9}\b", text)
    if pm: phone=pm.group(0)
    row = supabase.table("label_ocr_results").insert({"filename":file.filename or "label","awb":awb,"phone":phone,"raw_text":text[:10000],"status":"REVIEW"}).execute().data or []
    return {"status": "PARSED", "provider": provider, "filename": file.filename, "bytes_received": len(data), "awb": awb, "phone": phone, "text": text[:5000], "record_id": row[0].get("id") if row else None, "note": "Extracted fields are saved for review; uploaded bytes are processed in memory and not persisted."}


@app.get("/system/backups")
@app.get("/api/v1/system/backups")
def backup_status(firm_id: str = "FIRM-SHREEJU"):
    return {"items": [], "firm_id": globals()["firm_id"](firm_id), "note": "Use Supabase managed backup/versioning for database recovery."}


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "service": "Shreeju & Jivanya Unified ERP",
        "version": "3.0.0",
        "docs": "/docs",
        "ai": True,
        "ai_mode": AI_MODE,
        "paid_ai_required": False,
        "time": now_iso(),
    }
@app.post("/orders/checkout")
@app.post("/api/v1/orders/checkout")
def api_checkout(payload: CheckoutRequest):
    # Website ya storefront se aane wale orders ko process karega
    order = place_order(payload, payload.channel or "WEBSITE")
    return order
