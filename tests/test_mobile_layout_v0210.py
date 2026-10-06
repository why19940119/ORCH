"""v0.21.0 phone layout contract (375-430px; iOS Safari).

Measured in headless Chrome with device-width emulation at 375 / 390 / 430
for /chat, / and /inbox as an admin: document.scrollWidth <= innerWidth,
static opaque header, stacked composer, 16px fields.
The header is a compact bar (ORCH, current page, Menu button); nav, language
picker and the user line / Sign out sit in a collapsible menu that works
without JavaScript (checkbox + label), and desktop (>720px) is unchanged.
These tests pin the CSS and markup that produce that result.
"""

import re
import unittest

from auth_testing import signed_in
from orch_ui import BASE_TEMPLATE, app

MOBILE_MARK = "/* v0.21.0 mobile layout"


def stylesheet():
    return BASE_TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]


def mobile_block():
    css = stylesheet()
    start = css.index(MOBILE_MARK)
    return re.sub(r"/\*.*?\*/", "", css[start:], flags=re.S)   # declarations only


def rule(block, selector):
    """Body of the first rule in ``block`` whose selector list contains ``selector``."""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    raise AssertionError(f"no rule for {selector!r}")


class MobileCssContractTests(unittest.TestCase):
    def test_viewport_meta(self):
        self.assertRegex(BASE_TEMPLATE, r'name="viewport"\s+content="width=device-width, initial-scale=1"')

    def test_mobile_query_is_last_and_covers_phones(self):
        css = stylesheet()
        block = mobile_block()
        self.assertIn("@media (max-width: 720px) {", block)        # 375-430px phones included
        self.assertEqual(css.rstrip()[-1], "}")
        # nothing after the mobile block can override it
        self.assertEqual(block.count("@media"), 1)
        for later in ("position: sticky", "rgba(23, 16, 32"):
            self.assertNotIn(later, block)

    def test_header_static_opaque_nav_wraps_inside_menu(self):
        block = mobile_block()
        header = rule(block, "header")
        for decl in ("position: static;", "background: #171020;", "backdrop-filter: none;",
                     "-webkit-backdrop-filter: none;", "z-index: auto;"):
            self.assertIn(decl, header)
        nav = rule(block, "header nav")
        for decl in ("flex-wrap: wrap;", "overflow: visible;", "max-width: 100%;"):
            self.assertIn(decl, nav)

    def test_composer_stacked_and_not_overlapping(self):
        block = mobile_block()
        composer = rule(block, ".chat-page .chat-composer")
        self.assertIn("position: static;", composer)
        self.assertIn("z-index: auto;", composer)
        grid = rule(block, ".chat-page .composer-grid")
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto;", grid)
        textarea = rule(block, ".chat-page .composer-grid textarea")
        for decl in ("grid-column: 1 / -1;", "order: -1;", "min-height: 120px;", "width: 100%;"):
            self.assertIn(decl, textarea)
        self.assertIn("flex-wrap: wrap;", rule(block, ".chat-page .composer-tools"))
        self.assertIn("width: 100%;", rule(block, ".chat-page p.chat-data-source"))

    def test_ios_no_zoom_and_no_viewport_units(self):
        block = mobile_block()
        self.assertIn("font-size: 16px;", rule(block, ".chat-page textarea"))
        self.assertIn("font-size: 16px;", rule(block, "input"))
        css = stylesheet()
        self.assertNotRegex(css, r":[^;{}]*\b100vw\b")
        self.assertNotIn("overflow-x: hidden", block)   # overflow is fixed, not clipped

    def test_rendered_chat_order(self):
        client = app.test_client()
        app.config["TESTING"] = True
        signed_in(self, client)
        html = client.get("/chat").get_data(as_text=True)
        self.assertIn('name="viewport"', html)
        self.assertIn('href="/admin/users"', html)                 # admin nav rendered
        form = html.split('id="chat-form"', 1)[1]
        # data-source line sits above the grid, so it is always full width
        self.assertLess(form.index("chat-data-source"), form.index('class="composer-grid"'))
        self.assertIn(MOBILE_MARK, html)


def desktop_css():
    css = stylesheet()
    return re.sub(r"/\*.*?\*/", "", css[:css.index(MOBILE_MARK)], flags=re.S)


MENU_LABELS = {"en": "Menu", "zh-Hant": "選單", "zh-Hans": "菜单"}


