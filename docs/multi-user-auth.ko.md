[English](multi-user-auth.md) | [한국어](multi-user-auth.ko.md)

# 여러 사람이 쓰는 서버: 인증과 기기 등록

예전 설계를 남겨 둔 문서입니다. 지금 설계와 다른 곳은 [multi-user-rollout.ko.md](multi-user-rollout.ko.md)를 따릅니다.

지금 서버에는 인증이 없습니다. `/mcp`는 토큰 없이 불러도 200을 돌려주고, `set_weather_location`이나 `turn_on_led` 같은 쓰기 도구도 그대로 실행됩니다. `/dashboard`도 주소만 알면 누구나 열 수 있습니다. 보고는 `reports.jsonl` 한 파일에 쌓이는데, 어느 기기가 보낸 것인지 구분할 방법이 없습니다.

여기서는 여러 사용자가 한 서버를 같이 쓰면서 각자 자기 기기만 보게 하는 설계를 다룹니다. 작업 순서는 [multi-user-auth-plan.ko.md](multi-user-auth-plan.ko.md)에 있습니다.

## 접근 주체

| | 설명 | 자격증명 |
|---|---|---|
| 사용자 | 사람 | Google 로그인 |
| Claude | 사용자를 대신해 MCP로 접근하는 제3자 | OAuth 토큰 |
| 기기 | ESP32. 30분마다 보고 | 기기 토큰 |
| 브라우저 | 대시보드 | 세션 쿠키 |

## 인증 경로 셋, 권한 검사 한 곳

| 요청 | 신원 확인 | 권한 검사 |
|---|---|---|
| 기기 → `POST /weather` | 기기 토큰 | 자기 데이터만 쓰므로 검사하지 않음 |
| Claude → `/mcp` | OAuth 액세스 토큰의 `subject` | `device_access` |
| 브라우저 → `/dashboard` | 세션 쿠키의 `user_id` | `device_access` |

인증 방식은 세 가지지만 권한 검사는 한 군데에서 합니다. 어느 경로로 들어오든 `device_access`를 거치므로 권한 규칙이 여러 곳에 흩어지지 않습니다.

```python
def devices_for(user_id):      # 모든 조회가 이 함수를 거친다
    ...JOIN device_access...
```

그러려면 핸들러에서 쿼리를 직접 쓰면 안 됩니다. 필터를 빠뜨려도 에러 없이 데이터가 그대로 나가기 때문에, 검사를 건너뛸 수 있는 경로를 아예 두지 않습니다.

## 왜 OAuth가 필요한가

접근 경로가 브라우저뿐이라면 세션 쿠키로 충분합니다. OAuth가 필요한 건 Claude 때문입니다.

Claude는 사용자를 대신해 요청하는 제3자입니다. Claude에게 비밀번호를 그대로 넘기면 계정 전체를 기한 없이 쓰게 되고, 취소하려면 비밀번호를 바꾸는 수밖에 없습니다. OAuth는 비밀번호와 따로 토큰을 발급하고, 그 토큰만 폐기할 수 있게 합니다. OAuth를 구현하는 수고는 Claude로 데이터를 다루는 기능을 얻는 값입니다.

## OAuth 흐름

```
1. Claude → /mcp (토큰 없음)      → 401 + 어디서 인증하는지 안내
2. Claude → 메타데이터 조회        → authorize / token 주소 발견
3. Claude → 자기 등록              register_client
4. 브라우저 → /authorize           사용자가 Google 로 로그인
                                   → code 를 달고 Claude 로 리다이렉트
5. Claude → /token (code)          load_authorization_code
                                   exchange_authorization_code → 토큰 발급
6. Claude → /mcp (Bearer)          load_access_token            ← 매 호출
7. 만료                            exchange_refresh_token
8. 연결 해제                        revoke_token
```

사용자가 직접 보는 단계는 4번뿐이고, 나머지는 Claude가 알아서 합니다.

4단계와 5단계를 나눈 건 4번의 리다이렉트가 브라우저 주소창을 거치기 때문입니다. 여기에 액세스 토큰을 실으면 브라우저 방문 기록과 로그에 남습니다. 그래서 주소창에는 수십 초만 유효한 1회용 code만 싣고, 실제 토큰은 브라우저를 거치지 않는 직접 호출(5번)로 주고받습니다.

각 단계를 잇는 값은 `subject` 하나입니다.

```
Google 로그인          → users.id
authorize()           → code.subject 에 기록
exchange_auth_code()  → access_token.subject 로 전달
load_access_token()   → 매 호출마다 반환 → current_user()
                      → device_access 로 필터
```

