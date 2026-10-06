[English](multi-user-auth.md) | [한국어](multi-user-auth.ko.md)

# 여러 사람이 쓰는 서버 — 인증과 기기 등록

지금 서버에는 인증이 없다. `/mcp`는 토큰 없이 호출해도 200을 돌려주고, 그 경로로
`set_weather_location`이나 `turn_on_led` 같은 **쓰기** 도구까지 그대로 실행된다.
`/dashboard`도 주소만 알면 누구나 열 수 있다. 보고는 `reports.jsonl` 한 파일에 쌓이는데
어느 기기가 보낸 건지 구분할 방법이 없다.

이 문서는 여러 사용자가 한 서버를 같이 쓰면서 각자 자기 기기만 볼 수 있게 하는 설계를 정리한
것이다. 작업 순서는 [multi-user-auth-plan.ko.md](multi-user-auth-plan.ko.md)에 있다.

## 접근 주체

| | 설명 | 자격증명 |
|---|---|---|
| 사용자 | 사람 | Google 로그인 |
| Claude | 사용자를 대신해 MCP로 접근하는 제3자 | OAuth 토큰 |
| 기기 | ESP32. 30분마다 보고 | 기기 토큰 |
| 브라우저 | 대시보드 | 세션 쿠키 |

## 인증 경로는 셋, 권한 검사는 한 곳

| 요청 | 신원 확인 | 권한 검사 |
|---|---|---|
| 기기 → `POST /weather` | 기기 토큰 | 자기 데이터만 쓰므로 검사 불필요 |
| Claude → `/mcp` | OAuth 액세스 토큰의 `subject` | `device_access` |
| 브라우저 → `/dashboard` | 세션 쿠키의 `user_id` | `device_access` |

인증 방식은 세 가지지만 권한 검사는 한 군데서만 한다. 어느 경로로 들어오든 `device_access`를
거치므로 권한 규칙이 여러 곳으로 흩어지지 않는다.

```python
def devices_for(user_id):      # 모든 조회가 이 함수를 거친다
    ...JOIN device_access...
```

대신 지켜야 할 규칙이 하나 있다. 핸들러에서 쿼리를 직접 작성하지 않는 것이다. 필터를
빠뜨려도 에러가 나지 않고 데이터가 그대로 나가기 때문에, 검사를 건너뛸 수 있는 **경로를 아예
만들지 않는다**.

## 왜 OAuth가 필요한가

접근 경로가 브라우저뿐이라면 세션 쿠키만으로 충분하다. OAuth가 필요한 건 Claude 때문이다.

Claude는 사용자 본인이 아니라 **사용자를 대신해 요청하는 제3자**다. 여기에 비밀번호를 그대로
넘기면 Claude가 계정 전체를 기한 없이 쓰게 되고, 취소하려면 비밀번호를 바꾸는 방법밖에 없다.
OAuth는 비밀번호와 별개인 토큰을 발급하고, 그 토큰만 따로 폐기할 수 있게 한다.

**OAuth 구현에 드는 비용은 "Claude로 데이터를 다룬다"는 기능을 얻는 대가**로 보면 된다.

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

사용자가 직접 보는 단계는 4번뿐이고, 나머지는 Claude가 알아서 처리한다.

**4단계와 5단계를 나눈 이유**는 4번의 리다이렉트가 브라우저 주소창을 거치기 때문이다. 여기에
액세스 토큰을 실으면 브라우저 히스토리와 로그에 남는다. 그래서 주소창에는 수십 초만 유효한
1회용 code만 싣고, 실제 토큰은 브라우저를 거치지 않는 직접 호출(5번)로 주고받는다.

각 단계를 연결하는 값은 `subject` 하나다:

```
Google 로그인          → users.id
authorize()           → code.subject 에 기록
exchange_auth_code()  → access_token.subject 로 전달
load_access_token()   → 매 호출마다 반환 → current_user()
                      → device_access 로 필터
```

### 직접 구현할 것과 SDK가 해주는 것

MCP Python SDK(`mcp>=1.28`)에는 `OAuthAuthorizationServerProvider` 프로토콜과 라우팅,
PKCE 검증, 에러 형식이 들어 있다. **참고 구현은 없어서** 메서드 9개를 전부 직접 채워야 한다.

