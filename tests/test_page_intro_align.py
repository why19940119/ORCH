"""Page intro alignment (PR #9 follow-up).

At <=720px the /chat intro (eyebrow, heading, subtitle, "Advisory only"
pill) drifted toward the centre (~84px indent at 520px, ~184px at 720px)
while the history, composer and footer stayed left-aligned. Cause: the
narrow query stacked .chat-hero with flex-direction: column, but a later
top-level rule set align-items: center (meant to vertically centre the pill
next to the title in the desktop row). In a column flexbox align-items is the
horizontal axis, so the shrink-wrapped title block and the pill were centred.

These tests replay the cascade of the inline stylesheet for the intro
selectors (top-level rules plus @media (max-width: 720px) rules, in source
order) and pin the result: stacked + flex-start on narrow screens, the
unchanged row layout on desktop, and nothing that centres an intro.
"""

import re
import unittest

from auth_testing import signed_in
from orch_ui import BASE_TEMPLATE, app

INTROS = (".chat-hero", ".module-hero")
NARROW = "(max-width: 720px)"
CENTERING = (
    re.compile(r"^text-align:\s*center"),
    re.compile(r"^margin(-left|-right|-inline)?:[^;]*\bauto\b"),
    re.compile(r"^(justify-content|place-items|place-content|justify-items|justify-self|place-self):\s*center"),
)


def stylesheet():
    css = BASE_TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def rules(css):
    """(media, selectors, declarations) for every rule, in source order.

    ``media`` is the @media prelude for rules nested in a media query, else None.
    """
    out, media, i = [], None, 0
    while True:
        brace = css.find("{", i)
        if brace < 0:
            break
        close = css.find("}", i)
        if media is not None and 0 <= close < brace:   # end of the media block
            media, i = None, close + 1
            continue
        prelude = css[i:brace].strip()
        if prelude.startswith("@media"):
            media, i = prelude[len("@media"):].strip(), brace + 1
            continue
        end = css.index("}", brace)
        decls = [d.strip() for d in css[brace + 1:end].split(";") if d.strip()]
        out.append((media, [s.strip() for s in prelude.split(",")], decls))
        i = end + 1
    return out


def computed(selector, narrow):
    """Last declared value per property for ``selector`` (exact selector match)."""
    props = {}
    for media, selectors, decls in rules(stylesheet()):
        if selector not in selectors:
            continue
        if media is not None and not (narrow and media == NARROW):
            continue
        for decl in decls:
            name, _, value = decl.partition(":")
            props[name.strip()] = value.strip()
    return props


class PageIntroCssTests(unittest.TestCase):
    def test_parser_sees_media_rules(self):
        parsed = rules(stylesheet())
        self.assertTrue(any(m == NARROW for m, _, _ in parsed))
        self.assertTrue(any(m is None and "main" in s for m, s, _ in parsed))

    def test_narrow_intro_is_stacked_and_left_aligned(self):
        for selector in INTROS:
            with self.subTest(selector=selector):
                props = computed(selector, narrow=True)
                self.assertEqual(props.get("display"), "flex")
                self.assertEqual(props.get("flex-direction"), "column")
                # column flex: align-items is horizontal - must not centre
                self.assertEqual(props.get("align-items"), "flex-start")
                self.assertEqual(props.get("justify-content"), "flex-start")
                self.assertEqual(props.get("text-align"), "left")

    def test_one_shared_narrow_rule_for_every_intro(self):
        narrow = [(s, d) for m, s, d in rules(stylesheet()) if m == NARROW
                  and any(i in s for i in INTROS)]
        self.assertEqual(len(narrow), 1, narrow)
        selectors, decls = narrow[0]
        self.assertEqual(sorted(selectors), sorted(INTROS))
        self.assertIn("align-items: flex-start", decls)
        self.assertIn("flex-direction: column", decls)

    def test_desktop_row_layout_unchanged(self):
        chat = computed(".chat-hero", narrow=False)
        self.assertNotEqual(chat.get("flex-direction"), "column")
        self.assertEqual(chat.get("justify-content"), "space-between")
        self.assertEqual(chat.get("align-items"), "center")      # pill centred vertically
        module = computed(".module-hero", narrow=False)
        self.assertNotEqual(module.get("flex-direction"), "column")
        self.assertEqual(module.get("justify-content"), "space-between")

    def test_nothing_centres_an_intro_or_its_children(self):
        for media, selectors, decls in rules(stylesheet()):
            hits = [s for s in selectors if any(s == i or s.startswith(i + " ") for i in INTROS)]
            if not hits:
                continue
            for decl in decls:
                for pattern in CENTERING:
                    self.assertIsNone(pattern.search(decl), (media, hits, decl))

    def test_page_containers_are_not_centred_inside_main_on_phones(self):
        # main / .chat-page may centre a max-width column on wide screens; the
        # intro sits inside the same column as the content, so both share its
        # left edge. On phones that column is the full width.
        chat_page = computed(".chat-page", narrow=True)
        self.assertEqual(chat_page.get("width"), "100%")


class PageIntroMarkupTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        signed_in(self, self.client)

    def test_intro_is_first_in_the_same_column_as_the_content(self):
        html = self.client.get("/chat").get_data(as_text=True)
        page = html.split('<div class="chat-page">', 1)[1]
        self.assertTrue(page.lstrip().startswith('<div class="chat-hero">'))
        self.assertLess(page.index("chat-hero"), page.index('id="chat-form"'))
        html = self.client.get("/campaigns").get_data(as_text=True)
        main = html.split("<main", 1)[1]
        self.assertLess(main.index('class="module-hero"'), main.index('class="section"'))
