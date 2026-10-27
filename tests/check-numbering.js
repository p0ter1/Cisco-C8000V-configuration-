#!/usr/bin/env node
// Офлайн-проверка конфигурации CUBE: преобразования номеров, выбор
// dial-peer через dpg, порядок объявления объектов и SIP-профиль.
// Читает всё прямо из .cfg и сравнивает результат с ожидаемым.
// Запуск: node tests/check-numbering.js   (код выхода 1 при ошибке)
//
// Это модель логики IOS, а не сам IOS: синтаксис команд и поведение
// на реальном маршрутизаторе так не проверить.

const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');

function parseCfg(file) {
  const text = fs.readFileSync(path.join(root, file), 'utf8');
  const cfg = { rules: {}, profiles: {}, peers: {}, dpgs: {}, tenants: {}, sipProfiles: {}, policies: {}, ifaces: {}, order: [] };
  let block = null, id = null;
  for (const line of text.split(/\r?\n/)) {
    let m;
    if (!/^\s/.test(line)) {
      block = null;
      if ((m = line.match(/^voice translation-rule (\S+)/))) { block = 'rule'; id = m[1]; cfg.rules[id] = []; }
      else if ((m = line.match(/^voice translation-profile (\S+)/))) { block = 'profile'; id = m[1]; cfg.profiles[id] = {}; }
      else if ((m = line.match(/^dial-peer voice (\d+) voip/))) {
        block = 'peer'; id = m[1];
        if (!cfg.peers[id]) { cfg.peers[id] = {}; cfg.order.push(`peer ${id}`); }
      }
      else if ((m = line.match(/^voice class dpg (\S+)/))) { block = 'dpg'; id = m[1]; cfg.dpgs[id] = []; cfg.order.push(`dpg ${id}`); }
      else if ((m = line.match(/^voice class tenant (\S+)/))) { block = 'tenant'; id = m[1]; cfg.tenants[id] = {}; }
      else if ((m = line.match(/^voice class sip-profiles (\S+)/))) { block = 'sipp'; id = m[1]; cfg.sipProfiles[id] = []; }
      else if ((m = line.match(/^policy-map (\S+)/))) { block = 'policy'; id = m[1]; cfg.policies[id] = { classes: {} }; }
      else if ((m = line.match(/^interface (\S+)/))) { block = 'iface'; id = m[1]; cfg.ifaces[id] = {}; }
      continue;
    }
    if (block === 'policy') {
      const pm = cfg.policies[id];
      if ((m = line.match(/^\s+class (\S+)/))) { pm.current = m[1]; pm.classes[m[1]] = {}; }
      const cls = pm.classes[pm.current];
      if (cls && (m = line.match(/^\s+priority (\d+)$/))) cls.priorityKbps = Number(m[1]);
      if (cls && (m = line.match(/^\s+bandwidth (\d+)$/))) cls.bandwidthKbps = Number(m[1]);
      if (cls && (m = line.match(/^\s+shape average (\d+)$/))) cls.shapeKbps = Number(m[1]) / 1000;
      if (cls && (m = line.match(/^\s+service-policy (\S+)$/))) cls.child = m[1];
    }
    if (block === 'iface' && (m = line.match(/^\s+service-policy output (\S+)/))) cfg.ifaces[id].outPolicy = m[1];
    if (block === 'rule' && (m = line.match(/^\s+rule \d+ \/(.*?)\/\s+\/(.*?)\/\s*$/))) cfg.rules[id].push([m[1], m[2]]);
    if (block === 'profile' && (m = line.match(/^\s+translate (called|calling) (\S+)/))) cfg.profiles[id][m[1]] = m[2];
    if (block === 'dpg' && (m = line.match(/^\s+dial-peer (\d+)/))) cfg.dpgs[id].push({ peer: m[1], at: cfg.order.length });
    if (block === 'tenant') {
      if ((m = line.match(/^\s+sip-profiles (\S+)/))) cfg.tenants[id].sipProfile = m[1];
      if ((m = line.match(/^\s+bind control source-interface (\S+)/))) cfg.tenants[id].iface = m[1];
    }
    if (block === 'sipp' && (m = line.match(/^\s+rule \d+ (request|response) ANY sip-header (\S+) remove/)))
      cfg.sipProfiles[id].push({ dir: m[1], header: m[2].toLowerCase() });
    if (block === 'peer') {
      const p = cfg.peers[id];
      if ((m = line.match(/^\s+translation-profile outgoing (\S+)/))) p.out = m[1];
      if ((m = line.match(/^\s+translation-profile incoming (\S+)/))) p.in = m[1];
      if ((m = line.match(/^\s+destination dpg (\S+)/))) p.dpg = { id: m[1], at: cfg.order.length };
      if ((m = line.match(/^\s+voice-class sip tenant (\S+)/))) p.tenant = m[1];
      if ((m = line.match(/^\s+max-conn (\d+)/))) p.maxConn = Number(m[1]);
      if ((m = line.match(/^\s+ip qos dscp (\S+) (media|signaling)/))) p[`dscp_${m[2]}`] = m[1];
    }
  }
  return cfg;
}

