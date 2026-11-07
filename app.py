import streamlit as st
import pandas as pd
import yfinance as yf
import numpy as np
import re
import json
import time

from datetime import date, timedelta
from supabase import create_client

# =========================================================
# SUPABASE DATABASE — USER-SCOPED, RLS-PROTECTED
# =========================================================
# The active client is created per Streamlit browser session. Never cache
# an authenticated client globally: that can mix user sessions.
supabase = None
CURRENT_USER_ID = None


TABLE_COLUMNS = {
    "holdings": ["id", "user_id", "symbol", "name", "asset_type", "quantity", "cost_per_unit", "account", "created"],
    "transactions": ["id", "user_id", "date", "kind", "description", "amount", "notes"],
    "balances": ["id", "user_id", "date", "account", "amount"],
    "silver_transactions": ["id", "user_id", "date", "kind", "ticker", "ounces", "price_per_ounce", "total_amount", "notes"],
}


def _table_rows(table, columns="*"):
    if supabase is None or not CURRENT_USER_ID:
        raise RuntimeError("PALACE database is not connected to an authenticated user.")
    response = supabase.table(table).select(columns).execute()
    return response.data or []


def _table_df(table):
    return pd.DataFrame(_table_rows(table), columns=TABLE_COLUMNS[table])


def q(sql, params=()):
    """Small compatibility layer for the SELECT statements used by PALACE."""
    normalized = " ".join(str(sql).split())
    low = normalized.lower()

    if " from holdings " in low:
        df = _table_df("holdings")
        if "where symbol = ?" in low and "and account = ?" in low:
            df = df[(df["symbol"].astype(str) == str(params[0])) &
                    (df["account"].astype(str) == str(params[1]))]
        if "order by id desc" in low:
            df = df.sort_values("id", ascending=False)
        elif "order by id" in low:
            df = df.sort_values("id", ascending=True)
        return df.reset_index(drop=True)

    if " from transactions " in low:
        df = _table_df("transactions")
        if "kind in" in low:
            df = df[(df["date"].astype(str) > str(params[0])) &
                    df["kind"].isin(["Fidelity cash transfer", "Buy", "Sell"])]
        elif "kind = 'buy'" in low:
            df = df[df["kind"] == "Buy"]
        elif "kind = 'paycheck'" in low:
            df = df[df["kind"] == "Paycheck"]
        elif "kind = 'fidelity cash transfer'" in low and "date = ?" in low:
            df = df[(df["kind"] == "Fidelity cash transfer") &
                    (df["date"].astype(str) == str(params[0]))]
            return pd.DataFrame([[pd.to_numeric(df["amount"], errors="coerce").fillna(0).sum()]], columns=["sum"])
        elif "kind = 'fidelity cash transfer'" in low and "date >= ?" in low:
            df = df[(df["kind"] == "Fidelity cash transfer") &
                    (df["date"].astype(str) >= str(params[0]))]
            return pd.DataFrame([[pd.to_numeric(df["amount"], errors="coerce").fillna(0).sum()]], columns=["sum"])
        if "order by date, id" in low:
            df = df.sort_values(["date", "id"], ascending=True)
        elif "order by date desc, id desc" in low:
            df = df.sort_values(["date", "id"], ascending=[False, False])
        cols_match = re.match(r"select (.+?) from transactions", normalized, re.I)
        if cols_match and cols_match.group(1).strip() != "*":
            cols = [x.strip() for x in cols_match.group(1).split(",")]
            df = df[cols]
        return df.reset_index(drop=True)

    if " from balances " in low:
        df = _table_df("balances")
        if "order by date desc, id desc" in low:
            df = df.sort_values(["date", "id"], ascending=[False, False])
        return df.reset_index(drop=True)

    if " from silver_transactions " in low:
        df = _table_df("silver_transactions")
        if "kind = 'buy silver'" in low:
            df = df[df["kind"] == "Buy Silver"]
        if "sum(" in low:
            buys = pd.to_numeric(df.loc[df["kind"] == "Buy Silver", "ounces"], errors="coerce").fillna(0).sum()
            sells = pd.to_numeric(df.loc[df["kind"] == "Sell Silver", "ounces"], errors="coerce").fillna(0).sum()
            return pd.DataFrame({"ounces": [buys - sells]})
        if "order by date desc, id desc" in low:
            df = df.sort_values(["date", "id"], ascending=[False, False])
        elif "order by date, id" in low:
            df = df.sort_values(["date", "id"], ascending=[True, True])
        cols_match = re.match(r"select (.+?) from silver_transactions", normalized, re.I)
        if cols_match and cols_match.group(1).strip() != "*":
            df = df[[x.strip() for x in cols_match.group(1).split(",")]]
        return df.reset_index(drop=True)

    raise ValueError(f"PALACE query is not supported by the Supabase adapter: {normalized}")


def run(sql, params=()):
    """Execute the INSERT/UPDATE/DELETE statements used by PALACE."""
    normalized = " ".join(str(sql).split())
    low = normalized.lower()
    if supabase is None or not CURRENT_USER_ID:
        raise RuntimeError("PALACE database is not connected to an authenticated user.")

    delete_match = re.match(r"delete from (holdings|transactions|balances|silver_transactions) where id = \?", normalized, re.I)
    if delete_match:
        supabase.table(delete_match.group(1)).delete().eq("id", int(params[0])).eq("user_id", CURRENT_USER_ID).execute()
        return

    insert_match = re.match(r"insert into (holdings|transactions|balances|silver_transactions) \((.*?)\) values \((.*?)\)", normalized, re.I)
    if insert_match:
        table, col_text, value_text = insert_match.groups()
        columns = [x.strip() for x in col_text.split(",")]
        vals = []
        param_index = 0
        for token in [x.strip() for x in value_text.split(",")]:
            if token == "?":
                vals.append(params[param_index]); param_index += 1
            elif token.startswith("'") and token.endswith("'"):
                vals.append(token[1:-1])
            else:
                raise ValueError(f"Unsupported INSERT value: {token}")
        record = dict(zip(columns, vals))
        record["user_id"] = CURRENT_USER_ID
        supabase.table(table).insert(record).execute()
        return

    if low.startswith("update holdings set quantity = quantity -"):
        amount, row_id = params
        existing = supabase.table("holdings").select("quantity").eq("id", int(row_id)).eq("user_id", CURRENT_USER_ID).single().execute()
        current = float(existing.data["quantity"] or 0)
        supabase.table("holdings").update({"quantity": current - float(amount)}).eq("id", int(row_id)).eq("user_id", CURRENT_USER_ID).execute()
        return

    raise ValueError(f"PALACE write is not supported by the Supabase adapter: {normalized}")


def money(value):
    try:
        if value is None or pd.isna(value):
            return "—"
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def compact_money(value):
    value = float(value)

    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}k"

    return f"${value:,.2f}"


def safe_float(value, default=0.0):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# =========================================================
# MARKET DATA
# =========================================================

def yahoo_symbol(symbol):
    symbol = str(symbol).upper().strip()

    if symbol == "XAG":
        return "XAGUSD=X"

    return symbol


@st.cache_data(ttl=60)
def price(symbol):
    symbol = str(symbol).upper().strip()

    try:
        symbols_to_try = (
            ["XAGUSD=X", "SI=F"]
            if symbol == "XAG"
            else [yahoo_symbol(symbol)]
        )

        for ticker_symbol in symbols_to_try:
            history = yf.Ticker(ticker_symbol).history(
                period="5d",
                interval="1d",
                auto_adjust=False
            )

            if not history.empty:
                close = history["Close"].dropna()

                if not close.empty:
                    return float(close.iloc[-1])

        return None

    except Exception:
        return None


@st.cache_data(ttl=60)
def get_quotes(symbols):
    symbols = {
        str(symbol).upper().strip()
        for symbol in symbols
        if str(symbol).strip()
    }

    symbols.update(["GC=F", "SI=F", "XAG"])

    return {
        symbol: price(symbol)
        for symbol in symbols
    }


@st.cache_data(ttl=900)
def stock_history(symbol, period="max"):
    try:
        history = yf.Ticker(
            yahoo_symbol(symbol)
        ).history(
            period=period,
            interval="1d",
            auto_adjust=False
        )

        if history.empty:
            return pd.DataFrame()

        history = history.reset_index()

        history["Date"] = pd.to_datetime(
            history["Date"],
            errors="coerce"
        ).dt.tz_localize(None)

        return history[["Date", "Close"]].dropna(
            subset=["Date", "Close"]
        )

    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=900)
def silver_history(period="max"):
    try:
        history = yf.Ticker("XAGUSD=X").history(
            period=period,
            interval="1d",
            auto_adjust=False
        )

        if history.empty:
            history = yf.Ticker("SI=F").history(
                period=period,
                interval="1d",
                auto_adjust=False
            )

        if history.empty:
            return pd.DataFrame()

        history = history.reset_index()

        history["Date"] = pd.to_datetime(
            history["Date"],
            errors="coerce"
        ).dt.tz_localize(None)

        return history[["Date", "Close"]].dropna(
            subset=["Date", "Close"]
        )

    except Exception:
        return pd.DataFrame()


# =========================================================
# HISTORICAL PERFORMANCE ENGINE
# =========================================================

