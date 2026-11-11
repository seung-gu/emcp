"""테이블 변경 목록. 테이블을 바꿀 때는 MIGRATIONS 끝에 붙이기만 한다. 서버가 처음 DB 에 붙을 때 아직 적용하지
않은 번호만 실행한다 (db.core).
"""

# 순서대로 한 번씩만 실행된다. 이미 적용된 줄은 고치지 말고, 바꿀 게 있으면 끝에 새 줄을 붙인다.
MIGRATIONS = [
    """
    CREATE TABLE devices (
        id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        mac        TEXT,
        name       TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- 토큰이 들어오기 전(1~3단계)에는 MAC 으로 보드를 찾는다. MAC 을 보내지 않는 보드도 한 행에 모인다.
        CONSTRAINT devices_mac_key UNIQUE NULLS NOT DISTINCT (mac)
    )
    """,
    """
    CREATE TABLE reports (
        id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id      BIGINT NOT NULL REFERENCES devices(id),
        received_at    TIMESTAMPTZ NOT NULL,
        fw_version     TEXT,
        -- 보드 측정값
        battery_mv     INT,
        chip_c         DOUBLE PRECISION,
        room_c         DOUBLE PRECISION,
        room_rh        DOUBLE PRECISION,
        -- 연결
        wifi_ms        INT,
        rssi           INT,
        wifi_attempts  INT,
        -- 보드 상태
        reset_reason   INT,
        prev_awake_ms  INT,
        nvs_free       INT,
        nvs_total      INT,
        -- 날씨: 서버가 이 보고에 돌려준 값. 조회에 실패했거나 Neon 으로 옮기기 전 보고는 비어 있다
        city           TEXT,
        lat            DOUBLE PRECISION,
        lon            DOUBLE PRECISION,
        out_temp_c     DOUBLE PRECISION,
        out_humidity   INT,
        out_temp_max_c DOUBLE PRECISION,
        out_temp_min_c DOUBLE PRECISION,
        weather_code   INT,
        wind_kmh       DOUBLE PRECISION,
        pop            INT,
        UNIQUE (device_id, received_at)
    )
    """,
    """
    CREATE TABLE report_raw (
        report_id BIGINT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
        body      JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE sensor_logs (
        report_id  BIGINT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
        room_err   TEXT,
        room_sda   BOOLEAN,
        room_scl   BOOLEAN,
        room_ack   TEXT,
        room_stuck BOOLEAN
    )
    """,
    """
    CREATE TABLE wake_logs (
        report_id     BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
        idx           INT NOT NULL,
        kind          TEXT NOT NULL,
        wifi_attempts INT,
        reset_reason  INT,
        battery_mv    INT,
        wifi_ms       INT,
        rssi          INT,
        wifi_status   INT,
        http_code     INT,
        PRIMARY KEY (report_id, idx)
    )
    """,
    # 칸이 많은 reports 를 묶음별로 보는 창. 데이터는 따로 저장하지 않는다.
    """
    CREATE VIEW report_power AS
    SELECT r.id, d.mac, r.received_at, r.battery_mv, r.chip_c, r.room_c, r.room_rh
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    """
    CREATE VIEW report_network AS
    SELECT r.id, d.mac, r.received_at, r.wifi_ms, r.rssi, r.wifi_attempts
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    """
    CREATE VIEW report_status AS
    SELECT r.id, d.mac, r.received_at, r.reset_reason, r.prev_awake_ms, r.nvs_free, r.nvs_total,
           r.fw_version
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    """
    CREATE VIEW report_weather AS
    SELECT r.id, d.mac, r.received_at, r.city, r.lat, r.lon, r.out_temp_c, r.out_humidity,
           r.out_temp_max_c, r.out_temp_min_c, r.weather_code, r.wind_kmh, r.pop,
           r.room_c, r.room_rh
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    # 2단계: 누가 어떤 보드를 보는지. 보드 하나를 여러 사람이 공유할 수 있어서 devices 와 따로 둔다.
    """
    CREATE TABLE device_access (
        device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        user_id   TEXT NOT NULL,   -- WorkOS 사용자 ID
        role      TEXT NOT NULL CHECK (role IN ('owner', 'viewer')),
        PRIMARY KEY (device_id, user_id)
    )
    """,
    "CREATE INDEX device_access_user ON device_access (user_id)",
    # 3단계: 보드마다 날씨 위치. 비어 있으면 서버 기본 위치(뮌헨)로 조회한다.
    "ALTER TABLE devices ADD COLUMN city TEXT, ADD COLUMN lat DOUBLE PRECISION, ADD COLUMN lon DOUBLE PRECISION",
    # 4단계: 보드 토큰의 SHA-256 해시. 비어 있으면 아직 토큰이 없거나 주인이 등록을 해제한 보드다.
    # MAC 중복 금지(devices_mac_key)는 옛 앱이 토큰 없는 보고를 MAC 으로 찾는 동안 남겨 둔다.
    "ALTER TABLE devices ADD COLUMN token_hash TEXT UNIQUE",
    # 등록 코드 한 줄에 사람(user_id)과 보드(token_hash, mac)가 각자 자기 몫을 적는다. 둘 다 차면 연결한다.
    """
    CREATE TABLE claim_codes (
        code       TEXT PRIMARY KEY,
        user_id    TEXT,
        token_hash TEXT,
        mac        TEXT,
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    "ALTER TABLE wake_logs ADD COLUMN chip_c DOUBLE PRECISION, ADD COLUMN room_c DOUBLE PRECISION,"
    " ADD COLUMN room_rh DOUBLE PRECISION",
    # 서버가 기억해 둘 값 몇 개. 지금은 WorkOS 의 계정 삭제를 어디까지 확인했는지 하나뿐이다.
    "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # 등록된 적 있는 보드도 다른 계정이 등록할 수 있게 MAC 중복 금지를 푼다. 그런 등록은 새 행으로 시작한다.
    # owner_email 은 등록한 사람의 이메일. 다시 등록한 사람과 같으면 예전 기록을 가져올지 묻는다.
    # history_declined 는 그 질문에 "그냥 두기" 를 고른 보드라 다시 묻지 않는다.
    "ALTER TABLE devices DROP CONSTRAINT devices_mac_key, ADD COLUMN owner_email TEXT,"
    " ADD COLUMN history_declined BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX devices_mac ON devices (mac)",
    "ALTER TABLE claim_codes ADD COLUMN email TEXT",
    # 그 보고 때 날씨를 받은 위치의 시간대(IANA 이름, 예: Europe/Berlin). 대시보드가 시각을 현지 시각으로 보여 준다.
    "ALTER TABLE reports ADD COLUMN timezone TEXT",
    # 공유: 주인이 친구 이메일로 공유를 요청하고, 받는 사람이 수락해야(accepted) 보인다. 주인 줄과 지금까지의
    # 줄은 모두 수락한 것으로 둔다. email 은 누구인지 화면에 보여 주는 용도로만 적어 둔다(주인 줄은 등록할 때,
    # 공유받은 사람 줄은 공유할 때). 사람은 여전히 user_id 로 구분한다.
    "ALTER TABLE device_access ADD COLUMN accepted BOOLEAN NOT NULL DEFAULT true, ADD COLUMN email TEXT",
    # 보드마다 펌웨어 자동 업데이트. 켜 두면 최신 릴리스가 아닌 빌드로 보고한 보드에 최신 릴리스를 알려 준다.
    # 테스트 빌드를 USB 로 올릴 때는 주인이 끄고, 다시 켜면 다음 보고 때 최신 릴리스를 받는다.
    "ALTER TABLE devices ADD COLUMN auto_update BOOLEAN NOT NULL DEFAULT true",
    # 등록을 해제한 보드는 누구의 대시보드에도 남기지 않는다(db.boards.release_device). 그 전에 해제한 보드의
    # 주인 줄과 공유 줄을 지운다. 지난 보고는 남아서 같은 이메일로 다시 등록하면 예전 기록으로 가져올 수 있다.
    "DELETE FROM device_access WHERE device_id IN (SELECT id FROM devices WHERE token_hash IS NULL)",
    # 공유는 서로 한다(db.sharing.answer_share). 그 전에 수락한 공유도, 받은 사람이 주인인 보드를 요청한 주인에게
    # 보이게 한다.
    "INSERT INTO device_access (device_id, user_id, role, accepted, email)"
    " SELECT DISTINCT ON (vb.device_id, o.user_id) vb.device_id, o.user_id, 'viewer', true, o.email"
    " FROM device_access v"
    " JOIN device_access o ON o.device_id = v.device_id AND o.role = 'owner'"
    " JOIN device_access vb ON vb.user_id = v.user_id AND vb.role = 'owner'"
    " WHERE v.role = 'viewer' AND v.accepted"
    " ON CONFLICT (device_id, user_id) DO UPDATE SET accepted = true",
    # 지난번 wake 에서 라디오가 켜져 있던 시간(Wi-Fi 연결부터 끊기까지). prev_awake_ms 와 나란히 보면 깨어 있던
    # 시간이 통신(HTTPS)에서 늘었는지 화면 갱신에서 늘었는지 갈린다.
    "ALTER TABLE reports ADD COLUMN prev_radio_ms INT",
    # 보드가 업데이트를 몇 번 받아 보고도 설치하지 못해 포기하면(보고의 ota_failed) 서버가 자동 업데이트를 끄고
    # 그 버전(태그 이름, 모르면 짧은 커밋)을 적어 둔다. 대시보드가 꺼진 이유로 보여 주고, 주인이 다시 켜면 지운다.
    "ALTER TABLE devices ADD COLUMN update_failed TEXT",
]
