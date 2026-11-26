# -*- coding: utf-8 -*-
"""
reset_db_for_defense.py
───────────────────────
Script đưa cơ sở dữ liệu DroneOptAI (25 bảng chuẩn) về trạng thái SẠCH & TỐT NHẤT
để sẵn sàng demo trước hội đồng bảo vệ đồ án.

Nguyên tắc thiết kế:
  1. GIỮ NGUYÊN 100% hạ tầng & danh mục chuẩn (Master Data):
     hubs, accounts, battery_model_catalog, part_categories, hub_supplies, maintenance_schedules.
  2. GIỮ NGUYÊN toàn bộ đội bay (drones) & pin (batteries) đã chuẩn hóa mã, nhưng RESET
     trạng thái về 100% sẵn sàng bay (Available / Active, pin 100%, tháo pin khỏi drone/WO,
     giờ bay = 0, không có drone/pin mồ côi khỏi Hub).
  3. XÓA SẠCH toàn bộ dữ liệu nghiệp vụ phát sinh & nhật ký (17 bảng transactional/logs)
     theo đúng thứ tự khóa ngoại (FK), đồng thời reset các SERIAL/IDENTITY sequence về 1.
  4. AN TOÀN TUYỆT ĐỐI:
     - Đọc DATABASE_URL từ .env (không hardcode mật khẩu).
     - Mặc định chạy chế độ DRY-RUN (chỉ đọc, không sửa DB). Chỉ thực thi khi truyền `--execute`.
     - Toàn bộ thao tác chạy trong 1 PostgreSQL Transaction duy nhất (`engine.begin()`):
       gặp bất kỳ lỗi nào sẽ tự động ROLLBACK 100%.
     - Tự động chụp bản sao lưu JSON trước khi xóa (trừ khi truyền `--no-backup`).

Cách sử dụng:
  1. Xem trước tác động (Dry-run, an toàn 100% - không sửa DB):
       python -u app/shared/database/reset_db_for_defense.py

  2. Thực thi Reset sạch DB (có hỏi xác nhận + tự động backup JSON):
       python -u app/shared/database/reset_db_for_defense.py --execute

  3. Thực thi không hỏi lại (phù hợp chạy tự động):
       python -u app/shared/database/reset_db_for_defense.py --execute --yes
       python -u app/shared/database/reset_db_for_defense.py --execute --yes --no-backup
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

# Bảo vệ chống treo socket/DNS trên Windows (tuân thủ GEMINI.md)
_WATCHDOG_SEC = float(os.getenv("RESET_DB_TIMEOUT_SEC", "60"))
_wd = threading.Timer(
    _WATCHDOG_SEC,
    lambda: (print("\n[WATCHDOG_TIMEOUT] Quá thời gian chờ kết nối DB, tự thoát an toàn.", flush=True), os._exit(2)),
)
_wd.daemon = True
_wd.start()

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Đảm bảo import được `app.*` dù chạy từ bất kỳ thư mục nào
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from sqlalchemy import text
from app.core.timezone import get_vn_datetime, get_vn_naive_datetime
from app.database.db import engine


# ── Phân loại 25 bảng chính thức của hệ thống ──────────────────────────────────

# Nhóm A: Master Data (giữ nguyên cấu trúc & dữ liệu nền)
MASTER_TABLES = [
    "hubs",
    "accounts",
    "battery_model_catalog",
    "part_categories",
    "hub_supplies",
    "maintenance_schedules",
]

# Nhóm B: Đội bay & Pin (KHÔNG BAO GIỜ XÓA — giữ nguyên toàn bộ thiết bị tại tất cả các Hub,
# bao gồm Da Kao Hub (20) & Hiep Binh Hub (21), chỉ reset trạng thái về 100% sẵn sàng)
FLEET_RESET_TABLES = [
    "drones",
    "batteries",
]

# Nhóm C: Dữ liệu nghiệp vụ & nhật ký (XÓA SẠCH theo đúng thứ tự Con -> Cha để thỏa FK)
CLEAR_ORDER = [
    "delivery_ratings",       # FK -> orders, accounts
    "telemetry_logs",         # FK -> missions, drones
    "ai_predictions",         # ref -> missions
    "mission_legs",           # FK -> missions, batteries, hubs
    "replacement_requests",   # FK -> work_orders, drones, batteries, part_categories, hubs
    "parts_requests",         # FK -> work_orders, part_categories, hubs
    "supply_transactions",    # FK -> work_orders, part_categories, hubs
    "maintenance_alerts",     # FK -> work_orders, drones, batteries (đã gỡ vòng lặp FK trước)
    "incidents",              # FK -> missions, drones
    "work_orders",            # FK -> drones, batteries, maintenance_alerts (đã gỡ vòng lặp FK trước)
    "missions",               # FK -> orders, accounts, drones, batteries
    "orders",                 # FK -> accounts, hubs
    "drone_daily_stats",      # FK -> drones
    "saved_addresses",        # FK -> accounts, hubs
    "notifications",          # ref -> accounts, drones, work_orders, missions
    "activity_logs",          # ref -> accounts
    "system_logs",            # system cron logs
]

ALL_25_TABLES = MASTER_TABLES + FLEET_RESET_TABLES + CLEAR_ORDER

# Các bảng có khóa chính BIGSERIAL/SERIAL cần reset sequence về 1 sau khi DELETE
SERIAL_ID_TABLES = [
    "delivery_ratings",
    "telemetry_logs",
    "ai_predictions",
    "mission_legs",
    "replacement_requests",
    "parts_requests",
    "supply_transactions",
    "maintenance_alerts",
    "incidents",
    "work_orders",
    "drone_daily_stats",
    "saved_addresses",
    "notifications",
    "activity_logs",
    "system_logs",
]


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def _fetch_counts(conn, tables: list[str]) -> dict[str, int]:
    """Đếm số dòng hiện có của tất cả bảng tồn tại trong schema public."""
    existing = {
        r[0]
        for r in conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).fetchall()
    }
    counts: dict[str, int] = {}
    present = [t for t in tables if t in existing]
    if not present:
        return counts
    union_sql = " UNION ALL ".join(
        f"SELECT '{t}' AS tbl, COUNT(*)::bigint AS cnt FROM \"{t}\"" for t in present
    )
    for tbl, cnt in conn.execute(text(union_sql)).fetchall():
        counts[tbl] = int(cnt)
    return counts


def _backup_before_reset(conn, tables_to_backup: list[str], output_path: Path) -> None:
    """Chụp nhanh snapshot JSON của các bảng bị sửa/xóa trước khi reset để có thể khôi phục."""
    existing = {
        r[0]
        for r in conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        ).fetchall()
    }
    payload: dict[str, Any] = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": "Auto backup generated by reset_db_for_defense.py before clearing database",
        "tables": {},
    }
    for tbl in tables_to_backup:
        if tbl not in existing:
            continue
        res = conn.execute(text(f'SELECT * FROM "{tbl}"'))
        cols = list(res.keys())
        rows = [list(r) for r in res.fetchall()]
        payload["tables"][tbl] = {"columns": cols, "row_count": len(rows), "rows": rows}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=_json_default)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _seed_demo_incident_mission(
    conn,
    origin_hub_id: int = 16,
    dest_hub_id: int = 17,
) -> dict[str, Any]:
    """
    Tạo 1 mission đang bị sự cố hư linh kiện & rớt gần Hub đích (DISCONNECT_CRASH ở 85% chặng bay)
    khớp 100% logic `_trigger_disconnect_crash` trong `app/shared/drone_simulator.py`:
      - Hiện ngay trên Live Tracking (status='Failed', handling_status='Cannot continue', end_time=NOW)
      - Đầy đủ Order, Mission, MissionLeg (IN_PROGRESS), AI Prediction, 21 TelemetryLogs,
        Incident (High/Open), MaintenanceAlert (Pending), Notifications (Admin/Operator/Tech Hub đích/Customer),
        ActivityLogs, và khóa đúng 1 Drone (Maintenance) + 1 Battery gắn kèm chờ Tech Hub đích thu hồi.
    """
    now_naive = get_vn_naive_datetime()
    now_tz = get_vn_datetime()
    date_tag = now_naive.strftime("%Y%m%d")

    origin = conn.execute(
        text("SELECT id, name, latitude, longitude FROM hubs WHERE id = :hid"),
        {"hid": origin_hub_id},
    ).mappings().first()
    dest = conn.execute(
        text("SELECT id, name, latitude, longitude FROM hubs WHERE id = :hid"),
        {"hid": dest_hub_id},
    ).mappings().first()
    if not origin or not dest:
        raise RuntimeError(f"Không tìm thấy cặp Hub ({origin_hub_id} -> {dest_hub_id}) để seed incident!")

    # Lấy tài khoản thật đang hoạt động
    customer = conn.execute(
        text("""
            SELECT id, full_name, email FROM accounts
            WHERE role_id = 'CUSTOMER' AND is_active = true AND email != 'test@test.com'
            ORDER BY created_at ASC LIMIT 1
        """)
    ).mappings().first()
    operator = conn.execute(
        text("SELECT id, email FROM accounts WHERE role_id = 'OPERATOR' AND is_active = true ORDER BY created_at ASC LIMIT 1")
    ).mappings().first()
    admin_rows = conn.execute(
        text("SELECT id FROM accounts WHERE role_id IN ('ADMIN', 'OPERATOR') AND is_active = true")
    ).fetchall()
    origin_tech = conn.execute(
        text("SELECT id FROM accounts WHERE role_id = 'TECHNICIAN' AND is_active = true AND hub_id = :hid LIMIT 1"),
        {"hid": origin_hub_id},
    ).scalar()
    dest_techs = [
        r[0]
        for r in conn.execute(
            text("SELECT id, email FROM accounts WHERE role_id = 'TECHNICIAN' AND is_active = true AND hub_id = :hid"),
            {"hid": dest_hub_id},
        ).fetchall()
    ]
    dest_tech_email = conn.execute(
        text("SELECT email FROM accounts WHERE role_id = 'TECHNICIAN' AND is_active = true AND hub_id = :hid LIMIT 1"),
        {"hid": dest_hub_id},
    ).scalar()

    # Chọn 1 drone & 1 pin tương thích tại Origin Hub (ưu tiên M100 + TB48)
    drone = conn.execute(
        text("""
            SELECT id, name, model, max_speed FROM drones
            WHERE current_hub_id = :hid AND operational_status = 'Available'
            ORDER BY CASE WHEN model = 'DJI Matrice 100' THEN 0 ELSE 1 END, id ASC
            LIMIT 1
        """),
        {"hid": origin_hub_id},
    ).mappings().first()
    if not drone:
        raise RuntimeError(f"Hub {origin_hub_id} không có drone Available để seed incident!")

    bat_model_pref = (
        "DJI TB48D"
        if drone["model"] == "DJI Matrice 100"
        else ("Aurelia UltraPower 12S" if drone["model"] == "Aurelia X6 Pro" else "DJI DB2000")
    )
    battery = conn.execute(
        text("""
            SELECT id, battery_model, capacity_wh FROM batteries
            WHERE current_hub_id = :hid AND status = 'Active' AND drone_id IS NULL
            ORDER BY CASE WHEN battery_model = :bm THEN 0 ELSE 1 END, id ASC
            LIMIT 1
        """),
        {"hid": origin_hub_id, "bm": bat_model_pref},
    ).mappings().first()
    if not battery:
        raise RuntimeError(f"Hub {origin_hub_id} không có pin Active để seed incident!")

    # Sinh mã Order / Mission kế tiếp chưa tồn tại trong ngày (tránh trùng PK ở chế độ polish)
    existing_ords = [
        r[0]
        for r in conn.execute(
            text("SELECT id FROM orders WHERE id LIKE :pfx"),
            {"pfx": f"ORD-{date_tag}-%"},
        ).fetchall()
    ]
    next_ord_num = 1
    for oid in existing_ords:
        try:
            next_ord_num = max(next_ord_num, int(str(oid).rsplit("-", 1)[-1]) + 1)
        except ValueError:
            pass
    order_id = f"ORD-{date_tag}-{next_ord_num:04d}"

    existing_msns = [
        r[0]
        for r in conn.execute(
            text("SELECT id FROM missions WHERE id LIKE :pfx"),
            {"pfx": f"MSN-{date_tag}-%"},
        ).fetchall()
    ]
    next_msn_num = 1
    for mid in existing_msns:
        try:
            next_msn_num = max(next_msn_num, int(str(mid).rsplit("-", 1)[-1]) + 1)
        except ValueError:
            pass
    mission_id = f"MSN-{date_tag}-{next_msn_num:04d}"

    dist_m = round(
        _haversine_m(float(origin["latitude"]), float(origin["longitude"]), float(dest["latitude"]), float(dest["longitude"])),
        1,
    )
    est_duration_min = round((dist_m / max(float(drone["max_speed"] or 15.0), 10.0)) / 60.0, 2)
    est_energy_wh = round(max(18.0, (dist_m / 1000.0) * 6.5), 2)
    delivery_fee = round(max(25000.0, (dist_m / 1000.0) * 4500.0), -3)

    # Mốc thời gian gần hiện tại để Live Tracking giữ hiển thị trong 10 phút gần nhất
    created_at = now_naive - dt.timedelta(minutes=18)
    origin_recv_at = now_naive - dt.timedelta(minutes=12)
    departed_at = now_naive - dt.timedelta(minutes=7)
    crash_at = now_naive  # Ngay thời điểm hiện tại -> luôn thỏa end_time >= now - 10m

    # Tọa độ rơi tại 85% chặng bay (sát Hub đích -> recovery_hub = dest)
    crash_ratio = 0.85
    inc_lat = round(float(origin["latitude"]) + (float(dest["latitude"]) - float(origin["latitude"])) * crash_ratio, 6)
    inc_lng = round(float(origin["longitude"]) + (float(dest["longitude"]) - float(origin["longitude"])) * crash_ratio, 6)

    # 1. Insert Order (IN_DELIVERY, PAID)
    conn.execute(
        text("""
            INSERT INTO orders (
                id, customer_id, sender_phone, sender_email, receiver_name, receiver_phone,
                package_label, package_type, payload_kg, origin_hub_id, destination_hub_id,
                delivery_mode, requested_delivery_at, estimated_window_start, estimated_window_end,
                planning_at, status, origin_received_at, origin_received_by,
                created_at, updated_at, delivery_fee, payment_status, paid_at, is_aging_priority
            ) VALUES (
                :id, :cust_id, '0909123456', :cust_email, 'Nguyen Van Minh', '0918654321',
                'Medical & Precision Electronics Kit', 'Fragile', 0.8, :orig_id, :dest_id,
                'EXPRESS', :req_at, :win_s, :win_e,
                :plan_at, 'IN_DELIVERY', :orig_recv_at, :orig_tech,
                :created_at, :now_naive, :fee, 'PAID', :created_at, false
            )
        """),
        {
            "id": order_id,
            "cust_id": customer["id"] if customer else None,
            "cust_email": customer["email"] if customer else "customer@droneoptai.com",
            "orig_id": origin_hub_id,
            "dest_id": dest_hub_id,
            "req_at": now_naive + dt.timedelta(minutes=25),
            "win_s": now_naive + dt.timedelta(minutes=5),
            "win_e": now_naive + dt.timedelta(minutes=30),
            "plan_at": origin_recv_at + dt.timedelta(minutes=2),
            "orig_recv_at": origin_recv_at,
            "orig_tech": origin_tech,
            "created_at": created_at,
            "now_naive": now_naive,
            "fee": delivery_fee,
        },
    )

    # 2. Insert Mission (Failed + Cannot continue, chờ Tech Hub đích confirm-arrival thu hồi)
    checkpoints = [
        {
            "hub_id": int(origin["id"]),
            "hub_name": origin["name"],
            "lat": float(origin["latitude"]),
            "lng": float(origin["longitude"]),
            "type": "origin",
        },
        {
            "hub_id": int(dest["id"]),
            "hub_name": dest["name"],
            "lat": float(dest["latitude"]),
            "lng": float(dest["longitude"]),
            "type": "destination",
        },
    ]
    conn.execute(
        text("""
            INSERT INTO missions (
                id, order_id, customer_id, operator_id, drone_id, battery_id,
                payload_weight, distance_m, delivery_fee,
                status, handling_status,
                scheduled_time, start_time, end_time,
                origin_hub_id, destination_hub_id, departed_at, arrived_at,
                arrival_confirmed_by, arrival_confirmed_at,
                leg_destination_type, checkpoints_json
            ) VALUES (
                :id, :order_id, :cust_id, :op_id, :drone_id, :bat_id,
                0.8, :dist_m, :fee,
                'Failed', 'Cannot continue',
                :sched, :start_t, :end_t,
                :orig_id, :dest_id, :departed_at, NULL,
                NULL, NULL,
                'destination', CAST(:cp_json AS jsonb)
            )
        """),
        {
            "id": mission_id,
            "order_id": order_id,
            "cust_id": customer["id"] if customer else None,
            "op_id": operator["id"] if operator else None,
            "drone_id": drone["id"],
            "bat_id": battery["id"],
            "dist_m": dist_m,
            "fee": delivery_fee,
            "sched": departed_at - dt.timedelta(minutes=2),
            "start_t": departed_at,
            "end_t": crash_at,
            "orig_id": origin_hub_id,
            "dest_id": dest_hub_id,
            "departed_at": departed_at,
            "cp_json": json.dumps(checkpoints, ensure_ascii=False),
        },
    )

    # 3. Insert MissionLeg (sequence_no=1, status='IN_PROGRESS' khớp _trigger_disconnect_crash)
    conn.execute(
        text("""
            INSERT INTO mission_legs (
                mission_id, sequence_no, from_hub_id, to_hub_id, battery_id,
                status, started_at, completed_at, created_at, updated_at
            ) VALUES (
                :mid, 1, :orig_id, :dest_id, :bat_id,
                'IN_PROGRESS', :started_at, NULL, :created_at, :now_naive
            )
        """),
        {
            "mid": mission_id,
            "orig_id": origin_hub_id,
            "dest_id": dest_hub_id,
            "bat_id": battery["id"],
            "started_at": departed_at,
            "created_at": origin_recv_at,
            "now_naive": now_naive,
        },
    )

    # 4. Insert AI Prediction
    conn.execute(
        text("""
            INSERT INTO ai_predictions (
                mission_id, estimated_energy_wh, estimated_duration_m,
                risk_level, recommendation_content, is_recommendation_applied, predicted_at
            ) VALUES (
                :mid, :est_e, :est_d,
                'Medium', :rec, true, :pred_at
            )
        """),
        {
            "mid": mission_id,
            "est_e": est_energy_wh,
            "est_d": est_duration_min,
            "rec": (
                f"Direct route {origin['name']} -> {dest['name']} ({dist_m/1000:.2f} km) selected for "
                f"{drone['model']} ({drone['id']}) with {battery['battery_model']}. Moderate coastal crosswind observed."
            ),
            "pred_at": now_tz - dt.timedelta(minutes=10),
        },
    )

    # 5. Insert 21 Telemetry Logs (20 bước bay mượt tới 85% + 1 điểm rơi cuối alt=0, speed=0, volt=0, pct=0)
    steps = 20
    total_flight_sec = max(60.0, (crash_at - departed_at).total_seconds())
    for i in range(1, steps + 1):
        frac = (i / steps) * crash_ratio
        lat_i = round(float(origin["latitude"]) + (float(dest["latitude"]) - float(origin["latitude"])) * frac, 6)
        lng_i = round(float(origin["longitude"]) + (float(dest["longitude"]) - float(origin["longitude"])) * frac, 6)
        ts_i = departed_at + dt.timedelta(seconds=total_flight_sec * (i / (steps + 1)))
        alt_i = round(min(45.0, 12.0 + i * 3.5), 1)
        spd_i = round(float(drone["max_speed"] or 15.0) * (0.92 if i < steps - 1 else 0.55), 2)
        bat_pct_i = round(100.0 - (18.0 * (i / steps)), 1)
        volt_i = round(25.2 - (2.4 * (i / steps)), 2)
        energy_i = round(est_energy_wh * frac, 2)
        conn.execute(
            text("""
                INSERT INTO telemetry_logs (
                    mission_id, timestamp, latitude, longitude, altitude, speed,
                    battery_voltage, battery_pct, energy_consumed_wh, wind_speed
                ) VALUES (
                    :mid, :ts, :lat, :lng, :alt, :spd, :volt, :bpct, :en, 4.8
                )
            """),
            {
                "mid": mission_id, "ts": ts_i, "lat": lat_i, "lng": lng_i,
                "alt": alt_i, "spd": spd_i, "volt": volt_i, "bpct": bat_pct_i, "en": energy_i,
            },
        )
    # Điểm rơi cuối cùng (alt=0, speed=0, battery_voltage=0, battery_pct=0)
    conn.execute(
        text("""
            INSERT INTO telemetry_logs (
                mission_id, timestamp, latitude, longitude, altitude, speed,
                battery_voltage, battery_pct, energy_consumed_wh, wind_speed
            ) VALUES (
                :mid, :ts, :lat, :lng, 0.0, 0.0, 0.0, 0.0, :en, 6.2
            )
        """),
        {"mid": mission_id, "ts": crash_at, "lat": inc_lat, "lng": inc_lng, "en": round(est_energy_wh * crash_ratio, 2)},
    )

    # 6. Khóa Drone -> Maintenance (đang nằm ngoài hiện trường chờ thu hồi) & giữ Battery gắn trên Drone
    conn.execute(
        text("""
            UPDATE drones
            SET operational_status = 'Maintenance',
                current_hub_id = NULL,
                battery_level_pct = 0
            WHERE id = :did
        """),
        {"did": drone["id"]},
    )
    conn.execute(
        text("""
            UPDATE batteries
            SET drone_id = :did,
                current_hub_id = NULL,
                charge_level_pct = 82
            WHERE id = :bid
        """),
        {"did": drone["id"], "bid": battery["id"]},
    )

    # 7. Insert Incident (HIGH / Open — hư linh kiện Motor/ESC & rớt gần Hub đích)
    inc_desc = (
        f"[DISCONNECT / CRASH] Hardware component failure (Motor #2 / ESC overheat & thrust loss) — "
        f"complete telemetry signal loss / drone crash at GPS ({inc_lat:.5f}, {inc_lng:.5f}) "
        f"near {dest['name']}. Field recovery and component replacement assigned to {dest['name']}."
    )
    inc_id = conn.execute(
        text("""
            INSERT INTO incidents (
                mission_id, drone_id, reporter_id, severity, description,
                status, reported_at, requires_technical_inspection
            ) VALUES (
                :mid, :did, :rep_id, 'High', :desc,
                'Open', :rep_at, true
            )
            RETURNING id
        """),
        {
            "mid": mission_id,
            "did": drone["id"],
            "rep_id": operator["id"] if operator else None,
            "desc": inc_desc,
            "rep_at": now_tz,
        },
    ).scalar()

    # 8. Insert MaintenanceAlert (Pending, chờ Tech Hub đích xác nhận thu hồi & tạo Work Order thay linh kiện)
    alert_desc = (
        f"Mission: {mission_id}\n"
        f"Order: {order_id}\n"
        f"Drone: {drone['id']} ({drone['name']})\n"
        f"Incident Type: DISCONNECT_CRASH (Hardware Component Failure — Motor/ESC)\n"
        f"Location: GPS ({inc_lat:.5f}, {inc_lng:.5f})\n"
        f"Recovery Hub: {dest['name']}\n"
        f"Details: Motor #2 / ESC malfunction caused forced crash near {dest['name']}. "
        f"Field recovery & part replacement required."
    )
    conn.execute(
        text("""
            INSERT INTO maintenance_alerts (
                drone_id, source, incident_id, mission_id, title, description, status, created_at
            ) VALUES (
                :did, 'Incident', :inc_id, :mid,
                :title, :desc, 'Pending', :created_at
            )
        """),
        {
            "did": drone["id"],
            "inc_id": inc_id,
            "mid": mission_id,
            "title": f"Component failure & crash near {dest['name']} — Drone {drone['id']}",
            "desc": alert_desc,
            "created_at": crash_at,
        },
    )

    # 9. Insert Notifications cho Admin, Operator, Technician Hub đích và Customer
    for (uid,) in admin_rows:
        conn.execute(
            text("""
                INSERT INTO notifications (
                    user_id, title, message, notifications_type, reference_id,
                    is_read, created_at, drone_id, mission_id
                ) VALUES (
                    :uid, :title, :msg, 'System_Alert', :ref_id,
                    false, :now_tz, :did, :mid
                )
            """),
            {
                "uid": uid,
                "title": f"CRITICAL: Drone Disconnected — Mission #{mission_id}",
                "msg": (
                    f"Drone #{drone['id']} ({drone['name']}) suffered Motor/ESC component failure and crashed "
                    f"at GPS ({inc_lat:.5f}, {inc_lng:.5f}) near {dest['name']}. Recovery team notified."
                ),
                "ref_id": str(inc_id),
                "now_tz": now_tz,
                "did": drone["id"],
                "mid": mission_id,
            },
        )

    for tid in dest_techs:
        conn.execute(
            text("""
                INSERT INTO notifications (
                    user_id, title, message, notifications_type, reference_id,
                    is_read, created_at, drone_id, mission_id
                ) VALUES (
                    :uid, :title, :msg, 'System_Alert', :ref_id,
                    false, :now_tz, :did, :mid
                )
            """),
            {
                "uid": tid,
                "title": f"Field Recovery Needed: Drone #{drone['id']}",
                "msg": (
                    f"Mission #{mission_id} crashed near {dest['name']} at GPS ({inc_lat:.5f}, {inc_lng:.5f}) "
                    f"due to Motor/ESC failure. Please recover drone & package to {dest['name']}, click Confirm Arrival, "
                    f"and create a Work Order for component replacement."
                ),
                "ref_id": str(inc_id),
                "now_tz": now_tz,
                "did": drone["id"],
                "mid": mission_id,
            },
        )

    if customer:
        conn.execute(
            text("""
                INSERT INTO notifications (
                    user_id, title, message, notifications_type, reference_id,
                    is_read, created_at, drone_id, mission_id
                ) VALUES (
                    :uid, :title, :msg, 'Mission_Status', :ref_id,
                    false, :now_tz, :did, :mid
                )
            """),
            {
                "uid": customer["id"],
                "title": f"Delivery Incident Alert — Order {order_id}",
                "msg": (
                    f"Your flight mission #{mission_id} encountered an unexpected technical incident near {dest['name']}. "
                    f"Our ground team is recovering the package and will update you shortly."
                ),
                "ref_id": order_id,
                "now_tz": now_tz,
                "did": drone["id"],
                "mid": mission_id,
            },
        )

    # 10. Insert Activity Logs
    conn.execute(
        text("""
            INSERT INTO activity_logs (
                user_id, user_email, role, action, resource_type, resource_id, description, status, created_at
            ) VALUES
            (:op_id, :op_email, 'OPERATOR', 'CREATE_MISSION', 'mission', :mid,
             :desc1, 'success', :t1),
            (NULL, 'system@droneoptai.com', 'SYSTEM', 'INCIDENT_TRIGGERED', 'incident', :inc_id_str,
             :desc2, 'warning', :t2)
        """),
        {
            "op_id": operator["id"] if operator else None,
            "op_email": operator["email"] if operator else "operator@droneoptai.com",
            "mid": mission_id,
            "desc1": f"Assigned {drone['id']} & {battery['id']} for {order_id} ({origin['name']} -> {dest['name']})",
            "t1": now_tz - dt.timedelta(minutes=9),
            "inc_id_str": str(inc_id),
            "desc2": inc_desc,
            "t2": now_tz,
        },
    )

    return {
        "order_id": order_id,
        "mission_id": mission_id,
        "incident_id": inc_id,
        "origin_hub": f"{origin['id']} ({origin['name']})",
        "dest_hub": f"{dest['id']} ({dest['name']})",
        "dest_tech_email": dest_tech_email,
        "drone_id": drone["id"],
        "battery_id": battery["id"],
        "crash_gps": f"({inc_lat}, {inc_lng})",
    }


def _print_summary_table(before: dict[str, int], after: dict[str, int] | None, clear_tables: list[str]) -> None:
    print("\n" + "=" * 86, flush=True)
    header = f"{'Bảng':<26} | {'Nhóm':<16} | {'Trước':>8} | {'Sau' if after else 'Dự kiến sau':>12}"
    print(header, flush=True)
    print("-" * 86, flush=True)

    for tbl in ALL_25_TABLES:
        if tbl not in before:
            continue
        if tbl in MASTER_TABLES:
            grp = "GIỮ (Master)"
            expected = str(before[tbl])
        elif tbl in FLEET_RESET_TABLES:
            grp = "RESET trạng thái"
            expected = str(before[tbl])
        elif tbl in clear_tables:
            grp = "CLEAR sạch"
            expected = "0"
        else:
            grp = "GIỮ (Tùy chọn)"
            expected = str(before[tbl])

        after_val = str(after.get(tbl, "-")) if after is not None else expected
        print(f"{tbl:<26} | {grp:<16} | {before[tbl]:>8} | {after_val:>12}", flush=True)
    print("=" * 86, flush=True)


def _run_polish_cleanup(conn, now_vn: dt.datetime) -> dict[str, int]:
    """
    Chế độ Polish & Trim (Zero-Empty Clean):
    - Giữ nguyên 100% 4 In Flight + 6 Maintenance drones, các đơn thật, work orders,
      supply_transactions, activity_logs, delivery_ratings, drone_daily_stats.
    - Dọn gọn spam thông báo, giảm bớt đơn/mission hủy dư thừa, đóng bớt incidents/alerts cũ
      (vẫn đảm bảo MỌI trạng thái trên FE của 4 Role đều > 0).
    - Nạp đầy pin yếu đang rảnh ở kho và bổ sung hub_supplies dưới ngưỡng tối thiểu.
    """
    stats: dict[str, int] = {}

    # 1. Sạc đầy pin rảnh tại kho bị < 80% (không đụng pin đang gắn trên drone)
    r_bat = conn.execute(
        text("""
            UPDATE batteries
            SET charge_level_pct = 100, last_charged_at = :now_vn
            WHERE drone_id IS NULL AND status = 'Active' AND charge_level_pct < 80
        """),
        {"now_vn": now_vn},
    )
    stats["recharged_idle_batteries"] = r_bat.rowcount

    # 2. Bổ sung hub_supplies dưới ngưỡng tối thiểu lên mức khỏe
    r_sup = conn.execute(
        text("""
            UPDATE hub_supplies
            SET quantity = GREATEST(quantity, min_stock_threshold + 10, 20), updated_at = NOW()
            WHERE quantity < min_stock_threshold
        """)
    )
    stats["refilled_low_supplies"] = r_sup.rowcount

    # 3. Cắt gọn missions Cancelled dư thừa (giữ lại 2 mission Cancelled mới nhất để biểu đồ > 0)
    excess_cancelled_msns = [
        r[0]
        for r in conn.execute(
            text("""
                SELECT id FROM missions
                WHERE status = 'Cancelled'
                ORDER BY scheduled_time DESC NULLS LAST, id DESC
                OFFSET 2
            """)
        ).fetchall()
    ]
    if excess_cancelled_msns:
        conn.execute(text("DELETE FROM telemetry_logs WHERE mission_id = ANY(:ids)"), {"ids": excess_cancelled_msns})
        conn.execute(text("DELETE FROM ai_predictions WHERE mission_id = ANY(:ids)"), {"ids": excess_cancelled_msns})
        conn.execute(text("DELETE FROM mission_legs WHERE mission_id = ANY(:ids)"), {"ids": excess_cancelled_msns})
        conn.execute(text("UPDATE maintenance_alerts SET mission_id = NULL WHERE mission_id = ANY(:ids)"), {"ids": excess_cancelled_msns})
        conn.execute(text("UPDATE incidents SET mission_id = NULL WHERE mission_id = ANY(:ids)"), {"ids": excess_cancelled_msns})
        conn.execute(text("DELETE FROM missions WHERE id = ANY(:ids)"), {"ids": excess_cancelled_msns})
    stats["trimmed_cancelled_missions"] = len(excess_cancelled_msns)

    # 4. Cắt gọn orders CANCELLED dư thừa (giữ lại 2 đơn CANCELLED mới nhất, chỉ xóa đơn không còn mission nào trỏ tới)
    excess_cancelled_ords = [
        r[0]
        for r in conn.execute(
            text("""
                SELECT o.id FROM orders o
                WHERE o.status = 'CANCELLED'
                  AND NOT EXISTS (SELECT 1 FROM missions m WHERE m.order_id = o.id)
                ORDER BY o.created_at DESC NULLS LAST, o.id DESC
                OFFSET 2
            """)
        ).fetchall()
    ]
    if excess_cancelled_ords:
        conn.execute(text("DELETE FROM delivery_ratings WHERE order_id = ANY(:ids)"), {"ids": excess_cancelled_ords})
        conn.execute(text("DELETE FROM orders WHERE id = ANY(:ids)"), {"ids": excess_cancelled_ords})
    stats["trimmed_cancelled_orders"] = len(excess_cancelled_ords)

    # 5. Đóng bớt incidents Open cũ (giữ lại 2 Open mới nhất -> cộng thêm 1 demo incident = 3 Open, số còn lại Closed > 0)
    r_inc = conn.execute(
        text("""
            UPDATE incidents
            SET status = 'Closed'
            WHERE status = 'Open'
              AND id NOT IN (
                  SELECT id FROM incidents WHERE status = 'Open' ORDER BY reported_at DESC NULLS LAST, id DESC LIMIT 2
              )
        """)
    )
    stats["closed_old_incidents"] = r_inc.rowcount

    # 6. Dọn bớt maintenance_alerts Pending cũ (giữ lại 2 Pending mới nhất -> cộng thêm 1 demo alert = 3 Pending)
    r_al = conn.execute(
        text("""
            UPDATE maintenance_alerts
            SET status = 'Dismissed'
            WHERE status = 'Pending'
              AND id NOT IN (
                  SELECT id FROM maintenance_alerts WHERE status = 'Pending' ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 2
              )
        """)
    )
    stats["dismissed_old_alerts"] = r_al.rowcount

    # 7. Cắt gọn replacement_requests AWAITING_SUPPLY (giữ 2) & REJECTED (giữ 1)
    r_rep1 = conn.execute(
        text("""
            DELETE FROM replacement_requests
            WHERE status = 'AWAITING_SUPPLY'
              AND id NOT IN (
                  SELECT id FROM replacement_requests WHERE status = 'AWAITING_SUPPLY' ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 2
              )
        """)
    )
    r_rep2 = conn.execute(
        text("""
            DELETE FROM replacement_requests
            WHERE status = 'REJECTED'
              AND id NOT IN (
                  SELECT id FROM replacement_requests WHERE status = 'REJECTED' ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 1
              )
        """)
    )
    stats["trimmed_replacement_requests"] = r_rep1.rowcount + r_rep2.rowcount

    # 8. Cắt gọn parts_requests Rejected (giữ 1)
    r_pr = conn.execute(
        text("""
            DELETE FROM parts_requests
            WHERE status = 'Rejected'
              AND id NOT IN (
                  SELECT id FROM parts_requests WHERE status = 'Rejected' ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 1
              )
        """)
    )
    stats["trimmed_parts_requests"] = r_pr.rowcount

    # 9. Cắt gọn spam notifications (1,391 -> giữ lại 30 mới nhất, đánh dấu đã đọc để chuông gọn đẹp)
    r_notif_del = conn.execute(
        text("""
            DELETE FROM notifications
            WHERE id NOT IN (
                SELECT id FROM notifications ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 30
            )
        """)
    )
    conn.execute(text("UPDATE notifications SET is_read = true WHERE is_read = false"))
    stats["trimmed_spam_notifications"] = r_notif_del.rowcount

    # 10. Dọn reset_token cũ trong accounts
    conn.execute(
        text("""
            UPDATE accounts
            SET reset_token = NULL, reset_token_expires_at = NULL
            WHERE reset_token IS NOT NULL OR reset_token_expires_at IS NOT NULL
        """)
    )
    return stats


def run_reset(
    mode: str = "polish",
    execute: bool = False,
    yes: bool = False,
    backup: bool = True,
    keep_saved_addresses: bool = False,
    reactivate_all_accounts: bool = False,
    seed_incident: bool = True,
    incident_origin_hub: int = 16,
    incident_dest_hub: int = 17,
    default_hub_id: int = 1,
) -> None:
    t0 = time.perf_counter()
    clear_tables = [t for t in CLEAR_ORDER if not (keep_saved_addresses and t == "saved_addresses")]

    print(
        f"[{time.perf_counter()-t0:.2f}s] Kết nối Database "
        f"(mode: {mode.upper()} | {'EXECUTE' if execute else 'DRY-RUN'} | seed_incident={seed_incident})...",
        flush=True,
    )
    with engine.connect() as conn:
        before_counts = _fetch_counts(conn, ALL_25_TABLES)
        hub_exists = conn.execute(
            text("SELECT id FROM hubs WHERE id = :hid"), {"hid": default_hub_id}
        ).scalar()
        if hub_exists is None:
            first_hub = conn.execute(text("SELECT id FROM hubs ORDER BY id ASC LIMIT 1")).scalar()
            if first_hub is None:
                raise RuntimeError("Bảng hubs đang trống — dừng lại để tránh làm hỏng dữ liệu đội bay!")
            default_hub_id = int(first_hub)

    if mode == "full":
        _print_summary_table(before_counts, after=None, clear_tables=clear_tables)
    else:
        print(
            "\n[CHẾ ĐỘ POLISH & TRIM — ZERO-EMPTY CLEAN]"
            "\n  - GIỮ NGUYÊN: 4 In Flight + 6 Maintenance drones, toàn bộ 205 drones & 211 pin (gồm Da Kao & Hiep Binh),"
            "\n                lịch sử orders/missions hoàn thành, work_orders (13/1/2), supply_transactions, activity_logs, ratings."
            "\n  - DỌN GỌN   : Cắt spam notifications (~1,391 -> ~30), đóng bớt incidents/alerts cũ (giữ Open & Closed > 0),"
            "\n                cắt bớt đơn/mission hủy thừa (giữ >= 2), sạc pin rảnh yếu & bổ sung vật tư dưới ngưỡng."
            f"\n  - DEMO SEED : {'BẬT — Thêm 1 mission incident mới tại Hub ' + str(incident_origin_hub) + ' -> Hub ' + str(incident_dest_hub) if seed_incident else 'TẮT'}",
            flush=True,
        )

    if not execute:
        print(
            "\n[DRY-RUN] Không có dữ liệu nào bị thay đổi."
            f"\nĐể thực thi, chạy lại với cờ `--execute`:\n"
            f"  python -u app/shared/database/reset_db_for_defense.py --mode {mode} --execute",
            flush=True,
        )
        return

    if not yes:
        confirm = input(
            f"\nXÁC NHẬN chạy chế độ `{mode.upper()}` trên Live Database (có tự động backup JSON)?\n"
            "Nhập 'RESET' để tiếp tục: "
        ).strip()
        if confirm != "RESET":
            print("Đã hủy thao tác. Database giữ nguyên.", flush=True)
            return

    if backup:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = SCRIPT_DIR / f"backup_before_defense_reset_{ts}.json"
        print(f"\n[{time.perf_counter()-t0:.2f}s] Đang chụp backup JSON trước khi sửa -> {backup_path.name}...", flush=True)
        backup_tables = FLEET_RESET_TABLES + ["hub_supplies", "accounts"] + [t for t in clear_tables if t != "system_logs"]
        with engine.connect() as conn:
            _backup_before_reset(conn, backup_tables, backup_path)
        print(f"[{time.perf_counter()-t0:.2f}s] Backup hoàn tất ({backup_path.stat().st_size / 1024:.1f} KB).", flush=True)

    now_vn = get_vn_naive_datetime()
    print(f"\n[{time.perf_counter()-t0:.2f}s] Bắt đầu Transaction ({mode.upper()})...", flush=True)

    seeded_info = None
    with engine.begin() as conn:
        if mode == "polish":
            pstats = _run_polish_cleanup(conn, now_vn)
            for k, v in pstats.items():
                print(f"  - POLISH {k:<28}: {v:>6}", flush=True)
            if reactivate_all_accounts:
                conn.execute(text("UPDATE accounts SET is_active = true WHERE email != 'test@test.com'"))
            if seed_incident:
                seeded_info = _seed_demo_incident_mission(
                    conn,
                    origin_hub_id=incident_origin_hub,
                    dest_hub_id=incident_dest_hub,
                )
                print(
                    f"  - SEED   {'incident_mission':<28}: {seeded_info['mission_id']} "
                    f"({seeded_info['origin_hub']} -> {seeded_info['dest_hub']}, "
                    f"drone={seeded_info['drone_id']}, bat={seeded_info['battery_id']}, "
                    f"tech={seeded_info['dest_tech_email']})",
                    flush=True,
                )
        else:
            # Chế độ FULL: xóa sạch 17 bảng nghiệp vụ & reset 100% đội bay
            conn.execute(text("UPDATE work_orders SET alert_id = NULL WHERE alert_id IS NOT NULL"))
            conn.execute(text("UPDATE maintenance_alerts SET work_order_id = NULL WHERE work_order_id IS NOT NULL"))
            conn.execute(text("UPDATE batteries SET drone_id = NULL, work_order_id = NULL"))

            existing = {
                r[0]
                for r in conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
                ).fetchall()
            }
            for tbl in clear_tables:
                if tbl not in existing:
                    continue
                res = conn.execute(text(f'DELETE FROM "{tbl}"'))
                print(f"  - DELETE {tbl:<24}: {res.rowcount:>6} dòng", flush=True)

            for tbl in SERIAL_ID_TABLES:
                if tbl not in clear_tables or tbl not in existing:
                    continue
                seq_name = conn.execute(
                    text("SELECT pg_get_serial_sequence(:tbl, 'id')"), {"tbl": f"public.{tbl}"}
                ).scalar()
                if seq_name:
                    conn.execute(text("SELECT setval(:seq, 1, false)"), {"seq": seq_name})

            res_drones = conn.execute(
                text("""
                    UPDATE drones
                    SET operational_status = 'Available',
                        battery_level_pct  = 100,
                        utilization_pct    = 0.0,
                        total_flight_hours = 0.0,
                        last_maintenance_at = NULL,
                        current_hub_id     = COALESCE(current_hub_id, :default_hub_id)
                """),
                {"default_hub_id": default_hub_id},
            )
            print(f"  - RESET  {'drones':<24}: {res_drones.rowcount:>6} drone -> Available (100% pin, 0h bay)", flush=True)

            res_bats = conn.execute(
                text("""
                    UPDATE batteries
                    SET status           = 'Active',
                        charge_level_pct = 100,
                        drone_id         = NULL,
                        work_order_id    = NULL,
                        last_charged_at  = :now_vn,
                        current_hub_id   = COALESCE(current_hub_id, :default_hub_id)
                """),
                {"now_vn": now_vn, "default_hub_id": default_hub_id},
            )
            print(f"  - RESET  {'batteries':<24}: {res_bats.rowcount:>6} pin   -> Active (100% pin, đã tháo rời)", flush=True)

            res_sup = conn.execute(
                text("""
                    UPDATE hub_supplies
                    SET quantity   = GREATEST(quantity, min_stock_threshold + 10, 20),
                        updated_at = NOW()
                    WHERE quantity < GREATEST(min_stock_threshold + 10, 20)
                """)
            )
            print(f"  - REFILL {'hub_supplies':<24}: {res_sup.rowcount:>6} dòng vật tư được bổ sung mức tồn kho khỏe", flush=True)

            conn.execute(
                text("""
                    UPDATE accounts
                    SET reset_token = NULL,
                        reset_token_expires_at = NULL
                    WHERE reset_token IS NOT NULL OR reset_token_expires_at IS NOT NULL
                """)
            )
            if reactivate_all_accounts:
                conn.execute(text("UPDATE accounts SET is_active = true WHERE email != 'test@test.com'"))

            if seed_incident:
                seeded_info = _seed_demo_incident_mission(
                    conn,
                    origin_hub_id=incident_origin_hub,
                    dest_hub_id=incident_dest_hub,
                )
                print(
                    f"  - SEED   {'incident_mission':<24}: {seeded_info['mission_id']} "
                    f"({seeded_info['origin_hub']} -> {seeded_info['dest_hub']}, "
                    f"drone={seeded_info['drone_id']}, bat={seeded_info['battery_id']}, "
                    f"tech={seeded_info['dest_tech_email']})",
                    flush=True,
                )

    # Post-Reset Health Check (Read-Only)
    print(f"\n[{time.perf_counter()-t0:.2f}s] Đang chạy Post-Reset Health Check...", flush=True)
    with engine.connect() as conn:
        after_counts = _fetch_counts(conn, ALL_25_TABLES)
        drone_breakdown = dict(
            conn.execute(text("SELECT operational_status::text, COUNT(*) FROM drones GROUP BY 1")).fetchall()
        )
        low_sup = conn.execute(
            text("SELECT COUNT(*) FROM hub_supplies WHERE quantity < min_stock_threshold")
        ).scalar()

    _print_summary_table(before_counts, after=after_counts, clear_tables=clear_tables if mode == "full" else [])
    print(f"\n[HEALTH CHECK] drones={drone_breakdown} | low_supplies={low_sup}", flush=True)
    if low_sup:
        raise RuntimeError("Post-Reset Health Check phát hiện hub_supplies vẫn dưới ngưỡng!")

    if seeded_info:
        print(
            "\n[DEMO INCIDENT READY]"
            f"\n  - Order / Mission : {seeded_info['order_id']} / {seeded_info['mission_id']} (Incident #{seeded_info['incident_id']})"
            f"\n  - Tuyến bay       : {seeded_info['origin_hub']} -> {seeded_info['dest_hub']} | GPS rơi: {seeded_info['crash_gps']}"
            f"\n  - Thiết bị khóa   : Drone {seeded_info['drone_id']} (Maintenance) + Pin {seeded_info['battery_id']}"
            f"\n  - Tech xử lý      : {seeded_info['dest_tech_email']} (đăng nhập xem /incoming -> Confirm Arrival -> Work Order)"
            f"\n  - Chuyến bay sau  : Simulator mặc định TẮT incident (100% SUCCESS cho mọi chuyến bay demo tiếp theo).",
            flush=True,
        )

    print(
        f"\n[{time.perf_counter()-t0:.2f}s] HOÀN TẤT ({mode.upper()}) SẴN SÀNG BẢO VỆ ĐỒ ÁN!"
        "\nLưu ý: Nếu backend server đang bật cache RAM (hubs/routes), hãy restart uvicorn sau khi chạy.",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear & reset DroneOptAI database về trạng thái sạch chuẩn bảo vệ đồ án."
    )
    parser.add_argument(
        "--mode",
        choices=("polish", "full"),
        default="polish",
        help="Chế độ dọn DB: 'polish' (mặc định — giữ mọi chỉ số FE > 0, giữ 4 In Flight & 6 Maintenance, chỉ cắt rác/spam) hoặc 'full' (xóa trắng 17 bảng nghiệp vụ).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Thực thi thay đổi lên Database (mặc định không truyền là DRY-RUN chỉ đọc).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Bỏ qua bước nhập 'RESET' xác nhận thủ công.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Không xuất file JSON backup trước khi xóa.",
    )
    parser.add_argument(
        "--keep-saved-addresses",
        action="store_true",
        help="Giữ lại danh bạ địa chỉ đã lưu của Customer (saved_addresses).",
    )
    parser.add_argument(
        "--reactivate-accounts",
        action="store_true",
        help="Bật lại is_active=true cho toàn bộ tài khoản (trừ test@test.com).",
    )
    parser.add_argument(
        "--no-seed-incident",
        action="store_true",
        help="Không tạo sẵn 1 mission sự cố sau khi clear (để DB trống hoàn toàn 0 mission).",
    )
    parser.add_argument(
        "--origin-hub",
        type=int,
        default=16,
        help="Hub xuất phát cho mission incident mẫu (mặc định: 16 - Vung Tau Hub).",
    )
    parser.add_argument(
        "--dest-hub",
        type=int,
        default=17,
        help="Hub đích / thu hồi cho mission incident mẫu (mặc định: 17 - Tam Thang Hub).",
    )
    args = parser.parse_args()

    try:
        run_reset(
            mode=args.mode,
            execute=args.execute,
            yes=args.yes,
            backup=not args.no_backup,
            keep_saved_addresses=args.keep_saved_addresses,
            reactivate_all_accounts=args.reactivate_accounts,
            seed_incident=not args.no_seed_incident,
            incident_origin_hub=args.origin_hub,
            incident_dest_hub=args.dest_hub,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