`get_client` · `register_client` · `authorize` · `load_authorization_code` ·
`exchange_authorization_code` · `load_refresh_token` · `exchange_refresh_token` ·
`load_access_token` · `revoke_token`

`authorize()` 안에서 사용자를 어떻게 로그인시킬지는 OAuth 스펙이 정해주지 않는다. 사용자
테이블, 세션 쿠키, Google 연동은 전부 직접 만들어야 한다. **OAuth 서버 구현이 오래 걸리는
이유는 대부분 여기에 있다.**

## 왜 Google 로그인인가

개발자가 아닌 사람도 쓸 서버라서 GitHub 계정을 요구하면 그것부터 진입 장벽이 된다. 비밀번호를
직접 받는 방식은 해싱, 가입 폼, 비밀번호 재설정(이메일 발송), 시도 제한까지 전부 같이 만들어야
한다. Google 로그인을 쓰면 이 작업이 전부 없어진다.

Google은 OAuth 위에 OpenID Connect를 얹어 쓰기 때문에 토큰과 함께 **ID 토큰**(서명된 JWT)이
온다. 누가 로그인했는지 알아내려고 따로 호출할 필요가 없다.

사용자 키는 이메일이 아니라 `sub`(Google이 발급하는 고정 식별자)로 잡는다. 이메일은 바뀔 수
있고 재사용되기도 한다.

## 기기 등록

### 최초 부팅

```
Wi-Fi 설정 완료
  → 기기가 난수 토큰과 6자리 코드를 생성해 NVS 에 저장
  → e-Paper 에 코드 표시
  → 첫 보고: { mac, token, claim_code }
  → 서버: pending_registrations 행 생성 (토큰 해시 저장)
```

**토큰은 기기가 직접 생성한다.** 서버가 발급하려면 안전하게 전달할 경로가 필요한데, 서버에서
기기로 먼저 연결할 방법이 없다. 보고 응답에 실어 보내면 MAC만 아는 사람이 먼저 요청해서
받아갈 수 있다. 기기가 만들면 토큰이 네트워크를 타는 구간은 기기에서 서버로 가는 한
방향뿐이다.

### 등록

```
사용자 → Claude 또는 대시보드:  "4J7K2P 등록"
서버: 그 코드의 pending 을 찾아
      devices + device_access(role=owner) 생성
      같은 MAC 의 나머지 pending 폐기
기기: 다음 보고 응답에서 등록됨을 알고 화면에서 코드를 지움
```

등록은 서버 안에서 레코드를 연결하는 게 전부다. **기기로 내려보내는 값은 없다.**

`claim_device(user_id, code)` 함수 하나를 MCP 도구와 대시보드 폼이 같이 호출한다.

### 왜 화면에 코드를 띄우나

기기를 직접 본 사람만 등록할 수 있게 하기 위해서다. 사용자가 여럿이면 미등록 목록에 다른 사람
기기도 같이 보이지만, 코드는 기기 화면에만 표시되므로 등록할 수 없다.

**코드를 MAC에서 유도해 만들면 안 된다.** 누구나 계산할 수 있으면 화면을 봐야 한다는 조건이
사라지고, 서버 비밀값을 섞어 계산하는 방식은 난수를 쓰는 것과 차이가 없다.

### pending의 키를 MAC이 아니라 코드로 잡는 이유

`devices`를 MAC당 한 행으로 잡으면 먼저 등록한 쪽이 그 자리를 차지한다. 남의 MAC을 미리
등록해서 진짜 기기가 등록하지 못하게 막을 수 있다.

`pending_registrations`의 키를 `claim_code`로 두면 같은 MAC에 대기 행이 여러 개 있어도 된다.
남이 만들어 둔 행은 아무도 그 코드를 입력하지 않으니 그대로 만료되고, 진짜 기기의 코드는
화면에 있으니 소유자가 입력하면 된다. 먼저 등록해서 막는 방법 자체가 성립하지 않는다.

## NVS가 지워지면