def build_historical_portfolio(holdings, silver_ounces):
    """
    Reconstructs the historical market value of CURRENT holdings.

    Stocks:
        Historical close × currently owned shares.

    Silver:
        Historical silver close × currently owned ounces.

    This is a historical valuation of current positions. It is not
    a complete money-weighted return calculation for positions that
    were previously sold or for cash deposits/withdrawals.
    """

    if holdings.empty and silver_ounces <= 0:
        return pd.DataFrame()

    combined = None

    # -----------------------------------------------------
    # Stock / ETF / other holdings
    # -----------------------------------------------------

    if not holdings.empty:
        for symbol in holdings["symbol"].dropna().unique():

            symbol = str(symbol).upper().strip()

            if not symbol:
                continue

            quantity = safe_float(
                holdings.loc[
                    holdings["symbol"] == symbol,
                    "quantity"
                ].sum()
            )

            if quantity <= 0:
                continue

            prices = stock_history(symbol, "max")

            if prices.empty:
                continue

            prices = prices[["Date", "Close"]].copy()

            prices[symbol] = (
                pd.to_numeric(prices["Close"], errors="coerce")
                * quantity
            )

            prices = prices[["Date", symbol]].dropna()

            if combined is None:
                combined = prices
            else:
                combined = combined.merge(
                    prices,
                    on="Date",
                    how="outer"
                )

    # -----------------------------------------------------
    # Physical silver
    # -----------------------------------------------------

    if silver_ounces > 0:
        silver_prices = silver_history("max")

        if not silver_prices.empty:
            silver_prices = silver_prices[["Date", "Close"]].copy()

            silver_prices["Physical Silver"] = (
                pd.to_numeric(
                    silver_prices["Close"],
                    errors="coerce"
                )
                * float(silver_ounces)
            )

            silver_prices = silver_prices[
                ["Date", "Physical Silver"]
            ].dropna()

            if combined is None:
                combined = silver_prices
            else:
                combined = combined.merge(
                    silver_prices,
                    on="Date",
                    how="outer"
                )

    if combined is None or combined.empty:
        return pd.DataFrame()

    combined = combined.sort_values("Date")
    combined = combined.set_index("Date")

    # Fill market holidays / missing dates using last known price.
    combined = combined.ffill()

    value_columns = list(combined.columns)

    combined["Portfolio Value"] = (
        combined[value_columns]
        .sum(axis=1, min_count=1)
    )

    combined = combined.dropna(
        subset=["Portfolio Value"]
    )

    return combined


# =========================================================
# DELETE BUTTON
# =========================================================

def delete_button(table, row_id, label, key):
    with st.popover("🗑️", help=f"Delete {label}"):
        st.warning(
            f"Delete this {label}? This cannot be undone."
        )

        confirmed = st.checkbox(
            "Confirm deletion",
            key=f"confirm_{key}_{row_id}"
        )

        if st.button(
            "Delete",
            key=f"delete_{key}_{row_id}",
            type="primary",
            disabled=not confirmed
        ):
            allowed_tables = {
                "holdings",
                "transactions",
                "balances",
                "silver_transactions"
            }

            if table not in allowed_tables:
                st.error("Invalid delete request.")
                return

            run(
                f"DELETE FROM {table} WHERE id = ?",
                (int(row_id),)
            )

            st.rerun()

# =========================================================
# PAGE SETUP
# =========================================================

st.set_page_config(
    page_title="PALACE Wealth Tracker",
    page_icon="📈",
    layout="wide"
)


# =========================================================
# PALACE AUTHENTICATION — INVITE ONLY + PERSISTENT SESSION
# =========================================================
# Supabase Auth is the front door. Public sign-ups must also be
# disabled in Supabase Dashboard > Authentication > Settings.
# Encrypted browser cookies preserve the Supabase session across refreshes.
try:
    from streamlit_cookies_manager import EncryptedCookieManager
except ImportError:
    st.error(
        "Missing dependency: streamlit-cookies-manager-v2. "
        "Add streamlit-cookies-manager-v2 to requirements.txt and redeploy."
    )
    st.stop()


def palace_logo():
    st.html("""
    <div style="
        font-family: Georgia, serif;
        margin: 0 auto 28px auto;
        text-align: center;
        width: 100%;
    ">
        <div style="
            font-size: 42px;
            font-weight: bold;
            letter-spacing: 5px;
            color: #edf2ff;
        ">
            PALACE
        </div>
        <div style="
            font-size: 15px;
            font-style: italic;
            color: #aeb8cc;
            margin-top: 8px;
        ">
            Noun · From Basque Jauregi / Jauregui
        </div>
        <div style="
            font-size: 13px;
            font-style: italic;
            letter-spacing: 1px;
            color: #edf2ff;
            margin-top: 10px;
        ">
            THE SEAT OF A FAMILY AND ITS LEGACY
        </div>
    </div>
    """)


def palace_supabase(access_token=None, refresh_token=None):
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    client = create_client(url, key)
    if access_token and refresh_token:
        client.auth.set_session(access_token, refresh_token)
    return client


supabase = palace_supabase()
if supabase is None:
    st.error("PALACE is not configured. Add SUPABASE_URL and SUPABASE_KEY in Streamlit Secrets.")
    st.stop()

cookies_password = st.secrets.get("COOKIES_PASSWORD", "")
if not cookies_password or len(cookies_password) < 32:
    st.error(
        "PALACE persistent login is not configured. Add COOKIES_PASSWORD "
        "to Streamlit Secrets (use a unique random value of at least 32 characters)."
    )
    st.stop()

cookies = EncryptedCookieManager(
    prefix="palace-wealth-tracker/",
    password=cookies_password,
)
if not cookies.ready():
    st.info("Loading your secure PALACE session…")
    st.stop()

if "palace_access_token" not in st.session_state:
    st.session_state.palace_access_token = None
if "palace_refresh_token" not in st.session_state:
    st.session_state.palace_refresh_token = None
if "palace_user" not in st.session_state:
    st.session_state.palace_user = None


def save_palace_session(session, user):
    """Keep the auth tokens in Streamlit state and an encrypted browser cookie."""
    st.session_state.palace_access_token = session.access_token
    st.session_state.palace_refresh_token = session.refresh_token
    st.session_state.palace_user = {"id": str(user.id), "email": user.email}
    cookies["supabase_session"] = json.dumps({
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
    })
    cookies.save()


# Restore from the encrypted cookie after a browser refresh / new Streamlit session.
if not st.session_state.palace_user:
    saved_session = cookies.get("supabase_session")
    if saved_session:
        try:
            saved = json.loads(saved_session)
            access_token = saved.get("access_token")
            refresh_token = saved.get("refresh_token")
            if access_token and refresh_token:
                restored_client = palace_supabase()
                try:
                    auth_result = restored_client.auth.set_session(access_token, refresh_token)
                except Exception:
                    # If the access token expired, explicitly try the refresh token.
                    auth_result = restored_client.auth.refresh_session(refresh_token)
                restored_session = getattr(auth_result, "session", None)
                if restored_session is None:
                    restored_session = restored_client.auth.get_session()
                user_result = restored_client.auth.get_user(restored_session.access_token)
                restored_user = getattr(user_result, "user", None)
                if restored_user and restored_session:
                    supabase = restored_client
                    save_palace_session(restored_session, restored_user)
        except Exception:
            # Invalid/revoked/expired cookie: clear it and require a fresh login.
            try:
                del cookies["supabase_session"]
                cookies.save()
            except Exception:
                pass
            st.session_state.palace_access_token = None
            st.session_state.palace_refresh_token = None
            st.session_state.palace_user = None

# Rebuild an isolated client from the current Streamlit session on every rerun.
if st.session_state.palace_access_token and st.session_state.palace_refresh_token and st.session_state.palace_user:
    try:
        supabase = palace_supabase(
            st.session_state.palace_access_token,
            st.session_state.palace_refresh_token,
        )
        CURRENT_USER_ID = str(st.session_state.palace_user["id"])
        # Persist rotated tokens if Supabase refreshed them during set_session.
        active_session = supabase.auth.get_session()
        if active_session and active_session.refresh_token:
            cookies["supabase_session"] = json.dumps({
                "access_token": active_session.access_token,
                "refresh_token": active_session.refresh_token,
            })
            cookies.save()
            st.session_state.palace_access_token = active_session.access_token
            st.session_state.palace_refresh_token = active_session.refresh_token
    except Exception:
        st.session_state.palace_access_token = None
        st.session_state.palace_refresh_token = None
        st.session_state.palace_user = None
        CURRENT_USER_ID = None

if not st.session_state.palace_user:
    palace_logo()
    st.markdown(
        "<div style='text-align:center;color:#aeb8cc;margin:-12px 0 24px 0;"
        "font-family:Georgia,serif;letter-spacing:1px;'>PRIVATE CLIENT ACCESS</div>",
        unsafe_allow_html=True,
    )
    _, login_col, _ = st.columns([1, 1.15, 1])
    with login_col:
        with st.form("palace_sign_in", clear_on_submit=False):
            email = st.text_input("Email", placeholder="Your invited email")
            password = st.text_input("Password", type="password", placeholder="Password")
            submitted = st.form_submit_button("SIGN IN", use_container_width=True)
        if submitted:
            if not email.strip() or not password:
                st.error("Enter your email and password.")
            else:
                try:
                    result = supabase.auth.sign_in_with_password(
                        {"email": email.strip(), "password": password}
                    )
                    user = result.user
                    if not user or not result.session:
                        st.error("Sign-in could not be completed.")
                    else:
                        save_palace_session(result.session, user)
                        CURRENT_USER_ID = str(user.id)
                        # Give the browser component a moment to write the encrypted
                        # cookie before Streamlit starts the authenticated rerun.
                        time.sleep(0.3)
                        st.rerun()
                except Exception:
                    st.error("Sign-in failed. Check your credentials or ask the PALACE administrator for an invitation.")
    st.stop()

