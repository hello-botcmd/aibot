"""
bot/utils/keyboards.py
──────────────────────
All InlineKeyboardMarkup builders in one place.
Premium-style layout: wide single-column buttons for primary actions,
2×2 grid for the dashboard feature menu.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ── helpers ──────────────────────────────────────────────────────────────────

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, url=url)


# ── Start / Home ─────────────────────────────────────────────────────────────

def start_keyboard(contact_username: str) -> InlineKeyboardMarkup:
    """Two tall CTA buttons stacked vertically."""
    return InlineKeyboardMarkup([
        [_btn("🗂  Dashboard", "dashboard")],
        [_url_btn("📞  Contact", f"https://t.me/{contact_username.lstrip('@')}")],
    ])


# ── Dashboard feature grid (2 × 2 + back row) ────────────────────────────────

def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            _btn("➕  Add Account",     "add_account"),
            _btn("🗃  Manage Accounts", "manage_accounts"),
        ],
        [
            _btn("⚡  Toggle AI",       "toggle_ai"),
            _btn("📊  Stats",           "stats"),
        ],
        [
            _btn("💎  Set Paid Photo",  "set_paid_photo"),
            _btn("🔙  Back",            "back_home"),
        ],
    ])


# ── Account list ─────────────────────────────────────────────────────────────

def accounts_keyboard(accounts: dict) -> InlineKeyboardMarkup:
    """One button per account + back."""
    rows = []
    for sid, acc in accounts.items():
        label = acc.get("name") or acc.get("phone") or sid[:12]
        ai_icon = "🟢" if acc.get("ai_enabled") else "🔴"
        rows.append([_btn(f"{ai_icon}  {label}", f"acc_{sid}")])
    rows.append([_btn("🔙  Back to Dashboard", "dashboard")])
    return InlineKeyboardMarkup(rows)


def account_actions_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            _btn("🗑  Terminate",    f"terminate_{session_id}"),
            _btn("🔙  Back",         "manage_accounts"),
        ],
    ])


# ── Toggle confirmation ──────────────────────────────────────────────────────

def toggle_keyboard(current_state: bool) -> InlineKeyboardMarkup:
    action_label = "🔴  Turn OFF All"  if current_state else "🟢  Turn ON All"
    action_data  = "do_toggle_off"     if current_state else "do_toggle_on"
    return InlineKeyboardMarkup([
        [_btn(action_label, action_data)],
        [_btn("🔙  Back to Dashboard", "dashboard")],
    ])


# ── Paid photo confirm ───────────────────────────────────────────────────────

def paid_photo_account_keyboard(accounts: dict) -> InlineKeyboardMarkup:
    """Pick which account to set the paid photo for."""
    rows = []
    for sid, acc in accounts.items():
        label = acc.get("name") or acc.get("phone") or sid[:12]
        rows.append([_btn(f"📱  {label}", f"paidacc_{sid}")])
    rows.append([_btn("🔙  Cancel", "dashboard")])
    return InlineKeyboardMarkup(rows)


def cancel_keyboard(back_to: str = "dashboard") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("❌  Cancel", back_to)]])
