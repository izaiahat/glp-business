"""Focused default CLI regressions; all account/Drive custody is synthetic."""
import copy
import json
import unittest
from tests import test_gpt6pro_relay_binding as binding
from tests import test_pro_current_dom as dom

relay = binding.relay


class TerminalModeReceiptTests(unittest.TestCase):
    def emitted(self):
        sender = dom.TestCurrentReaderTransport()
        sender.setUp()
        try:
            tree = dom.fixture('chat_mode')
            for n in dom.walk(tree):
                if n.get('tag') == 'button':
                    n['attrs']['aria-pressed'] = str(n['children'][0]['text'] == 'Work').lower()
            original = sender.browser.js
            expression = '(()=>{' + relay.PRO_CHAT_SURFACE_JS + 'return ensureProChatSurface();})()'
            seen = []
            def fake_js(expr):
                if expr != expression:
                    return original(expr)
                value = dom.evaluate(tree, expr)
                seen.append(copy.deepcopy(value))
                for n in dom.walk(tree):
                    if n.get('tag') == 'button':
                        n['attrs']['aria-pressed'] = str(n['children'][0]['text'] == 'Chat').lower()
                return value
            sender.browser.js = fake_js
            receipt = relay.send_message('offline terminal compatibility fixture')
            self.assertEqual(receipt['mode_evidence'], seen[0])
            self.assertEqual(dom.mode(tree), {'switched': False})
            self.assertIs(receipt['typed'], False)
            self.assertIs(receipt['sent'], False)
            self.assertEqual(sender.browser.inserts, [])
            self.assertEqual(sender.browser.sent, [])
            receipt['tab'] = 'attributed-target'  # Synthetic custody target only.
            return receipt
        finally:
            sender.doCleanups()

    def cli(self, receipt, valid=True):
        case = binding.BindingTests()
        case.setUp()
        try:
            result = case._proof_bound_preinsert_terminal_actual_cli(
                'CHAT_SURFACE_REFUSED', error_receipt=receipt,
                default_only=True, expect_valid=valid)
            print(json.dumps({'variant_valid': valid, 'result': result}, sort_keys=True))
        finally:
            case.doCleanups()

    def test_actual_emitted_current_and_legacy_default_cli(self):
        receipt = self.emitted()
        self.cli(receipt)
        self.cli({k: v for k, v in receipt.items() if k not in ('mode_observation', 'mode_evidence')})
        no_diagnostics = copy.deepcopy(receipt)
        no_diagnostics['mode_evidence'].pop('diagnostics')
        self.cli(no_diagnostics)
        final_observation = copy.deepcopy(receipt)
        final_observation['mode_observation'] = 11
        self.cli(final_observation)

    def test_all_recognized_emitted_diagnostics_default_cli(self):
        trees = [({'tag': 'div', 'children': []}, 'mode_missing'),
                 ({'tag': 'main', 'children': []}, 'mode_missing')]
        duplicate = dom.fixture('chat_mode')
        duplicate['children'] *= 2
        trees.append((duplicate, 'mode_ambiguous'))
        unverified = dom.fixture('chat_mode')
        for n in dom.walk(unverified):
            if n.get('tag') == 'button':
                n['attrs']['aria-pressed'] = 'mixed'
        trees.append((unverified, 'mode_unverified'))
        overflow = dom.fixture('chat_mode')
        overflow['children'][0]['children'] *= 12
        trees.append((overflow, 'mode_ambiguous'))
        expression = '(()=>{' + relay.PRO_CHAT_SURFACE_JS + 'return ensureProChatSurface();})()'
        for tree, reason in trees:
            with self.subTest(reason=reason):
                evidence = dom.evaluate(tree, expression)
                self.assertEqual(evidence['reason'], reason)
                self.cli({'error': 'CHAT_SURFACE_REFUSED', 'typed': False, 'sent': False,
                          'tab': 'attributed-target', 'mode_observation': 1, 'mode_evidence': evidence})

    def test_default_cli_rejects_malformed_partial_and_success(self):
        current = self.emitted()
        variants = []
        def change(path, value):
            r = copy.deepcopy(current)
            node = r
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            variants.append((str(path) + '=' + repr(value), r))
        for key in ('typed', 'sent'):
            for value in (0, True, 'UNKNOWN', None):
                change((key,), value)
        for value in (True, 0, 12, 1.0, '1', None):
            change(('mode_observation',), value)
        for key in ('tab', 'mode_evidence', 'mode_observation'):
            r = copy.deepcopy(current)
            r.pop(key)
            variants.append(('missing ' + key, r))
        change(('tab',), 'other-target')
        change(('mode_evidence',), {'switched': False})
        change(('mode_evidence',), None)
        change(('mode_evidence', 'reason'), 'unknown')
        change(('mode_evidence', 'error'), 'SUCCESS')
        change(('mode_evidence', 'diagnostics'), {})
        change(('mode_evidence', 'diagnostics', 'main_count'), True)
        change(('mode_evidence', 'diagnostics', 'group_count'), -1)
        change(('mode_evidence', 'diagnostics', 'scope'), 'other')
        change(('mode_evidence', 'diagnostics', 'chat', 'raw_count'), 1.0)
        change(('mode_evidence', 'diagnostics', 'chat', 'overflow'), True)
        change(('mode_evidence', 'diagnostics', 'chat', 'candidates'), [{}] * 9)
        change(('mode_evidence', 'diagnostics', 'chat', 'candidates', 0, 'pressed'), 'private')
        change(('mode_evidence', 'diagnostics', 'chat', 'candidates', 0, 'excluded'), 'unknown')
        positive = copy.deepcopy(current)
        positive['mode_evidence']['diagnostics']['chat']['candidates'][0]['pressed'] = 'true'
        positive['mode_evidence']['diagnostics']['work']['candidates'][0]['pressed'] = 'false'
        variants.append(('success-shaped diagnostics', positive))
        extra = copy.deepcopy(current)
        extra['state'] = {'stop': False}
        variants.append(('unrecognized field', extra))
        for name, receipt in variants:
            with self.subTest(name=name):
                self.cli(receipt, valid=False)


if __name__ == '__main__':
    unittest.main()