# The existing full dashboard is shown only after successful authentication.
with st.sidebar:
    st.caption(f"Signed in as {st.session_state.palace_user.get('email', '')}")
    if st.button("Sign out", use_container_width=True):
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
        try:
            del cookies["supabase_session"]
            cookies.save()
        except Exception:
            pass
        for key in ("palace_user", "palace_access_token", "palace_refresh_token"):
            st.session_state.pop(key, None)
        CURRENT_USER_ID = None
        st.rerun()


st.markdown("""

<style>
/* =========================================================
   PALACE — BLACK + RED THEME
   ========================================================= */

:root {
    --palace-black: #050505;
    --palace-panel: #111111;
    --palace-panel-hover: #191919;
    --palace-red: #e32636;
    --palace-red-bright: #ff303f;
    --palace-border: #2b2b2b;
    --palace-text: #f5f5f5;
    --palace-muted: #a5a5a5;
}

/* Main app */
.stApp {
    background: var(--palace-black);
    color: var(--palace-text);
}

.block-container {
    max-width: 1400px;
    padding-top: 2.5rem;
}

/* Main headings */
h1, h2, h3 {
    color: #ffffff !important;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background: #0b0b0b;
    border-right: 1px solid #301014;
}

/* Metric cards */
[data-testid="stMetric"] {
    background: var(--palace-panel);
    border: 1px solid var(--palace-border);
    border-top: 2px solid var(--palace-red);
    padding: 16px;
    border-radius: 12px;
}

[data-testid="stMetricLabel"] {
    color: var(--palace-muted) !important;
}

[data-testid="stMetricValue"] {
    color: #ffffff !important;
}

[data-testid="stMetricDelta"] {
    font-weight: 700;
}

/* Buttons */
div.stButton > button {
    background: #111111;
    color: #f5f5f5;
    border: 1px solid #393939;
    border-radius: 9px;
    transition: all 0.2s ease;
}

div.stButton > button:hover {
    background: #261013;
    color: #ffffff;
    border: 1px solid var(--palace-red);
}

/* Selected radio buttons */
div[role="radiogroup"] label[data-checked="true"] {
    background: #351015 !important;
    border-color: var(--palace-red) !important;
    color: #ffffff !important;
}

/* Inputs */
input, textarea {
    background: #111111 !important;
    color: #ffffff !important;
    border-color: #393939 !important;
}

/* Dropdowns */
div[data-baseweb="select"] > div {
    background: #111111;
    border-color: #393939;
    color: #ffffff;
}

/* Tables */
div[data-testid="stDataFrame"] {
    border: 1px solid var(--palace-border);
    border-radius: 10px;
}

/* Accent links */
a {
    color: var(--palace-red-bright) !important;
}

/* Positive / negative values */
.positive {
    color: #31d18b;
    font-weight: 700;
}

.negative {
    color: var(--palace-red-bright);
    font-weight: 700;
}

/* PALACE header label */
.palace-label {
    color: #a5a5a5;
    font-size: 12px;
    letter-spacing: 3px;
}

/* Dividers */
hr {
    border-color: #292929;
}
</style>
""", unsafe_allow_html=True)


st.html("""
<div style="
    font-family: Georgia, serif;
    margin-bottom: 28px;
    text-align: center;
">
    <div style="
        font-size: 42px;
        font-weight: bold;
        letter-spacing: 5px;
        color: #edf2ff;
    ">
        PALACE
    </div>

    <div style="
        font-size: 15px;
        font-style: italic;
        color: #aeb8cc;
        margin-top: 8px;
    ">
        Noun · From Basque Jauregi / Jauregui
    </div>

    <div style="
        font-size: 13px;
        font-style: italic;
        letter-spacing: 1px;
        color: #edf2ff;
        margin-top: 10px;
    ">
        THE SEAT OF A FAMILY AND ITS LEGACY
    </div>
</div>
""")

# =========================================================
# LOAD DATA
# =========================================================

hold = q("SELECT * FROM holdings ORDER BY id DESC")

tx = q("""
    SELECT *
    FROM transactions
    ORDER BY date DESC, id DESC
""")

balances = q("""
    SELECT *
    FROM balances
    ORDER BY date DESC, id DESC
""")

silver_tx = q("""
    SELECT *
    FROM silver_transactions
    ORDER BY date DESC, id DESC
""")

if not hold.empty:
    hold["symbol"] = (
        hold["symbol"]
        .fillna("")
        .astype(str)
        .str.upper()
        .str.strip()
    )

    hold["quantity"] = pd.to_numeric(
        hold["quantity"],
        errors="coerce"
    ).fillna(0)

    hold["cost_per_unit"] = pd.to_numeric(
        hold["cost_per_unit"],
        errors="coerce"
    ).fillna(0)

symbols = (
    list(hold["symbol"].dropna().unique())
    if not hold.empty
    else []
)

quotes = get_quotes(symbols)


# =========================================================
# PORTFOLIO CALCULATIONS
# =========================================================

portfolio_value = 0.0
portfolio_cost = 0.0

for _, holding in hold.iterrows():
    current_price = quotes.get(holding["symbol"])

    quantity = safe_float(holding["quantity"])
    cost_per_unit = safe_float(holding["cost_per_unit"])

    portfolio_value += (
        safe_float(current_price) * quantity
    )

    portfolio_cost += quantity * cost_per_unit


# =========================================================
# PHYSICAL SILVER CALCULATIONS
# =========================================================

silver_ounces = 0.0
silver_cost_basis = 0.0

if not silver_tx.empty:
    silver_tx = silver_tx.sort_values(["date", "id"])

    for _, transaction in silver_tx.iterrows():
        ounces = safe_float(transaction["ounces"])
        unit_price = safe_float(transaction["price_per_ounce"])

        if transaction["kind"] == "Buy Silver":
            silver_ounces += ounces
            silver_cost_basis += ounces * unit_price

        elif transaction["kind"] == "Sell Silver" and silver_ounces > 0:
            sold_ounces = min(ounces, silver_ounces)

            average_cost = (
                silver_cost_basis / silver_ounces
                if silver_ounces
                else 0
            )

            silver_cost_basis -= sold_ounces * average_cost
            silver_ounces -= sold_ounces

silver_current_price = price("XAG")

silver_market_value = (
    silver_ounces * silver_current_price
    if silver_current_price is not None
    else None
)

silver_gain_loss = (
    silver_market_value - silver_cost_basis
    if silver_market_value is not None
    else None
)

if silver_market_value is not None:
    portfolio_value += silver_market_value

portfolio_cost += silver_cost_basis


# =========================================================
# CASH / NET WORTH
# =========================================================

latest_balances = pd.DataFrame()

if not balances.empty:
    latest_balances = (
        balances
        .sort_values(
            ["date", "id"],
            ascending=[False, False]
        )
        .drop_duplicates(
            subset=["account"],
            keep="first"
        )
        .copy()
    )

cash_total = (
    safe_float(latest_balances["amount"].sum())
    if not latest_balances.empty
    else 0.0
)

fidelity_rows = (
    latest_balances[
        latest_balances["account"]
        .astype(str)
        .str.strip()
        .str.lower()
        == "fidelity cash"
    ]
    if not latest_balances.empty
    else pd.DataFrame()
)

if not fidelity_rows.empty:
    fidelity_cash = safe_float(
        fidelity_rows.iloc[0]["amount"]
    )
    fidelity_balance_date = str(
        fidelity_rows.iloc[0]["date"]
    )
else:
    fidelity_cash = 0.0
    fidelity_balance_date = "0000-00-00"

fidelity_activity = q("""
    SELECT date, kind, amount, notes
    FROM transactions
    WHERE date > ?
      AND kind IN (
          'Fidelity cash transfer',
          'Buy',
          'Sell'
      )
    ORDER BY date, id
""", (fidelity_balance_date,))

if not fidelity_activity.empty:
    for _, activity in fidelity_activity.iterrows():
        # Historical imported holdings must not reduce current cash.
        notes = str(activity.get("notes", ""))

        if "Historical existing holding" in notes:
            continue

        amount = safe_float(activity["amount"])
        kind = str(activity["kind"]).strip()

        if kind == "Fidelity cash transfer":
            fidelity_cash += amount
        elif kind == "Buy":
            fidelity_cash -= amount
        elif kind == "Sell":
            fidelity_cash += amount

same_day_transfers = q("""
    SELECT COALESCE(SUM(CAST(amount AS REAL)), 0)
    FROM transactions
    WHERE date = ?
      AND kind = 'Fidelity cash transfer'
""", (fidelity_balance_date,))

if not same_day_transfers.empty:
    fidelity_cash += safe_float(
        same_day_transfers.iloc[0, 0]
    )

if abs(fidelity_cash) < 0.005:
    fidelity_cash = 0.0

if not fidelity_rows.empty:
    saved_fidelity_amount = safe_float(
        fidelity_rows.iloc[0]["amount"]
    )

    cash_total = (
        cash_total
        - saved_fidelity_amount
        + fidelity_cash
    )
