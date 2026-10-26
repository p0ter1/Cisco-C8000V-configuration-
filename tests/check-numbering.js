#!/usr/bin/env node
// Офлайн-проверка преобразований номеров и выбора dial-peer.
// Читает правила прямо из .cfg и сравнивает результат с ожидаемым.
// Запуск: node tests/check-numbering.js   (код выхода 1 при ошибке)
//
// Это модель логики IOS, а не сам IOS: синтаксис команд и поведение
// на реальном маршрутизаторе так не проверить.

const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');

function parseCfg(file) {
  const text = fs.readFileSync(path.join(root, file), 'utf8');
  const rules = {}, profiles = {}, peers = {}, maps = {}, dpgs = {};
  let block = null, id = null;
  for (const line of text.split(/\r?\n/)) {
    let m;
    if (!/^\s/.test(line)) {
      block = null;
      if ((m = line.match(/^voice translation-rule (\S+)/))) { block = 'rule'; id = m[1]; rules[id] = []; }
      else if ((m = line.match(/^voice translation-profile (\S+)/))) { block = 'profile'; id = m[1]; profiles[id] = {}; }
      else if ((m = line.match(/^dial-peer voice (\d+) voip/))) { block = 'peer'; id = m[1]; peers[id] = peers[id] || {}; }
      else if ((m = line.match(/^voice class e164-pattern-map (\S+)/))) { block = 'map'; id = m[1]; maps[id] = []; }
      else if ((m = line.match(/^voice class dpg (\S+)/))) { block = 'dpg'; id = m[1]; dpgs[id] = []; }
      continue;
    }
    if (block === 'rule' && (m = line.match(/^\s+rule \d+ \/(.*?)\/\s+\/(.*?)\/\s*$/))) rules[id].push([m[1], m[2]]);
    if (block === 'profile' && (m = line.match(/^\s+translate (called|calling) (\S+)/))) profiles[id][m[1]] = m[2];
    if (block === 'map' && (m = line.match(/^\s+e164 (\S+)/))) maps[id].push(m[1]);
    if (block === 'dpg' && (m = line.match(/^\s+dial-peer (\d+)/))) dpgs[id].push(m[1]);
    if (block === 'peer') {
      if ((m = line.match(/^\s+destination-pattern (\S+)/))) peers[id].patterns = [m[1]];
      if ((m = line.match(/^\s+destination e164-pattern-map (\S+)/))) peers[id].map = m[1];
      if ((m = line.match(/^\s+translation-profile outgoing (\S+)/))) peers[id].out = m[1];
      if ((m = line.match(/^\s+translation-profile incoming (\S+)/))) peers[id].in = m[1];
      if ((m = line.match(/^\s+destination dpg (\S+)/))) peers[id].dpg = m[1];
    }
  }
  for (const p of Object.values(peers)) if (p.map) p.patterns = maps[p.map];
  return { rules, profiles, peers, dpgs };
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

// destination-pattern: '.' — цифра, 'T' — переменная длина, '$' — конец
function patternRe(p) {
  const variable = p.endsWith('T');
  let body = p.replace(/T$/, '').replace(/\$$/, '').replace(/\./g, '\\d');
  return { re: new RegExp('^' + body + (variable ? '\\d*$' : '$')), variable };
}

// Выбор исходящего dial-peer: фиксированная длина важнее шаблона с T
function selectPeer(cfg, num, only) {
  const hits = Object.entries(cfg.peers)
    .filter(([id, p]) => p.patterns && (!only || only.includes(id)))
    .flatMap(([id, p]) => p.patterns.map(pt => ({ id, ...patternRe(pt) })))
    .filter(h => h.re.test(num));
  if (!hits.length) return null;
  return (hits.find(h => !h.variable) || hits[0]).id;
}

let failed = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(48)} ${String(actual).padEnd(16)}${ok ? '' : ` (ожидалось ${expected})`}`);
}

// ---------------------------------------------------------------------
// c8000v-cube-mts-site.cfg — площадка с реальными адресами
// ---------------------------------------------------------------------
{
  const cfg = parseCfg('c8000v-cube-mts-site.cfg');
  console.log('\n== c8000v-cube-mts-site.cfg: входящие от МТС (dial-peer 100) ==');
  const inPeer = cfg.peers['100'];
  const inbound = (called) => {
    const t = translate(cfg, cfg.profiles[inPeer.in].called, called);
    const peer = selectPeer(cfg, t, cfg.dpgs[inPeer.dpg]);
    return peer ? `${t} -> dp ${peer}` : `${t} -> отбой`;
  };
  check('called 4951234567', inbound('4951234567'), '2030 -> dp 201');
  check('called 74951234567', inbound('74951234567'), '2030 -> dp 201');
  check('called 84951234567', inbound('84951234567'), '2030 -> dp 201');
  check('called +74951234567', inbound('+74951234567'), '2030 -> dp 201');
  check('called 74951234568 (чужой, без петли в МТС)', inbound('74951234568'), '74951234568 -> отбой');
  const inCalling = (n) => translate(cfg, cfg.profiles[inPeer.in].calling, n);
  check('calling +79161234567', inCalling('+79161234567'), '89161234567');
  check('calling 79161234567', inCalling('79161234567'), '89161234567');
  check('calling 9161234567', inCalling('9161234567'), '89161234567');

  console.log('\n== c8000v-cube-mts-site.cfg: исходящие от CUCM (dial-peer 200) ==');
  const outbound = (called, calling = '2030') => {
    const id = selectPeer(cfg, called);
    if (!id) return 'нет dial-peer';
    const prof = cfg.profiles[cfg.peers[id].out] || {};
    return `dp ${id} ${translate(cfg, prof.called, called)} / ${translate(cfg, prof.calling, calling)}`;
  };
  check('84957654321', outbound('84957654321'), 'dp 101 74957654321 / 74951234567');
  check('89161234567', outbound('89161234567'), 'dp 101 79161234567 / 74951234567');
  check('74957654321', outbound('74957654321'), 'dp 101 74957654321 / 74951234567');
  check('810441234567890 (международный)', outbound('810441234567890'), 'dp 102 810441234567890 / 74951234567');
  check('81012345678 (международный, 11 цифр)', outbound('81012345678'), 'dp 101 81012345678 / 74951234567');
  check('112', outbound('112'), 'dp 103 112 / 74951234567');
  check('103', outbound('103'), 'dp 103 103 / 74951234567');
  check('01', outbound('01'), 'dp 103 01 / 74951234567');
}

// ---------------------------------------------------------------------
// c8000v-cube-mts-cucm.cfg — шаблон (маршрутизация через dpg, без шаблонов
// номеров, поэтому проверяются только translation-rule)
// ---------------------------------------------------------------------
{
  const cfg = parseCfg('c8000v-cube-mts-cucm.cfg');
  console.log('\n== c8000v-cube-mts-cucm.cfg: CUCM -> МТС ==');
  check('called +74957654321 (rule 100)', translate(cfg, '100', '+74957654321'), '74957654321');
  check('called 84957654321 (rule 100)', translate(cfg, '100', '84957654321'), '74957654321');
  check('called +441234567890 (rule 100)', translate(cfg, '100', '+441234567890'), '810441234567890');
  check('calling +74951234567 (rule 101)', translate(cfg, '101', '+74951234567'), '74951234567');
  check('calling 2030 (rule 101)', translate(cfg, '101', '2030'), '74951234567');
  console.log('\n== c8000v-cube-mts-cucm.cfg: МТС -> CUCM ==');
  check('74951234567 (rule 200)', translate(cfg, '200', '74951234567'), '+74951234567');
  check('84951234567 (rule 200)', translate(cfg, '200', '84951234567'), '+74951234567');
  check('4951234567 (rule 200)', translate(cfg, '200', '4951234567'), '+74951234567');
  check('810441234567890 (rule 200)', translate(cfg, '200', '810441234567890'), '+441234567890');
}

console.log(`\n${failed ? `FAILED: ${failed}` : 'Все проверки пройдены'}`);
process.exit(failed ? 1 : 0);
