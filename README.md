# Cisco C8000V: CUBE для SIP-транка МТС и кластера CUCM

Готовая конфигурация Cisco Catalyst 8000V в роли CUBE (Cisco Unified Border Element) между SIP-транком МТС и кластером Cisco Unified Communications Manager.

| Сторона | Подключение | Транспорт |
|---|---|---|
| МТС | SIP-транк, аутентификация по IP (без регистрации), два SBC | UDP/5060 |
| CUCM | Кластер из двух нод: Subscriber (основная) и Publisher (резервная) | TCP/5060 |

```
  МТС SBC1 / SBC2                                              CUCM SUB (основная)
  198.51.100.10 / .11 ──UDP 5060── Gi1 [ C8000V ] Gi2 ──TCP 5060── 10.10.20.12
                                203.0.113.10   10.10.10.1          CUCM PUB (резерв)
                                                                   10.10.20.11
```

## Содержание

- [Файлы](#файлы)
- [Требования](#требования)
- [Параметры для замены](#параметры-для-замены)
- [Логика работы](#логика-работы)
- [Настройка CUCM](#настройка-cucm)
- [Применение конфигурации](#применение-конфигурации)
- [Проверка](#проверка)
- [Вопросы к МТС](#вопросы-к-мтс)

## Файлы

| Файл | Назначение |
|---|---|
| [`c8000v-cube-mts-cucm.cfg`](c8000v-cube-mts-cucm.cfg) | Конфигурация маршрутизатора |
| `README.md` | Это описание |

## Требования

- Cisco Catalyst 8000V с IOS-XE **17.9** или новее;
- Smart Licensing: базовый уровень `network-essentials` и сессии CUBE по числу каналов;
- два интерфейса: Gi1 смотрит в сторону МТС, Gi2 во внутреннюю сеть с CUCM;
- CUCM 12.5 или новее (настройки ниже даны для этих версий).

## Параметры для замены

Все адреса и номера в конфигурации — примеры. Перед применением замените их на свои.

| Параметр | Пример | Где встречается в конфигурации |
|---|---|---|
| IP WAN и шлюз провайдера | `203.0.113.10/30`, `203.0.113.9` | Gi1, `ACL-WAN-IN`, маршруты |
| IP LAN и шлюз во внутреннюю сеть | `10.10.10.1/24`, `10.10.10.254` | Gi2, маршруты |
| SBC МТС | `198.51.100.10`, `198.51.100.11` | trusted list, `uri 100`, `server-group 100`, `ACL-WAN-IN` |
| Подсеть RTP МТС | `198.51.100.0/24` | `ACL-WAN-IN`, маршрут |
| CUCM Publisher / Subscriber | `10.10.20.11` / `10.10.20.12` | trusted list, `uri 200`, `server-group 200` |
| Основной номер организации | `74951234567` | `translation-rule 101` |
| Число каналов | `30` | `mode border-element license capacity`, `max-conn` |
| Пароли | `<ПАРОЛЬ>` | `username`, `enable` |

> **Важно.** Медиапоток МТС часто идёт не с адресов SBC сигнализации, а из отдельной подсети. Если подсеть RTP в ACL указана неверно, вызов установится, но звука не будет или он будет только в одну сторону.

## Логика работы

### Объекты конфигурации

Номера объектов согласованы: всё, что относится к МТС, начинается с `1xx`, к CUCM — с `2xx`.

| Объект | МТС | CUCM |
|---|---|---|
| Тенант (интерфейс сигнализации и медиа) | `tenant 100` → Gi1 | `tenant 200` → Gi2 |
| Распознавание входящих по IP | `uri 100` | `uri 200` |
| Группа серверов | `server-group 100` | `server-group 200` |
| OPTIONS keepalive | `sip-options-keepalive 100` | `sip-options-keepalive 200` |
| Исходящий dial-peer | `100` | `200` |
| Входящий dial-peer | `101` → `dpg 200` | `201` → `dpg 100` |
| Преобразование номеров | `TP-TO-MTS` (исходящие) | `TP-FROM-MTS` (входящие) |

### Прохождение вызова

**Входящий (МТС → CUCM).** INVITE приходит с SBC МТС. CUBE узнаёт его по адресу в заголовке Via (`uri 100`) и выбирает dial-peer 101. Номера приводятся к `+7XXXXXXXXXX`, вызов передаётся в `dpg 200`. Dial-peer 200 отправляет его на Subscriber, а при недоступности — на Publisher.

**Исходящий (CUCM → МТС).** INVITE приходит с любой из нод CUCM (`uri 200`) на dial-peer 201. Он передаёт вызов в `dpg 100`. Dial-peer 100 приводит номера к формату МТС и отправляет вызов на основной SBC, при недоступности — на резервный.

### Принятые решения

- **Защита от мошеннических вызовов (toll-fraud).** SIP принимается только с адресов SBC МТС и нод CUCM (`ip address trusted list`). Остальные пакеты отбрасываются без ответа (`silent-discard untrusted`). ACL на Gi1 дополнительно ограничивает SIP и RTP адресами МТС.
- **Маршрутизация без шаблонов номеров.** Входящий dial-peer однозначно указывает группу исходящих (`destination dpg`). Вызов от МТС всегда идёт в CUCM, от CUCM — всегда в МТС. Петли и случайные совпадения dial-peer исключены.
- **Резервирование.** Обе стороны описаны группами серверов с приоритетом. SIP OPTIONS раз в 30 секунд проверяют каждый сервер, и недоступный исключается заранее, без ожидания таймаута INVITE.
- **Early Offer forced.** CUBE сам формирует SDP в первом INVITE, поэтому на транке CUCM не нужен MTP.
- **Формат номеров.** Внутри CUCM используется `+E.164`. В сторону МТС уходит `7XXXXXXXXXX`, международные номера — с префиксом `810`. Если CUCM передал короткий внутренний номер, в АОН подставляется основной номер организации.
- **Кодеки.** Основной G.711 A-law, резервные G.711 µ-law и G.729. DTMF передаётся по RFC 2833 (`rtp-nte`), факс — по T.38 с переходом на G.711.
- **Переводы вызовов.** REFER и 302 Moved Temporarily к МТС не передаются: переводы и переадресации выполняет CUCM.
- **Скрытие топологии.** Внутренние адреса не выходят наружу (`address-hiding`), служебные заголовки Cisco удаляются SIP-профилем 100.

## Настройка CUCM

1. **SIP Trunk Security Profile**
   - Incoming Transport Type — `TCP+UDP`;
   - Incoming Port — `5060`.
2. **SIP Profile** (создайте копию Standard SIP Profile)
   - Enable OPTIONS Ping to monitor destination status — **включено**;
   - Early Offer support for voice and video calls — `Best Effort (no MTP inserted)`.
3. **SIP Trunk** к CUBE
   - Destination Address — `10.10.10.1`, порт `5060`;
   - Device Pool — с Call Manager Group, в которой есть обе ноды;
   - Run on all Active Unified CM Nodes — **включено**;
   - Media Termination Point Required — **выключено**;
   - DTMF Signaling Method — `RFC 2833` или `No Preference`;
   - Significant Digits — `All`;
   - Calling/Called Party Transformation CSS — чтобы номера уходили в формате `+E.164`.
4. **Маршрутизация**
   - Route Pattern для выхода в город направьте на этот SIP-транк (через Route List).
   - Во входящем CSS транка оставьте только партиции с внутренними номерами (DID). Так вызов от МТС не сможет снова выйти в МТС через CUCM.

## Применение конфигурации

Применяйте конфигурацию по частям в `configure terminal`, в таком порядке:

1. интерфейсы и маршруты;
2. управление (AAA, SSH, NTP, логирование);
3. `voice service voip` и все `voice class`;
4. преобразования номеров и dial-peer;
5. ACL на Gi1 (`ip access-group ACL-WAN-IN in`) — в последнюю очередь.

> **Важно.** Если включить ACL на Gi1 раньше, чем настроены маршруты и доступ к управлению, можно потерять связь с маршрутизатором.

После проверки сохраните конфигурацию:

```
write memory
```

## Проверка

Состояние транков и групп серверов:

```
show sip-ua status
show dial-peer voice summary              ! dial-peer 100 и 200: OPER active, KEEPALIVE up
show voice class server-group 100
show voice class server-group 200
show voice class sip-options-keepalive 100
show voice class sip-options-keepalive 200
show license summary
```

Активные и завершённые вызовы:

```
show call active voice brief
show sip-ua calls
show voip rtp connections
show call history voice brief
show voice iec description <код>          ! расшифровка причины разрыва
```

Проверка преобразования номеров без реального вызова:

```
test voice translation-rule 100 +74957654321
test voice translation-rule 101 1234
test voice translation-rule 200 84957654321
```

Отладка одного тестового вызова (не включайте на нагруженной системе, после проверки выполните `undebug all`):

```
debug ccsip messages
debug voip ccapi inout
```

## Вопросы к МТС

Перед подключением уточните у МТС:

- адреса SBC сигнализации и подсети RTP;
- транспорт (UDP или TCP) и порт;
- формат номеров: 10 или 11 цифр, префикс `7` или `8`, формат международных;
- какой номер передавать в From и в P-Asserted-Identity;
- поддерживаемые кодеки, ptime и T.38;
- отвечает ли SBC на SIP OPTIONS. Если нет, удалите строку `voice-class sip options-keepalive profile 100` из dial-peer 100, иначе CUBE будет считать транк недоступным.