### 직접 구현할 것과 SDK가 해 주는 것

MCP Python SDK(`mcp>=1.28`)에는 `OAuthAuthorizationServerProvider` 프로토콜과 라우팅, PKCE 검증, 에러 형식이 들어 있습니다. 참고 구현은 없어서 메서드 9개를 전부 직접 채워야 합니다.

`get_client` · `register_client` · `authorize` · `load_authorization_code` ·
`exchange_authorization_code` · `load_refresh_token` · `exchange_refresh_token` ·
`load_access_token` · `revoke_token`

`authorize()` 안에서 사용자를 어떻게 로그인시킬지는 OAuth 스펙에 없습니다. 사용자 테이블, 세션 쿠키, Google 연동을 전부 직접 만들어야 하고, OAuth 서버 구현이 오래 걸리는 이유도 대부분 여기에 있습니다.

## 왜 Google 로그인인가

개발자가 아닌 사람도 쓸 서버라서 GitHub 계정을 요구하면 그것부터 진입 장벽이 됩니다. 비밀번호를 직접 받으려면 해싱, 가입 폼, 비밀번호 재설정(이메일 발송), 시도 제한까지 같이 만들어야 합니다. Google 로그인을 쓰면 이 작업이 전부 없어집니다.

Google은 OAuth 위에 OpenID Connect를 얹어 쓰기 때문에 토큰과 함께 ID 토큰(서명된 JWT)이 옵니다. 누가 로그인했는지 알아내려고 따로 호출할 필요가 없습니다.

사용자 키는 `sub`(Google이 발급하는 고정 식별자)로 잡습니다. 이메일은 바뀔 수 있고 재사용되기도 해서 키로 쓰지 않습니다.

## 기기 등록

### 최초 부팅

```
Wi-Fi 설정 완료
  → 기기가 난수 토큰과 6자리 코드를 생성해 NVS 에 저장
  → e-Paper 에 코드 표시
  → 첫 보고: { mac, token, claim_code }
  → 서버: pending_registrations 행 생성 (토큰 해시 저장)
```

토큰은 기기가 직접 만듭니다. 서버가 발급하려면 기기에 안전하게 건넬 길이 있어야 하는데, 서버가 기기에 먼저 연결할 방법이 없습니다. 보고 응답에 실어 보내면 MAC만 아는 사람이 먼저 요청해서 받아 갈 수 있습니다. 기기가 만들면 토큰은 기기에서 서버로 가는 한 방향으로만 네트워크를 탑니다.

### 등록

```
사용자 → Claude 또는 대시보드:  "4J7K2P 등록"
서버: 그 코드의 pending 을 찾아
      devices + device_access(role=owner) 생성
      같은 MAC 의 나머지 pending 폐기
기기: 다음 보고 응답에서 등록됨을 알고 화면에서 코드를 지움
```

등록은 서버 안에서 레코드를 잇는 것으로 끝나고, 기기로 내려보내는 값은 없습니다.

MCP 도구와 대시보드 폼이 `claim_device(user_id, code)` 함수 하나를 같이 부릅니다.

### 왜 화면에 코드를 띄우나

기기를 직접 본 사람만 등록할 수 있어야 해서입니다. 사용자가 여럿이면 미등록 목록에 남의 기기도 보이지만, 코드는 그 기기 화면에만 뜨므로 남은 등록할 수 없습니다.

코드를 MAC에서 계산해 만들면 안 됩니다. 누구나 계산할 수 있으면 화면을 봐야 한다는 조건이 무의미해지고, 서버 비밀값을 섞어 계산한다면 난수를 쓰는 것과 다를 게 없습니다.

### pending의 키를 코드로 잡는 이유

`devices`를 MAC당 한 행으로 잡으면 먼저 등록한 쪽이 그 자리를 차지합니다. 남의 MAC을 미리 등록해서 진짜 기기가 등록하지 못하게 막을 수 있습니다.

`pending_registrations`의 키를 `claim_code`로 두면 같은 MAC에 대기 행이 여러 개 있어도 됩니다. 남이 만들어 둔 행은 아무도 그 코드를 입력하지 않으니 그대로 만료되고, 진짜 기기의 코드는 화면에 있으니 소유자가 입력하면 됩니다. 먼저 등록해서 막는 방법이 통하지 않습니다.

## NVS가 지워지면

Arduino-ESP32는 `nvs_flash_init()`이 `ESP_ERR_NVS_NO_FREE_PAGES` 또는 `ESP_ERR_NVS_NEW_VERSION_FOUND`를 내면 파티션을 통째로 지웁니다(`esp32-hal-misc.c:250-255`). 코드에서 따로 요청하지 않아도 그렇게 합니다.

