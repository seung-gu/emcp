[English](multi-user-auth-plan.md) | [한국어](multi-user-auth-plan.ko.md)

# 작업 순서

설계는 [multi-user-auth.ko.md](multi-user-auth.ko.md)에 있다. 티켓은 의존 순서대로
나열했고, 각각 단독으로 배포할 수 있는 크기로 나눴다.

앞의 세 개(1·2·3)는 OAuth를 결국 안 넣더라도 그 자체로 쓸모가 있다. 뒤쪽이 밀려도 헛일이
되지 않는다.

---

## 1. 보고에 기기 ID를 싣는다

**왜** 지금 `reports.jsonl`에는 어느 기기가 보낸 보고인지가 없다. 이 값이 없으면 "내 기기만"
걸러낼 기준 자체가 없다. 기기가 한 대뿐인 지금도 아래 티켓 전부의 선행 조건이다.

**범위**
- 펌웨어: `wakeReport()`에 `mac` 추가 (`WiFi.macAddress()`)
- 서버: `weather_report()`가 받아서 행에 저장
- 대시보드: 변화 없음

**완료 조건** 새 보고에 `mac`이 들어 있고, 기존 보고(`mac` 없음)도 대시보드가 그대로 그린다.

**의존** 없음

---

## 2. SQLite로 옮긴다

**왜** 기기별·사용자별 필터링이 들어가면 요청마다 파일 전체를 읽어 파이썬에서 거르는 방식으로는
감당이 안 된다. 앞으로 추가할 테이블도 열 개다.

**범위**
- `reports` 테이블 (`mac, at, battery_mv, wifi_ms, rssi, reset_reason, wifi_attempts, chip_c, prev_awake_ms, nvs_free, nvs_total, fw`)
- `log`는 별도 테이블 또는 JSON 컬럼
- `_append_report` / `_read_reports`를 SQL로 교체 — 나머지 코드는 리스트만 다루므로 수정하지 않는다
- 볼륨 위의 `reports.jsonl`을 한 번 옮기는 마이그레이션 스크립트
- `data/seed-reports.jsonl`은 기록용으로 남긴다

**완료 조건** 대시보드가 이전과 똑같이 보이고, 재배포 후에도 행 수가 유지된다.

**의존** 1

---

## 3. Google 로그인과 세션

**왜** "내 기기"를 구분하려면 서버가 로그인한 사용자를 알아야 한다. OAuth의 `authorize()`도
이 로그인을 그대로 쓰므로 먼저 만들어 둔다.

**범위**
- Google Cloud Console에서 OAuth 클라이언트 생성, 동의 화면 설정
- `users` (`id, google_sub, email, is_admin`), `sessions` (`sid, user_id, expires_at`)
- `/login` → Google, `/auth/callback` → 세션 쿠키 발급
  (`HttpOnly`, `Secure`, `SameSite=Lax`)
- `/logout`
- `/dashboard`를 로그인 뒤로 옮긴다

**완료 조건** 로그인하지 않으면 `/dashboard`에 접근할 수 없고, 로그인하면 지금 화면이 그대로
보인다.

**의존** 없음 (2와 병행 가능)

---

## 4. MCP에 OAuth 서버를 붙인다

**왜** Claude가 사용자를 대신해 도구를 호출하려면 필요하다. 이걸 붙이면 인증 없이 열려 있던
`/mcp`도 같이 막힌다.

**범위**
- `oauth_clients` · `auth_codes` · `access_tokens` · `refresh_tokens` 테이블
- `OAuthAuthorizationServerProvider` 메서드 9개
- `authorize()`는 3번의 세션을 확인하고, 없으면 Google 로그인으로 보낸다
- `FastMCP(auth_server_provider=..., auth=AuthSettings(...))`
- 커넥터를 다시 연결해 흐름 전체를 확인

**완료 조건** 인증 없이 `/mcp`를 호출하면 401이 오고, 커넥터를 다시 연결하면 Google 로그인
화면을 거쳐 도구가 동작한다.

**의존** 3

