import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf
import datetime
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import extra_streamlit_components as stx
except ImportError:
    stx = None


DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
    "META", "TSLA", "AMD", "NFLX", "JPM",
]
LEGACY_PREFERENCES_FILE = Path(__file__).with_name("preferences.json")
PREFERENCES_FILE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "local.educational.stock-scanner"
    / "preferences.json"
)
FALLBACK_PREFERENCES_FILE = Path("/tmp/local.educational.stock-scanner-preferences.json")
BROWSER_WATCHLIST_COOKIE = "stock_scanner_watchlist"
BROWSER_ALPACA_KEY_COOKIE = "stock_scanner_alpaca_key"
BROWSER_ALPACA_SECRET_COOKIE = "stock_scanner_alpaca_secret"
BROWSER_SEC_EMAIL_COOKIE = "stock_scanner_sec_email"
browser_cookie_manager = None
live_stock_search_component = components.declare_component(
    "live_stock_search",
    path=str(Path(__file__).with_name("live_stock_search_component")),
)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
OPENFDA_DRUGS_URL = "https://api.fda.gov/drug/drugsfda.json"
SEC_RISK_ITEMS = {
    "1.03": "Bankruptcy or receivership",
    "2.04": "Triggering events that accelerate an obligation",
    "3.01": "Delisting or listing-rule warning",
    "4.01": "Change in accountant/auditor",
    "4.02": "Prior financial statements should not be relied upon",
}
GRADE_DISPLAY = {
    "A": "🟢 A",
    "B": "🟢 B",
    "C": "🟡 C",
    "D": "🟠 D",
    "F": "🔴 F",
}
CONGRESS_TRADES_URL = "https://congressinfor-production.up.railway.app/trades?limit=200"
TRACKED_13F_MANAGERS = {
    "Berkshire Hathaway": "0001067983",
    "Bridgewater Associates": "0001350694",
    "Citadel Advisors": "0001423053",
    "Pershing Square Capital Management": "0001336528",
    "Renaissance Technologies": "0001037389",
    "Soros Fund Management": "0001029160",
}


def is_streamlit_cloud() -> bool:
    try:
        host = str(st.context.headers.get("Host", "")).lower().split(":", 1)[0]
    except Exception:
        host = ""
    return host.endswith(".streamlit.app") or os.environ.get(
        "STREAMLIT_SHARING_MODE", ""
    ).lower() == "streamlit"


def load_preferences() -> dict:
    if is_streamlit_cloud():
        browser_preferences = {}
        if browser_cookie_manager is not None:
            try:
                saved_symbols = browser_cookie_manager.get(
                    cookie=BROWSER_WATCHLIST_COOKIE
                )
                if saved_symbols:
                    st.session_state["_browser_watchlist_cookie"] = saved_symbols
                    browser_preferences["symbols"] = saved_symbols
                saved_alpaca_key = browser_cookie_manager.get(
                    cookie=BROWSER_ALPACA_KEY_COOKIE
                )
                saved_alpaca_secret = browser_cookie_manager.get(
                    cookie=BROWSER_ALPACA_SECRET_COOKIE
                )
                saved_sec_email = browser_cookie_manager.get(
                    cookie=BROWSER_SEC_EMAIL_COOKIE
                )
                if saved_alpaca_key:
                    browser_preferences["alpaca_api_key"] = saved_alpaca_key
                if saved_alpaca_secret:
                    browser_preferences["alpaca_api_secret"] = saved_alpaca_secret
                if saved_sec_email:
                    browser_preferences["sec_contact_email"] = saved_sec_email
            except Exception:
                pass
        return browser_preferences
    for preferences_path in (
        PREFERENCES_FILE,
        FALLBACK_PREFERENCES_FILE,
        LEGACY_PREFERENCES_FILE,
    ):
        if not preferences_path.exists():
            continue
        try:
            with preferences_path.open("r", encoding="utf-8") as preferences_file:
                return json.load(preferences_file)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def write_preferences(preferences: dict) -> None:
    if is_streamlit_cloud():
        saved_symbols = str(preferences.get("symbols", "")).strip()
        if (
            browser_cookie_manager is not None
            and saved_symbols
            and st.session_state.get("_browser_watchlist_cookie") != saved_symbols
        ):
            try:
                browser_cookie_manager.set(
                    BROWSER_WATCHLIST_COOKIE,
                    saved_symbols,
                    expires_at=datetime.datetime.now() + datetime.timedelta(days=365),
                )
                st.session_state["_browser_watchlist_cookie"] = saved_symbols
            except Exception:
                pass
        return
    try:
        PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
        preferences_path = PREFERENCES_FILE
    except PermissionError:
        preferences_path = FALLBACK_PREFERENCES_FILE

    temporary_file = preferences_path.with_suffix(".tmp")
    try:
        with temporary_file.open("w", encoding="utf-8") as preferences_file:
            json.dump(preferences, preferences_file, indent=2)
        temporary_file.replace(preferences_path)
    except PermissionError:
        # The Finder shortcut can be restricted from normal user folders.
        # /tmp remains writable and keeps settings between ordinary app sessions.
        temporary_file = FALLBACK_PREFERENCES_FILE.with_suffix(".tmp")
        try:
            with temporary_file.open("w", encoding="utf-8") as preferences_file:
                json.dump(preferences, preferences_file, indent=2)
            temporary_file.replace(FALLBACK_PREFERENCES_FILE)
        except OSError as exc:
            raise OSError(f"No writable settings location was available: {exc}") from exc


def save_symbol_preferences(symbols_text: str) -> None:
    preferences = load_preferences()
    preferences["symbols"] = symbols_text
    write_preferences(preferences)


def save_email_preference(sec_contact_email: str) -> None:
    preferences = load_preferences()
    preferences["sec_contact_email"] = sec_contact_email
    write_preferences(preferences)


def save_browser_credentials(
    alpaca_api_key: str = "",
    alpaca_api_secret: str = "",
    sec_contact_email: str = "",
) -> None:
    if browser_cookie_manager is None:
        raise OSError("Browser storage is unavailable.")
    expiration = datetime.datetime.now() + datetime.timedelta(days=365)
    values = {
        BROWSER_ALPACA_KEY_COOKIE: alpaca_api_key,
        BROWSER_ALPACA_SECRET_COOKIE: alpaca_api_secret,
        BROWSER_SEC_EMAIL_COOKIE: sec_contact_email,
    }
    for cookie_name, cookie_value in values.items():
        if cookie_value:
            browser_cookie_manager.set(
                cookie_name,
                cookie_value,
                key=f"save_{cookie_name}",
                expires_at=expiration,
                secure=True,
                same_site="strict",
            )


def forget_browser_credentials(include_alpaca: bool = True, include_sec: bool = True) -> None:
    if browser_cookie_manager is None:
        return
    cookie_names = []
    state_keys = []
    if include_alpaca:
        cookie_names.extend((BROWSER_ALPACA_KEY_COOKIE, BROWSER_ALPACA_SECRET_COOKIE))
        state_keys.extend(("alpaca_api_key", "alpaca_api_secret"))
    if include_sec:
        cookie_names.append(BROWSER_SEC_EMAIL_COOKIE)
        state_keys.append("sec_contact_email")
    for cookie_name in cookie_names:
        try:
            browser_cookie_manager.delete(cookie_name, key=f"forget_{cookie_name}")
        except Exception:
            pass
    for state_key in state_keys:
        st.session_state[state_key] = ""


def erase_selected_credentials(selection: str) -> None:
    include_sec = selection in ("SEC email", "Both")
    include_alpaca = selection in ("Alpaca API credentials", "Both")
    forget_browser_credentials(
        include_alpaca=include_alpaca,
        include_sec=include_sec,
    )
    if selection == "Both":
        message = "SEC email and Alpaca credentials were erased from this browser."
    elif selection == "SEC email":
        message = "SEC email was erased from this browser."
    else:
        message = "Alpaca credentials were erased from this browser."
    st.session_state["credentials_erased_message"] = message


def add_symbol_to_watchlist(symbol: str) -> None:
    current_text = st.session_state.get("symbols_text", "")
    current_symbols = [
        item.strip().upper()
        for item in current_text.replace("\n", ",").split(",")
        if item.strip()
    ]
    if symbol not in current_symbols:
        current_symbols.append(symbol)
        updated_text = ", ".join(current_symbols)
        st.session_state["symbols_text"] = updated_text
        try:
            save_symbol_preferences(updated_text)
            st.session_state["watchlist_message"] = (
                f"{symbol} was added for this session."
                if is_streamlit_cloud()
                else f"{symbol} was added and your ticker list was saved."
            )
        except OSError as exc:
            st.session_state["watchlist_message"] = (
                f"{symbol} was added, but could not be saved: {exc}"
            )
    else:
        st.session_state["watchlist_message"] = f"{symbol} is already in your stock symbols."


def toggle_sidebar_stock_search() -> None:
    opening_search = not st.session_state.get("sidebar_stock_search_open", False)
    st.session_state["sidebar_stock_search_open"] = opening_search
    if opening_search:
        st.session_state["floating_stock_lookup"] = None


def select_floating_stock() -> None:
    selected_stock = st.session_state.get("floating_stock_lookup")
    if selected_stock:
        selected_symbol = str(selected_stock).split(" · ", 1)[0].strip()
        add_symbol_to_watchlist(selected_symbol)
        st.session_state["floating_stock_lookup"] = None


def select_filtered_stock(symbol: str) -> None:
    add_symbol_to_watchlist(symbol)
    st.session_state["sidebar_stock_search_open"] = False
    st.session_state["floating_stock_search_query"] = ""


@st.dialog("⚠️ SEC email required")
def show_sec_email_required_dialog() -> None:
    st.warning(
        "Stock-name and ticker searches use the SEC company directory. The SEC requires "
        "a contact email before the app can request that directory."
    )
    st.write("Would you like to enter and save your SEC contact email now?")
    yes_column, cancel_column = st.columns(2)
    with yes_column:
        if st.button(
            "Yes — open Settings",
            type="primary",
            key="open_sec_email_settings",
            use_container_width=True,
        ):
            st.session_state["selected_main_page"] = "Settings"
            st.query_params["page"] = "Settings"
            st.query_params["focus"] = "sec_email"
            st.rerun()
    with cancel_column:
        if st.button("Cancel", key="cancel_sec_email_settings", use_container_width=True):
            st.rerun()


@st.dialog("⚠️ Stock lookup unavailable")
def show_stock_lookup_unavailable_dialog() -> None:
    st.warning(
        "The app could not download the company-and-ticker directory from SEC.gov. "
        "This can happen if the saved email needs attention or the SEC is temporarily unavailable."
    )
    st.markdown(
        """
Try these options:

1. Choose **Try Again** to make a fresh request.
2. If it still fails, choose **Open Settings** and confirm that your SEC contact email is correct and saved.
3. If SEC.gov is temporarily unavailable, wait a few minutes and try again.
        """
    )
    if st.button(
        "Try Again",
        type="primary",
        key="retry_stock_lookup",
        use_container_width=True,
    ):
        get_sec_company_records.clear()
        get_sec_company_directory.clear()
        st.session_state["stock_lookup_warning_shown"] = False
        st.rerun()
    if st.button(
        "Open Settings",
        key="open_stock_lookup_settings",
        use_container_width=True,
    ):
        st.session_state["selected_main_page"] = "Settings"
        st.query_params["page"] = "Settings"
        st.query_params["focus"] = "sec_email"
        st.rerun()
    if st.button(
        "Cancel",
        key="cancel_stock_lookup_warning",
        use_container_width=True,
    ):
        st.rerun()


@st.dialog("⚠️ Alpaca credentials required")
def show_alpaca_credentials_required_dialog() -> None:
    st.warning(
        "The Alpaca Live Market Data page needs both an Alpaca API key and API secret."
    )
    st.write("Would you like to enter and save your Alpaca credentials now?")
    yes_column, cancel_column = st.columns(2)
    with yes_column:
        if st.button(
            "Yes — open Settings",
            type="primary",
            key="open_alpaca_settings",
            use_container_width=True,
        ):
            st.session_state["selected_main_page"] = "Settings"
            st.query_params["page"] = "Settings"
            st.query_params["focus"] = "alpaca"
            st.rerun()
    with cancel_column:
        if st.button(
            "Cancel",
            key="cancel_alpaca_settings",
            use_container_width=True,
        ):
            st.session_state["selected_main_page"] = "My Watchlist"
            st.query_params["page"] = "My Watchlist"
            st.query_params.pop("focus", None)
            st.rerun()


@st.dialog("Erase saved credentials?")
def show_erase_credentials_dialog() -> None:
    st.warning(
        "This removes the selected information from this browser. It does not delete or "
        "change anything in your Alpaca account."
    )
    erase_choice = st.radio(
        "What would you like to erase?",
        ["SEC email", "Alpaca API credentials", "Both"],
        key="erase_credentials_choice",
    )
    confirm_column, cancel_column = st.columns(2)
    with confirm_column:
        st.button(
            "Erase selected",
            type="primary",
            key="confirm_erase_credentials",
            use_container_width=True,
            on_click=erase_selected_credentials,
            args=(erase_choice,),
        )
    with cancel_column:
        if st.button(
            "Cancel",
            key="cancel_erase_credentials",
            use_container_width=True,
        ):
            st.rerun()


def normalize_symbols_text() -> None:
    current_text = st.session_state.get("symbols_text", "")
    normalized = list(dict.fromkeys(
        item.strip().upper()
        for item in current_text.replace("\n", ",").split(",")
        if item.strip()
    ))
    updated_text = ", ".join(normalized)
    st.session_state["symbols_text"] = updated_text
    if not normalized:
        st.session_state["watchlist_message"] = "Keep at least one ticker in the scanner."
        return
    try:
        save_symbol_preferences(updated_text)
        st.session_state["watchlist_message"] = (
            "Ticker symbols updated for this session."
            if is_streamlit_cloud()
            else "Ticker symbols saved."
        )
    except OSError as exc:
        st.session_state["watchlist_message"] = f"Ticker symbols could not be saved: {exc}"


def remove_symbols_from_watchlist(symbols_to_remove: list) -> None:
    current_text = st.session_state.get("symbols_text", "")
    current_symbols = list(dict.fromkeys(
        item.strip().upper()
        for item in current_text.replace("\n", ",").split(",")
        if item.strip()
    ))
    remaining_symbols = [
        symbol for symbol in current_symbols if symbol not in symbols_to_remove
    ]
    if not remaining_symbols:
        st.session_state["watchlist_message"] = "Keep at least one ticker in the scanner."
        return
    updated_text = ", ".join(remaining_symbols)
    st.session_state["symbols_text"] = updated_text
    try:
        save_symbol_preferences(updated_text)
        removed_text = ", ".join(
            symbol for symbol in symbols_to_remove if symbol in current_symbols
        )
        st.session_state["watchlist_message"] = (
            f"Removed for this session: {removed_text}."
            if is_streamlit_cloud()
            else f"Removed and saved: {removed_text}."
        )
    except OSError as exc:
        st.session_state["watchlist_message"] = f"Tickers were removed, but could not be saved: {exc}"


