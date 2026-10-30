"""Офлайн-модель конфигурации CUBE для тестов.

Разбирает конфигурацию IOS-XE и эмулирует логику, которую можно проверить
без маршрутизатора: voice translation-rule, voice class uri, SIP-профили
и выбор dial-peer. Синтаксис команд IOS не проверяется - для этого нужен
реальный маршрутизатор или C8000V в лаборатории.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def config_files():
    return sorted(p for p in REPO_ROOT.rglob("*.cfg") if "tests" not in p.parts)


# ---------------------------------------------------------------------------
# Разбор конфигурации
# ---------------------------------------------------------------------------

@dataclass
class Block:
    text: str
    children: list = field(default_factory=list)

    def lines(self):
        return [c.text for c in self.children]

    def find(self, pattern):
        rx = re.compile(pattern)
        return [c for c in self.children if rx.match(c.text)]

    def first(self, pattern):
        found = self.find(pattern)
        return found[0] if found else None

    def value(self, pattern):
        """Первая группа regex по дочерним строкам или None."""
        rx = re.compile(pattern)
        for c in self.children:
            m = rx.match(c.text)
            if m:
                return m.group(1)
        return None


def parse(text):
    root = Block("")
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("!"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        node = Block(raw.strip())
        while stack[-1][0] >= indent:
            stack.pop()
        stack[-1][1].children.append(node)
        stack.append((indent, node))
    return root


class Config:
    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8")
        self.root = parse(self.text)

    # --- выборки по типам объектов ---

    def blocks(self, pattern):
        """{ключ: Block} для top-level блоков, ключ - первая группа regex."""
        rx = re.compile(pattern)
        result = {}
        for b in self.root.children:
            m = rx.match(b.text)
            if m:
                result[m.group(1)] = b
        return result

    @property
    def dial_peers(self):
        return self.blocks(r"^dial-peer voice (\d+) voip$")

    @property
    def tenants(self):
        return self.blocks(r"^voice class tenant (\d+)$")

    @property
    def dpgs(self):
        return self.blocks(r"^voice class dpg (\d+)$")

    @property
    def uri_classes(self):
        return self.blocks(r"^voice class uri (\d+) sip$")

    @property
    def server_groups(self):
        return self.blocks(r"^voice class server-group (\d+)$")

    @property
    def keepalives(self):
        return self.blocks(r"^voice class sip-options-keepalive (\d+)$")

    @property
    def codecs(self):
        return self.blocks(r"^voice class codec (\d+)$")

    @property
    def sip_profiles(self):
        return self.blocks(r"^voice class sip-profiles (\d+)$")

    @property
    def translation_profiles(self):
        return self.blocks(r"^voice translation-profile (\S+)$")

    @property
    def translation_rules(self):
        return {k: TranslationRule(b) for k, b in self.blocks(r"^voice translation-rule (\d+)$").items()}

    @property
    def interfaces(self):
        return self.blocks(r"^interface (\S+)$")

    def interface_ip(self, name):
        iface = self.interfaces[name]
        m = iface.value(r"^ip address (\S+ \S+)$")
        return ipaddress.ip_interface(m.replace(" ", "/")) if m else None

    def interface_vrf(self, name):
        return self.interfaces[name].value(r"^vrf forwarding (\S+)$")

    @property
    def trusted_hosts(self):
        vsv = self.root.first(r"^voice service voip$")
        trusted = vsv.first(r"^ip address trusted list$")
        nets = []
        for line in trusted.lines():
            m = re.match(r"^ipv4 (\S+) (\S+)$", line)
            if m:
                nets.append(ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}"))
        return nets

    def is_trusted(self, ip):
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in self.trusted_hosts)

    @property
    def static_routes(self):
        routes = []
        rx = re.compile(r"^ip route (?:vrf (\S+) )?(\S+) (\S+) (?:(\D\S+) )?(\d+\.\d+\.\d+\.\d+)")
        for b in self.root.children:
            m = rx.match(b.text)
            if m:
                vrf, net, mask, _iface, nh = m.groups()
                routes.append((vrf, ipaddress.ip_network(f"{net}/{mask}"), ipaddress.ip_address(nh)))
        return routes

    def acl(self, name):
        return self.root.first(rf"^ip access-list extended {re.escape(name)}$")

    # --- моделирование ---

    def apply_translation_profile(self, name, called, calling):
        prof = self.translation_profiles[name]
        rules = self.translation_rules
        called_rule = prof.value(r"^translate called (\d+)$")
        calling_rule = prof.value(r"^translate calling (\d+)$")
        if called_rule:
            called = rules[called_rule].apply(called)
        if calling_rule:
            calling = rules[calling_rule].apply(calling)
        return called, calling

    def uri_matches(self, cls, uri):
        block = self.uri_classes[cls]
        host = sip_uri_host(uri)
        for line in block.lines():
            m = re.match(r"^host ipv4:(\S+)$", line)
            if m and host == m.group(1):
                return True
            m = re.match(r"^pattern (\S+)$", line)
            if m and re.search(m.group(1), uri):
                return True
        return False

    def match_inbound(self, request_uri, via_host):
        """Входящий dial-peer: сначала Request-URI, затем Via (порядок IOS-XE)."""
        dps = self.dial_peers
        for key, arg in (("request", request_uri), ("via", f"sip:{via_host}")):
            for tag in sorted(dps, key=int):
                cls = dps[tag].value(rf"^incoming uri {key} (\d+)$")
                if cls and self.uri_matches(cls, arg):
                    return tag
        return None

    def route_call(self, request_uri, via_host, calling, inbound_sip_profile_msg=None):
        """Сквозной вызов: входящий DP -> преобразования -> DPG -> исходящий DP.

        Возвращает dict с выбранными dial-peer и номерами на выходе.
        Если у входящего DP задан входящий SIP-профиль, он применяется к
        сообщению inbound_sip_profile_msg, и вызываемый номер берётся из
        изменённого Request-URI.
        """
        inbound = self.match_inbound(request_uri, via_host)
        if inbound is None:
            return {"inbound": None}
        dp = self.dial_peers[inbound]

        prof_in = dp.value(r"^voice-class sip profiles (\d+) inbound$")
        if prof_in and inbound_sip_profile_msg is not None:
            msg = SipProfile(self.sip_profiles[prof_in]).apply(inbound_sip_profile_msg)
            request_uri = msg.request_line.split()[1]
        called = sip_uri_user(request_uri)

        tp_in = dp.value(r"^translation-profile incoming (\S+)$")
        if tp_in:
            called, calling = self.apply_translation_profile(tp_in, called, calling)

        dpg = dp.value(r"^destination dpg (\d+)$")
        outbound = self.dpgs[dpg].value(r"^dial-peer (\d+)")
        out_dp = self.dial_peers[outbound]
        tp_out = out_dp.value(r"^translation-profile outgoing (\S+)$")
        if tp_out:
            called, calling = self.apply_translation_profile(tp_out, called, calling)
        return {"inbound": inbound, "outbound": outbound, "called": called, "calling": calling}


# ---------------------------------------------------------------------------
# voice translation-rule
# ---------------------------------------------------------------------------

def ios_regex_to_python(pattern):
    """В IOS группы записываются как \\( \\), а голые скобки - литералы."""
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            out.append(nxt if nxt in "()" else "\\" + nxt)
            i += 2
            continue
        out.append("\\" + ch if ch in "()" else ch)
        i += 1
    return "".join(out)


def ios_replacement_to_python(repl):
    out = []
    i = 0
    while i < len(repl):
        if repl[i] == "\\" and i + 1 < len(repl) and repl[i + 1].isdigit():
            out.append(f"\\g<{repl[i + 1]}>")
            i += 2
            continue
        out.append("\\\\" if repl[i] == "\\" else repl[i])
        i += 1
    return "".join(out)


class TranslationRule:
    """Правила проверяются по возрастанию номера, срабатывает первое совпавшее."""

    def __init__(self, block):
        self.rules = []
        for line in block.lines():
            m = re.match(r"^rule (\d+) /(.*)/ /(.*)/$", line)
            if not m:
                raise ValueError(f"не разобрано правило: {line!r}")
            self.rules.append((int(m.group(1)),
                               re.compile(ios_regex_to_python(m.group(2))),
                               ios_replacement_to_python(m.group(3))))
        self.rules.sort(key=lambda r: r[0])

    def apply(self, number):
        for _num, rx, repl in self.rules:
            if rx.search(number):
                return rx.sub(repl, number, count=1)
        return number


# ---------------------------------------------------------------------------
# SIP
# ---------------------------------------------------------------------------

def sip_uri_host(uri):
    m = re.match(r"^<?sips?:(?:[^@>]*@)?([^:;>]+)", uri)
    return m.group(1) if m else None


def sip_uri_user(uri):
    m = re.match(r"^<?sips?:([^@>]*)@", uri)
    return m.group(1) if m else None


@dataclass
class SipMessage:
    request_line: str
    headers: list  # [(name, value)]

    @property
    def method(self):
        return self.request_line.split()[0]

    def header(self, name):
        for n, v in self.headers:
            if n.lower() == name.lower():
                return v
        return None

    @classmethod
    def parse(cls, text):
        lines = [l for l in text.strip().splitlines() if l.strip()]
        headers = []
        for l in lines[1:]:
            name, _, value = l.partition(":")
            headers.append((name.strip(), value.strip()))
        return cls(lines[0].strip(), headers)


class SipProfile:
    """Эмуляция voice class sip-profiles для запросов: modify, remove, copy."""

    RX = re.compile(
        r'^rule \d+ (request|response) (\S+) '
        r'(?:(request-line)|sip-header (\S+)) '
        r'(modify "(.*)" "(.*)"|remove|copy "(.*)" (u\d+))$'
    )

    def __init__(self, block):
        self.rules = []
        for line in block.lines():
            m = self.RX.match(line)
            if not m:
                raise ValueError(f"не разобрано правило SIP-профиля: {line!r}")
            self.rules.append(m.groups())

    def apply(self, msg):
        variables = {}
        request_line = msg.request_line
        headers = list(msg.headers)

        def subst_vars(s):
            return re.sub(r"\\(u\d+)", lambda m: variables.get(m.group(1), ""), s)

        for kind, method, is_rl, hname, action, mod_from, mod_to, copy_rx, copy_var in self.rules:
            if kind != "request" or method not in ("ANY", msg.method):
                continue
            if is_rl:
                if action.startswith("modify"):
                    request_line = re.sub(mod_from, ios_replacement_to_python(subst_vars(mod_to)),
                                          request_line, count=1)
                continue
            new_headers = []
            for name, value in headers:
                if name.lower() != hname.lower():
                    new_headers.append((name, value))
                    continue
                full = f"{name}: {value}"
                if action == "remove":
                    continue
                if action.startswith("modify"):
                    full = re.sub(mod_from, ios_replacement_to_python(subst_vars(mod_to)), full, count=1)
                    name, _, value = full.partition(":")
                    value = value.strip()
                elif action.startswith("copy"):
                    m = re.search(copy_rx, full)
                    if m:
                        variables[copy_var] = m.group(1)
                new_headers.append((name, value))
            headers = new_headers
        return SipMessage(request_line, headers)
