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

# Keep a separate Supabase client per Streamlit browser session.
# A globally cached client could mix authenticated tokens between users.
if "_palace_supabase_client" not in st.session_state:
    st.session_state["_palace_supabase_client"] = create_client(SUPABASE_URL, SUPABASE_KEY)

supabase = st.session_state["_palace_supabase_client"]

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

def palace_brand():
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


def auth_screen():
    # Centered PALACE wordmark and invitation-only sign-in screen.
    st.markdown(
        """
        <style>
        .palace-login-brand {
            text-align: center;
            padding-top: 2.5rem;
            padding-bottom: 1.25rem;
        }
        .palace-login-brand .wordmark {
            font-family: Georgia, 'Times New Roman', serif;
            font-size: clamp(3rem, 7vw, 5rem);
            font-weight: 600;
            letter-spacing: 0.20em;
            line-height: 1.1;
            margin: 0;
        }
        .palace-login-brand .definition {
            font-size: 0.9rem;
            opacity: 0.78;
            margin-top: 1rem;
        }
        .palace-login-brand .motto {
            font-size: 0.72rem;
            letter-spacing: 0.18em;
            margin-top: 0.55rem;
            opacity: 0.68;
        }
        </style>
        <div class="palace-login-brand">
            <div class="wordmark">PALACE</div>
            <div class="definition">Noun · From Basque Jauregi / Jauregui</div>
            <div class="motto">THE SEAT OF A FAMILY AND ITS LEGACY</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    left, center, right = st.columns([1, 1.4, 1])
    with center:
        st.markdown("### Welcome")
        with st.form("palace_login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submit = st.form_submit_button(
                "Log in", type="primary", use_container_width=True
            )
        if submit:
            if not email.strip() or not password:
                st.error("Enter your email and password.")
            else:
                try:
                    result = supabase.auth.sign_in_with_password(
                        {"email": email.strip(), "password": password}
                    )
                    if not result.session:
                        st.error("Login did not return a session. Check your account and email confirmation.")
                    else:
                        set_session(result.session)
                        st.rerun()
                except Exception:
                    # Avoid exposing backend details on the public login screen.
                    st.error("Could not log in. Check your email and password, then try again.")

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