def render_removable_stock_table(
    dataframe: pd.DataFrame,
    editor_key: str,
    column_config: dict = None,
):
    display = dataframe.reset_index(drop=True).copy()
    selection = st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        height=dataframe_height(display),
        column_config=column_config or {},
        key=editor_key,
        on_select="rerun",
        selection_mode="multi-row",
    )
    selected_rows = selection.selection.rows
    selected_symbols = (
        display.iloc[selected_rows]["Symbol"].drop_duplicates().tolist()
        if selected_rows
        else []
    )
    st.button(
        "Remove selected ticker" if len(selected_symbols) <= 1 else "Remove selected tickers",
        key=f"{editor_key}_remove_button",
        disabled=not selected_symbols,
        on_click=remove_symbols_from_watchlist,
        args=(selected_symbols,),
    )
    return selection


def render_stock_selector_table(
    dataframe: pd.DataFrame,
    selector_key: str,
    column_config: dict = None,
) -> str:
    selector_columns = [
        column for column in [
            "Symbol", "Price", "Trend", "Trend score", "Momentum score", "RSI",
            "Volume spike %",
        ]
        if column in dataframe.columns
    ]
    display = dataframe[selector_columns].reset_index(drop=True)
    selection = st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        height=dataframe_height(display),
        column_config=column_config or {},
        key=selector_key,
        on_select="rerun",
        selection_mode="single-cell",
    )
    selected_cells = selection.selection.cells
    selected_symbol = (
        display.iloc[selected_cells[0][0]]["Symbol"]
        if selected_cells
        else display.iloc[0]["Symbol"]
    )
    st.button(
        "Remove selected ticker",
        key=f"{selector_key}_remove_button",
        on_click=remove_symbols_from_watchlist,
        args=([selected_symbol],),
    )
    return selected_symbol


def fetch_json(url: str, user_agent: str) -> dict:
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def get_alpaca_live_quotes(
    symbols: list,
    api_key: str,
    api_secret: str,
    feed: str,
) -> pd.DataFrame:
    """Load the latest Alpaca snapshot for each selected ticker."""
    clean_symbols = [symbol for symbol in symbols if symbol][:30]
    if not clean_symbols:
        return pd.DataFrame()
    url = (
        "https://data.alpaca.markets/v2/stocks/snapshots?symbols="
        f"{quote(','.join(clean_symbols))}&feed={feed}"
    )
    request = Request(
        url,
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=20) as response:
        snapshots = json.load(response)

    rows = []
    for symbol in clean_symbols:
        snapshot = snapshots.get(symbol, {})
        trade = snapshot.get("latestTrade") or {}
        latest_quote = snapshot.get("latestQuote") or {}
        minute_bar = snapshot.get("minuteBar") or {}
        daily_bar = snapshot.get("dailyBar") or {}
        previous_bar = snapshot.get("prevDailyBar") or {}
        last_price = trade.get("p", minute_bar.get("c"))
        previous_close = previous_bar.get("c")
        daily_change = (
            (float(last_price) / float(previous_close) - 1) * 100
            if last_price is not None and previous_close not in (None, 0)
            else None
        )
        rows.append({
            "Symbol": symbol,
            "Last": last_price,
            "Bid": latest_quote.get("bp"),
            "Ask": latest_quote.get("ap"),
            "Change %": daily_change,
            "Day volume": daily_bar.get("v"),
            "Last update": trade.get("t", minute_bar.get("t", "")),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600, show_spinner=False)
def get_recent_congress_trades() -> pd.DataFrame:
    data = fetch_json(CONGRESS_TRADES_URL, "Educational Stock Scanner research")
    trades = data.get("trades", []) if isinstance(data, dict) else []
    rows = []
    for trade in trades:
        ticker = str(trade.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        rows.append({
            "Disclosed": trade.get("disclosed", ""),
            "Trade date": trade.get("tx_date", ""),
            "Member": trade.get("member", ""),
            "Chamber": trade.get("chamber", ""),
            "Ticker": ticker,
            "Action": str(trade.get("trade_type", "")).title(),
            "Amount range": trade.get("amount", ""),
            "Asset": trade.get("asset", ""),
            "Official disclosure": trade.get("link", ""),
        })
    return pd.DataFrame(rows)


def xml_text(element: ET.Element, local_name: str) -> str:
    for child in element.iter():
        if child.tag.split("}")[-1] == local_name:
            return (child.text or "").strip()
    return ""


@st.cache_data(ttl=3600, show_spinner=False)
def get_latest_13f_holdings(
    manager_name: str,
    sec_contact_email: str,
) -> tuple:
    cik = TRACKED_13F_MANAGERS[manager_name]
    user_agent = f"Educational Stock Scanner {sec_contact_email}"
    submissions = fetch_json(
        SEC_SUBMISSIONS_URL.format(cik=cik),
        user_agent,
    )
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_index = next(
        (index for index, form in enumerate(forms) if form in {"13F-HR", "13F-HR/A"}),
        None,
    )
    if filing_index is None:
        return pd.DataFrame(), {}

    accession = recent["accessionNumber"][filing_index]
    accession_compact = accession.replace("-", "")
    cik_compact = str(int(cik))
    primary_document = recent["primaryDocument"][filing_index]
    filing_date = recent.get("filingDate", [""])[filing_index]
    report_date = recent.get("reportDate", [""])[filing_index]
    directory_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_compact}/"
        f"{accession_compact}/index.json"
    )
    directory = fetch_json(directory_url, user_agent)
    items = directory.get("directory", {}).get("item", [])
    xml_names = [
        item.get("name", "")
        for item in items
        if str(item.get("name", "")).lower().endswith(".xml")
        and item.get("name") != primary_document
    ]
    information_name = next(
        (
            name for name in xml_names
            if "info" in name.lower() or "table" in name.lower()
        ),
        xml_names[0] if xml_names else None,
    )
    if not information_name:
        return pd.DataFrame(), {}

    information_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_compact}/"
        f"{accession_compact}/{information_name}"
    )
    request = Request(information_url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())

    holdings = []
    for element in root.iter():
        if element.tag.split("}")[-1] != "infoTable":
            continue
        reported_value = pd.to_numeric(xml_text(element, "value"), errors="coerce")
        shares = pd.to_numeric(xml_text(element, "sshPrnamt"), errors="coerce")
        holdings.append({
            "Issuer": xml_text(element, "nameOfIssuer"),
            "Security class": xml_text(element, "titleOfClass"),
            "CUSIP": xml_text(element, "cusip"),
            "Reported value": reported_value,
            "Shares/principal": shares,
            "Put/Call": xml_text(element, "putCall"),
        })
    holdings_df = pd.DataFrame(holdings)
    if not holdings_df.empty:
        holdings_df = holdings_df.sort_values("Reported value", ascending=False)
    filing_link = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_compact}/"
        f"{accession_compact}/{primary_document}"
    )
    metadata = {
        "Filed": filing_date,
        "Report period": report_date,
        "Filing": filing_link,
    }
    return holdings_df, metadata


@st.cache_data(ttl=86400, show_spinner=False)
def get_sec_company_records(contact_email: str) -> dict:
    return fetch_json(
        SEC_TICKERS_URL,
        f"Educational Stock Scanner {contact_email}",
    )


@st.cache_data(ttl=86400, show_spinner=False)
def get_sec_ticker_map(contact_email: str) -> dict:
    data = get_sec_company_records(contact_email)
    return {
        company["ticker"].upper(): str(company["cik_str"]).zfill(10)
        for company in data.values()
    }


@st.cache_data(ttl=86400, show_spinner=False)
def get_sec_company_directory(contact_email: str) -> pd.DataFrame:
    data = get_sec_company_records(contact_email)
    return pd.DataFrame([
        {
            "Company": company["title"],
            "Ticker": company["ticker"].upper(),
        }
        for company in data.values()
    ]).sort_values(["Company", "Ticker"], ignore_index=True)


@st.cache_data(ttl=900, show_spinner=False)
def get_sec_submission_data(symbol: str, contact_email: str) -> dict:
    ticker_map = get_sec_ticker_map(contact_email)
    cik = ticker_map.get(symbol.upper())
    if not cik:
        return {}

    data = fetch_json(
        SEC_SUBMISSIONS_URL.format(cik=cik),
        f"Educational Stock Scanner {contact_email}",
    )
    data["_scanner_cik"] = cik
    return data


@st.cache_data(ttl=900, show_spinner=False)
def get_sec_filings(symbol: str, contact_email: str) -> pd.DataFrame:
    data = get_sec_submission_data(symbol, contact_email)
    if not data:
        return pd.DataFrame()

    cik = data["_scanner_cik"]
    recent = data.get("filings", {}).get("recent", {})
    filings = []
    row_count = len(recent.get("form", []))

    for index in range(row_count):
        form = recent["form"][index]
        if form not in {
            "4", "4/A", "8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A",
            "F-1", "F-1/A", "6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
        }:
            continue

        accession = recent["accessionNumber"][index]
        document = recent["primaryDocument"][index]
        items = recent.get("items", [""] * row_count)[index] or ""
        item_numbers = {item.strip() for item in items.split(",") if item.strip()}
        warnings = [description for item, description in SEC_RISK_ITEMS.items() if item in item_numbers]
        filings.append({
            "Filed": recent["filingDate"][index],
            "Form": form,
            "Items": items or "—",
            "Review flag": "; ".join(warnings) if warnings else "",
            "Filing": SEC_ARCHIVES_URL.format(
                cik=int(cik),
                accession=accession.replace("-", ""),
                document=document,
            ),
        })

        if len(filings) == 30:
            break

    return pd.DataFrame(filings)


@st.cache_data(ttl=900, show_spinner=False)
def get_estimated_future_filings(symbol: str, contact_email: str) -> pd.DataFrame:
    data = get_sec_submission_data(symbol, contact_email)
    if not data:
        return pd.DataFrame()

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    today = pd.Timestamp.now().normalize()
    estimates = []

    for index, form in enumerate(forms):
        if form not in {"10-Q", "10-K"}:
            continue
        if index >= len(filing_dates) or index >= len(report_dates):
            continue

        historical_filing = pd.to_datetime(filing_dates[index], errors="coerce")
        historical_period = pd.to_datetime(report_dates[index], errors="coerce")
        if pd.isna(historical_filing) or pd.isna(historical_period):
            continue

        estimated_date = historical_filing + pd.DateOffset(years=1)
        estimated_period = historical_period + pd.DateOffset(years=1)
        if estimated_date < today or estimated_date > today + pd.Timedelta(days=370):
            continue

        estimates.append({
            "Expected form": form,
            "Estimated period end": estimated_period.strftime("%Y-%m-%d"),
            "Estimated filing window": (
                f"{(estimated_date - pd.Timedelta(days=7)).strftime('%b %d, %Y')} – "
                f"{(estimated_date + pd.Timedelta(days=7)).strftime('%b %d, %Y')}"
            ),
            "Estimate based on": f"Prior filing on {historical_filing.strftime('%b %d, %Y')}",
            "_estimated_date": estimated_date,
        })

    if not estimates:
        return pd.DataFrame()

    estimates_df = pd.DataFrame(estimates).sort_values("_estimated_date")
    estimates_df = estimates_df.drop_duplicates(
        subset=["Expected form", "Estimated period end"],
        keep="first",
    ).head(4)
    return estimates_df.drop(columns="_estimated_date")


@st.cache_data(ttl=3600, show_spinner=False)
def get_company_health(symbol: str) -> dict:
    return yf.Ticker(symbol).info


@st.cache_data(ttl=86400, show_spinner=False)
def get_fda_approval_matches(company_name: str) -> pd.DataFrame:
    sponsor_name = company_name
    for suffix in [
        ", Inc.", " Inc.", ", Inc", " Inc", " Corporation", " Corp.", " Corp",
        " plc", " PLC", " Ltd.", " Ltd", " Limited", " Holdings",
    ]:
        sponsor_name = sponsor_name.replace(suffix, "")
    sponsor_name = sponsor_name.strip()
    search = quote(f"sponsor_name:{sponsor_name.upper()}*")
    url = f"{OPENFDA_DRUGS_URL}?search={search}&limit=100"
    data = fetch_json(url, "Educational Stock Scanner openFDA research")
    approvals = []
    for application in data.get("results", []):
        products = application.get("products", [])
        product_names = sorted({
            product.get("brand_name", "")
            for product in products
            if product.get("brand_name")
        })
        product_text = ", ".join(name for name in product_names if name) or "Product not listed"
        for submission in application.get("submissions", []):
            if submission.get("submission_status") != "AP":
                continue
            status_date = submission.get("submission_status_date", "")
            approvals.append({
                "FDA decision date": status_date,
                "Product": product_text,
                "Application": application.get("application_number", ""),
                "Submission": submission.get("submission_type", ""),
                "Sponsor matched": application.get("sponsor_name", sponsor_name),
            })
    if not approvals:
        return pd.DataFrame()
    approvals_df = pd.DataFrame(approvals).drop_duplicates()
    approvals_df["_date"] = pd.to_datetime(
        approvals_df["FDA decision date"],
        format="%Y%m%d",
        errors="coerce",
    )
    return approvals_df.sort_values("_date", ascending=False).drop(columns="_date").head(20)


def safe_percent(value):
    return None if value is None else value * 100


def format_research_value(value, kind: str = "number") -> str:
    if value is None or pd.isna(value):
        return "Not available"
    if kind == "percent":
        return f"{value:,.1f}%"
    if kind == "ratio":
        return f"{value:,.2f}"
    if kind == "money":
        absolute = abs(value)
        if absolute >= 1_000_000_000:
            return f"${value / 1_000_000_000:,.2f}B"
        if absolute >= 1_000_000:
            return f"${value / 1_000_000:,.2f}M"
        return f"${value:,.0f}"
    return f"{value:,.2f}"


def dataframe_height(dataframe: pd.DataFrame, extra_rows: int = 0) -> int:
    """Fit the header, every data row, and the bottom scrollbar."""
    return 36 * (len(dataframe) + 1 + extra_rows) + 27


def trend_label(score: int) -> str:
    return {
        4: "Strong uptrend",
        3: "Uptrend",
        2: "Mixed",
        1: "Downtrend",
        0: "Strong downtrend",
    }[score]


