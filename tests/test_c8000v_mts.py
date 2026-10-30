"""Сценарии для C8000V + МТС (IP-аутентификация) + CUCM."""

import unittest

from ios_model import REPO_ROOT, Config, SipMessage, SipProfile

CFG = Config(REPO_ROOT / "c8000v-cube-mts-cucm.cfg")

MTS = ("198.51.100.10", "198.51.100.11")
WAN_IP = "203.0.113.10"
LAN_IP = "10.10.10.1"
CUCM = ("10.10.20.11", "10.10.20.12")


class TestInboundFromMTS(unittest.TestCase):
    def test_both_sbc_routed_to_cucm(self):
        for sbc in MTS:
            with self.subTest(sbc=sbc):
                r = CFG.route_call(f"sip:74951234567@{WAN_IP}:5060", sbc, "89161234567")
                self.assertEqual((r["inbound"], r["outbound"]), ("101", "200"))
                self.assertEqual(r["called"], "+74951234567")
                self.assertEqual(r["calling"], "+79161234567")

    def test_called_formats(self):
        rule = CFG.translation_rules["200"]
        for n in ("74951234567", "84951234567", "4951234567"):
            with self.subTest(n=n):
                self.assertEqual(rule.apply(n), "+74951234567")

    def test_unknown_source_rejected(self):
        self.assertIsNone(CFG.match_inbound(f"sip:74951234567@{WAN_IP}", "192.0.2.1"))

    def test_cucm_subscriber_first(self):
        sg = CFG.server_groups["200"].lines()
        self.assertIn(f"ipv4 {CUCM[1]} port 5060 preference 1", sg)
        self.assertIn(f"ipv4 {CUCM[0]} port 5060 preference 2", sg)


class TestOutboundToMTS(unittest.TestCase):
    def route(self, called, calling, node=CUCM[1]):
        return CFG.route_call(f"sip:{called}@{LAN_IP}:5060", node, calling)

    def test_from_both_cucm_nodes(self):
        for node in CUCM:
            with self.subTest(node=node):
                r = self.route("+74957654321", "+74951234567", node)
                self.assertEqual(r["outbound"], "100")
                self.assertEqual(r["called"], "74957654321")
                self.assertEqual(r["calling"], "74951234567")

    def test_called_formats(self):
        cases = {"+74957654321": "74957654321", "84957654321": "74957654321",
                 "+441234567890": "810441234567890"}
        for dialed, sent in cases.items():
            with self.subTest(dialed=dialed):
                self.assertEqual(self.route(dialed, "+74951234567")["called"], sent)

    def test_short_extension_replaced_by_main_number(self):
        self.assertEqual(self.route("+74957654321", "1234")["calling"], "74951234567")

    def test_cisco_headers_removed(self):
        msg = SipMessage.parse(f"""
            INVITE sip:74957654321@{MTS[0]}:5060 SIP/2.0
            Cisco-Guid: 1
            User-Agent: Cisco-SIPGateway
        """)
        out = SipProfile(CFG.sip_profiles["100"]).apply(msg)
        self.assertIsNone(out.header("Cisco-Guid"))
        self.assertIsNone(out.header("User-Agent"))


@unittest.skipUnless("110" in CFG.uri_classes, "экстренные вызовы ещё не в этой версии конфигурации")
class TestEmergency(unittest.TestCase):
    def test_emergency_numbers(self):
        expected = {"112": "112", "911": "112", "101": "101", "03": "103"}
        for dialed, sent in expected.items():
            with self.subTest(dialed=dialed):
                r = CFG.route_call(f"sip:{dialed}@{LAN_IP}:5060", CUCM[1], "+74951234567")
                self.assertEqual((r["inbound"], r["outbound"]), ("202", "110"))
                self.assertEqual(r["called"], sent)


if __name__ == "__main__":
    unittest.main()
