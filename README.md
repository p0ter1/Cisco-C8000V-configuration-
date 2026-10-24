# Cisco C8000V: CUBE для SIP-транка МТС и кластера CUCM

Конфигурация лежит в файле [`c8000v-cube-mts-cucm.cfg`](c8000v-cube-mts-cucm.cfg). Это Cisco Catalyst 8000V, работающий как CUBE (Cisco Unified Border Element) между:

- **МТС**: SIP-транк, аутентификация по IP (без регистрации), UDP/5060;
- **CUCM**: кластер из двух нод (Publisher + Subscriber), TCP/5060.

```
  МТС SBC1/SBC2 ──UDP 5060── Gi1 [ C8000V / CUBE ] Gi2 ──TCP 5060── CUCM SUB (осн.)
  198.51.100.10/.11        203.0.113.10    10.10.10.1          CUCM PUB (рез.)
```

## Что заменить перед применением

| Параметр | В примере | Где встречается |
|---|---|---|
| IP WAN / шлюз | `203.0.113.10/30`, `203.0.113.9` | Gi1, ACL-WAN-IN, маршруты |
| IP LAN / шлюз | `10.10.10.1/24`, `10.10.10.254` | Gi2, маршруты |
| SBC МТС | `198.51.100.10`, `198.51.100.11` | trusted list, uri 100, server-group 100, ACL |
| Медиа-сеть МТС | `198.51.100.0/24` | ACL-WAN-IN, маршрут |
| CUCM PUB / SUB | `10.10.20.11` / `10.10.20.12` | trusted list, uri 200, server-group 200 |
| Основной номер | `74951234567` | translation-rule 101 |
| Число каналов | `30` | `mode border-element license capacity`, `max-conn` |
| Пароли | `<ПАРОЛЬ>` | username, enable |

Адреса SBC, подсеть медиа и формат номеров выдаёт МТС в бланке подключения. Медиа часто идёт с других адресов, не с SBC сигнализации; если подсеть RTP указана неправильно, будет односторонняя слышимость или тишина.

## Основные решения

- **Защита от toll-fraud.** `ip address trusted list` и ACL на Gi1 пропускают SIP только от SBC МТС и нод CUCM. Пакеты с других адресов отбрасываются молча (`silent-discard untrusted`).
- **Входящие вызовы сопоставляются по IP.** `incoming uri via` смотрит на заголовок Via, поэтому вызов попадает на нужный dial-peer, какой бы номер ни набрали.
- **Маршрутизация через группы dial-peer.** Вызов от МТС всегда идёт на CUCM, от CUCM — всегда в МТС. Поиск по шаблонам номеров не используется, поэтому петель и случайных совпадений нет.
- **Отказоустойчивость CUCM.** Server-group 200: сначала Subscriber, при его отказе Publisher. OPTIONS keepalive исключает недоступную ноду, и CUBE не ждёт таймаута INVITE. Входящие вызовы принимаются от любой из двух нод.
- **Отказоустойчивость МТС.** Если МТС выдаёт два SBC, используется та же схема: server-group 100 + OPTIONS.
- **Early Offer forced.** CUCM может отправлять Delayed Offer, на транке не нужен MTP.
- **Тенанты** привязывают сигнализацию и медиа к своим интерфейсам: Gi1 для МТС, Gi2 для CUCM.
- **Нормализация номеров.** Внутри CUCM используется `+E.164`. В сторону МТС уходит `7XXXXXXXXXX`, международные номера — с префиксом `810`. Короткий внутренний АОН заменяется основным номером.
- **Кодеки.** Основной G.711 A-law, резервные G.711 µ-law и G.729. DTMF — RFC 2833 (`rtp-nte`), факс — T.38 с переходом на G.711 passthrough.
- **REFER и 302** не уходят к МТС: переводы вызовов выполняет CUCM.

## Настройки на стороне CUCM

1. **SIP Trunk Security Profile**: Incoming Transport Type — `TCP+UDP`, порт 5060.
2. **SIP Profile** (копия Standard SIP Profile):
   - `Enable OPTIONS Ping to monitor destination status` — включено;
   - `Early Offer support for voice and video calls` — Best Effort (no MTP inserted).
3. **SIP Trunk** к CUBE:
   - Destination — `10.10.10.1`, порт 5060;
   - Device Pool с Call Manager Group, где есть обе ноды (SUB, PUB);
   - **Run on all Active Unified CM Nodes** — включено;
   - Media Termination Point Required — выключено;
   - DTMF Signaling Method — RFC 2833 или No Preference;
   - Significant Digits — All;
   - Calling/Called Party Transformation CSS — для передачи номеров в формате `+E.164`.
4. **Route Pattern / SIP Route Pattern** на транк, а CSS на входящей стороне транка разрешает только внутренние DID. Это закрывает транзит «МТС → CUCM → МТС».

## Применение

```
copy scp: running-config      ! или вставка по частям в config terminal
write memory
```

Лучше применять секциями: сначала интерфейсы и маршруты, потом `voice service voip` и остальное. Если на Gi1 включить ACL раньше, чем задан маршрут управления, можно потерять доступ к маршрутизатору.

## Проверка

```
show sip-ua status
show voice class server-group 100
show voice class server-group 200
show dial-peer voice summary              ! OPER/KEEPALIVE должны быть active/up
show voice class sip-options-keepalive 100
show voice class sip-options-keepalive 200
show call active voice brief
show sip-ua calls
show voip rtp connections
show call history voice brief
show voice iec description <код>          ! причина разрыва
show license summary
```

Отладка на одном тестовом вызове (не включайте на нагруженной системе):

```
debug ccsip messages
debug voip ccapi inout
test voice translation-rule 100 +74957654321
```

## Что уточнить у МТС

- адреса SBC сигнализации и подсети RTP;
- транспорт (UDP/TCP) и порт;
- формат номеров: 10 или 11 цифр, префикс `7`/`8`, формат международных;
- какой номер ставить в From и в P-Asserted-Identity;
- поддерживаемые кодеки, ptime, T.38;
- отвечает ли SBC на SIP OPTIONS. Если нет, уберите `voice-class sip options-keepalive profile 100` с dial-peer 100.