def score_purchase_research(stock: pd.Series, company: dict, filings: pd.DataFrame):
    checks = []

    def add_check(category: str, check: str, passed: bool, points: int, evidence: str):
        checks.append({
            "Category": category,
            "Check": check,
            "Result": "Pass" if passed else "Needs review",
            "Points": points if passed else 0,
            "Available": points,
            "Evidence": evidence,
        })

    add_check("Trend", "Price above SMA20", bool(stock["Above SMA20"]), 7, f"${stock['Price']:.2f}")
    add_check("Trend", "SMA20 above SMA50", bool(stock["Bullish trend"]), 7, f"${stock['SMA20']:.2f} vs ${stock['SMA50']:.2f}")
    add_check("Trend", "Price above SMA200", bool(stock["Above SMA200"]), 6, format_research_value(stock["SMA200"], "money"))
    add_check("Trend", "MACD above signal", bool(stock["MACD bullish"]), 5, f"{stock['MACD']:.2f} vs {stock['MACD signal']:.2f}")
    add_check("Momentum", "Meets volume-spike setting", bool(stock["Strong volume"]), 10, f"{stock['Volume spike %']:+.1f}%")
    add_check("Momentum", "Near 20-day high", bool(stock["Near 20-day high"]), 10, f"Momentum score {int(stock['Momentum score'])}/4")

    financial_checks = [
        ("Positive revenue growth", company.get("revenueGrowth"), lambda value: value > 0, 5, "percent"),
        ("Positive profit margin", company.get("profitMargins"), lambda value: value > 0, 5, "percent"),
        ("Positive operating margin", company.get("operatingMargins"), lambda value: value > 0, 5, "percent"),
        ("Positive free cash flow", company.get("freeCashflow"), lambda value: value > 0, 5, "money"),
        ("Current ratio at least 1", company.get("currentRatio"), lambda value: value >= 1, 5, "ratio"),
        ("Debt-to-equity below 100", company.get("debtToEquity"), lambda value: value < 100, 5, "ratio"),
    ]
    for check, value, rule, points, kind in financial_checks:
        passed = value is not None and not pd.isna(value) and rule(value)
        display_value = safe_percent(value) if kind == "percent" and value is not None else value
        add_check("Financial health", check, passed, points, format_research_value(display_value, kind))

    valuation_checks = [
        ("Positive P/E no higher than 35", company.get("trailingPE"), lambda value: 0 < value <= 35),
        ("Positive forward P/E no higher than 35", company.get("forwardPE"), lambda value: 0 < value <= 35),
        ("Price-to-sales no higher than 5", company.get("priceToSalesTrailing12Months"), lambda value: 0 < value <= 5),
    ]
    for check, value, rule in valuation_checks:
        passed = value is not None and not pd.isna(value) and rule(value)
        add_check("Valuation", check, passed, 5, format_research_value(value, "ratio"))

    has_sec_data = not filings.empty
    has_sec_risk_flag = has_sec_data and filings["Review flag"].fillna("").astype(bool).any()
    add_check(
        "SEC filing review",
        "No selected SEC risk flags in recent filings",
        has_sec_data and not has_sec_risk_flag,
        10,
        (
            "Flag found—read the filing"
            if has_sec_risk_flag
            else "No selected flags found"
            if has_sec_data
            else "SEC data unavailable"
        ),
    )

    details = pd.DataFrame(checks)
    total = int(details["Points"].sum())
    grade = "A" if total >= 80 else "B" if total >= 70 else "C" if total >= 60 else "D" if total >= 50 else "F"
    profile = "Stronger" if total >= 70 else "Mixed" if total >= 50 else "Weaker"
    return total, grade, profile, details


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    change = close.diff()
    gains = change.clip(lower=0)
    losses = -change.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    average_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + relative_strength))


