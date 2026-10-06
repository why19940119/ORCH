"""Collapsed header menu at every width (desktop and phone identical).

The top bar shows only the ORCH title (+ branding), the current page name and
the Menu button. Nav (grouped: console / e-commerce demo / admin), the
language popup (one compact 「繁中 ▾」 button; the three choices still POST to
/locale with csrf_token and next) and the account popup (username; role and a
CSRF Sign out) live in the menu panel. No JavaScript needed: checkbox + label
for the panel, <details> for the popups; the script only syncs aria-expanded
and adds Esc / click-outside. Screenshots at 1280 / 1440 / 375 / 390 / 430
were checked in headless Chromium (opaque panel above content, no horizontal
overflow at 375).
"""

import re
import unittest
from html import unescape

from auth_testing import signed_in, use_temp_auth
from orch_ui import BASE_TEMPLATE, app
from test_mobile_layout_v0210 import MOBILE_MARK, mobile_block, rule, site_menu, stylesheet
from ui_i18n import LOCALE_LABELS, LOCALE_SHORT_LABELS, SUPPORTED_LOCALES, ui_strings

NEW_KEYS = ("menu_nav_aria", "nav_main_group", "lang_menu_prefix", "account_menu_prefix",
            "account_signed_in_as", "account_role")
GROUPS = {
    "main": ["/", "/tasks", "/events", "/artifacts", "/chat"],
    "demo": ["/sales", "/content", "/knowledge", "/leads", "/campaigns", "/market",
             "/inbox", "/audit", "/import"],
    "admin": ["/admin/users", "/admin/approvers", "/admin/retention", "/admin/permissions"],
}


HEADER_MARK = "/* Collapsed header (every width)"


def desktop_css():
    """Top-level header rules (after older header rules, before the phone block)."""
    css = stylesheet()
    block = css[css.index(HEADER_MARK):css.index(MOBILE_MARK)]
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


def rules(block, selector):
    """All declarations of every rule in ``block`` whose selector list has ``selector``."""
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        if selector in [s.strip() for s in match.group(1).split(",")]:
            found.append(match.group(2))
    assert found, selector
    return "\n".join(found)


def header_of(html):
    return html.split("<header", 1)[1].split("</header>", 1)[0]


def top_bar(html):
    """Header markup before the menu panel: what is visible while closed."""
    return header_of(html).split('<div class="site-menu"', 1)[0]


def details(menu, cls):
    return re.search(rf'<details class="menu-pop {cls}[^"]*".*?</details>', menu, re.S).group(0)


class DesktopCollapseCssTests(unittest.TestCase):
    def test_panel_collapsed_at_every_width(self):
        desktop = desktop_css()
        self.assertNotIn("display: contents", stylesheet())   # no always-open desktop nav
        panel = rule(desktop, ".site-menu")
        self.assertIn("display: none;", panel)
        self.assertIn("display: block;", rule(desktop, ".menu-toggle-input:checked ~ .site-menu"))
        # the Menu button and the page name are shown on desktop too
        self.assertIn("display: inline-flex;", rule(desktop, ".menu-toggle"))
        self.assertNotIn("display: none", rule(desktop, ".page-name"))
        # top-level rules, not inside a media query
        self.assertNotIn("@media", desktop)
        self.assertEqual(desktop.count("{"), desktop.count("}"))

    def test_panel_and_popups_opaque_and_above_content(self):
        desktop = desktop_css()
        header = rule(desktop, "header")
        self.assertIn("flex-wrap: nowrap;", header)            # one row, never ~3 rows
        header_z = int(re.search(r"z-index: (\d+);", header).group(1))
        self.assertGreater(header_z, 50)                       # above the sticky chat composer (50)
        for selector in (".site-menu", ".pop-panel"):
            body = rule(desktop, selector)
            self.assertIn("position: absolute;", body)
            self.assertRegex(body, r"background: #[0-9a-f]{6};")   # solid colour, no alpha
            self.assertRegex(body, r"z-index: \d+;")
        self.assertIn("max-width: calc(100% - 24px);", rule(desktop, ".site-menu"))
        # popups open inside the panel edges: language left, account right
        self.assertIn("left: 0;", rule(desktop, ".lang-pop .pop-panel"))
        self.assertIn("right: 0;", rule(desktop, ".user-pop .pop-panel"))
        self.assertIn("margin-left: auto;", rule(desktop, ".user-pop"))

    def test_focus_visible_outlines(self):
        desktop = desktop_css()
        for selector in (".menu-toggle:focus-visible", ".menu-pop > summary:focus-visible",
                         ".site-menu a:focus-visible", ".pop-item:focus-visible"):
            self.assertIn("outline: 2px solid", rule(desktop, selector))
        self.assertIn("list-style: none;", rules(desktop, ".menu-pop > summary"))

    def test_no_fixed_widths_that_overflow_phones(self):
        block = mobile_block()
        self.assertIn("width: auto;", rule(block, ".site-menu"))
        css = stylesheet()
        self.assertNotRegex(css, r":[^;{}]*\b100vw\b")


class HeaderMarkupTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def page(self, path="/chat", locale=None):
        if locale:
            with self.client.session_transaction() as stored:
                stored["locale"] = locale
        return self.client.get(path).get_data(as_text=True)

    def test_top_bar_has_only_title_page_name_and_menu_button(self):
        signed_in(self, self.client)
        bar = top_bar(self.page("/admin/users"))
        self.assertIn('class="brand-title"', bar)
        self.assertIn("data-page-name", bar)
        self.assertIn("data-menu-button", bar)
        for absent in ("<a ", "<form", "<button", "<details", "<nav", 'action="/locale"',
                       'action="/logout"', "data-current-user"):
            self.assertNotIn(absent, bar)

    def test_menu_groups_with_headings_in_order(self):
        signed_in(self, self.client)
        menu = site_menu(self.page("/chat"))
        nav = menu.split("<nav", 1)[1].split("</nav>", 1)[0]
        t = ui_strings("zh-Hant")
        headings = {"main": t["nav_main_group"], "demo": t["nav_demo_group"],
                    "admin": t["nav_admin_group"]}
        positions = []
        for group, hrefs in GROUPS.items():
            block = re.search(rf'<div class="nav-group nav-group-{group}" role="group" '
                              rf'aria-labelledby="nav-group-{group}".*?</div>\s*</div>', nav, re.S).group(0)
            self.assertIn(f'<span class="nav-group-label" id="nav-group-{group}">{headings[group]}</span>',
                          block)
            self.assertEqual(re.findall(r'<a href="([^"]+)"', block), hrefs)
            positions.append(nav.index(f"nav-group-{group}"))
        self.assertEqual(positions, sorted(positions))
        self.assertIn(f'aria-label="{t["menu_nav_aria"]}"', menu)
        # active page: highlighted and aria-current
        self.assertRegex(nav, r'<a href="/chat" class="active" aria-current="page">')
        self.assertEqual(nav.count('aria-current="page"'), 1)

    def test_admin_group_only_for_admins(self):
        signed_in(self, self.client, username="Eve Editor", role="editor")
        menu = site_menu(self.page("/chat"))
        self.assertNotIn("nav-group-admin", menu)
        self.assertNotIn('href="/admin/users"', menu)
        self.assertIn("nav-group-demo", menu)

    def test_panel_aria(self):
        signed_in(self, self.client)
        html = self.page()
        panel = re.search(r'<div class="site-menu"[^>]*>', html).group(0)
        for attr in ('id="site-menu"', 'role="region"', 'aria-label="選單"'):
            self.assertIn(attr, panel)
        button = re.search(r"<label[^>]*data-menu-button[^>]*>", html, re.S).group(0)
        self.assertIn('aria-controls="site-menu"', button)
        self.assertIn('aria-expanded="false"', button)

    def test_language_is_one_compact_button_per_locale(self):
        signed_in(self, self.client)
        for code in SUPPORTED_LOCALES:
            t = ui_strings(code)
            menu = site_menu(self.page("/inbox", code))
            lang = details(menu, "lang-pop")
            self.assertEqual(menu.count("<details class=\"menu-pop lang-pop"), 1)
            summary = re.search(r"<summary[^>]*>(.*?)</summary>", lang, re.S).group(1)
            visible = re.sub(r'<span class="visually-hidden">.*?</span>', "", summary)
            visible = re.sub(r"<[^>]+>", "", visible)
            self.assertEqual(" ".join(visible.split()), f"{LOCALE_SHORT_LABELS[code]} ▾")
            self.assertIn(f'<span class="visually-hidden">{t["lang_menu_prefix"]}</span>', summary)
            self.assertIn('aria-controls="lang-menu-panel"', lang)
            self.assertIn(f'id="lang-menu-panel" role="group" aria-label="{t["lang_label"]}"', lang)
            # the three choices, each its own CSRF POST to /locale with next
            forms = re.findall(r'<form method="post" action="/locale">.*?</form>', lang, re.S)
            self.assertEqual(len(forms), 3)
            for form, choice in zip(forms, SUPPORTED_LOCALES):
                self.assertRegex(form, r'name="csrf_token" value="[^"]+"')
                self.assertIn(f'name="locale" value="{choice}"', form)
                self.assertIn('name="next" value="/inbox"', form)
                self.assertIn(f'lang="{choice}"', form)
                self.assertIn(LOCALE_LABELS[choice], form)
                self.assertEqual('aria-current="true"' in form, choice == code)

    def test_language_form_still_switches_locale(self):
        signed_in(self, self.client)
        lang = details(site_menu(self.page("/tasks")), "lang-pop")
        form = re.findall(r'<form method="post" action="/locale">.*?</form>', lang, re.S)[2]
        fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', form))
        self.assertEqual(fields["locale"], "en")
        response = self.client.post("/locale", data={k: unescape(v) for k, v in fields.items()})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/tasks"))
        html = self.client.get("/tasks").get_data(as_text=True)
        self.assertIn('<span data-lang-current>EN</span>', html)
        self.assertIn('<html lang="en">', html)
        # without the token it is still refused
        self.assertEqual(self.client.post("/locale", data={"locale": "zh-Hans", "next": "/"}).status_code, 400)

    def test_account_popup_top_right_with_role_and_csrf_logout(self):
        signed_in(self, self.client)
        t = ui_strings("zh-Hant")
        menu = site_menu(self.page())
        head = menu.split('<div class="menu-head">', 1)[1].split("<nav", 1)[0]
        self.assertLess(head.index("lang-pop"), head.index("user-pop"))   # account after = right
        user = details(menu, "user-pop")
        self.assertIn('data-current-user="Test Admin"', user)
        summary = re.search(r"<summary[^>]*>(.*?)</summary>", user, re.S).group(1)
        self.assertIn('<span class="user-name">Test Admin</span>', summary)
        self.assertIn(f'<span class="visually-hidden">{t["account_menu_prefix"]}</span>', summary)
        self.assertIn("▾", summary)
        self.assertNotIn(t["role_admin"], summary)               # role only in the popup
        panel = user.split("</summary>", 1)[1]
        self.assertIn(f'{t["account_role"]}<strong>{t["role_admin"]}</strong>', panel)
        logout = re.search(r'<form method="post" action="/logout">.*?</form>', panel, re.S).group(0)
        self.assertRegex(logout, r'name="csrf_token" value="[^"]+"')
        self.assertIn(t["auth_logout"], logout)

    def test_logged_out_login_page_has_language_control(self):
        use_temp_auth(self)
        html = self.page("/login")
        self.assertIn("data-menu-button", top_bar(html))
        menu = site_menu(html)
        lang = details(menu, "lang-pop")
        self.assertEqual(len(re.findall(r'action="/locale"', lang)), 3)
        self.assertNotIn("user-pop", menu)
        self.assertNotIn("<nav", menu)

    def test_setup_page_has_language_control(self):
        use_temp_auth(self, users=())
        response = self.client.get("/setup")
        self.assertEqual(response.status_code, 503)
        menu = site_menu(response.get_data(as_text=True))
        self.assertEqual(len(re.findall(r'action="/locale"', details(menu, "lang-pop"))), 3)
        self.assertNotIn("<nav", menu)


