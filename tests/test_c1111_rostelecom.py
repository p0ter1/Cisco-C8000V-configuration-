"""Сценарии для C1111-4P + Ростелеком (регистрация) + CUCM."""

import unittest

from ios_model import REPO_ROOT, Config, SipMessage, SipProfile

CFG = Config(REPO_ROOT / "C1111-4P-Rostelecom" / "c1111-cube-rostelecom-cucm.cfg")

SBC = "95.167.123.123"
REALM = "123456.78.rt.ru"
WAN_IP = "123.45.68.97"
LAN_IP = "10.50.51.62"
CUCM_PUB = "10.97.0.145"
CUCM_SUB = "10.97.0.146"

DID_TO_EXT = {"3521234567": "2030", "3521234568": "2031", "3521234569": "2032"}


def invite_from_rt(did, calling="89121234567"):
    """INVITE от РТ: Request-URI на зарегистрированный Contact, DID - в To."""
    return SipMessage.parse(f"""
        INVITE sip:3521234567@{WAN_IP}:5060 SIP/2.0
        Via: SIP/2.0/UDP {SBC}:5060;branch=z9hG4bK1
        From: <sip:{calling}@{REALM}>;tag=1
        To: <sip:{did}@{REALM}>
    """)


class TestAddressing(unittest.TestCase):
    def test_interfaces_and_vrfs(self):
        self.assertEqual(str(CFG.interface_ip("GigabitEthernet0/0/0")), f"{WAN_IP}/30")
        self.assertEqual(CFG.interface_vrf("GigabitEthernet0/0/0"), "INET-PUBLIC1")
        self.assertEqual(str(CFG.interface_ip("GigabitEthernet0/0/1")), f"{LAN_IP}/27")
        self.assertEqual(CFG.interface_vrf("GigabitEthernet0/0/1"), "VoIP")

    def test_vrf_forwarding_before_ip_address(self):
        """vrf forwarding удаляет IP, поэтому должен идти раньше ip address."""
        for name in ("GigabitEthernet0/0/0", "GigabitEthernet0/0/1"):
            lines = CFG.interfaces[name].lines()
            vrf = next(i for i, l in enumerate(lines) if l.startswith("vrf forwarding"))
            ip = next(i for i, l in enumerate(lines) if l.startswith("ip address"))
            self.assertLess(vrf, ip, name)

    def test_default_route_to_rt_gateway(self):
        self.assertIn(("INET-PUBLIC1", "0.0.0.0/0", "123.45.68.98"),
                      [(v, str(n), str(h)) for v, n, h in CFG.static_routes])

    def test_cucm_reachable_in_voip_vrf(self):
        import ipaddress
        for ip in (CUCM_PUB, CUCM_SUB):
            self.assertTrue(any(v == "VoIP" and ipaddress.ip_address(ip) in n for v, n, _ in CFG.static_routes), ip)

    def test_tenants_bound_to_correct_sides(self):
        t = CFG.tenants
        self.assertEqual(t["100"].value(r"^bind control source-interface (\S+)$"), "GigabitEthernet0/0/0")
        self.assertEqual(t["200"].value(r"^bind control source-interface (\S+)$"), "GigabitEthernet0/0/1")


class TestRegistration(unittest.TestCase):
    tenant = CFG.tenants["100"]

    def test_registrar_and_credentials(self):
        self.assertTrue(self.tenant.find(rf"^registrar ipv4:{SBC}:5060 expires \d+$"))
        self.assertTrue(self.tenant.find(
            rf"^credentials number 3521234567 username 3521234567 password 0 \S+ realm {REALM}$"))
        self.assertTrue(self.tenant.find(
            rf"^authentication username 3521234567 password 0 \S+ realm {REALM}$"))
        self.assertEqual(self.tenant.value(r"^localhost dns:(\S+)$"), REALM)

    def test_register_request_uri_uses_realm(self):
        msg = SipMessage.parse(f"""
            REGISTER sip:{SBC}:5060 SIP/2.0
            From: <sip:3521234567@{REALM}>;tag=1
            To: <sip:3521234567@{SBC}>
            User-Agent: Cisco-SIPGateway/IOS-17.12
        """)
        out = SipProfile(CFG.sip_profiles["100"]).apply(msg)
        self.assertEqual(out.request_line, f"REGISTER sip:{REALM}:5060 SIP/2.0")
        self.assertEqual(out.header("To"), f"<sip:3521234567@{REALM}>")
        self.assertIsNone(out.header("User-Agent"))

    def test_outgoing_invite_uses_realm_and_hides_cisco_headers(self):
        msg = SipMessage.parse(f"""
            INVITE sip:84951234567@{SBC}:5060 SIP/2.0
            From: <sip:3521234568@{REALM}>;tag=1
            To: <sip:84951234567@{SBC}>
            Cisco-Guid: 12345
            User-Agent: Cisco-SIPGateway/IOS-17.12
        """)
        out = SipProfile(CFG.sip_profiles["100"]).apply(msg)
        self.assertEqual(out.request_line, f"INVITE sip:84951234567@{REALM}:5060 SIP/2.0")
        self.assertEqual(out.header("To"), f"<sip:84951234567@{REALM}>")
        self.assertIsNone(out.header("Cisco-Guid"))
        self.assertIsNone(out.header("User-Agent"))