// Правило Cisco: \( \) — группа, \1 — подстановка; применяется первое совпавшее
function translate(cfg, ruleId, num) {
  if (!ruleId) return num;
  for (const [pat, rep] of cfg.rules[ruleId]) {
    const re = new RegExp(pat.replace(/\\\(/g, '(').replace(/\\\)/g, ')'));
    if (re.test(num)) return num.replace(re, rep.replace(/\\(\d)/g, '$$$1'));
  }
  return num;
}

// Вызов через входящий dial-peer с destination dpg: исходящий берётся из
// группы по порядку, destination-pattern исходящего не учитывается.
function call(cfg, inPeerId, called, calling) {
  const inPeer = cfg.peers[inPeerId];
  const inProf = cfg.profiles[inPeer.in] || {};
  called = translate(cfg, inProf.called, called);
  calling = translate(cfg, inProf.calling, calling);
  const outId = cfg.dpgs[inPeer.dpg.id][0].peer;
  const outProf = cfg.profiles[cfg.peers[outId].out] || {};
  return `dp ${outId} ${translate(cfg, outProf.called, called)} / ${translate(cfg, outProf.calling, calling)}`;
}

// Порядок объявления: dpg ссылается на уже созданные dial-peer,
// dial-peer ссылается на уже созданную dpg (иначе вставка конфига
// целиком оставит группу пустой и вызовы не пройдут).
function orderIssues(cfg) {
  const issues = [];
  const pos = (key) => cfg.order.indexOf(key);
  for (const [id, members] of Object.entries(cfg.dpgs))
    for (const { peer, at } of members)
      if (pos(`peer ${peer}`) < 0 || pos(`peer ${peer}`) >= at) issues.push(`dpg ${id} ссылается на dial-peer ${peer} до его объявления`);
  for (const [id, p] of Object.entries(cfg.peers))
    if (p.dpg && (pos(`dpg ${p.dpg.id}`) < 0 || pos(`dpg ${p.dpg.id}`) >= p.dpg.at))
      issues.push(`dial-peer ${id} ссылается на dpg ${p.dpg.id} до её объявления`);
  return issues;
}

// Применить SIP-профиль tenant'а dial-peer к сообщению: вернуть оставшиеся заголовки
function applySipProfile(cfg, peerId, dir, headers) {
  const t = cfg.tenants[cfg.peers[peerId].tenant] || {};
  const rules = cfg.sipProfiles[t.sipProfile] || [];
  return headers.filter(h => !rules.some(r => r.dir === dir && r.header === h.toLowerCase()));
}