class HeaderStringsAndScriptTests(unittest.TestCase):
    def test_new_strings_in_every_locale(self):
        for code in SUPPORTED_LOCALES:
            t = ui_strings(code)
            for key in NEW_KEYS:
                self.assertTrue(t.get(key, "").strip(), (code, key))
        self.assertEqual(LOCALE_SHORT_LABELS, {"zh-Hant": "繁中", "zh-Hans": "簡中", "en": "EN"})
        self.assertEqual(ui_strings("zh-Hant")["nav_admin_group"], "管理")
        self.assertEqual(ui_strings("zh-Hant")["auth_logout"], "登出")

    def test_script_is_enhancement_only(self):
        script = BASE_TEMPLATE.split("</footer>", 1)[1]
        # one popup at a time, Esc closes the innermost popup then the menu,
        # click outside closes; no inline handlers in the header markup
        for needle in ('pop.addEventListener("toggle"', "closePops(pop)",
                       'event.key !== "Escape"', 'open.querySelector("summary").focus()',
                       "button.focus()", 'document.addEventListener("click"'):
            self.assertIn(needle, script)
        header = BASE_TEMPLATE.split("<header>", 1)[1].split("</header>", 1)[0]
        self.assertNotRegex(header, r"\son[a-z]+=")


if __name__ == "__main__":
    unittest.main()