class TestInboundFromRT(unittest.TestCase):
    def test_to_header_copied_into_request_uri(self):
        out = SipProfile(CFG.sip_profiles["101"]).apply(invite_from_rt("3521234569"))
        self.assertEqual(out.request_line, f"INVITE sip:3521234569@{WAN_IP}:5060 SIP/2.0")

    def test_each_did_lands_on_its_extension(self):
        for did, ext in DID_TO_EXT.items():
            with self.subTest(did=did):
                msg = invite_from_rt(did)
                r = CFG.route_call(msg.request_line.split()[1], SBC, "89121234567", msg)
                self.assertEqual(r["inbound"], "101")
                self.assertEqual(r["outbound"], "200")
                self.assertEqual(r["called"], ext)
                self.assertEqual(r["calling"], "+79121234567")

    def test_did_in_any_prefix_format(self):
        rule = CFG.translation_rules["200"]
        for prefix in ("", "7", "8", "+7"):
            for did, ext in DID_TO_EXT.items():
                with self.subTest(number=prefix + did):
                    self.assertEqual(rule.apply(prefix + did), ext)

    def test_foreign_number_is_not_mapped(self):
        self.assertEqual(CFG.translation_rules["200"].apply("3521234570"), "3521234570")

    def test_calling_number_normalized_to_e164(self):
        rule = CFG.translation_rules["201"]
        self.assertEqual(rule.apply("83521112233"), "+73521112233")
        self.assertEqual(rule.apply("73521112233"), "+73521112233")
        self.assertEqual(rule.apply("3521112233"), "+73521112233")
        self.assertEqual(rule.apply("810441234567890"), "+441234567890")

    def test_unknown_source_not_accepted(self):
        self.assertIsNone(CFG.match_inbound(f"sip:3521234567@{WAN_IP}", "198.51.100.66"))
        self.assertFalse(CFG.is_trusted("198.51.100.66"))

    def test_cucm_server_group_order(self):
        sg = CFG.server_groups["200"].lines()
        self.assertIn(f"ipv4 {CUCM_SUB} port 5060 preference 1", sg)
        self.assertIn(f"ipv4 {CUCM_PUB} port 5060 preference 2", sg)
        self.assertIn("hunt-scheme preference", sg)


class TestOutboundToRT(unittest.TestCase):
    def route(self, called, calling, node=CUCM_SUB):
        return CFG.route_call(f"sip:{called}@{LAN_IP}:5060", node, calling)

    def test_extension_presents_own_did(self):
        for did, ext in DID_TO_EXT.items():
            with self.subTest(ext=ext):
                r = self.route("+74951234567", ext)
                self.assertEqual((r["inbound"], r["outbound"]), ("201", "100"))
                self.assertEqual(r["calling"], did)

    def test_e164_did_presented_as_10_digits(self):
        self.assertEqual(self.route("+74951234567", "+73521234568")["calling"], "3521234568")

    def test_other_extensions_present_main_number(self):
        for ext in ("2033", "1000", "+73520000000"):
            with self.subTest(ext=ext):
                self.assertEqual(self.route("+74951234567", ext)["calling"], "3521234567")

    def test_called_number_formats(self):
        cases = {
            "+74951234567": "84951234567",
            "74951234567": "84951234567",
            "84951234567": "84951234567",
            "+441234567890": "810441234567890",
            "81044123": "81044123",
        }
        for dialed, sent in cases.items():
            with self.subTest(dialed=dialed):
                self.assertEqual(self.route(dialed, "2030")["called"], sent)

    def test_both_cucm_nodes_accepted(self):
        for node in (CUCM_PUB, CUCM_SUB):
            with self.subTest(node=node):
                self.assertEqual(self.route("84951234567", "2030", node)["inbound"], "201")


class TestEmergency(unittest.TestCase):
    EXPECTED = {
        "112": "112", "911": "112",
        "101": "101", "102": "102", "103": "103", "104": "104",
        "01": "101", "02": "102", "03": "103", "04": "104",
    }

    def test_emergency_routed_via_dedicated_dial_peers(self):
        for dialed, sent in self.EXPECTED.items():
            with self.subTest(dialed=dialed):
                r = CFG.route_call(f"sip:{dialed}@{LAN_IP}:5060", CUCM_SUB, "2031")
                self.assertEqual((r["inbound"], r["outbound"]), ("202", "110"))
                self.assertEqual(r["called"], sent)
                self.assertEqual(r["calling"], "3521234568")

    def test_non_emergency_short_numbers_not_caught(self):
        for dialed in ("100", "105", "1120", "05", "1012", "0112"):
            with self.subTest(dialed=dialed):
                self.assertEqual(CFG.match_inbound(f"sip:{dialed}@{LAN_IP}:5060", CUCM_SUB), "201")

    def test_emergency_pattern_ignores_wan_side(self):
        """Вызов на 112 со стороны РТ не должен попасть в экстренный dial-peer."""
        self.assertEqual(CFG.match_inbound(f"sip:112@{WAN_IP}:5060", SBC), "101")

    def test_emergency_dial_peer_has_no_call_limit(self):
        self.assertFalse(CFG.dial_peers["110"].find(r"^max-conn"))


if __name__ == "__main__":
    unittest.main()
