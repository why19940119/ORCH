"""v0.21.0 phone layout contract (375-430px; iOS Safari).

Measured in headless Chrome with device-width emulation at 375 / 390 / 430
for /chat, / and /inbox as an admin: document.scrollWidth <= innerWidth,
static opaque header, one-row scrolling nav, stacked composer, 16px fields.
These tests pin the CSS that produces that result.
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

    def test_header_static_opaque_nav_scrolls_inside_itself(self):
        block = mobile_block()
        header = rule(block, "header")
        for decl in ("position: static;", "background: #171020;", "backdrop-filter: none;",
                     "-webkit-backdrop-filter: none;", "z-index: auto;"):
            self.assertIn(decl, header)
        nav = rule(block, "header nav")
        for decl in ("flex-wrap: nowrap;", "overflow-x: auto;", "max-width: 100%;"):
            self.assertIn(decl, nav)
        self.assertIn("flex: 0 0 auto;", rule(block, "header nav a"))

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


if __name__ == "__main__":
    unittest.main()