**주의** 이 티켓이 가장 크다. 더 나눠야 하면 (a) 메타데이터와 클라이언트 등록,
(b) authorize와 token, (c) 검증과 폐기로 쪼갠다.

---

## 5. 기기 토큰과 등록 대기

**왜** 기기가 신원을 증명해야 아무나 남의 기기 데이터에 값을 밀어 넣지 못한다.

**범위**
- 펌웨어: 최초 설정 때 난수 토큰과 6자리 코드를 만들어 NVS에 저장하고, 보고마다 실어 보낸다
- 서버: `pending_registrations` (`claim_code`가 키), `devices`
- `POST /weather`가 토큰 해시를 검증한다. 모르는 기기는 pending으로 받는다
- 등록된 MAC인데 토큰이 다르면 새 pending으로 돌린다 (NVS 초기화 대응)

**완료 조건** 토큰 없는 보고는 거절되고, 새 기기는 pending으로 들어가며, 기존 보고는 그대로
남는다.

**의존** 1, 2

---

## 6. 화면에 등록 코드를 띄운다

**왜** 기기를 직접 본 사람만 등록할 수 있게 하기 위해서다.

**범위**
- 펌웨어: 미등록 상태면 e-Paper에 코드를 띄운다
- 등록되면 다음 보고의 응답으로 확인하고 코드를 지운다
- 서버: 응답에 등록 여부를 싣는다

**완료 조건** 새로 설정한 기기의 화면에 코드가 뜨고, 등록하면 사라진다.

**의존** 5

---

## 7. 소유와 권한 필터

**왜** 이 티켓부터 "자기 기기만 보인다"가 실제로 동작한다.

**범위**
- `device_access` (`mac, user_id, role`)
- `claim_device(user_id, code)` — MCP 도구와 대시보드 폼이 같이 호출한다
- `devices_for(user_id)` — 모든 조회가 이 함수를 거친다
- 핸들러에서 쿼리를 직접 쓰는 곳을 전부 없앤다

**완료 조건** 등록한 기기만 목록에 보이고, 남의 MAC을 주소에 넣으면 404가 온다.

**의존** 4, 5

---

## 8. 대시보드를 기기별로 나눈다

**범위**
- `/dashboard` — 내 기기 목록, 마지막 보고 시각, 등록 코드 입력 폼
- `/dashboard/<mac>` — 지금의 차트와 표. 접근 검사를 통과하지 못하면 404
- `dashboard.py`의 차트·표 코드는 그대로 둔다. `rows`에 이미 걸러진 데이터가 들어온다

**완료 조건** 기기가 두 대 이상일 때 각각 따로 보이고, 서로 섞이지 않는다.

**의존** 7

---

## 9. 공유와 admin

**범위**
- `share_device(mac, email)` / `unshare_device(mac, email)` — MCP 도구와 대시보드
- 계정이 없는 이메일은 대기 행으로 두고 첫 로그인 때 연결
- `users.is_admin` — 기기 개수와 상태만 보여주는 화면. 보고 내용은 조회하지 않는다

**완료 조건** 공유받은 사람에게 그 기기가 보이고, 취소하면 바로 사라진다.

**의존** 7

---

## 10. MCP로 데이터를 조회한다

**왜** 원래 이걸 하려고 시작한 프로젝트다. 대시보드는 상태를 눈으로 확인하는 용도고, 구체적인
질문은 Claude에게 물어본다.

**범위**
- `get_reports(mac, since)` — `devices_for`를 거친다
- `list_devices()`
- `_location`을 전역 변수에서 기기별 값으로 옮긴다. 지금은 모든 사용자가 같은 도시를 쓴다

**완료 조건** Claude에게 "지난주 브라운아웃 몇 번이야" 같은 질문을 하면 자기 기기 데이터로
답한다.

**의존** 7

---

## 순서를 지키지 않아도 되는 것

- **3번**은 1·2와 병행할 수 있다
- **10번**은 7번만 끝나면 언제 해도 되고, 이 프로젝트의 원래 목적에 가장 가깝다
- **9번**은 미뤄도 된다. 혼자 쓰는 동안은 쓸 일이 없다