else:
    cash_total += fidelity_cash

total_liabilities = 0.0

net_worth = (
    portfolio_value
    + cash_total
    - total_liabilities
)

ready_to_invest = fidelity_cash


# =========================================================
# TOTAL INVESTED + PURCHASE HISTORY
# =========================================================

stock_buys = q("""
    SELECT id, date, kind, description, amount, notes
    FROM transactions
    WHERE kind = 'Buy'
    ORDER BY date, id
""")

silver_buys = q("""
    SELECT id, date, kind, ticker, ounces,
           price_per_ounce, total_amount, notes
    FROM silver_transactions
    WHERE kind = 'Buy Silver'
    ORDER BY date, id
""")

stock_total_invested = (
    pd.to_numeric(
        stock_buys["amount"],
        errors="coerce"
    ).fillna(0).sum()
    if not stock_buys.empty
    else 0.0
)

silver_total_invested = (
    pd.to_numeric(
        silver_buys["total_amount"],
        errors="coerce"
    ).fillna(0).sum()
    if not silver_buys.empty
    else 0.0
)

total_invested = (
    float(stock_total_invested)
    + float(silver_total_invested)
)

stock_history_rows = pd.DataFrame()

if not stock_buys.empty:
    stock_history_rows = pd.DataFrame({
        "Date": stock_buys["date"],
        "Asset": stock_buys["description"],
        "Type": "Stock / ETF Buy",
        "Amount": pd.to_numeric(
            stock_buys["amount"],
            errors="coerce"
        ).fillna(0),
        "Details": stock_buys["notes"].fillna("")
    })

silver_history_rows = pd.DataFrame()

if not silver_buys.empty:
    silver_history_rows = pd.DataFrame({
        "Date": silver_buys["date"],
        "Asset": "Physical Silver (XAG)",
        "Type": "Silver Buy",
        "Amount": pd.to_numeric(
            silver_buys["total_amount"],
            errors="coerce"
        ).fillna(0),
        "Details": silver_buys.apply(
            lambda row: (
                f"{safe_float(row['ounces']):,.4f} oz at "
                f"{money(row['price_per_ounce'])}/oz"
                + (
                    f" · {row['notes']}"
                    if pd.notna(row["notes"])
                    and str(row["notes"]).strip()
                    else ""
                )
            ),
            axis=1
        )
    })

investing_history = pd.concat(
    [stock_history_rows, silver_history_rows],
    ignore_index=True
)

if not investing_history.empty:
    investing_history["Date"] = pd.to_datetime(
        investing_history["Date"],
        errors="coerce"
    )

    investing_history["Amount"] = pd.to_numeric(
        investing_history["Amount"],
        errors="coerce"
    ).fillna(0)

    investing_history = (
        investing_history
        .dropna(subset=["Date"])
        .sort_values("Date", ascending=False)
    )

# Current portfolio growth percentage
growth_percent = (
    ((portfolio_value - portfolio_cost) / portfolio_cost) * 100
    if portfolio_cost > 0
    else 0.0
)

# =========================================================
# TOP DASHBOARD METRICS
# =========================================================

metric_cols = st.columns(4)

metric_cols[0].metric(
    "NET WORTH",
    money(net_worth)
)

metric_cols[1].metric(
    "PORTFOLIO VALUE",
    money(portfolio_value),
    delta=f"{growth_percent:+.2f}% growth"
)

metric_cols[2].metric(
    "TOTAL INVESTED",
    money(total_invested)
)

metric_cols[3].metric(
    "READY TO INVEST",
    money(ready_to_invest)
)

# =========================================================
# PORTFOLIO VALUE WITH GROWTH + HISTORICAL CHART
# =========================================================

# Growth is measured against total recorded investment cost.
growth_percent = (
    ((portfolio_value - total_invested) / total_invested) * 100
    if total_invested > 0
    else 0.0
)

growth_color = "#31d18b" if growth_percent >= 0 else "#ff647c"

# Calculate current portfolio growth
growth_percent = (
    ((portfolio_value - portfolio_cost) / portfolio_cost) * 100
    if portfolio_cost > 0
    else 0
)

growth_color = "#31d18b" if growth_percent >= 0 else "#ff5c5c"

# ---------------------------------------------------------
# HISTORICAL PORTFOLIO CHART
# ---------------------------------------------------------

st.subheader("Portfolio Growth")

# Zoom controls
zoom = st.radio(
    "Chart range",
    ["1M", "3M", "6M", "1Y", "YTD", "All"],
    index=5,
    horizontal=True,
    label_visibility="collapsed",
)

today = pd.Timestamp.today().normalize()

if zoom == "1M":
    chart_start = today - pd.DateOffset(months=1)

elif zoom == "3M":
    chart_start = today - pd.DateOffset(months=3)

elif zoom == "6M":
    chart_start = today - pd.DateOffset(months=6)

elif zoom == "1Y":
    chart_start = today - pd.DateOffset(years=1)

elif zoom == "YTD":
    chart_start = pd.Timestamp(
        year=today.year,
        month=1,
        day=1
    )

# Find the earliest recorded investment date
investment_dates = []

# Stock/ETF purchases
if not tx.empty and "date" in tx.columns and "kind" in tx.columns:
    stock_buys = tx[
        tx["kind"].astype(str).str.strip().str.lower().isin(["buy", "purchase"])
    ]

    investment_dates.extend(
        pd.to_datetime(stock_buys["date"], errors="coerce")
        .dropna()
        .tolist()
    )

# Silver purchases
if not silver_tx.empty and "date" in silver_tx.columns and "kind" in silver_tx.columns:
    silver_buys = silver_tx[
        silver_tx["kind"].astype(str).str.strip().str.lower().isin(["buy", "purchase"])
    ]

    investment_dates.extend(
        pd.to_datetime(silver_buys["date"], errors="coerce")
        .dropna()
        .tolist()
    )

# Existing holdings' creation dates
if not hold.empty and "created" in hold.columns:
    investment_dates.extend(
        pd.to_datetime(hold["created"], errors="coerce")
        .dropna()
        .tolist()
    )

first_investment_date = (
    pd.Timestamp(min(investment_dates)).normalize()
    if investment_dates
    else pd.Timestamp.today().normalize()
)

# Start at the first recorded investment date
chart_start = pd.Timestamp(first_investment_date).normalize()

# Never let the chart start before the first investment
chart_start = max(
    pd.Timestamp(chart_start).normalize(),
    pd.Timestamp(first_investment_date).normalize()
)


# Never let the chart start before the first investment.
chart_start = max(
    pd.Timestamp(chart_start).normalize(),
    pd.Timestamp(first_investment_date).normalize()
)


# ---------------------------------------------------------
# BUILD HISTORICAL DATA
# ---------------------------------------------------------

with st.spinner("Loading historical portfolio prices..."):

    historical_portfolio = build_historical_portfolio(
        hold,
        silver_ounces
    )


if not historical_portfolio.empty:

    chart_data = historical_portfolio.copy()

    # Ensure Date is a proper datetime column.
    if "Date" not in chart_data.columns:
        chart_data = chart_data.reset_index()

    chart_data["Date"] = pd.to_datetime(
        chart_data["Date"],
        errors="coerce"
    ).dt.tz_localize(None)

    chart_data = chart_data.dropna(
        subset=["Date", "Portfolio Value"]
    )

    # Apply selected zoom range.
    chart_data = chart_data[
        (chart_data["Date"] >= chart_start)
        & (chart_data["Date"] <= today)
    ].sort_values("Date")


    if not chart_data.empty:

        # Anchor the most recent historical point to current live value.
        latest_historical_value = safe_float(
            chart_data["Portfolio Value"].iloc[-1]
        )

        if latest_historical_value > 0:

            scale_factor = (
                portfolio_value / latest_historical_value
            )

            chart_data["Portfolio Value"] = (
                chart_data["Portfolio Value"] * scale_factor
            )


        # -------------------------------------------------
        # DRAW CHART
        # -------------------------------------------------

        import plotly.graph_objects as go

        fig = go.Figure()
        

        fig.add_trace(
            go.Scatter(
                x=chart_data["Date"],
                y=chart_data["Portfolio Value"],
                mode="lines",
                name="Portfolio Value",
                line=dict(
                    color="#31d18b",
                    width=3
                ),
                fill="tozeroy",
                fillcolor="rgba(49, 209, 139, 0.08)",
                hovertemplate=(
                    "%{x|%b %d, %Y}<br>"
                    "$%{y:,.2f}"
                    "<extra></extra>"
                ),
            )
        )

        fig.update_layout(
            height=370,
            margin=dict(
                l=10,
                r=10,
                t=15,
                b=10
            ),
            paper_bgcolor="#0b1020",
            plot_bgcolor="#0b1020",
            font=dict(
                color="#edf2ff"
            ),
            showlegend=False,
            xaxis_title=None,
            yaxis_title=None,
            hovermode="x unified",
        )

        fig.update_xaxes(
            showgrid=False,
            rangeslider_visible=False,
        )

               # =========================================================
        # SET GRAPH VALUE RANGE
        # =========================================================

        value_range_options = [
            "Auto",
            "$0 – $1K",
            "$0 – $10K",
            "$0 – $100K",
            "$0 – $1M",
            "$100K – $1M",
        ]

        # Initialize only once; don't reset it on every rerun
        if "portfolio_value_range" not in st.session_state:
            st.session_state["portfolio_value_range"] = "Auto"

        value_range = st.selectbox(
            "Value range",
            value_range_options,
            key="portfolio_value_range",
        )

        y_ranges = {
            "Auto": None,
            "$0 – $1K": [0, 1_000],
            "$0 – $10K": [0, 10_000],
            "$0 – $100K": [0, 100_000],
            "$0 – $1M": [0, 1_000_000],
            "$100K – $1M": [100_000, 1_000_000],
        }

        chosen_y_range = y_ranges[value_range]

        fig.update_yaxes(
            tickprefix="$",
            tickformat=",.0f",
            gridcolor="#293650",
            zeroline=False,
            range=chosen_y_range,
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
            config={
                "displayModeBar": True,
                "scrollZoom": False,
            },
        )