let failed = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(46)} ${String(actual).padEnd(16)}${ok ? '' : ` (ожидалось ${expected})`}`);
}

// ---------------------------------------------------------------------
// c8000v-cube-mts-site.cfg — площадка с реальными адресами
// ---------------------------------------------------------------------
{
  const cfg = parseCfg('c8000v-cube-mts-site.cfg');

  console.log('\n== site: структура ==');
  check('порядок объявления dpg / dial-peer', orderIssues(cfg).join('; ') || 'OK', 'OK');
  check('dial-peer 100 (от МТС) -> dpg', cfg.peers['100'].dpg.id, '200');
  check('dial-peer 200 (от CUCM) -> dpg', cfg.peers['200'].dpg.id, '100');
  for (const id of ['100']) check(`dial-peer ${id}: tenant -> интерфейс`, cfg.tenants[cfg.peers[id].tenant].iface, 'GigabitEthernet1');
  for (const id of ['200', '201']) check(`dial-peer ${id}: tenant -> интерфейс`, cfg.tenants[cfg.peers[id].tenant].iface, 'GigabitEthernet2');

  console.log('\n== site: входящие от МТС (dial-peer 100) ==');
  check('4951234567 от 9031112233', call(cfg, '100', '4951234567', '9031112233'), 'dp 201 2030 / 89031112233');
  check('74951234567 от 79161234567', call(cfg, '100', '74951234567', '79161234567'), 'dp 201 2030 / 89161234567');
  check('84951234567 от +79161234567', call(cfg, '100', '84951234567', '+79161234567'), 'dp 201 2030 / 89161234567');
  check('+74951234567 от 9161234567', call(cfg, '100', '+74951234567', '9161234567'), 'dp 201 2030 / 89161234567');
  // Чужой номер не преобразуется и уходит в CUCM (тот ответит 404),
  // но не обратно в МТС: в dpg 200 только dial-peer 201.
  check('74951234568 (чужой, без петли в МТС)', call(cfg, '100', '74951234568', '9161234567'), 'dp 201 74951234568 / 89161234567');

  console.log('\n== site: исходящие от CUCM (dial-peer 200), АОН 2030 ==');
  check('84957654321', call(cfg, '200', '84957654321', '2030'), 'dp 100 74957654321 / 74951234567');
  check('89161234567', call(cfg, '200', '89161234567', '2030'), 'dp 100 79161234567 / 74951234567');
  check('+74957654321', call(cfg, '200', '+74957654321', '2030'), 'dp 100 74957654321 / 74951234567');
  check('810441234567890 (международный)', call(cfg, '200', '810441234567890', '2030'), 'dp 100 810441234567890 / 74951234567');
  check('81012345678 (международный, 11 цифр)', call(cfg, '200', '81012345678', '2030'), 'dp 100 81012345678 / 74951234567');
  check('112', call(cfg, '200', '112', '2030'), 'dp 100 112 / 74951234567');
  check('103', call(cfg, '200', '103', '2030'), 'dp 100 103 / 74951234567');
  check('01', call(cfg, '200', '01', '2030'), 'dp 100 01 / 74951234567');

  console.log('\n== site: SIP-профиль в сторону МТС ==');
  // Заголовки ответа 404 из реального debug (CUBE -> SBC МТС)
  const resp404 = ['Via', 'From', 'To', 'Date', 'Call-ID', 'CSeq', 'Warning', 'Reason', 'Allow-Events', 'Session-ID', 'Server', 'Content-Length'];
  check('ответ 404 к МТС (dial-peer 100)', applySipProfile(cfg, '100', 'response', resp404).join(','),
    'Via,From,To,Date,Call-ID,CSeq,Reason,Content-Length');
  const invite = ['Via', 'From', 'To', 'Call-ID', 'CSeq', 'Contact', 'P-Asserted-Identity', 'User-Agent', 'Cisco-Guid', 'Session-ID', 'Remote-Party-ID', 'Call-Info', 'Allow-Events', 'Content-Type'];
  check('INVITE к МТС (dial-peer 100)', applySipProfile(cfg, '100', 'request', invite).join(','),
    'Via,From,To,Call-ID,CSeq,Contact,P-Asserted-Identity,Content-Type');
  check('INVITE к CUCM не трогается (dial-peer 201)', applySipProfile(cfg, '201', 'request', invite).length, invite.length);

  console.log('\n== site: QoS к МТС (16 x G.711, канал 2 Мбит/с) ==');
  const LINK_KBPS = 2000;
  const MAX_CALLS = 16;
  // G.711, 20 мс: 160 Б нагрузки + RTP/UDP/IP 40 Б + Ethernet/FCS 18 Б, 50 пак/с
  const G711_L2_KBPS = ((160 + 40 + 18) * 8 * 50) / 1000;
  const voiceKbps = MAX_CALLS * G711_L2_KBPS;
  const wanPolicy = cfg.policies[cfg.ifaces['GigabitEthernet1'].outPolicy];
  const shaper = wanPolicy.classes['class-default'];
  const child = cfg.policies[shaper.child].classes;
  const priorityKbps = child['CM-VOICE-RTP'].priorityKbps;
  const sipKbps = child['CM-VOICE-SIP'].bandwidthKbps;
  check('полоса одного вызова G.711, кбит/с', G711_L2_KBPS, 87.2);
  check('max-conn на транке МТС (вход + выход)', cfg.peers['100'].maxConn, MAX_CALLS);
  check('шейпер не больше канала', shaper.shapeKbps <= LINK_KBPS, true);
  check(`LLQ ${priorityKbps} >= ${voiceKbps} кбит/с для ${MAX_CALLS} вызовов`, priorityKbps >= voiceKbps, true);
  check('LLQ + SIP укладываются в шейпер', priorityKbps + sipKbps <= shaper.shapeKbps, true);
  check('17-й вызов не помещается в LLQ', priorityKbps < (MAX_CALLS + 1) * G711_L2_KBPS, true);
  check('разметка RTP к МТС', cfg.peers['100'].dscp_media, 'ef');
  check('разметка SIP к МТС', cfg.peers['100'].dscp_signaling, 'cs3');
}

// ---------------------------------------------------------------------
// c8000v-cube-mts-cucm.cfg — шаблон: проверяются translation-rule,
// найденные проблемы порядка выводятся как информация
// ---------------------------------------------------------------------
{
  const cfg = parseCfg('c8000v-cube-mts-cucm.cfg');
  console.log('\n== шаблон c8000v-cube-mts-cucm.cfg: CUCM -> МТС ==');
  check('called +74957654321 (rule 100)', translate(cfg, '100', '+74957654321'), '74957654321');
  check('called 84957654321 (rule 100)', translate(cfg, '100', '84957654321'), '74957654321');
  check('called +441234567890 (rule 100)', translate(cfg, '100', '+441234567890'), '810441234567890');
  check('calling +74951234567 (rule 101)', translate(cfg, '101', '+74951234567'), '74951234567');
  check('calling 2030 (rule 101)', translate(cfg, '101', '2030'), '74951234567');
  console.log('\n== шаблон c8000v-cube-mts-cucm.cfg: МТС -> CUCM ==');
  check('74951234567 (rule 200)', translate(cfg, '200', '74951234567'), '+74951234567');
  check('84951234567 (rule 200)', translate(cfg, '200', '84951234567'), '+74951234567');
  check('4951234567 (rule 200)', translate(cfg, '200', '4951234567'), '+74951234567');
  check('810441234567890 (rule 200)', translate(cfg, '200', '810441234567890'), '+441234567890');
  for (const issue of orderIssues(cfg)) console.log(`INFO  шаблон: ${issue}`);
}

console.log(`\n${failed ? `FAILED: ${failed}` : 'Все проверки пройдены'}`);
process.exit(failed ? 1 : 0);