class MobileMenuTests(unittest.TestCase):
    """v0.21.0 collapsible phone menu (WP-ORCH-14)."""

    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        signed_in(self, self.client)

    def page(self, path="/chat", locale=None):
        if locale:
            with self.client.session_transaction() as stored:
                stored["locale"] = locale
        return self.client.get(path).get_data(as_text=True)

    def test_toggle_markup_and_aria(self):
        html = self.page()
        toggle = re.search(r"<input[^>]*data-menu-toggle[^>]*>", html, re.S).group(0)
        for attr in ('type="checkbox"', 'id="site-menu-toggle"', 'autocomplete="off"',
                     'tabindex="-1"', 'aria-hidden="true"'):
            self.assertIn(attr, toggle)
        self.assertNotIn("checked", toggle)                       # collapsed by default
        # v0.21.1: aria-expanded / aria-controls live on the label acting as the control
        self.assertNotIn("aria-expanded", toggle)
        self.assertNotIn("aria-controls", toggle)
        button = re.search(r"<label[^>]*data-menu-button[^>]*>", html, re.S).group(0)
        for attr in ('for="site-menu-toggle"', 'class="menu-toggle"', 'role="button"',
                     'tabindex="0"', 'aria-controls="site-menu"', 'aria-expanded="false"',
                     'aria-label="'):
            self.assertIn(attr, button)
        self.assertNotIn("aria-hidden", button)
        self.assertIn('id="site-menu" data-site-menu', html)
        # input -> label -> menu are siblings in that order (CSS uses + and ~)
        header = html.split("<header", 1)[1].split("</header>", 1)[0]
        self.assertLess(header.index("data-menu-toggle"), header.index("data-menu-button"))
        self.assertLess(header.index("data-menu-button"), header.index("data-site-menu"))

    def test_labels_localized_in_every_locale(self):
        from ui_i18n import ui_strings
        for locale, label in MENU_LABELS.items():
            t = ui_strings(locale)
            self.assertEqual(t["menu_label"], label)
            html = self.page("/inbox", locale)
            header = html.split("<header", 1)[1].split("</header>", 1)[0]
            self.assertIn(f"<span>{label}</span>", header)
            self.assertIn(f'aria-label="{t["menu_toggle_aria"]}"', header)
            self.assertIn(f'aria-label="{t["menu_current_page"]}"', header)
            # current page name is the localized page title
            title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
            name = re.search(r"data-mobile-page-name[^>]*>(.*?)</span>", header, re.S).group(1)
            self.assertIn(name.strip(), title)

    def test_menu_holds_nav_language_and_logout_with_csrf(self):
        html = self.page()
        menu = html.split('data-site-menu>', 1)[1].split("</header>", 1)[0]
        self.assertIn("<nav>", menu)
        self.assertIn('href="/admin/users"', menu)
        self.assertIn('action="/locale"', menu)
        logout = re.search(r'<form method="post" action="/logout">.*?</form>', menu, re.S).group(0)
        self.assertRegex(logout, r'name="csrf_token" value="[^"]+"')
        self.assertIn('type="submit"', logout)

    def test_mobile_css_collapses_menu_until_checked(self):
        block = mobile_block()
        self.assertIn("display: none;", rule(block, ".site-menu"))
        opened = rule(block, ".menu-toggle-input:checked ~ .site-menu")
        self.assertIn("display: flex;", opened)
        self.assertIn("display: inline-flex;", rule(block, ".menu-toggle"))
        self.assertIn("display: block;", rule(block, ".mobile-page-name"))
        hidden_input = rule(block, ".menu-toggle-input")
        # visually hidden but still focusable (no display: none)
        for decl in ("position: absolute;", "opacity: 0;", "display: block;"):
            self.assertIn(decl, hidden_input)
        self.assertIn("outline:", rule(block, ".menu-toggle:focus-visible"))   # the control
        self.assertIn("outline:", rule(block, ".menu-toggle-input:focus-visible + .menu-toggle"))

    def test_desktop_keeps_nav_visible_and_hides_toggle(self):
        desktop = desktop_css()
        self.assertIn("display: contents;", rule(desktop, ".site-menu"))
        for selector in (".mobile-page-name", ".menu-toggle", ".menu-toggle-input"):
            self.assertIn("display: none;", rule(desktop, selector))
        # those desktop rules are top level, not inside a media query
        top = desktop.split(".site-menu {", 1)[0]
        self.assertEqual(top.count("{") - top.count("}"), 0)

    def test_js_syncs_aria_and_closes_on_link(self):
        script = BASE_TEMPLATE.split("</footer>", 1)[1]
        for needle in ('getElementById("site-menu-toggle")', 'setAttribute("aria-expanded"',
                       'closest("a")', '"Escape"', '"pageshow"'):
            self.assertIn(needle, script)
        # v0.21.1: the sync targets the label/button, which is also keyboard operable
        self.assertIn('querySelector("[data-menu-button]")', script)
        self.assertIn('button.setAttribute("aria-expanded"', script)
        self.assertNotIn('toggle.setAttribute("aria-expanded"', script)
        self.assertIn('event.key === "Enter" || event.key === " "', script)

    def test_logged_out_pages_keep_language_menu(self):
        client = app.test_client()
        html = client.get("/login", follow_redirects=True).get_data(as_text=True)
        self.assertIn("data-menu-toggle", html)
        menu = html.split('data-site-menu>', 1)[1].split("</header>", 1)[0]
        self.assertIn('action="/locale"', menu)
        self.assertNotIn("<nav>", menu)


if __name__ == "__main__":
    unittest.main()
