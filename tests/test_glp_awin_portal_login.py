"""Focused Awin browser/vault handoff tests; no live credentials or browser."""
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "awin_login", Path(__file__).resolve().parents[1] / "scripts/glp_awin_portal_login.py")
assert SPEC is not None and SPEC.loader is not None
login = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(login)


class FakePage:
    def __init__(self):
        self.url = login.PRELOGIN + "?redirect=test"
        self.identifier = False
        self.password = False
        self.clicks = []
        self.body = "Tilton Group (2979387) Advertisers Results (1000+)"

    def page_info(self):
        return {"url": self.url}

    def js(self, expression):
        if "getBoundingClientRect" in expression:
            return {"visible": True, "x": 20, "y": 30}
        if "value===" in expression:
            return self.identifier
        if "value.length" in expression:
            return self.password
        if "button#login').click()" in expression:
            self.url = "https://id.awin.com/u/login/password"
        if "button[type='submit']" in expression and ".click()" in expression:
            self.clicks.append("submit")
            self.url = "https://ui.awin.com/dashboard/awin/publisher/2979387/us"
        if "body?.innerText" in expression:
            return self.body
        return None

    def click(self, x, y):
        self.clicks.append((x, y))
        if len(self.clicks) == 2:
            self.url = login.PASSWORD_ORIGIN + "/u/login/password"

    def cdp(self, method, **kwargs):
        assert method == "Input.insertText"
        assert kwargs == {"text": login.IDENTIFIER}
        self.identifier = True

    def goto(self, url):
        self.url = url

    def wait(self):
        pass


class CallerTest(unittest.TestCase):
    def test_vault_boundary_and_publisher_readback(self):
        page = FakePage()
        prepared = login.prepare(page.page_info, page.js, page.click, page.cdp, page.wait)
        self.assertEqual(prepared["stage"], "vault_fill_required")
        with self.assertRaisesRegex(login.LoginBoundaryError, "vault_fill_not_observed"):
            login.finish(page.page_info, page.js, page.wait, page.goto)
        self.assertNotIn("submit", page.clicks)
        page.password = True  # mocked vault boundary; never a secret value
        result = login.finish(page.page_info, page.js, page.wait, page.goto)
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["publisher_id"], "2979387")
        self.assertEqual(result["provider_writes"], 0)

    def test_wrong_origin_refuses_before_input(self):
        page = FakePage()
        page.url = "https://evil.example/prelogin"
        with self.assertRaisesRegex(login.LoginBoundaryError, "expected_ui_awin_prelogin"):
            login.prepare(page.page_info, page.js, page.click, page.cdp, page.wait)
        self.assertEqual(page.clicks, [])

    def test_wrong_publisher_refuses(self):
        page = FakePage()
        login.prepare(page.page_info, page.js, page.click, page.cdp, page.wait)
        page.password = True
        page.body = "Other Group (12345)"
        with self.assertRaisesRegex(login.LoginBoundaryError, "publisher_identity_not_verified"):
            login.finish(page.page_info, page.js, page.wait, page.goto)


if __name__ == "__main__":
    unittest.main()
