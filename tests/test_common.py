"""Общие проверки для каждого *.cfg в репозитории."""

import ipaddress
import re
import unittest

from ios_model import Config, TranslationRule, config_files


class CommonChecks:
    """Миксин: self.cfg задаётся в подклассах, созданных ниже для каждого файла."""

    cfg: Config

    # --- целостность ссылок ---

    def test_dial_peer_tags_unique(self):
        tags = re.findall(r"^dial-peer voice (\d+) voip$", self.cfg.text, re.M)
        self.assertEqual(len(tags), len(set(tags)), "повторяющиеся номера dial-peer")

    def test_dial_peer_references_exist(self):
        c = self.cfg
        refs = [
            (r"^voice-class sip tenant (\d+)$", c.tenants, "tenant"),
            (r"^destination dpg (\d+)$", c.dpgs, "dpg"),
            (r"^incoming uri \S+ (\d+)$", c.uri_classes, "uri"),
            (r"^session server-group (\d+)$", c.server_groups, "server-group"),
            (r"^voice-class sip options-keepalive profile (\d+)$", c.keepalives, "options-keepalive"),
            (r"^voice-class codec (\d+)", c.codecs, "codec"),
            (r"^translation-profile \S+ (\S+)$", c.translation_profiles, "translation-profile"),
            (r"^voice-class sip profiles (\d+)", c.sip_profiles, "sip-profiles"),
        ]
        for tag, dp in c.dial_peers.items():
            for line in dp.lines():
                for rx, known, what in refs:
                    m = re.match(rx, line)
                    if m:
                        with self.subTest(dial_peer=tag, ref=line):
                            self.assertIn(m.group(1), known, f"dial-peer {tag}: нет {what} {m.group(1)}")

    def test_translation_profiles_reference_existing_rules(self):
        rules = self.cfg.translation_rules
        for name, prof in self.cfg.translation_profiles.items():
            for line in prof.lines():
                m = re.match(r"^translate \S+ (\d+)$", line)
                with self.subTest(profile=name, line=line):
                    self.assertIsNotNone(m)
                    self.assertIn(m.group(1), rules)

    def test_tenant_sip_profiles_exist(self):
        for tag, tenant in self.cfg.tenants.items():
            prof = tenant.value(r"^sip-profiles (\d+)$")
            if prof:
                self.assertIn(prof, self.cfg.sip_profiles, f"tenant {tag}")

    def test_tenant_bind_interfaces_exist_with_ip(self):
        for tag, tenant in self.cfg.tenants.items():
            for line in tenant.lines():
                m = re.match(r"^bind (?:control|media) source-interface (\S+)$", line)
                if m:
                    with self.subTest(tenant=tag, iface=m.group(1)):
                        self.assertIn(m.group(1), self.cfg.interfaces)
                        self.assertIsNotNone(self.cfg.interface_ip(m.group(1)))

    def test_tenant_control_and_media_on_same_interface(self):
        for tag, tenant in self.cfg.tenants.items():
            ctl = tenant.value(r"^bind control source-interface (\S+)$")
            med = tenant.value(r"^bind media source-interface (\S+)$")
            self.assertEqual(ctl, med, f"tenant {tag}")

    # --- логика маршрутизации ---

    def test_dpg_members_are_outbound_dial_peers(self):
        dps = self.cfg.dial_peers
        for tag, dpg in self.cfg.dpgs.items():
            members = [re.match(r"^dial-peer (\d+)", l).group(1) for l in dpg.lines() if l.startswith("dial-peer")]
            self.assertTrue(members, f"dpg {tag} пуст")
            for m in members:
                with self.subTest(dpg=tag, dial_peer=m):
                    self.assertIn(m, dps)
                    self.assertTrue(dps[m].find(r"^session (server-group|target) "),
                                    "в dpg должен быть исходящий dial-peer")
                    self.assertFalse(dps[m].find(r"^incoming uri "),
                                     "исходящий dial-peer не должен ловить входящие")

    def test_inbound_dial_peers_have_destination_dpg(self):
        for tag, dp in self.cfg.dial_peers.items():
            if dp.find(r"^incoming uri "):
                self.assertTrue(dp.find(r"^destination dpg \d+$"), f"dial-peer {tag}")

    def test_calls_cross_to_other_side(self):
        """Входящий dial-peer и исходящий из его dpg - в разных тенантах."""
        dps = self.cfg.dial_peers
        for tag, dp in dps.items():
            dpg = dp.value(r"^destination dpg (\d+)$")
            if not dpg:
                continue
            out = self.cfg.dpgs[dpg].value(r"^dial-peer (\d+)")
            t_in = dp.value(r"^voice-class sip tenant (\d+)$")
            t_out = dps[out].value(r"^voice-class sip tenant (\d+)$")
            with self.subTest(inbound=tag, outbound=out):
                self.assertNotEqual(t_in, t_out, "вызов должен переходить на другую сторону CUBE")

    def test_keepalive_transport_matches_dial_peer(self):
        for tag, dp in self.cfg.dial_peers.items():
            prof = dp.value(r"^voice-class sip options-keepalive profile (\d+)$")
            if not prof:
                continue
            ka_t = self.cfg.keepalives[prof].value(r"^transport (\S+)$")
            dp_t = dp.value(r"^session transport (\S+)$")
            self.assertEqual(ka_t, dp_t, f"dial-peer {tag}")

    def test_translation_rules_compile(self):
        self.assertTrue(self.cfg.translation_rules)  # TranslationRule бросит ошибку при разборе

    def test_uri_patterns_compile(self):
        for tag, cls in self.cfg.uri_classes.items():
            for line in cls.lines():
                if line.startswith("pattern "):
                    re.compile(line.split(" ", 1)[1])

    # --- безопасность ---

    def test_signaling_peers_are_trusted(self):
        hosts = []
        for sg in self.cfg.server_groups.values():
            hosts += [m.group(1) for l in sg.lines() if (m := re.match(r"^ipv4 (\S+)", l))]
        for cls in self.cfg.uri_classes.values():
            hosts += [m.group(1) for l in cls.lines() if (m := re.match(r"^host ipv4:(\S+)$", l))]
        for tenant in self.cfg.tenants.values():
            for line in tenant.lines():
                m = re.match(r"^(?:registrar|sip-server) ipv4:([\d.]+)", line)
                if m:
                    hosts.append(m.group(1))
        self.assertTrue(hosts)
        for h in hosts:
            with self.subTest(host=h):
                self.assertTrue(self.cfg.is_trusted(h), f"{h} не в ip address trusted list")

    def test_trusted_list_has_no_wide_networks(self):
        for net in self.cfg.trusted_hosts:
            self.assertGreaterEqual(net.prefixlen, 24, f"слишком широкая сеть в trusted list: {net}")

    def test_public_trusted_hosts_allowed_by_wan_acl(self):
        wan = [(n, b) for n, b in self.cfg.interfaces.items() if b.value(r"^ip access-group (\S+) in$")]
        self.assertEqual(len(wan), 1, "ожидается ACL на одном WAN-интерфейсе")
        name, iface = wan[0]
        acl = self.cfg.acl(iface.value(r"^ip access-group (\S+) in$"))
        self.assertIsNotNone(acl)
        wan_ip = str(self.cfg.interface_ip(name).ip)
        for net in self.cfg.trusted_hosts:
            if net.is_private:
                continue
            host = str(net.network_address)
            rx = rf"^\d+ permit udp host {re.escape(host)} host {re.escape(wan_ip)} eq 5060$"
            with self.subTest(host=host):
                self.assertTrue(acl.find(rx), f"ACL не пропускает SIP от {host}")
        self.assertTrue(acl.find(r"^\d+ deny ip any any"), "ACL должен заканчиваться deny")

    def test_no_plaintext_secrets(self):
        for m in re.finditer(r"(?:password 0|secret) (\S+)", self.cfg.text):
            with self.subTest(value=m.group(1)):
                self.assertRegex(m.group(1), r"^<.+>$", "в публичном репозитории только подстановки")

    # --- адресация ---

    def test_static_route_next_hops_are_connected(self):
        for vrf, net, nh in self.cfg.static_routes:
            connected = [
                self.cfg.interface_ip(n) for n in self.cfg.interfaces
                if self.cfg.interface_ip(n) and self.cfg.interface_vrf(n) == vrf
            ]
            with self.subTest(route=str(net), next_hop=str(nh), vrf=vrf):
                self.assertTrue(any(nh in i.network for i in connected),
                                "next-hop не в подключённой сети того же VRF")
                self.assertTrue(all(nh != i.ip for i in connected), "next-hop совпадает с адресом интерфейса")

    def test_interface_addresses_are_hosts(self):
        for name in self.cfg.interfaces:
            ip = self.cfg.interface_ip(name)
            if ip:
                with self.subTest(iface=name):
                    self.assertNotIn(ip.ip, (ip.network.network_address, ip.network.broadcast_address))


# Отдельный TestCase на каждый файл конфигурации
for _path in config_files():
    _name = "Test_" + re.sub(r"\W", "_", _path.stem)
    globals()[_name] = type(_name, (CommonChecks, unittest.TestCase), {"cfg": Config(_path)})


if __name__ == "__main__":
    unittest.main()