Arduino-ESP32는 `nvs_flash_init()`이 `ESP_ERR_NVS_NO_FREE_PAGES` 또는
`ESP_ERR_NVS_NEW_VERSION_FOUND`를 내면 **파티션을 통째로 지운다**
(`esp32-hal-misc.c:250-255`). 코드에서 명시적으로 요청하지 않아도 실행된다.

이때 토큰과 등록 코드, Wi-Fi 자격증명이 같이 사라진다. 기기는 설정 포털을 띄우고 토큰과 코드를
새로 만들고, 서버 입장에서는 **이미 등록된 MAC인데 토큰이 맞지 않는** 보고를 받게 된다.

| 서버의 선택 | 결과 |
|---|---|
| 거절 | 안전하지만 기기가 멈춘다. 수동으로 지우고 다시 등록해야 한다 |
| 토큰 교체 | MAC만 알면 남의 기기를 가져갈 수 있다 |
| **재등록 대기로 전환** | 화면에 코드가 다시 뜨고 소유자가 다시 등록한다 |

세 번째 방식을 쓴다. pending 행이 하나 더 생기는 것뿐이라 위에서 설계한 구조를 그대로 쓸 수
있다. 보고 이력은 MAC 기준으로 쌓이므로 재등록해도 끊기지 않는다.

이미 등록된 MAC을 다시 등록할 때 **현재 소유자의 승인**을 받게 하면 몰래 가져가는 경우도
막을 수 있다. 어차피 Wi-Fi를 다시 잡아줘야 하는 상황이라 소유자가 기기 앞에 있는 게 전제된다.

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

토큰은 평문이 아니라 **해시**로 저장한다. DB가 통째로 유출돼도 그대로 쓸 수 있는 자격증명은
넘어가지 않는다.

이 규모(30분에 한 건, 1년에 1MB 미만)에서는 SQLite로 충분하다.

## 공유와 admin

소유권을 `devices`의 컬럼이 아니라 `device_access`의 행으로 관리하면 공유와 admin이 추가
작업 없이 처리된다.

| 기능 | 구현 |
|---|---|
| 공유 | `role=viewer` 행 추가 |
| 공유 취소 | 그 행 삭제 |
| admin | `users.is_admin`, 조회 시 접근 검사 생략 |

계정이 없는 사람에게 공유하려면 대기 행을 만들어 두고, 그 이메일로 처음 로그인할 때 연결한다.

`is_admin`은 접근 검사를 통째로 우회하는 권한이다. 이 서버에는 사용자가 어디 사는지(날씨
도시)와 언제 집에 있는지(기기가 깨어나는 시각)가 들어 있다. 그래서 admin 화면에서는 **기기
개수와 상태만** 보여주고 보고 내용은 조회하지 않는다.

## 대시보드

```
/                    로그인 안 됨 → Google 로그인 버튼
/dashboard           내 기기 목록 + 등록 코드 입력 폼
/dashboard/<mac>     그 기기의 차트와 표
```

`/dashboard/<mac>`에 남의 MAC을 넣으면 **404**를 돌려준다. 403을 주면 "그 기기는 존재하는데
네 것이 아니다"라는 정보를 알려주는 셈이라, MAC을 하나씩 넣어보는 것만으로 누가 무슨 기기를
가졌는지 알아낼 수 있다.

`dashboard.py`의 차트와 표 코드는 수정하지 않는다. `rows`에 이미 걸러진 데이터가 들어온다.

## 알려진 한계

- **MAC은 위조할 수 있다.** 신원 증명은 토큰이 하고 MAC은 식별자 역할만 한다. 등록되지 않은
  MAC으로 가짜 pending을 만들 수는 있지만 아무도 그 코드를 입력하지 않으니 그대로 만료된다.
- **기기에 물리적으로 접근할 수 있으면 등록할 수 있다.** 화면의 코드를 읽을 수 있는 사람은
  누구나 등록 가능하다. 집 안에 두는 기기라면 이 정도면 된다고 봤다. 이미 등록된 기기는
  소유자 승인 절차가 막아준다.
- **admin 계정이 뚫리면 전부 뚫린다.** 위에 적은 완화책 참고.