# =========================================================
# SEPARATE INVESTMENT HISTORY PAGE
# =========================================================

if st.session_state.get("palace_page") == "investing_history":

    st.title("📅 Investment History")

    if st.button(
        "← Back to Dashboard",
        key="back_to_dashboard"
    ):
        st.session_state["palace_page"] = "dashboard"
        st.rerun()

    st.caption(
        "Search recorded stock, ETF, and physical silver purchases."
    )

    if investing_history.empty:
        st.info(
            "No stock, ETF, or physical silver purchases recorded yet."
        )

    else:
        period_options = [
            "All time",
            "This month",
            "Last month",
            "Last 3 months",
            "Last 6 months",
            "This year",
            "Last year",
            "Custom range"
        ]

        selected_period = st.selectbox(
            "Date range",
            period_options,
            key="history_page_period"
        )

        today = pd.Timestamp(date.today())

        if selected_period == "This month":
            start_date = today.replace(day=1)
            end_date = today

        elif selected_period == "Last month":
            first_this_month = today.replace(day=1)
            end_date = first_this_month - pd.Timedelta(days=1)
            start_date = end_date.replace(day=1)

        elif selected_period == "Last 3 months":
            start_date = today - pd.DateOffset(months=3)
            end_date = today

        elif selected_period == "Last 6 months":
            start_date = today - pd.DateOffset(months=6)
            end_date = today

        elif selected_period == "This year":
            start_date = today.replace(month=1, day=1)
            end_date = today

        elif selected_period == "Last year":
            start_date = today.replace(
                year=today.year - 1,
                month=1,
                day=1
            )
            end_date = today.replace(
                year=today.year - 1,
                month=12,
                day=31
            )

        elif selected_period == "Custom range":
            date_col1, date_col2 = st.columns(2)

            with date_col1:
                custom_start = st.date_input(
                    "Start date",
                    value=date.today().replace(month=1, day=1),
                    key="history_page_custom_start"
                )

            with date_col2:
                custom_end = st.date_input(
                    "End date",
                    value=date.today(),
                    key="history_page_custom_end"
                )

            start_date = pd.Timestamp(custom_start)
            end_date = pd.Timestamp(custom_end)

        else:
            start_date = investing_history["Date"].min()
            end_date = today

        if start_date > end_date:
            st.error("Start date must be before the end date.")

        else:
            filtered_history = investing_history[
                (investing_history["Date"] >= start_date)
                & (investing_history["Date"] <= end_date)
            ].copy()

            search_text = st.text_input(
                "Search purchases",
                placeholder="Search ticker, asset, type, or notes...",
                key="history_page_search"
            ).strip()

            if search_text:
                searchable = (
                    filtered_history["Asset"].astype(str)
                    + " "
                    + filtered_history["Type"].astype(str)
                    + " "
                    + filtered_history["Details"].astype(str)
                )

                filtered_history = filtered_history[
                    searchable.str.contains(
                        search_text,
                        case=False,
                        regex=False,
                        na=False
                    )
                ]

            filtered_stock_total = filtered_history.loc[
                filtered_history["Type"] == "Stock / ETF Buy",
                "Amount"
            ].sum()

            filtered_silver_total = filtered_history.loc[
                filtered_history["Type"] == "Silver Buy",
                "Amount"
            ].sum()

            filtered_total = filtered_history["Amount"].sum()

            history_metrics = st.columns(3)

            history_metrics[0].metric(
                "Invested in Period",
                money(filtered_total)
            )

            history_metrics[1].metric(
                "Stock / ETF Purchases",
                money(filtered_stock_total)
            )

            history_metrics[2].metric(
                "Silver Purchases",
                money(filtered_silver_total)
            )

            display_history = filtered_history.copy()

            display_history["Date"] = (
                display_history["Date"].dt.strftime("%Y-%m-%d")
            )

            display_history["Amount"] = (
                display_history["Amount"].map(money)
            )

            st.dataframe(
                display_history[
                    ["Date", "Asset", "Type", "Amount", "Details"]
                ],
                use_container_width=True,
                hide_index=True
            )

            st.caption(
                f"Showing {len(filtered_history)} purchase(s) "
                f"from {start_date.date()} through {end_date.date()}."
            )

    st.stop()

# =========================================================
# TABS
# =========================================================

dashboard_tab, activity_tab, add_tab = st.tabs([
    "Dashboard",
    "Recent Activity",
    "＋ Add transaction"
])


# =========================================================
# DASHBOARD
# =========================================================

