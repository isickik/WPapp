import os
from datetime import date
import pandas as pd
import streamlit as st
import yfinance as yf
from supabase import create_client

st.set_page_config(page_title="PALACE Wealth Tracker", page_icon="📈", layout="wide")

# Credentials are supplied through Streamlit Community Cloud Secrets.
def get_setting(name):
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name)

SUPABASE_URL = get_setting("SUPABASE_URL")
SUPABASE_KEY = get_setting("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("PALACE is not configured yet. Add SUPABASE_URL and SUPABASE_KEY in Streamlit Secrets.")
    st.stop()

@st.cache_resource
def make_supabase(url, key):
    return create_client(url, key)

supabase = make_supabase(SUPABASE_URL, SUPABASE_KEY)

# Keep the authenticated access token in Streamlit session state and apply it
# to the Supabase client so Postgres RLS sees the current auth.uid().
def set_session(auth_session):
    supabase.auth.set_session(auth_session.access_token, auth_session.refresh_token)
    st.session_state["palace_user"] = auth_session.user
    st.session_state["palace_access_token"] = auth_session.access_token


def current_user():
    user = st.session_state.get("palace_user")
    token = st.session_state.get("palace_access_token")
    if user and token:
        try:
            supabase.postgrest.auth(token)
            return user
        except Exception:
            pass
    return None


def money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def fetch_rows(table, order_col="id", descending=True):
    response = supabase.table(table).select("*").order(order_col, desc=descending).execute()
    return pd.DataFrame(response.data or [])


def add_row(table, values):
    # RLS sets/validates owner. Never accept a user_id from the form.
    return supabase.table(table).insert(values).execute()


def delete_row(table, row_id):
    # RLS prevents deletion of another user's row even if an ID is guessed.
    return supabase.table(table).delete().eq("id", row_id).execute()


def auth_screen():
    st.markdown("# PALACE")
    st.caption("Noun · From Basque Jauregi / Jauregui")
    st.caption("THE SEAT OF A FAMILY AND ITS LEGACY")
    login, signup = st.tabs(["Log in", "Create account"])
    with login:
        with st.form("palace_login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submit = st.form_submit_button("Log in", type="primary", use_container_width=True)
        if submit:
            try:
                result = supabase.auth.sign_in_with_password({"email": email.strip(), "password": password})
                set_session(result.session)
                st.rerun()
            except Exception as e:
                st.error(f"Could not log in: {e}")
    with signup:
        with st.form("palace_signup"):
            full_name = st.text_input("Name")
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_password", help="Use at least 8 characters.")
            confirm = st.text_input("Confirm password", type="password")
            submit = st.form_submit_button("Create account", type="primary", use_container_width=True)
        if submit:
            if not full_name.strip() or not email.strip():
                st.error("Enter your name and email.")
            elif len(password) < 8:
                st.error("Password must be at least 8 characters.")
            elif password != confirm:
                st.error("Passwords do not match.")
            else:
                try:
                    result = supabase.auth.sign_up({"email": email.strip(), "password": password,
                        "options": {"data": {"full_name": full_name.strip()}}})
                    if result.session:
                        set_session(result.session)
                        st.rerun()
                    else:
                        st.success("Account created. Check your email to confirm, then log in.")
                except Exception as e:
                    st.error(f"Could not create account: {e}")

user = current_user()
if not user:
    auth_screen()
    st.stop()

with st.sidebar:
    st.markdown("## PALACE")
    st.caption(user.email or "Signed in")
    if st.button("Log out", use_container_width=True):
        try:
            supabase.auth.sign_out()
        finally:
            for key in ("palace_user", "palace_access_token"):
                st.session_state.pop(key, None)
        st.rerun()

st.markdown("# PALACE")
st.caption("Noun · From Basque Jauregi / Jauregui")
st.caption("THE SEAT OF A FAMILY AND ITS LEGACY")
st.info("You are signed in. Your Supabase Row Level Security policies isolate your records by account.")

tab_dashboard, tab_holdings, tab_activity, tab_add = st.tabs(["Dashboard", "Holdings", "Recent Activity", "+ Add"])

with tab_dashboard:
    try:
        holdings = fetch_rows("holdings", "id")
        transactions = fetch_rows("transactions", "date")
        balances = fetch_rows("balances", "date")
        if not holdings.empty:
            symbols = holdings["symbol"].fillna("").astype(str).str.upper().unique().tolist()
            quotes = {}
            for symbol in symbols:
                if symbol:
                    try:
                        hist = yf.Ticker(symbol).history(period="5d")
                        quotes[symbol] = float(hist["Close"].dropna().iloc[-1]) if not hist.empty else 0
                    except Exception:
                        quotes[symbol] = 0
            holdings["market_price"] = holdings["symbol"].map(quotes).fillna(0)
            holdings["market_value"] = pd.to_numeric(holdings["quantity"], errors="coerce").fillna(0) * holdings["market_price"]
            portfolio_value = float(holdings["market_value"].sum())
        else:
            portfolio_value = 0.0
        cash_total = float(pd.to_numeric(balances.get("amount", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()) if not balances.empty else 0.0
        invested_ytd = 0.0
        if not transactions.empty and "kind" in transactions:
            year = str(date.today().year)
            tx_ytd = transactions[transactions["date"].astype(str).str.startswith(year)]
            invested_ytd = float(pd.to_numeric(tx_ytd.loc[tx_ytd["kind"].isin(["Buy", "Manual contribution"]), "amount"], errors="coerce").fillna(0).sum())
        a, b, c = st.columns(3)
        a.metric("Net Worth", money(portfolio_value + cash_total))
        b.metric("Portfolio Value", money(portfolio_value))
        c.metric("Invested YTD", money(invested_ytd))
        st.caption("Market quotes are for tracking only and may be delayed. Refresh the page to update.")
    except Exception as e:
        st.error(f"Could not load dashboard data: {e}")

with tab_holdings:
    try:
        holdings = fetch_rows("holdings", "id")
        if holdings.empty:
            st.info("No holdings yet. Add your first holding in + Add.")
        else:
            for _, row in holdings.iterrows():
                left, right = st.columns([8, 1])
                with left:
                    st.markdown(f"**{row.get('symbol','')}** · {row.get('name','')}  ")
                    st.caption(f"{row.get('asset_type','')} · {row.get('account','')} · Quantity: {row.get('quantity',0)} · Cost/unit: {money(row.get('cost_per_unit',0))}")
                with right:
                    if st.button("🗑️", key=f"del_hold_{row['id']}"):
                        st.session_state["delete_hold_id"] = int(row["id"])
            if st.session_state.get("delete_hold_id"):
                st.warning("Confirm deleting this holding?")
                yes, no = st.columns(2)
                if yes.button("Confirm delete", type="primary"):
                    delete_row("holdings", st.session_state["delete_hold_id"])
                    st.session_state.pop("delete_hold_id", None)
                    st.rerun()
                if no.button("Cancel"):
                    st.session_state.pop("delete_hold_id", None)
                    st.rerun()
    except Exception as e:
        st.error(f"Could not load holdings: {e}")

with tab_activity:
    try:
        transactions = fetch_rows("transactions", "date")
        if transactions.empty:
            st.info("No activity recorded yet.")
        else:
            st.dataframe(transactions, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"Could not load activity: {e}")

with tab_add:
    add_type = st.radio("Record", ["Holding", "Transaction", "Cash balance"], horizontal=True)
    if add_type == "Holding":
        with st.form("add_holding"):
            symbol = st.text_input("Ticker / symbol", placeholder="GME, AAPL, VOO").upper().strip()
            name = st.text_input("Asset name")
            asset_type = st.selectbox("Asset type", ["Stock", "ETF", "Gold", "Silver", "Other"])
            quantity = st.number_input("Quantity / shares / ounces", min_value=0.0, step=0.001, format="%.4f")
            cost = st.number_input("Cost per unit ($)", min_value=0.0, step=0.01)
            account = st.text_input("Account", placeholder="Fidelity, Computershare, Physical")
            submit = st.form_submit_button("Save holding", type="primary")
        if submit:
            if not symbol or quantity <= 0:
                st.error("Enter a symbol and positive quantity.")
            else:
                try:
                    add_row("holdings", {"symbol": symbol, "name": name, "asset_type": asset_type,
                        "quantity": quantity, "cost_per_unit": cost, "account": account, "created": str(date.today())})
                    st.success("Holding saved.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Could not save holding: {e}")
    elif add_type == "Transaction":
        with st.form("add_transaction"):
            tx_date = st.date_input("Date", value=date.today())
            kind = st.selectbox("Type", ["Buy", "Sell", "Paycheck", "Bonus", "Fidelity cash transfer", "Manual contribution", "Dividend"])
            description = st.text_input("Description")
            amount = st.number_input("Amount ($)", min_value=0.0, step=0.01)
            notes = st.text_area("Notes")
            submit = st.form_submit_button("Save transaction", type="primary")
        if submit:
            try:
                add_row("transactions", {"date": str(tx_date), "kind": kind, "description": description,
                    "amount": amount, "notes": notes})
                st.success("Transaction saved.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save transaction: {e}")
    else:
        with st.form("add_balance"):
            balance_date = st.date_input("Balance date", value=date.today())
            account = st.text_input("Account", placeholder="Checking, Savings, Fidelity")
            amount = st.number_input("Balance ($)", step=0.01)
            submit = st.form_submit_button("Save balance", type="primary")
        if submit:
            try:
                add_row("balances", {"date": str(balance_date), "account": account, "amount": amount})
                st.success("Balance saved.")
                st.rerun()
            except Exception as e:
                st.error(f"Could not save balance: {e}")
