from pathlib import Path


APP_TSX = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"


def _nav_items_block() -> str:
    source = APP_TSX.read_text(encoding="utf-8")
    start = source.index("const NAV_ITEMS: NavItem[] = [")
    end = source.index("];", start)
    return source[start:end]


def test_sidebar_top_level_nav_keeps_public_entries():
    block = _nav_items_block()

    assert block.count("path:") == 7
    assert 'path: "/"' in block
    assert 'labelKey: "nav.dashboard"' in block
    assert 'path: "/accounts/chatgpt"' in block
    assert 'labelKey: "nav.chatgptAccounts"' in block
    assert 'path: "/kakao-pipeline"' in block
    assert 'label: "Kakao 流水线"' in block
    assert 'path: "/mailboxes"' in block
    assert 'labelKey: "nav.mailboxResources"' in block
    assert 'path: "/tasks"' in block
    assert 'labelKey: "nav.tasks"' in block
    assert 'path: "/workflows"' in block
    assert 'labelKey: "nav.workflows"' in block
    assert 'path: "/settings"' in block
    assert 'labelKey: "nav.settings"' in block


def test_sidebar_hides_accounts_menu_and_other_business_links():
    source = APP_TSX.read_text(encoding="utf-8")

    assert "setAccountsOpen" not in source
    assert "getPlatforms" not in source
    assert "nav.accounts" not in source
    assert "nav.ctfGptPlus" not in source
    assert "nav.gopayGptPlus" not in source
    assert "nav.plusManager" not in source


def test_sidebar_keeps_general_mailbox_sms_proxy_and_push_settings_submenu_items():
    source = APP_TSX.read_text(encoding="utf-8")

    start = source.index("const SETTINGS_NAV_ITEMS:")
    end = source.index("];", start)
    block = source[start:end]

    assert block.count('hash: "') == 5
    assert 'labelKey: "nav.settings.general", hash: "general"' in block
    assert 'labelKey: "nav.settings.mailbox", hash: "mailbox"' in block
    assert 'labelKey: "nav.settings.sms", hash: "sms"' in block
    assert 'labelKey: "nav.settings.proxy", hash: "proxy"' in block
    assert 'labelKey: "nav.settings.push", hash: "push"' in block

    assert "currentTab" in source
    assert "/settings?tab=${item.hash}" in source