@st.cache_data(ttl=900, show_spinner=False)
def get_history(symbol: str) -> pd.DataFrame:
    data = yf.download(
        symbol,
        period="3y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if data.empty:
        return data

    # yfinance sometimes returns two-level column names, even for one ticker.
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.dropna(subset=["Close", "Volume"]).copy()
    data["SMA20"] = data["Close"].rolling(20).mean()
    data["SMA50"] = data["Close"].rolling(50).mean()
    data["SMA200"] = data["Close"].rolling(200).mean()
    ema12 = data["Close"].ewm(span=12, adjust=False).mean()
    ema26 = data["Close"].ewm(span=26, adjust=False).mean()
    data["MACD"] = ema12 - ema26
    data["MACDSignal"] = data["MACD"].ewm(span=9, adjust=False).mean()
    data["MACDHistogram"] = data["MACD"] - data["MACDSignal"]
    data["RSI14"] = calculate_rsi(data["Close"])
    data["AvgVolume20"] = data["Volume"].rolling(20).mean()
    data["High20"] = data["High"].rolling(20).max()
    return data


@st.cache_data(ttl=3600, show_spinner=False)
def get_long_term_history(symbol: str) -> pd.DataFrame:
    data = yf.download(
        symbol,
        period="max",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.dropna(subset=["Close"]).copy()
    data["SMA20"] = data["Close"].rolling(20).mean()
    data["SMA50"] = data["Close"].rolling(50).mean()
    data["SMA200"] = data["Close"].rolling(200).mean()
    ema12 = data["Close"].ewm(span=12, adjust=False).mean()
    ema26 = data["Close"].ewm(span=26, adjust=False).mean()
    data["MACD"] = ema12 - ema26
    data["MACDSignal"] = data["MACD"].ewm(span=9, adjust=False).mean()
    data["MACDHistogram"] = data["MACD"] - data["MACDSignal"]
    data["AvgVolume20"] = data["Volume"].rolling(20).mean()
    data["RVOL"] = data["Volume"] / data["AvgVolume20"]
    return data


@st.cache_data(ttl=300, show_spinner=False)
def get_premarket_indicators(symbol: str) -> dict:
    data = yf.download(
        symbol,
        period="5d",
        interval="5m",
        prepost=True,
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if data.empty:
        return {}

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.dropna(subset=["Close"]).copy()
    if data.empty:
        return {}

    if data.index.tz is None:
        data.index = data.index.tz_localize("America/New_York")
    else:
        data.index = data.index.tz_convert("America/New_York")

    minutes_after_midnight = data.index.hour * 60 + data.index.minute
    premarket_rows = data[
        (minutes_after_midnight >= 4 * 60)
        & (minutes_after_midnight < 9 * 60 + 30)
    ]
    if premarket_rows.empty:
        return {}

    session_date = premarket_rows.index[-1].date()
    session_rows = premarket_rows[premarket_rows.index.date == session_date]

    regular_rows = data[
        (minutes_after_midnight >= 9 * 60 + 30)
        & (minutes_after_midnight <= 16 * 60)
        & (data.index.date < session_date)
    ]
    if regular_rows.empty:
        return {}

    previous_close = regular_rows.iloc[-1]["Close"]
    premarket_price = session_rows.iloc[-1]["Close"]
    return {
        "Symbol": symbol,
        "Premarket price": premarket_price,
        "Gap %": (premarket_price / previous_close - 1) * 100,
        "Premarket high": session_rows["High"].max(),
        "Premarket low": session_rows["Low"].min(),
        "Premarket volume": session_rows["Volume"].fillna(0).sum(),
        "Previous close": previous_close,
        "Latest premarket quote": session_rows.index[-1].strftime("%Y-%m-%d %I:%M %p ET"),
    }


def analyze_history(
    symbol: str,
    history: pd.DataFrame,
    minimum_volume_spike_percent: int,
    high_distance_percent: int,
    maximum_rsi: int,
    require_up_day: bool,
):
    if len(history) < 51:
        return None

    latest = history.iloc[-1]
    previous = history.iloc[-2]
    relative_volume = latest["Volume"] / latest["AvgVolume20"]
    volume_spike_percent = (relative_volume - 1) * 100

    above_sma20 = latest["Close"] > latest["SMA20"]
    bullish_trend = latest["SMA20"] > latest["SMA50"]
    above_sma200 = latest["Close"] > latest["SMA200"]
    macd_bullish = latest["MACD"] > latest["MACDSignal"]
    overall_trend_score = sum([above_sma20, bullish_trend, above_sma200, macd_bullish])
    strong_volume = volume_spike_percent >= minimum_volume_spike_percent
    near_high = latest["Close"] >= latest["High20"] * (1 - high_distance_percent / 100)
    resistance_20d = history["High"].iloc[-21:-1].max()
    breaking_resistance = (
        latest["Close"] > resistance_20d
        and previous["Close"] <= resistance_20d
    )
    momentum_score = sum([above_sma20, bullish_trend, strong_volume, near_high])
    momentum = momentum_score == 4
    low_rsi = latest["RSI14"] <= maximum_rsi
    up_day = latest["Close"] > previous["Close"]
    oversold_score = sum([low_rsi, up_day])
    oversold = low_rsi and (up_day or not require_up_day)

    result = {
        "Symbol": symbol,
        "Price": latest["Close"],
        "Daily %": (latest["Close"] / previous["Close"] - 1) * 100,
        "RSI": latest["RSI14"],
        "Relative volume": relative_volume,
        "Volume spike %": volume_spike_percent,
        "SMA20": latest["SMA20"],
        "SMA50": latest["SMA50"],
        "SMA200": latest["SMA200"],
        "MACD": latest["MACD"],
        "MACD signal": latest["MACDSignal"],
        "Above SMA200": above_sma200,
        "MACD bullish": macd_bullish,
        "Trend score": overall_trend_score,
        "Trend": trend_label(overall_trend_score),
        "Momentum score": momentum_score,
        "Above SMA20": above_sma20,
        "Bullish trend": bullish_trend,
        "Strong volume": strong_volume,
        "Near 20-day high": near_high,
        "20-day resistance": resistance_20d,
        "Breaking resistance": breaking_resistance,
        "Momentum": momentum,
        "Oversold score": oversold_score,
        "Below RSI limit": low_rsi,
        "Price turned up": up_day,
        "Oversold reversal": oversold,
    }
    return result


def scan_symbol(
    symbol: str,
    minimum_volume_spike_percent: int,
    high_distance_percent: int,
    maximum_rsi: int,
    require_up_day: bool,
):
    history = get_history(symbol)
    result = analyze_history(
        symbol,
        history,
        minimum_volume_spike_percent,
        high_distance_percent,
        maximum_rsi,
        require_up_day,
    )
    return result, history


@st.cache_data(ttl=900, show_spinner=False)
def get_marketwide_candidates(
) -> tuple:
    """Merge candidates from Yahoo, CNN Markets, and FINVIZ."""
    symbols = []

    def add_symbols(new_symbols):
        for raw_symbol in new_symbols:
            symbol = str(raw_symbol).strip().upper().replace(".", "-")
            if re.fullmatch(r"[A-Z][A-Z0-9-]{0,9}", symbol) and symbol not in symbols:
                symbols.append(symbol)

    try:
        query = yf.EquityQuery("and", [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("gt", ["dayvolume", 100_000]),
            yf.EquityQuery("gt", ["intradaymarketcap", 100_000_000]),
        ])
        response = yf.screen(
            query,
            size=250,
            sortField="dayvolume",
            sortAsc=False,
        )
        add_symbols(quote_data.get("symbol", "") for quote_data in response.get("quotes", []))
    except Exception:
        pass

    headers = {
        "User-Agent": "Educational Stock Scanner/1.0",
        "Accept": "text/html,application/xhtml+xml",
    }

    def load_html(url: str) -> str:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="ignore")

    try:
        cnn_html = load_html("https://www.cnn.com/markets")
        add_symbols(re.findall(r"/markets/stocks/([A-Z][A-Z0-9.-]{0,9})", cnn_html))
        add_symbols(re.findall(r'"symbol"\s*:\s*"([A-Z][A-Z0-9.-]{0,9})"', cnn_html))
    except Exception:
        pass

    try:
        finviz_symbols = []
        for first_row in (1, 21, 41, 61, 81):
            finviz_url = (
                "https://finviz.com/screener.ashx?v=111"
                "&f=cap_smallover,sh_avgvol_o100"
                f"&ft=4&o=-volume&r={first_row}"
            )
            finviz_html = load_html(finviz_url)
            finviz_symbols.extend(
                re.findall(r"quote\.ashx\?t=([A-Z][A-Z0-9.-]{0,9})", finviz_html)
            )
        add_symbols(finviz_symbols)
    except Exception:
        pass

    return tuple(symbols)


@st.cache_data(ttl=900, show_spinner=False)
def scan_marketwide_candidates(
    symbols: tuple,
    minimum_volume_spike_percent: int,
    high_distance_percent: int,
    maximum_rsi: int,
    require_up_day: bool,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    rows = []
    batch_size = 100
    for batch_start in range(0, len(symbols), batch_size):
        batch_symbols = symbols[batch_start:batch_start + batch_size]
        try:
            downloaded = yf.download(
                list(batch_symbols),
                period="1y",
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception:
            continue

        for symbol in batch_symbols:
            try:
                if isinstance(downloaded.columns, pd.MultiIndex):
                    if symbol in downloaded.columns.get_level_values(0):
                        history = downloaded[symbol].copy()
                    elif symbol in downloaded.columns.get_level_values(1):
                        history = downloaded.xs(symbol, axis=1, level=1).copy()
                    else:
                        continue
                else:
                    history = downloaded.copy()

                history = history.dropna(subset=["Close", "Volume"])
                if history.empty:
                    continue
                history["SMA20"] = history["Close"].rolling(20).mean()
                history["SMA50"] = history["Close"].rolling(50).mean()
                history["SMA200"] = history["Close"].rolling(200).mean()
                ema12 = history["Close"].ewm(span=12, adjust=False).mean()
                ema26 = history["Close"].ewm(span=26, adjust=False).mean()
                history["MACD"] = ema12 - ema26
                history["MACDSignal"] = history["MACD"].ewm(span=9, adjust=False).mean()
                history["RSI14"] = calculate_rsi(history["Close"])
                history["AvgVolume20"] = history["Volume"].rolling(20).mean()
                history["High20"] = history["High"].rolling(20).max()
                result = analyze_history(
                    symbol,
                    history,
                    minimum_volume_spike_percent,
                    high_distance_percent,
                    maximum_rsi,
                    require_up_day,
                )
                if result is not None:
                    rows.append(result)
            except (KeyError, TypeError, ValueError):
                continue
    return pd.DataFrame(rows)


@st.cache_data(ttl=1800, show_spinner=False)
def grade_marketwide_shortlist(market_results: pd.DataFrame) -> pd.DataFrame:
    if market_results.empty:
        return pd.DataFrame()
    shortlist = market_results.sort_values(
        ["Trend score", "Momentum score", "Relative volume"],
        ascending=[False, False, False],
    ).head(40)
    graded_rows = []
    for _, stock in shortlist.iterrows():
        try:
            company = get_company_health(stock["Symbol"])
            score, grade, profile, _ = score_purchase_research(
                stock,
                company,
                pd.DataFrame(),
            )
            graded_rows.append({
                "Symbol": stock["Symbol"],
                "Company": (
                    company.get("longName")
                    or company.get("shortName")
                    or stock["Symbol"]
                ),
                "Score": score,
                "Grade": grade,
                "Profile": profile,
                "Price": stock["Price"],
                "Trend score": int(stock["Trend score"]),
                "Trend": stock["Trend"],
                "Momentum score": int(stock["Momentum score"]),
                "Momentum match": bool(stock["Momentum"]),
                "RSI": stock["RSI"],
                "Relative volume": stock["Relative volume"],
                "Volume spike %": stock["Volume spike %"],
            })
        except Exception:
            continue
    if not graded_rows:
        return pd.DataFrame()
    return pd.DataFrame(graded_rows).sort_values(
        ["Score", "Trend score", "Momentum score", "Relative volume"],
        ascending=[False, False, False, False],
    )


def render_live_combined_chart(
    figure: go.Figure,
    symbol: str,
    start_year: int,
    end_year: int,
    height: int = 1040,
) -> None:
    """Render Plotly with a compact client-side date-only range control."""
    chart_id = "live-stock-chart-" + "".join(
        character if character.isalnum() else "-" for character in symbol.lower()
    )
    control_id = f"{chart_id}-date-control"
    start_input_id = f"{chart_id}-start"
    end_input_id = f"{chart_id}-end"
    start_output_id = f"{chart_id}-start-value"
    end_output_id = f"{chart_id}-end-value"
    selected_track_id = f"{chart_id}-selected-track"
    control_html = f"""
    <style>
    #{control_id} {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #55595e;
        padding: 4px 42px 12px;
    }}
    #{control_id} .date-labels {{
        display: flex;
        justify-content: space-between;
        margin-bottom: 5px;
        font-size: 13px;
        font-weight: 500;
    }}
    #{control_id} .date-track {{
        position: relative;
        height: 18px;
    }}
    #{control_id} .date-track::before,
    #{selected_track_id} {{
        content: "";
        position: absolute;
        left: 0;
        right: 0;
        top: 8px;
        height: 4px;
        border-radius: 999px;
        background: #c5c8cb;
    }}
    #{selected_track_id} {{
        right: auto;
        background: #777c82;
    }}
    #{control_id} input[type="range"] {{
        position: absolute;
        inset: 0;
        width: 100%;
        height: 18px;
        margin: 0;
        appearance: none;
        -webkit-appearance: none;
        background: transparent;
        pointer-events: none;
    }}
    #{control_id} input[type="range"]::-webkit-slider-thumb {{
        appearance: none;
        -webkit-appearance: none;
        width: 16px;
        height: 16px;
        border-radius: 50%;
        border: 2px solid #f3f4f5;
        background: #777c82;
        box-shadow: 0 0 0 1px #777c82;
        pointer-events: auto;
        cursor: grab;
    }}
    #{control_id} input[type="range"]::-moz-range-thumb {{
        width: 14px;
        height: 14px;
        border-radius: 50%;
        border: 2px solid #f3f4f5;
        background: #777c82;
        pointer-events: auto;
        cursor: grab;
    }}
    </style>
    <div id="{control_id}" aria-label="Chart date range">
        <div class="date-labels">
            <span>Start: <output id="{start_output_id}">{start_year}</output></span>
            <span>End: <output id="{end_output_id}">{end_year}</output></span>
        </div>
        <div class="date-track">
            <div id="{selected_track_id}"></div>
            <input id="{start_input_id}" type="range" min="{start_year}" max="{end_year}"
                value="{start_year}" step="1" aria-label="Start year">
            <input id="{end_input_id}" type="range" min="{start_year}" max="{end_year}"
                value="{end_year}" step="1" aria-label="End year">
        </div>
    </div>
    """
    chart_html = pio.to_html(
        figure,
        full_html=False,
        include_plotlyjs="cdn",
        div_id=chart_id,
        config={
            "responsive": True,
            "scrollZoom": False,
            "displayModeBar": False,
            "displaylogo": False,
        },
    )
    symbol_json = json.dumps(symbol)
    live_title_script = f"""
    <script>
    (() => {{
        const chart = document.getElementById({json.dumps(chart_id)});
        const symbol = {symbol_json};
        const startInput = document.getElementById({json.dumps(start_input_id)});
        const endInput = document.getElementById({json.dumps(end_input_id)});
        const startOutput = document.getElementById({json.dumps(start_output_id)});
        const endOutput = document.getElementById({json.dumps(end_output_id)});
        const selectedTrack = document.getElementById({json.dumps(selected_track_id)});
        if (!chart || !startInput || !endInput) return;

        const updateChart = (changedInput) => {{
            let start = Number(startInput.value);
            let end = Number(endInput.value);
            if (start > end) {{
                if (changedInput === startInput) end = start;
                else start = end;
            }}
            startInput.value = String(start);
            endInput.value = String(end);
            startOutput.textContent = String(start);
            endOutput.textContent = String(end);

            const totalYears = {end_year - start_year};
            const left = ((start - {start_year}) / totalYears) * 100;
            const right = (({end_year} - end) / totalYears) * 100;
            selectedTrack.style.left = `${{left}}%`;
            selectedTrack.style.right = `${{right}}%`;
            selectedTrack.style.width = 'auto';

            const dateRange = [`${{start}}-01-01`, `${{end}}-12-31`];
            Plotly.relayout(chart, {{
                'xaxis.range': dateRange,
                'xaxis2.range': dateRange,
                'xaxis3.range': dateRange,
                'title.text': `${{symbol}}: ${{start}}–${{end}}`
            }});
        }};
        startInput.addEventListener('input', () => updateChart(startInput));
        endInput.addEventListener('input', () => updateChart(endInput));
        updateChart(startInput);
    }})();
    </script>
    """
    components.html(control_html + chart_html + live_title_script, height=height, scrolling=False)


st.set_page_config(page_title="Stock Scanner", page_icon="📈", layout="wide")
if is_streamlit_cloud() and stx is not None:
    browser_cookie_manager = stx.CookieManager(key="stock_scanner_cookie_manager")
main_pages = [
    "My Watchlist",
    "Alpaca Live Market Data",
    "Catalysts",
    "Premarket",
    "Trends",
    "Momentum",
    "Oversold Reversals",
    "Purchase Grade",
    "Autopilot Research",
    "Company Research",
    "Top 20",
    "Settings",
]
if "selected_main_page" not in st.session_state:
    st.session_state["selected_main_page"] = main_pages[0]
if st.session_state["selected_main_page"] not in main_pages:
    st.session_state["selected_main_page"] = main_pages[0]
requested_main_page = st.query_params.get("page")
if requested_main_page in main_pages:
    st.session_state["selected_main_page"] = requested_main_page

with st.sidebar.container(key="native_watchlist_navigation"):
    st.markdown(
        '<a class="scanner-home-button" target="_self" '
        'href="?page=My%20Watchlist">⌂ My Watchlist</a>',
        unsafe_allow_html=True,
    )

with st.container(key="native_hamburger_navigation"):
    with st.popover("☰", help="Open navigation menu", use_container_width=True):
        with st.container(key="hamburger_menu_links"):
            for page_name in main_pages:
                link_label = (
                    f"● {page_name}"
                    if page_name == st.session_state["selected_main_page"]
                    else page_name
                )
                st.markdown(
                    f'<a class="scanner-menu-link" target="_self" '
                    f'href="?page={quote(page_name)}">{link_label}</a>',
                    unsafe_allow_html=True,
                )

st.markdown(
    f'<div class="scanner-main-title">{st.session_state["selected_main_page"]}</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="scanner-main-subtitle">Educational scanner using delayed/public '
    'market data — not financial advice.</div>',
    unsafe_allow_html=True,
)
if (
    st.session_state["selected_main_page"] == "Settings"
    and is_streamlit_cloud()
):
    with st.container(key="header_erase_credentials"):
        _, erase_credentials_column, _ = st.columns([1, 1.2, 1])
        with erase_credentials_column:
            if st.button(
                "Erase Credentials",
                key="erase_saved_credentials",
                use_container_width=True,
            ):
                show_erase_credentials_dialog()
        erased_message = st.session_state.pop("credentials_erased_message", "")
        if erased_message:
            st.success(erased_message)

saved_preferences = load_preferences()
cloud_deployment = is_streamlit_cloud()
saved_browser_symbols = saved_preferences.get("symbols", "")
if (
    "symbols_text" not in st.session_state
    or (
        cloud_deployment
        and saved_browser_symbols
        and not st.session_state.get("_browser_watchlist_loaded", False)
    )
):
    st.session_state["symbols_text"] = saved_preferences.get(
        "symbols",
        ", ".join(DEFAULT_SYMBOLS),
    )
if cloud_deployment and saved_browser_symbols:
    st.session_state["_browser_watchlist_loaded"] = True
saved_browser_alpaca_key = saved_preferences.get("alpaca_api_key", "")
saved_browser_alpaca_secret = saved_preferences.get("alpaca_api_secret", "")
saved_browser_sec_email = saved_preferences.get("sec_contact_email", "")
browser_credentials_available = bool(
    saved_browser_alpaca_key
    or saved_browser_alpaca_secret
    or saved_browser_sec_email
)
load_browser_credentials = bool(
    cloud_deployment
    and browser_credentials_available
    and not st.session_state.get("_browser_credentials_loaded", False)
)
if "sec_contact_email" not in st.session_state or load_browser_credentials:
    st.session_state["sec_contact_email"] = saved_preferences.get("sec_contact_email", "")
if "alpaca_api_key" not in st.session_state or load_browser_credentials:
    st.session_state["alpaca_api_key"] = saved_preferences.get("alpaca_api_key", "")
if "alpaca_api_secret" not in st.session_state or load_browser_credentials:
    st.session_state["alpaca_api_secret"] = saved_preferences.get("alpaca_api_secret", "")
if load_browser_credentials:
    st.session_state["_browser_credentials_loaded"] = True

symbols_text = st.sidebar.text_area(
    "Symbols (separated by commas)",
    height=130,
    key="symbols_text",
    on_change=normalize_symbols_text,
)
symbols = list(dict.fromkeys(
    symbol.strip().upper()
    for symbol in symbols_text.replace("\n", ",").split(",")
    if symbol.strip()
))
normalized_symbols_text = ", ".join(symbols)
if symbols and normalized_symbols_text != saved_preferences.get("symbols", ""):
    try:
        save_symbol_preferences(normalized_symbols_text)
    except OSError as exc:
        st.sidebar.error(f"Ticker symbols could not be saved: {exc}")
if "watchlist_message" in st.session_state:
    st.toast(st.session_state.pop("watchlist_message"))

alpaca_key = st.session_state.get("alpaca_api_key", "").strip()
alpaca_secret = st.session_state.get("alpaca_api_secret", "").strip()
alpaca_feed_label = st.session_state.get("alpaca_feed", "IEX — free, one exchange")
alpaca_feed = "sip" if alpaca_feed_label.startswith("SIP") else "iex"
alpaca_credentials_missing = not alpaca_key or not alpaca_secret
sec_contact_email = st.session_state.get("sec_contact_email", "").strip()

with st.container(key="floating_stock_search"):
    if not sec_contact_email or "@" not in sec_contact_email:
        if st.button(
            "Search stocks…",
            icon=":material/search:",
            key="stock_search_requires_email",
            use_container_width=True,
        ):
            show_sec_email_required_dialog()
    else:
        try:
            sidebar_directory = get_sec_company_directory(sec_contact_email)
            sidebar_directory = sidebar_directory.drop_duplicates("Ticker")
            sidebar_company_names = dict(
                zip(sidebar_directory["Ticker"], sidebar_directory["Company"])
            )
            sidebar_search_options = [
                {
                    "ticker": ticker,
                    "label": f"{ticker} · {sidebar_company_names.get(ticker, '')}",
                    "search": f"{ticker} {sidebar_company_names.get(ticker, '')}",
                }
                for ticker in sidebar_directory["Ticker"].tolist()
            ]
            search_version = st.session_state.get("live_stock_search_version", 0)
            selected_search_symbol = live_stock_search_component(
                options=sidebar_search_options,
                placeholder="Search stocks…",
                key=f"live_stock_search_{search_version}",
                default=None,
            )
            st.session_state["stock_lookup_warning_shown"] = False
            if selected_search_symbol:
                add_symbol_to_watchlist(str(selected_search_symbol))
                st.session_state["live_stock_search_version"] = search_version + 1
                st.rerun()
        except Exception:
            open_lookup_warning = st.button(
                "Stock lookup unavailable",
                icon=":material/search_off:",
                key="stock_search_unavailable",
                use_container_width=True,
            )
            if open_lookup_warning or (
                not st.session_state.get("stock_lookup_warning_shown", False)
                and not (
                    st.session_state["selected_main_page"] == "Alpaca Live Market Data"
                    and alpaca_credentials_missing
                )
            ):
                st.session_state["stock_lookup_warning_shown"] = True
                show_stock_lookup_unavailable_dialog()

if (
    st.session_state["selected_main_page"] == "Alpaca Live Market Data"
    and alpaca_credentials_missing
):
    show_alpaca_credentials_required_dialog()

st.sidebar.subheader("Momentum settings")
minimum_volume_spike_percent = st.sidebar.slider(
    "Minimum volume spike",
    min_value=0,
    max_value=300,
    value=50,
    step=10,
    format="%d%%",
    help="50% means today's volume must be at least 1.5 times its 20-day average.",
)
high_distance_percent = st.sidebar.slider(
    "Distance from 20-day high",
    min_value=1,
    max_value=15,
    value=5,
    step=1,
    format="%d%%",
)
st.sidebar.subheader("Oversold settings")
maximum_rsi = st.sidebar.slider(
    "Maximum RSI",
    min_value=20,
    max_value=50,
    value=40,
    step=1,
    help="Lower values identify more deeply oversold stocks.",
)
require_up_day = st.sidebar.checkbox(
    "Require price to turn up",
    value=True,
    help="When enabled, the latest close must be higher than the prior close.",
)

if st.sidebar.button("Run scan again", type="primary", use_container_width=True):
    st.session_state["run_scan"] = True
    try:
        save_symbol_preferences(symbols_text)
        st.sidebar.success(
            "Ticker symbols updated for this session."
            if cloud_deployment
            else "Ticker symbols saved."
        )
    except OSError as exc:
        st.sidebar.error(f"Ticker symbols could not be saved: {exc}")

st.html(
    """
    <style>
    .stApp {
        background:
            radial-gradient(circle at 88% 8%, rgba(59, 130, 246, 0.12), transparent 24rem),
            radial-gradient(circle at 12% 92%, rgba(34, 197, 94, 0.10), transparent 26rem),
            #f8fafc;
    }
    .st-key-native_watchlist_navigation {
        position: sticky;
        top: 0.35rem;
        width: 100%;
        z-index: 1000001;
        overflow: visible;
    }
    .st-key-floating_stock_search {
        position: fixed;
        top: 0.35rem;
        left: 20.25rem;
        right: auto;
        width: min(24rem, calc(100vw - 2rem));
        z-index: 1000000;
        padding: 0;
        margin: 0;
        background: transparent;
        border: 0;
        box-shadow: none;
        backdrop-filter: none;
    }
    div[data-testid="stElementContainer"]:has(.st-key-floating_stock_search),
    div[data-testid="stElementContainer"]:has(.st-key-native_hamburger_navigation),
    div[data-testid="stElementContainer"]:has(.st-key-floating_scan_controls) {
        height: 0 !important;
        min-height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    .st-key-floating_stock_search div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        height: 2.35rem;
        min-height: 2.35rem;
    }
    .st-key-native_hamburger_navigation {
        position: fixed;
        top: 0.35rem;
        left: 16.5rem;
        width: 3.25rem;
        z-index: 1000001;
        overflow: visible;
    }
    .st-key-native_hamburger_navigation div[data-testid="stPopover"] button {
        min-height: 2.35rem;
    }
    @media (max-width: 700px) {
        .st-key-native_hamburger_navigation {
            left: min(14.25rem, calc(100vw - 6rem));
            width: 3.25rem;
        }
        .st-key-floating_stock_search {
            top: 3.25rem;
            left: auto;
            right: 0.75rem;
            width: min(24rem, calc(100vw - 1.5rem));
        }
    }
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #eef6ff 0%, #f2fff7 52%, #fff8ed 100%);
        border-right: 1px solid #bfd7f5;
        overflow-x: hidden !important;
    }
    section[data-testid="stSidebar"] > div {
        overflow-x: hidden !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        padding-top: 0 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        padding-top: 0.35rem !important;
    }
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapsedControl"] {
        display: none !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
        display: none !important;
        height: 0 !important;
        min-height: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
    }
    section[data-testid="stSidebar"] h3 {
        color: #155e75;
        border-bottom: 2px solid rgba(14, 165, 233, 0.28);
        padding-bottom: 0.25rem;
    }
    h1 {
        background: linear-gradient(90deg, #2563eb, #0891b2, #16a34a);
        background-clip: text;
        -webkit-background-clip: text;
        color: transparent !important;
        -webkit-text-fill-color: transparent;
    }
    .scanner-main-title {
        background: linear-gradient(90deg, #2563eb, #0891b2, #16a34a);
        background-clip: text;
        -webkit-background-clip: text;
        color: transparent;
        -webkit-text-fill-color: transparent;
        font-size: clamp(2.6rem, 4vw, 3.5rem);
        font-weight: 800;
        line-height: 1.05;
        letter-spacing: -0.04em;
        text-align: center;
        padding-top: 0.15rem;
    }
    .scanner-main-subtitle {
        color: #64748b;
        font-size: 0.95rem;
        text-align: center;
        margin-top: 0.25rem;
        margin-bottom: 0;
    }
    div[data-testid="stElementContainer"]:has(.scanner-main-subtitle) {
        margin-bottom: -1.6rem;
    }
    .st-key-header_erase_credentials {
        margin-top: 2.35rem;
    }
    div[data-testid="stMainBlockContainer"]
        > div[data-testid="stVerticalBlock"] {
        gap: 0.3rem !important;
    }
    h2, h3 {
        color: #1e3a8a;
    }
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #ffffff, #eff6ff);
        border: 1px solid #bfdbfe;
        border-left: 5px solid #3b82f6;
        border-radius: 12px;
        padding: 0.7rem 0.9rem;
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.09);
    }
    div[data-testid="stMetric"]:nth-of-type(2) {
        border-left-color: #22c55e;
        background: linear-gradient(135deg, #ffffff, #f0fdf4);
    }
    div[data-testid="stMetric"]:nth-of-type(3) {
        border-left-color: #f59e0b;
        background: linear-gradient(135deg, #ffffff, #fffbeb);
    }
    div[data-testid="stButton"] button,
    div[data-testid="stPopover"] button {
        border-color: #93c5fd;
        transition: transform 120ms ease, box-shadow 120ms ease;
    }
    div[data-testid="stButton"] button:hover,
    div[data-testid="stPopover"] button:hover {
        border-color: #2563eb;
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.18);
        transform: translateY(-1px);
    }
    button[kind="primary"] {
        background: linear-gradient(90deg, #2563eb, #0891b2) !important;
        border: none !important;
    }
    .st-key-hamburger_menu_links div[data-testid="stButton"] button {
        justify-content: flex-start !important;
        background: transparent !important;
        border: none !important;
        border-radius: 5px !important;
        color: #2563eb !important;
        min-height: 2rem !important;
        padding: 0.3rem 0.55rem !important;
        box-shadow: none !important;
        text-align: left !important;
    }
    .st-key-hamburger_menu_links div[data-testid="stButton"] button:hover {
        background: #eff6ff !important;
        color: #1d4ed8 !important;
        text-decoration: underline;
        transform: none !important;
    }
    .st-key-hamburger_menu_links div[data-testid="stButton"] button p {
        font-weight: 600;
    }
    .st-key-hamburger_menu_links .scanner-menu-link {
        display: block;
        width: 100%;
        color: #2563eb !important;
        font-weight: 600;
        font-size: 0.88rem;
        line-height: 1rem;
        padding: 0.18rem 0.45rem;
        margin: 0;
        border-radius: 5px;
        text-decoration: none !important;
    }
    .st-key-hamburger_menu_links div[data-testid="stMarkdownContainer"] p {
        margin: 0 !important;
        padding: 0 !important;
    }
    .st-key-hamburger_menu_links div[data-testid="stVerticalBlock"] {
        gap: 0.05rem !important;
    }
    .st-key-hamburger_menu_links .scanner-menu-link:hover {
        color: #1d4ed8 !important;
        background: #eff6ff;
        text-decoration: underline !important;
    }
    .scanner-home-button {
        display: block;
        width: 100%;
        box-sizing: border-box;
        color: #1f2937 !important;
        background: rgba(255, 255, 255, 0.72);
        border: 1px solid #93c5fd;
        border-radius: 8px;
        padding: 0.42rem 0.65rem;
        min-height: 2.35rem;
        font-weight: 600;
        text-align: center;
        text-decoration: none !important;
        white-space: nowrap;
        margin-bottom: 0;
    }
    .scanner-home-button:hover {
        color: #1d4ed8 !important;
        background: #eff6ff;
        border-color: #2563eb;
        box-shadow: 0 4px 12px rgba(37, 99, 235, 0.15);
    }
    .st-key-alpaca_credentials_corner details {
        border-radius: 8px !important;
        border-color: #bfdbfe !important;
    }
    .st-key-alpaca_credentials_corner details summary {
        min-height: 2rem !important;
        padding: 0.25rem 0.5rem !important;
        font-size: 0.82rem !important;
    }
    .st-key-alpaca_credentials_corner details > div {
        padding: 0.35rem 0.5rem 0.5rem !important;
    }
    .st-key-alpaca_credentials_corner div[data-testid="stTextInput"] label p {
        font-size: 0.72rem !important;
    }
    .st-key-alpaca_credentials_corner div[data-testid="stTextInput"] input {
        min-height: 1.9rem !important;
        font-size: 0.76rem !important;
        padding: 0.25rem 0.4rem !important;
    }
    div[data-testid="stAlert"] {
        border-radius: 12px;
        border-left-width: 5px;
    }
    div[data-testid="stDataFrame"] {
        border: 1px solid #bfdbfe;
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 4px 14px rgba(15, 23, 42, 0.06);
    }
    .st-key-floating_scan_controls {
        position: fixed;
        right: 1.5rem;
        bottom: 1.5rem;
        width: auto !important;
        z-index: 1000;
    }
    .st-key-floating_scan_controls button {
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
        white-space: nowrap;
    }
    div[data-baseweb="tab-list"] {
        overflow-x: auto;
        overflow-y: hidden;
        scrollbar-width: thin;
        padding-bottom: 0.45rem;
    }
    div[data-baseweb="tab-list"] button {
        flex: 0 0 auto;
    }
    div[data-baseweb="tab-panel"] {
        padding-top: 0 !important;
    }
    div[data-baseweb="tab-list"]::-webkit-scrollbar {
        height: 8px;
    }
    div[data-baseweb="tab-list"]::-webkit-scrollbar-thumb {
        background: rgba(128, 128, 128, 0.55);
        border-radius: 999px;
    }
    /* Hide only the top-level page tabs; the hamburger is the page navigation. */
    div[data-testid="stMainBlockContainer"]
        > div[data-testid="stVerticalBlock"]
        > div[data-testid="stTabs"] {
        margin-top: -0.75rem !important;
    }
    div[data-testid="stMainBlockContainer"]
        > div[data-testid="stVerticalBlock"]
        > div[data-testid="stTabs"]
        > div > div > div[data-baseweb="tab-list"] {
        display: none !important;
    }
    /* Turn Streamlit's upper-right running indicator into a miniature market ticker. */
    @keyframes scanner-market-ticker {
        from { transform: translateX(100%); }
        to { transform: translateX(-100%); }
    }
    div[data-testid="stStatusWidget"] {
        position: fixed !important;
        top: 0.55rem !important;
        right: 0.75rem !important;
        width: 300px !important;
        height: 36px !important;
        overflow: hidden !important;
        border: 1px solid rgba(255, 255, 255, 0.16) !important;
        border-radius: 8px !important;
        background: #17191d !important;
        box-shadow: 0 3px 12px rgba(0, 0, 0, 0.28) !important;
        z-index: 1001 !important;
    }
    div[data-testid="stStatusWidget"] > * {
        visibility: hidden !important;
    }
    div[data-testid="stStatusWidget"]::after {
        content: "SCANNING  •  AAPL ▲  •  MSFT ▲  •  NVDA ▼  •  AMZN ▲  •  SPY ▼  •  MARKET DATA";
        position: absolute;
        left: 0;
        top: 8px;
        color: transparent;
        background: linear-gradient(
            90deg,
            #f3f4f6 0% 23.07%,
            #22c55e 23.07% 24.36%,
            #f3f4f6 24.36% 37.18%,
            #22c55e 37.18% 38.46%,
            #f3f4f6 38.46% 51.28%,
            #ef4444 51.28% 52.56%,
            #f3f4f6 52.56% 65.38%,
            #22c55e 65.38% 66.67%,
            #f3f4f6 66.67% 78.21%,
            #ef4444 78.21% 79.49%,
            #f3f4f6 79.49% 100%
        );
        background-clip: text;
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-size: 13px;
        font-weight: 700;
        line-height: 20px;
        white-space: nowrap;
        animation: scanner-market-ticker 8s linear infinite;
    }
    /* Muted gray sliders with round handles, similar to the range control reference. */
    div[data-testid="stSlider"] div[data-baseweb="slider"] > div > div {
        background-color: #a7abb0 !important;
    }
    div[data-testid="stSlider"] div[role="slider"] {
        background-color: #777c82 !important;
        border: 2px solid #f3f4f5 !important;
        box-shadow: 0 0 0 1px #777c82, 0 2px 5px rgba(0, 0, 0, 0.2) !important;
    }
    div[data-testid="stSlider"] div[role="slider"]:hover,
    div[data-testid="stSlider"] div[role="slider"]:focus {
        background-color: #5f646a !important;
        box-shadow: 0 0 0 2px rgba(119, 124, 130, 0.28) !important;
    }
    @media (max-width: 640px) {
        .st-key-floating_scan_controls {
            right: 0.75rem;
            bottom: 0.75rem;
        }
        div[data-testid="stStatusWidget"] {
            width: 220px !important;
        }
    }
    </style>
    """,
)
if st.session_state["selected_main_page"] != "Top 20":
    with st.container(key="floating_scan_controls"):
        if st.button("↻ Run scan again", type="primary", key="floating_run_scan"):
            st.session_state["run_scan"] = True
            try:
                save_symbol_preferences(symbols_text)
                st.toast(
                    "Ticker symbols updated for this session."
                    if cloud_deployment
                    else "Ticker symbols saved."
                )
            except OSError as exc:
                st.error(f"Ticker symbols could not be saved: {exc}")

if not symbols:
    st.info(
        "Add at least one ticker symbol in the box on the left to run the scanner. "
        "For example: AAPL, MSFT, NVDA"
    )
    st.stop()

results = []
histories = {}
premarket_results = []
errors = []
progress = st.sidebar.progress(0, text="Starting scan…")

for index, symbol in enumerate(symbols):
    try:
        result, history = scan_symbol(
            symbol,
            minimum_volume_spike_percent,
            high_distance_percent,
            maximum_rsi,
            require_up_day,
        )
        if result is None:
            errors.append(f"{symbol}: not enough price history")
        else:
            results.append(result)
            histories[symbol] = history
            try:
                premarket_result = get_premarket_indicators(symbol)
                if premarket_result:
                    premarket_results.append(premarket_result)
            except Exception as exc:
                errors.append(f"{symbol} premarket data: {exc}")
    except Exception as exc:
        errors.append(f"{symbol}: {exc}")
    progress.progress((index + 1) / max(len(symbols), 1), text=f"Scanning {symbol}")

progress.empty()

if not results:
    st.error("No results were returned. Check your internet connection and ticker symbols.")
    if errors:
        st.write(errors)
    st.stop()

results_df = pd.DataFrame(results)

histories = {
    symbol: history
    for symbol, history in histories.items()
    if symbol in results_df["Symbol"].values
}
premarket_df = pd.DataFrame(premarket_results)
if not premarket_df.empty:
    premarket_df = premarket_df[
        premarket_df["Symbol"].isin(results_df["Symbol"])
    ].sort_values("Gap %", key=lambda values: values.abs(), ascending=False)

catalyst_rows = []
premarket_by_symbol = (
    premarket_df.set_index("Symbol").to_dict("index") if not premarket_df.empty else {}
)
for _, stock in results_df.iterrows():
    symbol = stock["Symbol"]
    premarket = premarket_by_symbol.get(symbol, {})
    premarket_gap = premarket.get("Gap %")

    if premarket_gap is not None and abs(premarket_gap) >= 2:
        catalyst_rows.append({
            "Symbol": symbol,
            "Potential catalyst": "Premarket price gap",
            "Indicator": f"{premarket_gap:+.2f}%",
            "Why review it": "A move of at least 2% may reflect news or changing expectations.",
        })
    if stock["Volume spike %"] >= minimum_volume_spike_percent:
        catalyst_rows.append({
            "Symbol": symbol,
            "Potential catalyst": "Unusual trading volume",
            "Indicator": f"{stock['Volume spike %']:+.1f}% vs. 20-day average",
            "Why review it": "Higher-than-usual participation can accompany a material event.",
        })
    if stock["Near 20-day high"] and stock["Strong volume"]:
        catalyst_rows.append({
            "Symbol": symbol,
            "Potential catalyst": "High-volume move near 20-day high",
            "Indicator": f"Momentum score {int(stock['Momentum score'])}/4",
            "Why review it": "Price strength and volume are occurring together.",
        })
    if stock["Oversold reversal"]:
        catalyst_rows.append({
            "Symbol": symbol,
            "Potential catalyst": "Oversold price turn",
            "Indicator": f"RSI {stock['RSI']:.1f}",
            "Why review it": "Price turned upward while RSI remains below your selected limit.",
        })

catalysts_df = pd.DataFrame(catalyst_rows)

momentum_df = results_df[results_df["Momentum"]].sort_values(
    "Relative volume", ascending=False
)
ranked_momentum_df = results_df.sort_values(
    ["Momentum score", "Relative volume"], ascending=[False, False]
)
oversold_df = results_df[results_df["Oversold reversal"]].sort_values("RSI")
ranked_oversold_df = results_df.sort_values(
    ["Oversold score", "RSI"], ascending=[False, True]
)
trends_df = results_df.sort_values(
    ["Trend score", "Momentum score", "Relative volume"],
    ascending=[False, False, False],
)

graded_rows = []
with st.spinner("Grading the scanned companies…"):
    for _, stock in results_df.iterrows():
        try:
            grade_company = get_company_health(stock["Symbol"])
            grade_filings = (
                get_sec_filings(stock["Symbol"], sec_contact_email)
                if sec_contact_email and "@" in sec_contact_email
                else pd.DataFrame()
            )
            score, grade, profile, _ = score_purchase_research(
                stock,
                grade_company,
                grade_filings,
            )
            graded_rows.append({
                "Symbol": stock["Symbol"],
                "Score": score,
                "Grade": grade,
                "Profile": profile,
                "Price": stock["Price"],
                "Trend score": int(stock["Trend score"]),
                "Trend": stock["Trend"],
                "Momentum score": int(stock["Momentum score"]),
                "Momentum match": bool(stock["Momentum"]),
                "RSI": stock["RSI"],
                "Volume spike %": stock["Volume spike %"],
            })
        except Exception as exc:
            errors.append(f"{stock['Symbol']} grade: {exc}")

graded_df = pd.DataFrame(graded_rows)
if not graded_df.empty:
    graded_df = graded_df.sort_values(
        ["Score", "Trend score", "Momentum score"],
        ascending=[False, False, False],
    )

selected_main_page = st.session_state["selected_main_page"]
selected_page_index = main_pages.index(selected_main_page)
# Changing this invisible marker gives the hidden tabs a fresh identity, ensuring
# the hamburger choice becomes the active panel after every click.
internal_page_labels = [
    page_name + ("\u200b" * (selected_page_index + 1) if index == selected_page_index else "")
    for index, page_name in enumerate(main_pages)
]
selected_internal_label = internal_page_labels[selected_page_index]

(
    all_tab,
    alpaca_tab,
    catalyst_tab,
    premarket_tab,
    trends_tab,
    momentum_tab,
    oversold_tab,
    purchase_grade_tab,
    autopilot_tab,
    research_tab,
    top_graded_tab,
    settings_tab,
) = st.tabs(internal_page_labels, default=selected_internal_label)

display_columns = [
    "Symbol", "Price", "Daily %", "RSI", "Relative volume", "Volume spike %", "SMA20", "SMA50"
]
column_config = {
    "Price": st.column_config.NumberColumn(format="$%.2f"),
    "Daily %": st.column_config.NumberColumn(format="%.2f%%"),
    "RSI": st.column_config.NumberColumn(format="%.1f"),
    "Relative volume": st.column_config.NumberColumn(format="%.2fx"),
    "Volume spike %": st.column_config.NumberColumn(format="%.1f%%"),
    "SMA20": st.column_config.NumberColumn(format="$%.2f"),
    "SMA50": st.column_config.NumberColumn(format="$%.2f"),
    "SMA200": st.column_config.NumberColumn(format="$%.2f"),
    "MACD": st.column_config.NumberColumn(format="%.2f"),
    "MACD signal": st.column_config.NumberColumn(format="%.2f"),
    "20-day resistance": st.column_config.NumberColumn(format="$%.2f"),
    "Breaking resistance": st.column_config.CheckboxColumn(
        "Breaking resistance",
        help="Checked when today's close crosses above the highest price from the prior 20 trading days.",
    ),
}

with momentum_tab:
    st.write("A match passes all four momentum checks. Adjust the settings in the sidebar.")
    if momentum_df.empty:
        st.warning(
            "No exact matches right now. The closest candidates are ranked below; "
            "a score of 4 passes every check."
        )
        momentum_display = ranked_momentum_df
    else:
        momentum_display = momentum_df
    momentum_columns = [
        "Symbol", "Momentum score", "Price", "Daily %", "RSI",
        "Relative volume", "Volume spike %", "Above SMA20", "Bullish trend",
        "Strong volume", "Near 20-day high", "20-day resistance",
        "Breaking resistance",
    ]
    render_removable_stock_table(
        momentum_display[momentum_columns],
        "momentum_remove_table",
        column_config,
    )

with oversold_tab:
    st.write("A match is below your RSI limit and, if required, has turned upward.")
    if oversold_df.empty:
        st.warning(
            "No exact matches right now. The closest candidates are ranked below; "
            "lower RSI indicates weaker recent momentum."
        )
        oversold_display = ranked_oversold_df
    else:
        oversold_display = oversold_df
    oversold_columns = [
        "Symbol", "Oversold score", "Price", "Daily %", "RSI",
        "Below RSI limit", "Price turned up", "Relative volume", "Volume spike %",
    ]
    render_removable_stock_table(
        oversold_display[oversold_columns],
        "oversold_remove_table",
        column_config,
    )

with trends_tab:
    st.write(
        "Select any cell to open the combined price, trend, MACD, volume, and RVOL view."
    )
    premarket_volume_by_symbol = (
        premarket_df.set_index("Symbol")["Premarket volume"].to_dict()
        if not premarket_df.empty and "Premarket volume" in premarket_df.columns
        else {}
    )
    trend_columns = [
        "Symbol", "Trend score", "Trend", "Price", "RVOL",
        "Premarket volume", "SMA20", "SMA50", "SMA200", "MACD", "MACD signal",
        "20-day resistance", "Breaking resistance", "Above SMA20", "Bullish trend",
        "Above SMA200", "MACD bullish",
    ]
    trend_display = trends_df.copy()
    trend_display["RVOL"] = trend_display["Relative volume"]
    trend_display["Premarket volume"] = trend_display["Symbol"].map(
        premarket_volume_by_symbol
    )
    trend_display = trend_display[trend_columns].reset_index(drop=True)
    trend_selection = st.dataframe(
        trend_display,
        hide_index=True,
        use_container_width=True,
        height=dataframe_height(trend_display),
        key="trends_interactive_table",
        on_select="rerun",
        selection_mode="single-cell",
        column_config={
            **column_config,
            "RVOL": st.column_config.NumberColumn(format="%.2fx"),
            "Premarket volume": st.column_config.NumberColumn(format="localized"),
        },
    )

    selected_trend_cells = trend_selection.selection.cells
    pattern_symbol = (
        trend_display.iloc[selected_trend_cells[0][0]]["Symbol"]
        if selected_trend_cells
        else None
    )
    st.button(
        "Remove selected ticker",
        key="trends_remove_button",
        disabled=not pattern_symbol,
        on_click=remove_symbols_from_watchlist,
        args=([pattern_symbol] if pattern_symbol else [],),
    )
    if not pattern_symbol:
        st.info("Click any cell in a stock row to display its combined live chart.")
    else:
        with st.spinner(f"Loading all available history for {pattern_symbol}…"):
            trend_history = get_long_term_history(pattern_symbol)
        if trend_history.empty:
            st.info("Full price history is unavailable for this stock.")
        else:
            current_year = pd.Timestamp.now().year
            chart_start = pd.Timestamp(year=1990, month=1, day=1)
            chart_end = pd.Timestamp(year=current_year, month=12, day=31)
            visible_trend_history = trend_history.loc[
                (trend_history.index >= chart_start)
                & (trend_history.index <= chart_end)
            ]

            latest_rvol = visible_trend_history["RVOL"].dropna()
            latest_rvol_value = latest_rvol.iloc[-1] if not latest_rvol.empty else float("nan")
            selected_premarket_volume = premarket_volume_by_symbol.get(pattern_symbol)
            metric1, metric2, metric3 = st.columns(3)
            metric1.metric(
                "Latest price",
                f"${visible_trend_history['Close'].iloc[-1]:,.2f}",
            )
            metric2.metric(
                "Latest RVOL",
                f"{latest_rvol_value:.2f}x" if pd.notna(latest_rvol_value) else "Not available",
            )
            metric3.metric(
                "Premarket volume",
                f"{selected_premarket_volume:,.0f}"
                if selected_premarket_volume is not None and pd.notna(selected_premarket_volume)
                else "Not available",
            )

            trend_figure = make_subplots(
                rows=3,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.055,
                row_heights=[0.34, 0.30, 0.36],
                specs=[[{}], [{}], [{"secondary_y": True}]],
                subplot_titles=(
                    "Price and moving averages",
                    "MACD (12, 26, 9)",
                    "Volume and relative volume (RVOL)",
                ),
            )
            trend_figure.add_trace(
                go.Candlestick(
                    x=visible_trend_history.index,
                    open=visible_trend_history["Open"],
                    high=visible_trend_history["High"],
                    low=visible_trend_history["Low"],
                    close=visible_trend_history["Close"],
                    name=pattern_symbol,
                    increasing_line_color="#16a34a",
                    increasing_fillcolor="#22c55e",
                    decreasing_line_color="#dc2626",
                    decreasing_fillcolor="#ef4444",
                ),
                row=1,
                col=1,
            )
            for column, name, color in [
                ("SMA20", "SMA 20", "#2563eb"),
                ("SMA50", "SMA 50", "#f59e0b"),
                ("SMA200", "SMA 200", "#7c3aed"),
            ]:
                trend_figure.add_trace(
                    go.Scatter(
                        x=visible_trend_history.index,
                        y=visible_trend_history[column],
                        name=name,
                        line={"color": color, "width": 1.8},
                    ),
                    row=1,
                    col=1,
                )
            histogram_colors = [
                "#2ca02c" if value >= 0 else "#d62728"
                for value in visible_trend_history["MACDHistogram"]
            ]
            trend_figure.add_trace(
                go.Bar(
                    x=visible_trend_history.index,
                    y=visible_trend_history["MACDHistogram"],
                    name="MACD histogram",
                    marker_color=histogram_colors,
                ),
                row=2,
                col=1,
            )
            trend_figure.add_trace(
                go.Scatter(
                    x=visible_trend_history.index,
                    y=visible_trend_history["MACD"],
                    name="MACD",
                    line={"color": "#2563eb", "width": 2},
                ),
                row=2,
                col=1,
            )
            trend_figure.add_trace(
                go.Scatter(
                    x=visible_trend_history.index,
                    y=visible_trend_history["MACDSignal"],
                    name="Signal",
                    line={"color": "#f97316", "width": 2},
                ),
                row=2,
                col=1,
            )
            volume_colors = [
                "#22c55e" if close >= opened else "#ef4444"
                for close, opened in zip(
                    visible_trend_history["Close"],
                    visible_trend_history["Open"],
                )
            ]
            trend_figure.add_trace(
                go.Bar(
                    x=visible_trend_history.index,
                    y=visible_trend_history["Volume"],
                    name="Daily volume",
                    marker_color=volume_colors,
                    opacity=0.65,
                ),
                row=3,
                col=1,
                secondary_y=False,
            )
            trend_figure.add_trace(
                go.Scatter(
                    x=visible_trend_history.index,
                    y=visible_trend_history["RVOL"],
                    name="RVOL",
                    line={"color": "#7f3c8d", "width": 1.5},
                ),
                row=3,
                col=1,
                secondary_y=True,
            )
            if selected_premarket_volume is not None and pd.notna(selected_premarket_volume):
                trend_figure.add_hline(
                    y=selected_premarket_volume,
                    line_dash="dot",
                    line_color="#55595e",
                    annotation_text="Latest premarket volume",
                    row=3,
                    col=1,
                )

            trend_figure.update_layout(
                height=850,
                template="plotly_white",
                paper_bgcolor="#ffffff",
                plot_bgcolor="#ffffff",
                title={
                    "text": f"{pattern_symbol}: 1990–{current_year}",
                    "x": 0.5,
                },
                hovermode="x unified",
                dragmode=False,
                legend={"orientation": "h", "y": -0.08},
                margin={"t": 85, "b": 75},
            )
            trend_figure.update_xaxes(
                range=[chart_start, chart_end],
                fixedrange=False,
                rangeslider_visible=False,
            )
            trend_figure.update_yaxes(fixedrange=True)
            trend_figure.update_yaxes(title_text="Price ($)", row=1, col=1)
            trend_figure.update_yaxes(title_text="MACD", row=2, col=1)
            trend_figure.update_yaxes(title_text="Volume", row=3, col=1, secondary_y=False)
            trend_figure.update_yaxes(title_text="RVOL (x)", row=3, col=1, secondary_y=True)
            render_live_combined_chart(
                trend_figure,
                pattern_symbol,
                1990,
                current_year,
                height=920,
            )
        st.caption(
            "Use the compact gray year control above the chart. The price, MACD, and volume/RVOL "
            "panels update together. Premarket volume is partial-session data."
        )

with premarket_tab:
    st.write(
        "Latest available U.S. premarket activity (4:00–9:30 a.m. Eastern). "
        "Larger gaps and volume can identify stocks that deserve additional research."
    )
    if premarket_df.empty:
        st.info(
            "No premarket data is available for these symbols right now. Some securities "
            "do not trade in extended hours, and quotes may be unavailable on weekends or holidays."
        )
    else:
        premarket_columns = [
            "Symbol", "Premarket price", "Gap %", "Premarket high", "Premarket low",
            "Premarket volume", "Previous close", "Latest premarket quote",
        ]
        render_removable_stock_table(
            premarket_df[premarket_columns],
            "premarket_remove_table",
            {
                "Premarket price": st.column_config.NumberColumn(format="$%.2f"),
                "Gap %": st.column_config.NumberColumn(format="%.2f%%"),
                "Premarket high": st.column_config.NumberColumn(format="$%.2f"),
                "Premarket low": st.column_config.NumberColumn(format="$%.2f"),
                "Premarket volume": st.column_config.NumberColumn(format="localized"),
                "Previous close": st.column_config.NumberColumn(format="$%.2f"),
            },
        )
        st.caption(
            "Premarket data may be delayed, incomplete, or thinly traded. A price gap alone "
            "does not indicate that the move will continue after the market opens."
        )

with catalyst_tab:
    st.subheader("Potential catalysts")
    st.caption(
        "This page highlights unusual activity and scheduled or reported events that may "
        "deserve investigation. It does not determine whether an event is positive or negative."
    )

    if catalysts_df.empty:
        st.info("No technical or premarket catalyst conditions are active for this scan.")
    else:
        render_removable_stock_table(
            catalysts_df,
            "catalysts_remove_table",
        )

    st.markdown("### Company event check")
    st.write("Select a stock row to check its earnings and filing events.")
    catalyst_symbol = render_stock_selector_table(
        results_df,
        "catalyst_symbol_table",
        column_config,
    )

    try:
        catalyst_company = get_company_health(catalyst_symbol)
        earnings_timestamp = (
            catalyst_company.get("earningsTimestamp")
            or catalyst_company.get("earningsTimestampStart")
        )
        if earnings_timestamp:
            earnings_date = (
                pd.to_datetime(earnings_timestamp, unit="s", utc=True)
                .tz_convert("America/New_York")
                .strftime("%A, %B %d, %Y")
            )
            st.metric("Reported next earnings date", earnings_date)
            st.caption("Earnings dates can be estimated or changed by the company.")
        else:
            st.info("A future earnings date is not currently available for this company.")
    except Exception as exc:
        st.warning(f"The earnings date could not be loaded: {exc}")

    st.markdown("#### Estimated future SEC filings")
    if not sec_contact_email or "@" not in sec_contact_email:
        st.info("Enter your SEC contact email in Company research to estimate future filings.")
    else:
        try:
            future_filings = get_estimated_future_filings(
                catalyst_symbol,
                sec_contact_email,
            )
            if future_filings.empty:
                st.info("A future 10-Q or 10-K filing window could not be estimated.")
            else:
                st.dataframe(
                    future_filings,
                    hide_index=True,
                    use_container_width=True,
                    height=dataframe_height(future_filings),
                )
                st.caption(
                    "These are estimates based on the company's prior-year filing cadence, "
                    "not confirmed filing dates. Actual timing can change, extensions may be "
                    "available, and deadlines vary by filer status."
                )
                st.link_button(
                    "Read the SEC filing-deadline guidance",
                    "https://www.sec.gov/about/divisions-offices/division-corporation-finance/financial-reporting-manual/frm-topic-1",
                )
        except (HTTPError, URLError, TimeoutError) as exc:
            st.warning(f"Future filing estimates could not be loaded right now: {exc}")
        except Exception as exc:
            st.warning(f"Future filing dates could not be estimated: {exc}")

    st.markdown("#### Recent 8-K events")
    if not sec_contact_email or "@" not in sec_contact_email:
        st.info("Enter your SEC contact email in Company research to check recent 8-K filings.")
    else:
        try:
            catalyst_filings = get_sec_filings(catalyst_symbol, sec_contact_email)
            if not catalyst_filings.empty:
                catalyst_filings = catalyst_filings[
                    catalyst_filings["Form"].isin(["8-K", "8-K/A"])
                ].copy()
            if catalyst_filings.empty:
                st.info("No recent 8-K filings were found for this company.")
            else:
                cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=45)
                recent_8k = catalyst_filings[
                    pd.to_datetime(catalyst_filings["Filed"]) >= cutoff
                ].head(6)
                if recent_8k.empty:
                    st.info("No 8-K filings were found in the last 45 days.")
                else:
                    st.dataframe(
                        recent_8k,
                        hide_index=True,
                        use_container_width=True,
                        height=dataframe_height(recent_8k),
                        column_config={
                            "Filing": st.column_config.LinkColumn(
                                "Official filing",
                                display_text="Open on SEC.gov",
                            ),
                        },
                    )
                    st.caption(
                        "Open each filing and read its context. An 8-K can describe favorable, "
                        "unfavorable, or neutral events."
                    )
        except (HTTPError, URLError, TimeoutError) as exc:
            st.warning(f"SEC filings could not be loaded right now: {exc}")
        except Exception as exc:
            st.warning(f"SEC catalyst data could not be processed: {exc}")

with purchase_grade_tab:
    st.subheader("Purchase research grades")
    st.caption(
        "Select a stock's row to see every indicator behind its grade. Grades summarize "
        "selected measurable factors; they are not personalized advice."
    )
    if graded_df.empty:
        st.warning("No complete grades could be calculated for the current symbols.")
    else:
        grade_summary_columns = [
            "Symbol", "Grade", "Score", "Profile", "Price", "Trend score", "Trend",
            "Momentum score", "RSI", "Volume spike %",
        ]
        grade_summary = graded_df[grade_summary_columns].reset_index(drop=True)
        grade_summary["Grade"] = grade_summary["Grade"].map(GRADE_DISPLAY).fillna(
            grade_summary["Grade"]
        )
        grade_selection = st.dataframe(
            grade_summary,
            hide_index=True,
            use_container_width=True,
            height=dataframe_height(grade_summary),
            key="purchase_grade_table",
            on_select="rerun",
            selection_mode="single-cell",
            column_config={
                **column_config,
                "Score": st.column_config.ProgressColumn(
                    "Research score",
                    min_value=0,
                    max_value=100,
                    format="%d",
                ),
            },
        )

        selected_grade_cells = grade_selection.selection.cells
        purchase_symbol = (
            grade_summary.iloc[selected_grade_cells[0][0]]["Symbol"]
            if selected_grade_cells
            else None
        )
        st.button(
            "Remove selected ticker",
            key="purchase_grade_remove_button",
            disabled=not purchase_symbol,
            on_click=remove_symbols_from_watchlist,
            args=([purchase_symbol] if purchase_symbol else [],),
        )
        if not purchase_symbol:
            st.info("Click a stock row above to see its indicator breakdown.")
        else:
            purchase_stock = results_df.set_index("Symbol").loc[purchase_symbol]
            try:
                with st.spinner(f"Loading the indicators for {purchase_symbol}…"):
                    purchase_company = get_company_health(purchase_symbol)
                    purchase_filings = (
                        get_sec_filings(purchase_symbol, sec_contact_email)
                        if sec_contact_email and "@" in sec_contact_email
                        else pd.DataFrame()
                    )
                    purchase_score, purchase_grade, purchase_profile, grade_details = (
                        score_purchase_research(
                            purchase_stock,
                            purchase_company,
                            purchase_filings,
                        )
                    )

                st.markdown(f"### {purchase_symbol} indicator breakdown")
                grade_col1, grade_col2, grade_col3 = st.columns(3)
                grade_col1.metric("Research grade", purchase_grade)
                grade_col2.metric("Score", f"{purchase_score}/100")
                grade_col3.metric("Research profile", purchase_profile)

                if purchase_score >= 70:
                    st.success(
                        "The available indicators form a stronger research profile. This means the "
                        "company may merit deeper due diligence—not that it is automatically a good purchase."
                    )
                elif purchase_score >= 50:
                    st.warning(
                        "The available indicators form a mixed research profile. Review the failed "
                        "checks, valuation, filings, and company-specific risks."
                    )
                else:
                    st.error(
                        "The available indicators form a weaker research profile. Several measured "
                        "areas need review; missing data can also lower the grade."
                    )

                st.dataframe(
                    grade_details,
                    hide_index=True,
                    use_container_width=True,
                    height=dataframe_height(grade_details),
                    column_config={
                        "Points": st.column_config.ProgressColumn(
                            "Points earned",
                            min_value=0,
                            max_value=10,
                            format="%d",
                        ),
                    },
                )
                st.caption(
                    "Scoring: trend 25 points, momentum 20, financial health 30, valuation 15, "
                    "and selected SEC filing-risk checks 10. Broad thresholds do not account for "
                    "industry differences, and unavailable data receives no points."
                )
            except Exception as exc:
                st.warning(f"Indicators for {purchase_symbol} could not be loaded: {exc}")

with top_graded_tab:
    st.subheader("Top 20 grades from the broader U.S. stock market")
    st.caption(
        "This search is independent of your ticker box. It combines Yahoo's U.S. screener, "
        "CNN Markets movers, and FINVIZ, then removes duplicates before grading."
    )
    top_control1, top_control2, top_control3 = st.columns(3)
    with top_control1:
        minimum_grade_score = st.slider(
            "Minimum research score",
            min_value=0,
            max_value=100,
            value=70,
            step=5,
            key="top20_minimum_grade",
        )
    with top_control2:
        minimum_trend_score = st.slider(
            "Minimum trend score",
            min_value=0,
            max_value=4,
            value=3,
            step=1,
            key="top20_minimum_trend",
        )
    with top_control3:
        require_momentum_match = st.checkbox(
            "Require exact momentum match",
            value=False,
            key="top20_require_momentum",
        )

    if st.button(
        "Scan all stocks",
        type="primary",
        key="run_marketwide_scan",
        help="The first market-wide scan can take a minute or two.",
    ):
        st.session_state["marketwide_scan_requested"] = True

    if not st.session_state.get("marketwide_scan_requested", False):
        st.info(
            "Select Scan all stocks to search beyond your saved ticker list. Results are cached "
            "so adjusting the grade requirements afterward is faster."
        )
    else:
        try:
            with st.spinner("Screening the broader U.S. market…"):
                market_symbols = get_marketwide_candidates()
                market_results = scan_marketwide_candidates(
                    market_symbols,
                    minimum_volume_spike_percent,
                    high_distance_percent,
                    maximum_rsi,
                    require_up_day,
                )
            with st.spinner("Grading the strongest market-wide candidates…"):
                market_grades = grade_marketwide_shortlist(market_results)

            if market_grades.empty:
                st.warning("The broad-market scan did not return enough data to calculate grades.")
            else:
                qualifying_grades = market_grades[
                    (market_grades["Score"] >= minimum_grade_score)
                    & (market_grades["Trend score"] >= minimum_trend_score)
                ].copy()
                if require_momentum_match:
                    qualifying_grades = qualifying_grades[
                        qualifying_grades["Momentum match"]
                    ]
                qualifying_grades = qualifying_grades.head(20).reset_index(drop=True)

                if qualifying_grades.empty:
                    st.warning(
                        "No broad-market candidates meet every selected requirement. "
                        "Try lowering the minimum score or trend setting."
                    )
                else:
                    top_grade_columns = [
                        "Symbol", "Company", "Score", "Grade", "Profile", "Price", "Trend score",
                        "Trend", "Momentum score", "Momentum match", "RSI",
                        "Relative volume", "Volume spike %",
                    ]
                    display_qualifying_grades = qualifying_grades.copy()
                    display_qualifying_grades["Grade"] = (
                        display_qualifying_grades["Grade"]
                        .map(GRADE_DISPLAY)
                        .fillna(display_qualifying_grades["Grade"])
                    )
                    broad_selection = st.dataframe(
                        display_qualifying_grades[top_grade_columns],
                        hide_index=True,
                        use_container_width=True,
                        height=dataframe_height(qualifying_grades),
                        key="all_stocks_grade_table",
                        on_select="rerun",
                        selection_mode="single-cell",
                        column_config={
                            **column_config,
                            "Score": st.column_config.ProgressColumn(
                                "Research score",
                                min_value=0,
                                max_value=100,
                                format="%d",
                            ),
                        },
                    )
                    broad_cells = broad_selection.selection.cells
                    selected_broad_symbol = (
                        display_qualifying_grades.iloc[broad_cells[0][0]]["Symbol"]
                        if broad_cells
                        else None
                    )
                    st.button(
                        "Add selected ticker to my symbols",
                        key="add_marketwide_ticker",
                        disabled=not selected_broad_symbol,
                        on_click=add_symbol_to_watchlist,
                        args=(selected_broad_symbol or "",),
                    )
                    st.caption(
                        f"Showing {len(qualifying_grades)} qualifying stock(s) from "
                        f"{len(market_symbols)} combined market candidates. Public pages can change "
                        "or temporarily block automated access, so unavailable sources are skipped "
                        "without stopping the scan. Grades are research summaries, not buy signals."
                    )
        except Exception as exc:
            st.warning(f"The all-stocks scan could not be completed right now: {exc}")

with all_tab:
    render_removable_stock_table(
        results_df,
        "all_results_remove_table",
        column_config,
    )

with alpaca_tab:
    st.caption(
        "Current quotes for your watchlist. Free IEX covers one exchange; paid SIP "
        "covers all U.S. exchanges. API settings are in the left sidebar."
    )

    refresh_alpaca_quotes = st.button(
        "↻ Refresh live quotes",
        type="primary",
        key="refresh_alpaca_quotes",
        disabled=not alpaca_key or not alpaca_secret or not symbols,
    )
    alpaca_request_signature = (
        tuple(symbols[:30]),
        alpaca_feed,
        alpaca_key[-6:],
    )
    auto_load_alpaca = (
        selected_main_page == "Alpaca Live Market Data"
        and bool(alpaca_key)
        and bool(alpaca_secret)
        and bool(symbols)
        and st.session_state.get("alpaca_loaded_signature") != alpaca_request_signature
    )
    if auto_load_alpaca or refresh_alpaca_quotes:
        try:
            with st.spinner("Connecting to Alpaca and loading current quotes…"):
                st.session_state["alpaca_live_quotes"] = get_alpaca_live_quotes(
                    symbols,
                    alpaca_key,
                    alpaca_secret,
                    alpaca_feed,
                )
                st.session_state["alpaca_live_feed"] = alpaca_feed.upper()
                st.session_state["alpaca_loaded_signature"] = alpaca_request_signature
                st.session_state["alpaca_last_refreshed"] = pd.Timestamp.now().strftime(
                    "%b %d, %Y at %I:%M:%S %p"
                )
            if refresh_alpaca_quotes:
                st.success(f"Refreshed Alpaca {alpaca_feed.upper()} quotes.")
        except HTTPError as exc:
            if exc.code in (401, 403):
                st.error(
                    "Alpaca rejected the connection. Check the API key and secret, and make "
                    "sure your account is allowed to use the selected feed."
                )
            else:
                st.error(f"Alpaca returned an error ({exc.code}). Please try again.")
        except (URLError, TimeoutError) as exc:
            st.error(f"Alpaca could not be reached right now: {exc}")
        except Exception as exc:
            st.error(f"Live quotes could not be loaded: {exc}")

    alpaca_quotes = st.session_state.get("alpaca_live_quotes", pd.DataFrame())
    if not isinstance(alpaca_quotes, pd.DataFrame) or alpaca_quotes.empty:
        if not symbols:
            st.info("Add at least one stock symbol before loading live quotes.")
        elif not alpaca_key or not alpaca_secret:
            st.info("Open the small API box in the upper-right and add your credentials.")
        else:
            st.info("Connecting to Alpaca…")
    else:
        live_feed = st.session_state.get("alpaca_live_feed", "IEX")
        st.markdown(f"### 🟢 LIVE — {live_feed}")
        st.dataframe(
            alpaca_quotes,
            hide_index=True,
            use_container_width=True,
            height=dataframe_height(alpaca_quotes),
            column_config={
                "Last": st.column_config.NumberColumn(format="$%.2f"),
                "Bid": st.column_config.NumberColumn(format="$%.2f"),
                "Ask": st.column_config.NumberColumn(format="$%.2f"),
                "Change %": st.column_config.NumberColumn(format="%.2f%%"),
                "Day volume": st.column_config.NumberColumn(format="localized"),
                "Last update": st.column_config.DatetimeColumn(format="MM/DD/YYYY h:mm:ss a"),
            },
        )
        st.caption(
            f"Last refreshed {st.session_state.get('alpaca_last_refreshed', 'this session')}. "
            "IEX volume represents only IEX activity and is not total market volume."
        )

    with st.expander("How to get and save your Alpaca API keys"):
        st.markdown(
            """
1. Create or sign in to your Alpaca account.
2. Open the Alpaca dashboard and generate an API key and secret.
3. Paste them into the protected fields above.
4. Choose **Save on this Mac** to remember them only in this browser, or leave them
   unsaved to use them for the current session only.
5. Open **Settings** and choose **Erase Credentials** whenever you want to remove the
   saved copy from this browser.

Never place the secret directly inside `app.py`, email it, or share it in a screenshot.
            """
        )

with autopilot_tab:
    st.subheader("Autopilot-style disclosure research")
    st.caption(
        "Research delayed public disclosures from members of Congress and selected institutional "
        "investment managers. This tab does not connect to a brokerage, copy portfolios, or place trades."
    )

    politician_section, hedge_fund_section = st.tabs(
        ["Congressional disclosures", "Hedge-fund 13F holdings"]
    )

    with politician_section:
        st.write(
            "Congressional transactions are reported after the trade and use value ranges rather "
            "than exact amounts. A disclosure is not a real-time trading signal."
        )
        congress_filter1, congress_filter2 = st.columns(2)
        with congress_filter1:
            member_search = st.text_input(
                "Filter by member or ticker",
                key="autopilot_member_search",
                placeholder="For example: Pelosi or NVDA",
            ).strip()
        with congress_filter2:
            chamber_filter = st.selectbox(
                "Chamber",
                ["All", "House", "Senate"],
                key="autopilot_chamber_filter",
            )
        try:
            congress_trades = get_recent_congress_trades()
            if chamber_filter != "All" and not congress_trades.empty:
                congress_trades = congress_trades[
                    congress_trades["Chamber"].str.casefold() == chamber_filter.casefold()
                ]
            if member_search and not congress_trades.empty:
                search_lower = member_search.casefold()
                congress_trades = congress_trades[
                    congress_trades["Member"].str.casefold().str.contains(
                        search_lower, regex=False, na=False
                    )
                    | congress_trades["Ticker"].str.casefold().str.contains(
                        search_lower, regex=False, na=False
                    )
                ]
            congress_trades = congress_trades.head(100).reset_index(drop=True)
            if congress_trades.empty:
                st.info("No matching congressional disclosures are available right now.")
            else:
                congress_selection = st.dataframe(
                    congress_trades,
                    hide_index=True,
                    use_container_width=True,
                    height=min(dataframe_height(congress_trades), 650),
                    key="autopilot_congress_table",
                    on_select="rerun",
                    selection_mode="single-cell",
                    column_config={
                        "Official disclosure": st.column_config.LinkColumn(
                            "Official disclosure",
                            display_text="Open filing",
                        ),
                    },
                )
                congress_cells = congress_selection.selection.cells
                congress_symbol = (
                    congress_trades.iloc[congress_cells[0][0]]["Ticker"]
                    if congress_cells
                    else None
                )
                st.button(
                    "Add selected ticker to my symbols",
                    key="add_congress_ticker",
                    disabled=not congress_symbol,
                    on_click=add_symbol_to_watchlist,
                    args=(congress_symbol or "",),
                )
        except Exception as exc:
            st.warning(f"Congressional disclosures could not be loaded right now: {exc}")

        official_col1, official_col2 = st.columns(2)
        with official_col1:
            st.link_button(
                "Search official House disclosures",
                "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewSearch",
                use_container_width=True,
            )
        with official_col2:
            st.link_button(
                "Search official Senate disclosures",
                "https://efdsearch.senate.gov/search/home/",
                use_container_width=True,
            )

    with hedge_fund_section:
        st.write(
            "Form 13F reports selected long U.S. holdings after each quarter. It can omit short "
            "positions and other assets, and it does not show the manager's current portfolio."
        )
        manager_name = st.selectbox(
            "Institutional manager",
            list(TRACKED_13F_MANAGERS),
            key="autopilot_13f_manager",
        )
        if not sec_contact_email or "@" not in sec_contact_email:
            st.info("Enter your SEC contact email in Company research to load 13F holdings.")
        else:
            try:
                with st.spinner(f"Loading the latest 13F filing for {manager_name}…"):
                    holdings, filing_metadata = get_latest_13f_holdings(
                        manager_name,
                        sec_contact_email,
                    )
                if holdings.empty:
                    st.info("No holdings table was available in the latest 13F filing.")
                else:
                    filing_col1, filing_col2 = st.columns(2)
                    filing_col1.metric("Report period", filing_metadata.get("Report period", ""))
                    filing_col2.metric("Filed", filing_metadata.get("Filed", ""))
                    st.dataframe(
                        holdings.head(100),
                        hide_index=True,
                        use_container_width=True,
                        height=min(dataframe_height(holdings.head(100)), 650),
                        column_config={
                            "Reported value": st.column_config.NumberColumn(format="localized"),
                            "Shares/principal": st.column_config.NumberColumn(format="localized"),
                        },
                    )
                    st.link_button(
                        "Open the official SEC 13F filing",
                        filing_metadata["Filing"],
                    )
            except Exception as exc:
                st.warning(f"The 13F holdings could not be loaded right now: {exc}")

    st.caption(
        "Research use only. Congressional disclosures may be delayed by weeks, and 13F holdings "
        "are quarterly snapshots that can be stale before publication."
    )

with research_tab:
    st.subheader("Financial health and SEC filings")
    st.caption(
        "Use this page to investigate a company behind the chart. Indicators and filing "
        "flags are research prompts, not buy or sell recommendations. SEC settings are in "
        "the left sidebar."
    )

    st.write("Select a stock row to load its financial health and SEC filings.")
    research_symbol = render_stock_selector_table(
        results_df,
        "research_symbol_table",
        column_config,
    )

    try:
        with st.spinner(f"Loading financial indicators for {research_symbol}…"):
            company = get_company_health(research_symbol)

        st.markdown(f"### {company.get('longName', research_symbol)} ({research_symbol})")
        if company.get("sector") or company.get("industry"):
            st.write(
                " · ".join(
                    value for value in [company.get("sector"), company.get("industry")] if value
                )
            )

        financial_rows = [
            {
                "Group": "Size and trading",
                "Indicator": "Market capitalization",
                "Value": format_research_value(company.get("marketCap"), "money"),
                "Why it matters": "Smaller companies can be more volatile and less liquid.",
            },
            {
                "Group": "Growth",
                "Indicator": "Revenue growth",
                "Value": format_research_value(safe_percent(company.get("revenueGrowth")), "percent"),
                "Why it matters": "Compares recent revenue with the prior comparable period.",
            },
            {
                "Group": "Growth",
                "Indicator": "Earnings growth",
                "Value": format_research_value(safe_percent(company.get("earningsGrowth")), "percent"),
                "Why it matters": "Shows whether reported earnings are expanding or shrinking.",
            },
            {
                "Group": "Profitability",
                "Indicator": "Profit margin",
                "Value": format_research_value(safe_percent(company.get("profitMargins")), "percent"),
                "Why it matters": "The portion of revenue remaining as net profit.",
            },
            {
                "Group": "Profitability",
                "Indicator": "Operating margin",
                "Value": format_research_value(safe_percent(company.get("operatingMargins")), "percent"),
                "Why it matters": "Measures profit from the company's core operations.",
            },
            {
                "Group": "Profitability",
                "Indicator": "Return on equity",
                "Value": format_research_value(safe_percent(company.get("returnOnEquity")), "percent"),
                "Why it matters": "Relates profit to shareholder equity; debt can influence it.",
            },
            {
                "Group": "Financial health",
                "Indicator": "Cash",
                "Value": format_research_value(company.get("totalCash"), "money"),
                "Why it matters": "Cash can provide flexibility during difficult periods.",
            },
            {
                "Group": "Financial health",
                "Indicator": "Total debt",
                "Value": format_research_value(company.get("totalDebt"), "money"),
                "Why it matters": "Debt creates repayment and interest obligations.",
            },
            {
                "Group": "Financial health",
                "Indicator": "Debt to equity",
                "Value": format_research_value(company.get("debtToEquity"), "ratio"),
                "Why it matters": "A leverage measure best compared with similar companies.",
            },
            {
                "Group": "Financial health",
                "Indicator": "Current ratio",
                "Value": format_research_value(company.get("currentRatio"), "ratio"),
                "Why it matters": "Compares current assets with short-term obligations.",
            },
            {
                "Group": "Cash flow",
                "Indicator": "Operating cash flow",
                "Value": format_research_value(company.get("operatingCashflow"), "money"),
                "Why it matters": "Cash generated by the company's regular operations.",
            },
            {
                "Group": "Cash flow",
                "Indicator": "Free cash flow",
                "Value": format_research_value(company.get("freeCashflow"), "money"),
                "Why it matters": "Cash remaining after capital expenditures.",
            },
            {
                "Group": "Valuation",
                "Indicator": "Trailing P/E",
                "Value": format_research_value(company.get("trailingPE"), "ratio"),
                "Why it matters": "Price relative to the last twelve months of earnings.",
            },
            {
                "Group": "Valuation",
                "Indicator": "Forward P/E",
                "Value": format_research_value(company.get("forwardPE"), "ratio"),
                "Why it matters": "Price relative to estimated future earnings; estimates can change.",
            },
            {
                "Group": "Valuation",
                "Indicator": "Price to sales",
                "Value": format_research_value(company.get("priceToSalesTrailing12Months"), "ratio"),
                "Why it matters": "Price relative to revenue; it does not measure profitability.",
            },
        ]
        st.dataframe(
            pd.DataFrame(financial_rows),
            hide_index=True,
            use_container_width=True,
            height=dataframe_height(pd.DataFrame(financial_rows)),
        )
        st.caption(
            "Financial indicators are supplied by Yahoo Finance and may be delayed, estimated, "
            "or unavailable. Confirm important figures in the company's 10-Q or 10-K."
        )
    except Exception as exc:
        st.warning(f"Financial indicators could not be loaded for {research_symbol}: {exc}")

    st.markdown("### FDA drug approval matches")
    try:
        with st.spinner(f"Checking openFDA sponsor records for {research_symbol}…"):
            fda_approvals = get_fda_approval_matches(
                company.get("longName", research_symbol)
            )
        if fda_approvals.empty:
            st.info("No FDA drug approval records matched this company's sponsor name.")
        else:
            st.dataframe(
                fda_approvals,
                hide_index=True,
                use_container_width=True,
                height=dataframe_height(fda_approvals),
            )
            st.caption(
                "Official openFDA Drugs@FDA data, matched by sponsor name. Subsidiaries, acquired "
                "companies, devices, and name variations may be missing; verify each application."
            )
    except HTTPError as exc:
        if exc.code == 404:
            st.info("No FDA drug approval records matched this company's sponsor name.")
        else:
            st.warning(f"FDA approval data could not be loaded right now: {exc}")
    except (URLError, TimeoutError) as exc:
        st.warning(f"FDA approval data could not be loaded right now: {exc}")
    except Exception as exc:
        st.warning(f"FDA approval data could not be processed: {exc}")

    st.markdown("### Recent SEC and insider filings")
    if not sec_contact_email or "@" not in sec_contact_email:
        st.info("Enter a valid SEC contact email above to load official EDGAR filings.")
    else:
        try:
            with st.spinner(f"Checking EDGAR for {research_symbol}…"):
                filings_df = get_sec_filings(research_symbol, sec_contact_email)
            if filings_df.empty:
                st.info("No supported recent SEC filings were found for this symbol.")
            else:
                st.dataframe(
                    filings_df,
                    hide_index=True,
                    use_container_width=True,
                    height=dataframe_height(filings_df),
                    column_config={
                        "Filing": st.column_config.LinkColumn(
                            "Official filing",
                            display_text="Open on SEC.gov",
                        ),
                    },
                )
                if filings_df["Review flag"].astype(bool).any():
                    st.warning(
                        "One or more filings contain an item that deserves careful review. "
                        "A flag identifies the filing topic; it does not determine its impact."
                    )
                st.caption(
                    "Includes Forms 4, 8-K, 10-Q, 10-K, F-1, 6-K, 20-F, and 40-F when available. "
                    "Form 4 reports insider ownership changes; foreign forms serve different purposes."
                )
        except (HTTPError, URLError, TimeoutError) as exc:
            st.warning(f"SEC filings could not be loaded right now: {exc}")
        except Exception as exc:
            st.warning(f"SEC filing data could not be processed: {exc}")

    with st.expander("What the SEC review flags mean"):
        st.write(
            "The app highlights selected 8-K topics: bankruptcy or receivership (1.03), "
            "events affecting financial obligations (2.04), possible delisting (3.01), "
            "auditor changes (4.01), and financial statements that should no longer be "
            "relied upon (4.02). Always open and read the filing because context matters."
        )

with settings_tab:
    if st.query_params.get("focus") == "alpaca":
        st.info(
            "Enter both Alpaca credentials in the Alpaca section below, then choose "
            "Save on this Mac."
        )
    if st.query_params.get("focus") == "sec_email":
        st.info("Enter your SEC contact email below, then choose Save on this Mac.")
    sec_explanation_column, sec_form_column = st.columns([1.35, 1])
    with sec_explanation_column:
        st.markdown("### SEC search access")
        st.write(
            "The SEC asks automated tools to identify themselves with contact information. "
            "The scanner uses this email only in the User-Agent sent with requests to SEC.gov. "
            "It is not used to create an SEC account, subscribe you to email, or contact your friend."
        )
        st.write(
            "A valid email is also required to download the official SEC company directory that "
            "powers the stock-name and ticker search at the top of the app."
        )
        st.caption(
            "Use an email address you monitor. Each person should enter and save their own email "
            "on their own Mac."
        )
    with sec_form_column:
        with st.container(border=True):
            st.markdown("#### SEC contact email")
            sec_contact_email = st.text_input(
                "Email address",
                placeholder="you@example.com",
                help="Saved only in this browser when you choose Save on this Mac.",
                key="sec_contact_email",
            ).strip()
            if cloud_deployment:
                if st.button(
                    "Save on this Mac",
                    key="save_browser_sec_email",
                    use_container_width=True,
                ):
                    if not sec_contact_email or "@" not in sec_contact_email:
                        st.error("Enter a valid email address before saving.")
                    else:
                        try:
                            save_browser_credentials(sec_contact_email=sec_contact_email)
                            st.success("Saved in this browser.")
                            st.query_params.pop("focus", None)
                        except OSError as exc:
                            st.error(str(exc))
            elif st.button(
                "Save email",
                key="save_sec_email_default",
                use_container_width=True,
            ):
                if not sec_contact_email or "@" not in sec_contact_email:
                    st.error("Enter a valid email address before saving.")
                else:
                    try:
                        save_email_preference(sec_contact_email)
                        st.success("SEC email saved as the default.")
                        st.query_params.pop("focus", None)
                    except OSError as exc:
                        st.error(f"The SEC email could not be saved: {exc}")

    st.divider()
    alpaca_instructions_column, alpaca_form_column = st.columns([1.35, 1])
    with alpaca_instructions_column:
        st.markdown("### Alpaca market-data access")
        st.write(
            "The Alpaca key and secret allow the scanner to request live market quotes. "
            "They do not give this app permission to place trades."
        )
        st.markdown(
            """
1. Open the Alpaca sign-in page using the button below.
2. Create a free account, or sign in to an account you already have.
3. Open your **Paper Trading** account. Paper Trading is the safest choice and does not use real money.
4. On the Alpaca dashboard, find **API Keys**. Depending on the dashboard version, it may be on the Home page, in the right sidebar, or under **Manage Accounts**.
5. Choose **Generate New Keys** (or **Regenerate** if keys already exist).
6. Copy the **API Key ID** and paste it into **API key** on this page.
7. Copy the **Secret Key** and paste it into **API secret**. Alpaca normally shows the secret only once. If it is lost, generate a new pair.
8. Leave the market-data feed on **IEX — free** unless the Alpaca account includes paid SIP data.
9. Choose **Save on this Mac**. Repeat these steps separately on each person’s Mac.
            """
        )
        st.link_button(
            "Open Alpaca sign in",
            "https://app.alpaca.markets/account/login",
            use_container_width=True,
        )
        st.caption(
            "Never email, text, or screenshot an API secret. Your friend should create and save "
            "their own keys on their own Mac."
        )
    with alpaca_form_column:
        with st.container(border=True):
            st.markdown("#### Alpaca credentials")
            alpaca_key = st.text_input(
                "API key",
                type="password",
                key="alpaca_api_key",
                help="Saved only in this browser when you choose Save on this Mac.",
            ).strip()
            alpaca_secret = st.text_input(
                "API secret",
                type="password",
                key="alpaca_api_secret",
                help="Saved only in this browser when you choose Save on this Mac.",
            ).strip()
            alpaca_feed_label = st.radio(
                "Market-data feed",
                ["IEX — free, one exchange", "SIP — paid, all U.S. exchanges"],
                key="alpaca_feed",
            )
            if cloud_deployment:
                if st.button(
                    "Save on this Mac",
                    key="save_browser_alpaca",
                    use_container_width=True,
                ):
                    if not alpaca_key or not alpaca_secret:
                        st.error("Enter both Alpaca credentials before saving.")
                    else:
                        try:
                            save_browser_credentials(
                                alpaca_api_key=alpaca_key,
                                alpaca_api_secret=alpaca_secret,
                            )
                            st.success("Saved in this browser.")
                        except OSError as exc:
                            st.error(str(exc))
if errors:
    with st.expander("Symbols with warnings"):
        st.write(errors)