with dashboard_tab:

    st.divider()
    st.subheader("Holdings")

    if not hold.empty:

        categories = {
            "Stocks": ["stock", "stocks"],
            "ETFs": ["etf", "etfs"],
            "Gold": ["gold"],
            "Silver": ["silver"],
            "Other": ["other"]
        }

        for category, types in categories.items():

            category_holdings = hold[
                hold["asset_type"]
                .astype(str)
                .str.lower()
                .isin(types)
            ].copy()

            if category_holdings.empty:
                continue

            consolidated = (
                category_holdings
                .groupby("symbol", as_index=False)
                .agg(
                    name=("name", "first"),
                    quantity=("quantity", "sum")
                )
            )

            st.markdown(f"### {category}")

            for _, holding in consolidated.iterrows():

                symbol = holding["symbol"]

                symbol_rows = category_holdings[
                    category_holdings["symbol"] == symbol
                ]

                total_shares = safe_float(
                    symbol_rows["quantity"].sum()
                )

                total_cost = safe_float(
                    (
                        symbol_rows["quantity"]
                        * symbol_rows["cost_per_unit"]
                    ).sum()
                )

                current_price = quotes.get(symbol)

                market_value = (
                    safe_float(current_price) * total_shares
                    if current_price is not None
                    else None
                )

                gain_loss = (
                    market_value - total_cost
                    if market_value is not None
                    else None
                )

                gain_pct = (
                    gain_loss / total_cost * 100
                    if gain_loss is not None and total_cost
                    else 0.0
                )

                value_text = (
                    money(market_value)
                    if market_value is not None
                    else "Unavailable"
                )

                gain_text = (
                    f"{money(gain_loss)} ({gain_pct:+.2f}%)"
                    if gain_loss is not None
                    else "Unavailable"
                )

                gain_color = (
                    "#31d18b"
                    if gain_loss is not None and gain_loss >= 0
                    else "#ff303f"
                )

                # Compact holding row
                name_col, shares_col, value_col, gain_col, detail_col = st.columns(
                    [2.2, 1.2, 1.3, 1.6, 1]
                )

                with name_col:
                    st.markdown(
                        f"""
                        <div style="padding: 8px 0;">
                            <div style="font-size: 16px; font-weight: 700;">
                                {holding['name']}
                                <span style="
                                    background: #191c22;
                                    color: #31d18b;
                                    padding: 2px 6px;
                                    border-radius: 5px;
                                    font-size: 12px;
                                    margin-left: 4px;
                                ">{symbol}</span>
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                with shares_col:
                    quantity_label = (
                        "Shares"
                        if category in ["Stocks", "ETFs"]
                        else "Quantity"
                    )

                    st.markdown(
                        f"""
                        <div style="padding: 5px 0;">
                            <div style="color: #a5a5a5; font-size: 11px;">
                                {quantity_label}
                            </div>
                            <div style="font-size: 15px; font-weight: 600;">
                                {total_shares:,.4f}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                with value_col:
                    st.markdown(
                        f"""
                        <div style="padding: 5px 0;">
                            <div style="color: #a5a5a5; font-size: 11px;">
                                Market Value
                            </div>
                            <div style="font-size: 15px; font-weight: 600;">
                                {value_text}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                with gain_col:
                    st.markdown(
                        f"""
                        <div style="padding: 5px 0;">
                            <div style="color: #a5a5a5; font-size: 11px;">
                                Profit / Loss
                            </div>
                            <div style="
                                font-size: 15px;
                                font-weight: 700;
                                color: {gain_color};
                            ">
                                {gain_text}
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )

                with detail_col:
                    if st.button(
                        "Details",
                        key=f"detail_{symbol}"
                    ):
                        st.session_state["selected_stock_detail"] = symbol
                        st.session_state.pop("selected_silver_detail", None)
                        st.rerun()

                st.divider()

    else:
        st.info("No holdings yet. Add your first investment.")

    # -------------------------------------------------
    # SELECTED STOCK DETAILS
    # -------------------------------------------------

    selected_symbol = st.session_state.get(
        "selected_stock_detail"
    )

    if selected_symbol:

        selected_rows = hold[
            hold["symbol"] == selected_symbol
        ]

        if not selected_rows.empty:

            selected_name = selected_rows.iloc[0]["name"]

            total_shares = safe_float(
                selected_rows["quantity"].sum()
            )

            total_cost = safe_float(
                (
                    selected_rows["quantity"]
                    * selected_rows["cost_per_unit"]
                ).sum()
            )

            average_cost = (
                total_cost / total_shares
                if total_shares
                else 0.0
            )

            current_price = quotes.get(selected_symbol)

            market_value = (
                safe_float(current_price) * total_shares
                if current_price is not None
                else None
            )

            gain_loss = (
                market_value - total_cost
                if market_value is not None
                else None
            )

            gain_pct = (
                gain_loss / total_cost * 100
                if gain_loss is not None and total_cost
                else 0.0
            )

            st.markdown("---")
            st.markdown(f"# {selected_name} · {selected_symbol}")

            if st.button("← Back to portfolio"):
                st.session_state.pop("selected_stock_detail", None)
                st.rerun()

            detail_metrics = st.columns(4)

            detail_metrics[0].metric(
                "Current Price",
                money(current_price)
                if current_price is not None
                else "Unavailable"
            )

            detail_metrics[1].metric(
                "Shares Owned",
                f"{total_shares:,.4f}"
            )

            detail_metrics[2].metric(
                "Average Cost",
                money(average_cost)
            )

            detail_metrics[3].metric(
                "Unrealized Gain/Loss",
                money(gain_loss)
                if gain_loss is not None
                else "Unavailable",
                f"{gain_pct:+.2f}%"
                if gain_loss is not None
                else None
            )

            st.metric(
                "Your Position Value",
                money(market_value)
                if market_value is not None
                else "Unavailable"
            )

            st.subheader("Where This Position Is Held")

            brokerage_breakdown = (
                selected_rows
                .groupby("account", dropna=False, as_index=False)
                .agg(
                    Shares=("quantity", "sum"),
                    Cost_Basis=(
                        "quantity",
                        lambda quantities: float(
                            (
                                quantities
                                * selected_rows.loc[
                                    quantities.index,
                                    "cost_per_unit"
                                ]
                            ).sum()
                        )
                    )
                )
            )

            brokerage_breakdown["Account"] = (
                brokerage_breakdown["account"]
                .fillna("Unspecified")
                .replace("", "Unspecified")
            )

            brokerage_breakdown["Market Value"] = (
                brokerage_breakdown["Shares"]
                * safe_float(current_price)
            )

            brokerage_breakdown["Shares"] = (
                brokerage_breakdown["Shares"].map(
                    lambda x: f"{x:,.4f}"
                )
            )

            brokerage_breakdown["Cost Basis"] = (
                brokerage_breakdown["Cost_Basis"].map(money)
            )

            brokerage_breakdown["Market Value"] = (
                brokerage_breakdown["Market Value"].map(money)
            )

            st.dataframe(
                brokerage_breakdown[
                    ["Account", "Shares", "Cost Basis", "Market Value"]
                ],
                use_container_width=True,
                hide_index=True
            )

            # Each holding lot can be removed directly, even if it has no
            # matching transaction record. This is important for manually
            # entered/imported positions and is scoped to the signed-in user.
            st.subheader("Individual Holding Lots")
            st.caption(
                "Delete a specific lot here. This does not require a Buy or Sell transaction."
            )

            for _, lot in selected_rows.sort_values("id", ascending=False).iterrows():
                lot_col, account_col, quantity_col, cost_col, delete_col = st.columns(
                    [2.2, 1.5, 1.1, 1.3, 0.6]
                )
                with lot_col:
                    st.markdown(f"**{lot.get('name', selected_symbol)}**")
                    st.caption(f"Lot #{int(lot['id'])}")
                with account_col:
                    st.caption("Account")
                    st.write(str(lot.get("account") or "Unspecified"))
                with quantity_col:
                    st.caption("Shares")
                    st.write(f"{safe_float(lot.get('quantity')):,.4f}")
                with cost_col:
                    st.caption("Cost per share")
                    st.write(money(lot.get("cost_per_unit")))
                with delete_col:
                    delete_button(
                        "holdings",
                        lot["id"],
                        f"{selected_symbol} holding lot",
                        f"holding_{selected_symbol}"
                    )

            price_data = stock_history(selected_symbol, "1y")

            st.subheader("1-Year Price History")

            if not price_data.empty:
                st.line_chart(
                    price_data.set_index("Date")["Close"],
                    height=300
                )
            else:
                st.info("Price history is currently unavailable.")

            st.subheader("Your Transaction History")

            if not tx.empty:

                ticker_transactions = tx[
                    tx["description"]
                    .astype(str)
                    .str.upper()
                    .str.contains(
                        selected_symbol,
                        regex=False
                    )
                ].copy()

                if not ticker_transactions.empty:
                    st.dataframe(
                        ticker_transactions[
                            [
                                "date",
                                "kind",
                                "description",
                                "amount",
                                "notes"
                            ]
                        ],
                        use_container_width=True,
                        hide_index=True
                    )
                else:
                    st.info(
                        "No transactions matching this ticker were found."
                    )

            else:
                st.info("No transactions recorded yet.")

    # -------------------------------------------------
    # COMMODITIES — OUTSIDE STOCK DETAIL CONDITION
    # -------------------------------------------------

       # -------------------------------------------------
    # COMMODITIES — COMPACT HOLDING ROW
    # -------------------------------------------------

    if silver_ounces > 0 or silver_market_value is not None:

        st.markdown("### Commodities")

        silver_gain_pct = (
            silver_gain_loss / silver_cost_basis * 100
            if silver_gain_loss is not None and silver_cost_basis
            else 0.0
        )

        silver_gain_color = (
            "#31d18b"
            if silver_gain_loss is not None and silver_gain_loss >= 0
            else "#ff303f"
        )

        silver_gain_text = (
            f"{money(silver_gain_loss)} ({silver_gain_pct:+.2f}%)"
            if silver_gain_loss is not None
            else "Unavailable"
        )

        # Same compact layout as stock holdings
        silver_name_col, silver_qty_col, silver_value_col, silver_gain_col, silver_detail_col = st.columns(
            [2.2, 1.2, 1.3, 1.6, 1]
        )

        with silver_name_col:
            st.markdown(
                """
                <div style="padding: 8px 0;">
                    <div style="font-size: 16px; font-weight: 700;">
                        Physical Silver
                        <span style="
                            background: #191c22;
                            color: #31d18b;
                            padding: 2px 6px;
                            border-radius: 5px;
                            font-size: 12px;
                            margin-left: 4px;
                        ">XAG</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        with silver_qty_col:
            st.markdown(
                f"""
                <div style="padding: 5px 0;">
                    <div style="color: #a5a5a5; font-size: 11px;">
                        Ounces
                    </div>
                    <div style="font-size: 15px; font-weight: 600;">
                        {silver_ounces:,.4f} oz
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        with silver_value_col:
            st.markdown(
                f"""
                <div style="padding: 5px 0;">
                    <div style="color: #a5a5a5; font-size: 11px;">
                        Market Value
                    </div>
                    <div style="font-size: 15px; font-weight: 600;">
                        {money(silver_market_value)}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        with silver_gain_col:
            st.markdown(
                f"""
                <div style="padding: 5px 0;">
                    <div style="color: #a5a5a5; font-size: 11px;">
                        Profit / Loss
                    </div>
                    <div style="
                        font-size: 15px;
                        font-weight: 700;
                        color: {silver_gain_color};
                    ">
                        {silver_gain_text}
                    </div>
                </div>
                """,
                unsafe_allow_html=True
            )

        with silver_detail_col:
            if st.button(
                "Details",
                key="silver_view_details"
            ):
                st.session_state["selected_silver_detail"] = True
                st.session_state.pop("selected_stock_detail", None)
                st.rerun()

        st.divider()

    # -------------------------------------------------
    # SELECTED SILVER DETAILS
    # -------------------------------------------------

    if st.session_state.get("selected_silver_detail"):

        st.markdown("---")
        st.markdown("# Physical Silver · XAG")

        if st.button(
            "← Back to portfolio",
            key="back_from_silver"
        ):
            st.session_state.pop("selected_silver_detail", None)
            st.rerun()

        silver_metrics = st.columns(4)

        silver_metrics[0].metric(
            "Ounces Owned",
            f"{silver_ounces:,.4f} oz"
        )

        silver_metrics[1].metric(
            "Silver Spot / Reference Price",
            money(silver_current_price)
            if silver_current_price is not None
            else "Unavailable"
        )

        silver_metrics[2].metric(
            "Silver Market Value",
            money(silver_market_value)
            if silver_market_value is not None
            else "Waiting for quote"
        )

        silver_metrics[3].metric(
            "Unrealized Gain/Loss",
            money(silver_gain_loss)
            if silver_gain_loss is not None
            else "Waiting for quote",
            f"{silver_gain_pct:+.2f}%"
            if silver_gain_loss is not None
            else None
        )

        st.caption(
            "Market value uses the latest available Yahoo Finance "
            "silver reference price. Physical dealer premiums "
            "and buyback prices may differ."
        )

        silver_chart = silver_history("1y")

        st.subheader("XAG · 1-Year Price History")

        if not silver_chart.empty:
            st.line_chart(
                silver_chart.set_index("Date")["Close"],
                height=250
            )
        else:
            st.info(
                "Silver price history is currently unavailable."
            )

        st.subheader("Your Silver Transaction History")

        if not silver_tx.empty:
            st.dataframe(
                silver_tx[
                    [
                        "date",
                        "kind",
                        "ounces",
                        "price_per_ounce",
                        "total_amount",
                        "notes"
                    ]
                ],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.info(
                "No physical silver transactions recorded yet."
            )

# -----------------------------------------------------
# NEEDS ALLOCATION
# -----------------------------------------------------

paycheck_rows = q("""
    SELECT id, date, amount, notes
    FROM transactions
    WHERE kind = 'Paycheck'
    ORDER BY date, id
""")

remaining = []

for _, paycheck_row in paycheck_rows.iterrows():
    amount = safe_float(paycheck_row["amount"])
    notes = str(paycheck_row.get("notes", "") or "")

    # Read the saved 70% allocation from notes when available.
    if "allocated $" in notes:
        try:
            allocated_text = notes.split("allocated $", 1)[1]
            allocated_text = allocated_text.split(";", 1)[0]
            amount = float(allocated_text.replace(",", "").strip())
        except (ValueError, IndexError):
            pass

    # Only count Fidelity transfers dated on/after this paycheck.
    transfer_total = safe_float(q("""
        SELECT COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE kind = 'Fidelity cash transfer'
          AND date >= ?
    """, (str(paycheck_row["date"]),)).iloc[0, 0])

    amount = max(0, amount - transfer_total)

    if amount > 0.005:
        remaining.append({
            "Date": paycheck_row["date"],
            "Needs Allocation": money(amount)
        })

needs_allocation = sum(
    safe_float(
        row["Needs Allocation"].replace("$", "").replace(",", "")
    )
    for row in remaining
)

st.subheader("Needs Allocation")
st.metric("Outstanding paycheck allocations", money(needs_allocation))

if remaining:
    st.dataframe(
        pd.DataFrame(remaining),
        use_container_width=True,
        hide_index=True
    )
else:
    st.caption("No paycheck allocations waiting to be invested.")
    

    # -----------------------------------------------------
    # MARKET SNAPSHOT
    # -----------------------------------------------------

    st.subheader("Market Snapshot")

    market_cols = st.columns(2)

    for col, symbol, label in [
        (market_cols[0], "GC=F", "Gold futures (USD/oz)"),
        (market_cols[1], "SI=F", "Silver futures (USD/oz)")
    ]:

        current_price = quotes.get(symbol)

        col.metric(
            label,
            money(current_price)
            if current_price is not None
            else "Unavailable"
        )

# =========================================================
# RECENT ACTIVITY
# =========================================================

with activity_tab:

    st.subheader("Recent Activity")

    if not tx.empty:

        for _, transaction in tx.head(25).iterrows():

            item_col, trash_col = st.columns([12, 1])

            with item_col:
                st.markdown(
                    f"""
                    **{transaction['kind']}** · {transaction['description']}  
                    {transaction['date']} · **{money(transaction['amount'])}**
                    """
                )

                if transaction["notes"]:
                    st.caption(transaction["notes"])

            with trash_col:
                delete_button(
                    "transactions",
                    transaction["id"],
                    "transaction",
                    "transaction"
                )

            st.divider()

    else:
        st.info("No transactions recorded yet.")

    st.subheader("Physical Silver Activity")

    if not silver_tx.empty:

        for _, transaction in silver_tx.head(25).iterrows():

            item_col, trash_col = st.columns([12, 1])

            with item_col:
                st.markdown(
                    f"""
                    **{transaction['kind']}** · XAG  
                    {transaction['date']} ·
                    **{safe_float(transaction['ounces']):,.4f} oz** ·
                    **{money(transaction['total_amount'])}**
                    """
                )

                if transaction["notes"]:
                    st.caption(transaction["notes"])

            with trash_col:

                with st.popover(
                    "🗑️",
                    help="Delete silver transaction"
                ):

                    st.warning(
                        "Delete this silver transaction? "
                        "This cannot be undone."
                    )

                    confirm_silver_delete = st.checkbox(
                        "Confirm deletion",
                        key=f"confirm_silver_{transaction['id']}"
                    )

                    if st.button(
                        "Delete",
                        key=f"delete_silver_{transaction['id']}",
                        disabled=not confirm_silver_delete,
                        type="primary"
                    ):

                        run(
                            """
                            DELETE FROM silver_transactions
                            WHERE id = ?
                            """,
                            (int(transaction["id"]),)
                        )

                        st.cache_data.clear()
                        st.rerun()

            st.divider()

    else:
        st.info("No physical silver transactions recorded yet.")


# =========================================================
# ADD TRANSACTION
# =========================================================

with add_tab:

    st.subheader("Add Transaction")

    transaction_type = st.selectbox(
        "Choose activity",
        [
            "Buy",
            "Add Existing Holding",
            "Sell",
            "Silver (Physical)",
            "Paycheck",
            "Bonus",
            "Cash invested to Fidelity",
            "Cash balance",
            "Manual contribution",
            "Dividend"
        ]
    )

    st.divider()

    # -----------------------------------------------------
    # ADD EXISTING HOLDING
    # -----------------------------------------------------

    if transaction_type == "Add Existing Holding":

        st.subheader("Add Existing Holding")

        st.caption(
            "Records an existing investment and its historical purchase. "
            "It does not subtract from Ready to Invest."
        )

        with st.form("existing_holding_form"):

            holding_date = st.date_input(
                "Original purchase date",
                date.today()
            )

            symbol = st.text_input(
                "Ticker / symbol",
                placeholder="GME, AAPL, VOO"
            ).upper().strip()

            name = st.text_input("Asset name")

            asset_type = st.selectbox(
                "Asset type",
                ["Stock", "ETF", "Gold", "Other"]
            )

            quantity = st.number_input(
                "Quantity / shares",
                min_value=0.0,
                step=0.001,
                format="%.4f"
            )

            unit_cost = st.number_input(
                "Original cost per share ($)",
                min_value=0.0,
                step=0.01
            )

            account = st.text_input(
                "Brokerage / account",
                placeholder="Computershare, Fidelity"
            )

            submitted = st.form_submit_button(
                "Add Existing Holding",
                type="primary"
            )

        if submitted:

            if not symbol or quantity <= 0:
                st.error(
                    "Enter a ticker and quantity greater than zero."
                )

            else:

                asset_name = name or symbol
                account_name = account.strip() or "Unspecified"
                historical_amount = quantity * unit_cost

                run("""
                    INSERT INTO holdings
                    (
                        symbol, name, asset_type, quantity,
                        cost_per_unit, account, created
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol,
                    asset_name,
                    asset_type,
                    quantity,
                    unit_cost,
                    account_name,
                    str(holding_date)
                ))

                run("""
                    INSERT INTO transactions
                    (date, kind, description, amount, notes)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    str(holding_date),
                    "Buy",
                    f"{symbol} {asset_name} ({account_name})",
                    historical_amount,
                    (
                        "Historical existing holding; "
                        f"{quantity:,.4f} shares at ${unit_cost:,.2f} each. "
                        "Imported without changing cash."
                    )
                ))

                st.success(
                    f"Added {quantity:,.4f} shares of {symbol}. "
                    f"Historical purchase: {money(historical_amount)}. "
                    "Ready to Invest was not changed."
                )

                st.rerun()


    # -----------------------------------------------------
    # PHYSICAL SILVER
    # -----------------------------------------------------

    elif transaction_type == "Silver (Physical)":

        st.subheader("Physical Silver")

        with st.form("silver_transaction_form"):

            silver_action = st.selectbox(
                "Action",
                ["Buy Silver", "Sell Silver"]
            )

            silver_date = st.date_input(
                "Transaction date",
                date.today()
            )

            silver_ounces_input = st.number_input(
                "Quantity (troy ounces)",
                min_value=0.0001,
                value=1.0,
                step=0.1,
                format="%.4f"
            )

            live_silver_price = price("XAG")

            if live_silver_price is not None:
                st.caption(
                    f"Market reference: "
                    f"${live_silver_price:,.2f} per troy oz"
                )

                default_silver_price = float(live_silver_price)

            else:
                st.warning(
                    "Live silver price unavailable. "
                    "Enter your transaction price."
                )

                default_silver_price = 0.0

            silver_unit_price = st.number_input(
                "Your price per troy ounce (USD)",
                min_value=0.0,
                value=default_silver_price,
                step=0.01,
                format="%.2f"
            )

            silver_notes_input = st.text_input(
                "Notes (optional)",
                placeholder="Coins, bars, dealer, premium, etc."
            )

            silver_total_input = (
                silver_ounces_input * silver_unit_price
            )

            st.metric(
                "Transaction total",
                money(silver_total_input)
            )

            silver_submitted = st.form_submit_button(
                "Save Silver Transaction",
                type="primary"
            )

        if silver_submitted:

            current_silver = q("""
                SELECT COALESCE(
                    SUM(
                        CASE
                            WHEN kind = 'Buy Silver'
                                THEN ounces
                            WHEN kind = 'Sell Silver'
                                THEN -ounces
                            ELSE 0
                        END
                    ),
                    0
                ) AS ounces
                FROM silver_transactions
            """)

            currently_owned = (
                safe_float(current_silver.iloc[0]["ounces"])
                if not current_silver.empty
                else 0.0
            )

            if (
                silver_action == "Sell Silver"
                and silver_ounces_input > currently_owned
            ):

                st.error(
                    f"You have {currently_owned:.4f} oz recorded. "
                    "You cannot sell more than your tracked amount."
                )

            else:

                run("""
                    INSERT INTO silver_transactions
                    (
                        date, kind, ticker, ounces,
                        price_per_ounce, total_amount, notes
                    )
                    VALUES (?, ?, 'XAG', ?, ?, ?, ?)
                """, (
                    str(silver_date),
                    silver_action,
                    silver_ounces_input,
                    silver_unit_price,
                    silver_total_input,
                    silver_notes_input
                ))

                st.success("Silver transaction saved.")
                st.rerun()


    # -----------------------------------------------------
    # BUY STOCK / ETF / OTHER
    # -----------------------------------------------------

    elif transaction_type == "Buy":

        with st.form("buy_form"):

            transaction_date = st.date_input(
                "Purchase date",
                date.today()
            )

            symbol = st.text_input(
                "Ticker / symbol",
                placeholder="AAPL, VOO, GLD"
            ).upper().strip()

            name = st.text_input("Asset name")

            asset_type = st.selectbox(
                "Asset type",
                ["Stock", "ETF", "Gold", "Silver", "Other"]
            )

            quantity = st.number_input(
                "Quantity / shares",
                min_value=0.0,
                step=0.001,
                format="%.4f"
            )

            unit_price = st.number_input(
                "Purchase price per unit ($)",
                min_value=0.0,
                step=0.01
            )

            account = st.text_input(
                "Account",
                "Fidelity"
            )

            notes = st.text_input("Notes")

            submitted = st.form_submit_button("Save Buy")

        if submitted:

            if not symbol or quantity <= 0:
                st.error("Enter a ticker and quantity.")

            else:

                amount = quantity * unit_price

                run("""
                    INSERT INTO holdings
                    (
                        symbol, name, asset_type, quantity,
                        cost_per_unit, account, created
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol,
                    name or symbol,
                    asset_type,
                    quantity,
                    unit_price,
                    account,
                    str(transaction_date)
                ))

                run("""
                    INSERT INTO transactions
                    (date, kind, description, amount, notes)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    str(transaction_date),
                    "Buy",
                    f"{symbol} {name}".strip(),
                    amount,
                    notes
                ))

                st.success("Purchase recorded.")
                st.rerun()


    # -----------------------------------------------------
    # SELL STOCK / ETF / OTHER
    # -----------------------------------------------------

    elif transaction_type == "Sell":

        with st.form("sell_form"):

            transaction_date = st.date_input(
                "Sale date",
                date.today()
            )

            symbol = st.text_input(
                "Ticker / symbol"
            ).upper().strip()

            quantity = st.number_input(
                "Quantity sold",
                min_value=0.0,
                step=0.001,
                format="%.4f"
            )

            sale_price = st.number_input(
                "Sale price per unit ($)",
                min_value=0.0,
                step=0.01
            )

            account = st.text_input(
                "Account",
                "Fidelity"
            )

            notes = st.text_input("Notes")

            submitted = st.form_submit_button("Save Sell")

        if submitted:

            existing = q("""
                SELECT *
                FROM holdings
                WHERE symbol = ?
                  AND account = ?
                ORDER BY id
            """, (symbol, account))

            if not symbol or quantity <= 0:
                st.error("Enter a ticker and quantity.")

            elif existing.empty:
                st.error("No matching holding found.")

            elif safe_float(existing["quantity"].sum()) < quantity:
                st.error("Insufficient tracked quantity to sell.")

            else:

                remaining_to_sell = float(quantity)
                amount = quantity * sale_price

                for _, holding_row in existing.iterrows():

                    if remaining_to_sell <= 0:
                        break

                    available = safe_float(holding_row["quantity"])
                    sell_from_row = min(
                        available,
                        remaining_to_sell
                    )

                    run("""
                        UPDATE holdings
                        SET quantity = quantity - ?
                        WHERE id = ?
                    """, (
                        sell_from_row,
                        int(holding_row["id"])
                    ))

                    remaining_to_sell -= sell_from_row

                run("""
                    INSERT INTO transactions
                    (date, kind, description, amount, notes)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    str(transaction_date),
                    "Sell",
                    f"{symbol} sale from {account}",
                    amount,
                    notes
                ))

                st.success("Sale recorded.")
                st.rerun()


    # -----------------------------------------------------
    # PAYCHECK / BONUS
    # -----------------------------------------------------

    elif transaction_type in ["Paycheck", "Bonus"]:

        with st.form("paycheck_form"):

            transaction_date = st.date_input(
                "Pay date",
                date.today()
            )

            paycheck = st.number_input(
                "Net paycheck / bonus ($)",
                min_value=0.0,
                step=50.0
            )

            if transaction_type == "Bonus":
                percent_invested = 100
                st.info("Bonus allocation is set to 100% invested.")

            else:
                percent_invested = st.slider(
                    "Percent allocated to investing",
                    0,
                    100,
                    70
                )

            notes = st.text_input("Notes")

            submitted = st.form_submit_button(
                "Save Paycheck / Bonus"
            )

        if submitted:

            invested = paycheck * percent_invested / 100
            retained = paycheck - invested

            run("""
                INSERT INTO transactions
                (date, kind, description, amount, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(transaction_date),
                transaction_type,
                f"{transaction_type} allocation",
                invested,
                (
                    f"Income {money(paycheck)}; "
                    f"allocated {money(invested)}; "
                    f"retained {money(retained)}. {notes}"
                )
            ))

            st.success(
                f"Allocated {money(invested)} to investing · "
                f"Retained {money(retained)}"
            )

            st.caption(
                "This records your allocation. "
                "Record actual purchases separately."
            )

            st.rerun()


    # -----------------------------------------------------
    # FIDELITY CASH TRANSFER
    # -----------------------------------------------------

    elif transaction_type == "Cash invested to Fidelity":

        with st.form("fidelity_transfer_form"):

            transaction_date = st.date_input(
                "Transfer date",
                date.today()
            )

            amount = st.number_input(
                "Amount added to Fidelity cash ($)",
                min_value=0.0,
                step=25.0
            )

            notes = st.text_input("Notes")

            submitted = st.form_submit_button(
                "Record Fidelity Cash Addition"
            )

        if submitted:

            run("""
                INSERT INTO transactions
                (date, kind, description, amount, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(transaction_date),
                "Fidelity cash transfer",
                "Cash added to Fidelity",
                amount,
                notes
            ))

            st.success("Fidelity cash addition recorded.")
            st.caption(
                "Update your current Fidelity cash amount using Cash balance."
            )

            st.rerun()


    # -----------------------------------------------------
    # CASH BALANCE
    # -----------------------------------------------------

    elif transaction_type == "Cash balance":

        with st.form("cash_balance_form"):

            balance_date = st.date_input(
                "Balance date",
                date.today()
            )

            account = st.selectbox(
                "Account",
                ["Fidelity cash", "Checking", "Savings", "Other"]
            )

            amount = st.number_input(
                "Current account balance ($)",
                min_value=0.0,
                step=25.0
            )

            submitted = st.form_submit_button("Save Cash Balance")

        if submitted:

            run("""
                INSERT INTO balances
                (date, account, amount)
                VALUES (?, ?, ?)
            """, (
                str(balance_date),
                account,
                amount
            ))

            st.success("Cash balance saved.")
            st.rerun()


    # -----------------------------------------------------
    # MANUAL CONTRIBUTION
    # -----------------------------------------------------

    elif transaction_type == "Manual contribution":

        with st.form("manual_contribution_form"):

            transaction_date = st.date_input(
                "Contribution date",
                date.today()
            )

            amount = st.number_input(
                "Contribution amount ($)",
                min_value=0.0,
                step=25.0
            )

            notes = st.text_input("Notes")

            submitted = st.form_submit_button("Save Contribution")

        if submitted:

            run("""
                INSERT INTO transactions
                (date, kind, description, amount, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(transaction_date),
                "Manual contribution",
                "Manual investment contribution",
                amount,
                notes
            ))

            st.success("Contribution recorded.")
            st.rerun()


    # -----------------------------------------------------
    # DIVIDEND
    # -----------------------------------------------------

    elif transaction_type == "Dividend":

        with st.form("dividend_form"):

            transaction_date = st.date_input(
                "Dividend date",
                date.today()
            )

            symbol = st.text_input(
                "Ticker / symbol"
            ).upper().strip()

            amount = st.number_input(
                "Dividend received ($)",
                min_value=0.0,
                step=0.01
            )

            notes = st.text_input("Notes")

            submitted = st.form_submit_button("Save Dividend")

        if submitted:

            run("""
                INSERT INTO transactions
                (date, kind, description, amount, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(transaction_date),
                "Dividend",
                f"{symbol} dividend",
                amount,
                notes
            ))

            st.success("Dividend recorded.")
            st.rerun()


# =========================================================
# SIDEBAR
# =========================================================

st.sidebar.header("PALACE")


if st.sidebar.button("Refresh market prices"):

    get_quotes.clear()
    price.clear()
    stock_history.clear()
    silver_history.clear()

    st.rerun()

st.sidebar.download_button(
    "Export transactions CSV",
    tx.to_csv(index=False).encode("utf-8"),
    "palace_transactions.csv",
    "text/csv"
)