그러면 토큰, 등록 코드, Wi-Fi 자격증명이 함께 사라집니다. 기기는 설정 포털을 띄우고 토큰과 코드를 새로 만들며, 서버는 이미 등록된 MAC인데 토큰이 맞지 않는 보고를 받습니다.

| 서버의 선택 | 결과 |
|---|---|
| 거절 | 안전하지만 기기가 멈춤. 수동으로 지우고 다시 등록해야 함 |
| 토큰 교체 | MAC만 알면 남의 기기를 가져갈 수 있음 |
| 재등록 대기로 전환 | 화면에 코드가 다시 뜨고 소유자가 다시 등록함 |

세 번째인 재등록 대기 전환을 씁니다. pending 행이 하나 더 생길 뿐이라 위의 구조를 그대로 쓸 수 있습니다. 보고 이력은 MAC 기준으로 쌓이므로 재등록해도 끊기지 않습니다.

이미 등록된 MAC을 다시 등록할 때 현재 소유자의 승인을 받게 하면 몰래 가져가는 경우도 막을 수 있습니다. 어차피 Wi-Fi를 다시 잡아 줘야 하는 상황이라 소유자는 기기 앞에 있을 수밖에 없습니다.

## 테이블

```
── 사람 ────────────────────────────────────────────
users                  id, google_sub, email, is_admin
sessions               sid, user_id, expires_at

── OAuth (Claude 용) ───────────────────────────────
oauth_clients          client_id, redirect_uris, ...
auth_codes             code, client_id, subject, code_challenge, expires_at
access_tokens          token_hash, client_id, subject, expires_at
refresh_tokens         token_hash, client_id, subject, expires_at

── 기기 ────────────────────────────────────────────
pending_registrations  claim_code, mac, token_hash, expires_at
devices                mac, name, token_hash
device_access          mac, user_id, role          owner / viewer
reports                mac, at, battery_mv, ...
```

토큰은 해시로 저장합니다. DB가 통째로 유출돼도 바로 쓸 수 있는 자격증명은 넘어가지 않습니다.

이 규모(30분에 한 건, 1년에 1MB 미만)에서는 SQLite로 충분합니다.

## 공유와 admin

소유권을 `devices`의 컬럼으로 두지 않고 `device_access`의 행으로 관리하면, 공유와 admin은 추가 작업 없이 해결됩니다.

| 기능 | 구현 |
|---|---|
| 공유 | `role=viewer` 행 추가 |
| 공유 취소 | 그 행 삭제 |
| admin | `users.is_admin`, 조회 시 접근 검사 생략 |

계정이 없는 사람에게 공유하려면 대기 행을 만들어 두고, 그 이메일로 처음 로그인할 때 연결합니다.

`is_admin`은 접근 검사를 통째로 건너뛰는 권한입니다. 이 서버에는 사용자가 어디 사는지(날씨 도시)와 언제 집에 있는지(기기가 깨어나는 시각)가 들어 있습니다. 그래서 admin 화면에서는 기기 개수와 상태만 보여 주고 보고 내용은 조회하지 않습니다.

## 대시보드

```
/                    로그인 안 됨 → Google 로그인 버튼
/dashboard           내 기기 목록 + 등록 코드 입력 폼
/dashboard/<mac>     그 기기의 차트와 표
```

`/dashboard/<mac>`에 남의 MAC을 넣으면 404를 돌려줍니다. 403을 주면 "그 기기는 있는데 네 것이 아니다"라고 알려 주는 셈이라, MAC을 하나씩 넣어 보는 것만으로 누가 무슨 기기를 가졌는지 알아낼 수 있습니다.

`dashboard/`의 차트와 표 코드는 고치지 않습니다. `rows`에 이미 걸러진 데이터가 들어옵니다.

## 알려진 한계

- MAC은 위조할 수 있습니다. 신원은 토큰으로 증명하고 MAC은 식별자일 뿐입니다. 등록되지 않은 MAC으로 가짜 pending을 만들 수는 있어도, 아무도 그 코드를 입력하지 않으니 그대로 만료됩니다.
- 기기에 손이 닿는 사람은 등록할 수 있습니다. 화면의 코드를 읽을 수 있으면 누구나 등록할 수 있는데, 집 안에 두는 기기라면 이 정도면 된다고 봤습니다. 이미 등록된 기기는 소유자 승인 절차가 막아 줍니다.
- admin 계정이 뚫리면 전부 뚫립니다. 완화책은 위의 "공유와 admin"에 적었습니다.
