# 🆕 Technician - Các Tính Năng & Cải Thiện

> **⚠️ PHIÊN BẢN CHÍNH XÁC** - Xem `TECHNICIAN_ACTUAL_API.md` cho API thực tế
>
> **Base URL:** `https://isp492.onrender.com/api/v1/technician`

---

## 📍 1. Live Incoming Drones

### GET `/incoming`

**Trả về drones bay tới hub:**
```json
{
  "mission_code": "MSN-20261018-0001",
  "drone_id": 28,
  "drone_code": "DJI-M300-028",
  "handling_status": "AT_HUB",
  "battery_percent": 87,
  "arrival_confirmed_at": "2026-10-18T14:23:45"
}
```

---

## 🔋 2. Battery Management

### POST `/missions/{mission_id}/swap-battery`

**Thay pin tại relay hub:**
```json
{
  "current_battery_id": "BAT-001",
  "new_battery_id": "BAT-002",
  "damaged": false
}
```

**Response:**
```json
{
  "message": "Battery swapped successfully",
  "old_battery_id": "BAT-001",
  "new_battery_id": "BAT-002"
}
```

---


---

## ✈️ 3. Confirm Arrival

### POST `/missions/{mission_id}/confirm-arrival`

**Xác nhận drone hạ cánh & lưu pin:**
```json
{
  "current_hub_id": 2,
  "handling_notes": "Drone hạ cánh an toàn"
}
```

**Response:**
```json
{
  "handling_status": "AT_HUB",
  "battery_charge_at_arrival": 87
}
```

**✅ Có:** Pin lúc hạ cánh được lưu ✓

---

## 📊 4. Mission History

### GET `/history`

**Lịch sử chuyến bay đã hoàn thành:**
```json
[
  {
    "mission_code": "MSN-20261018-0001",
    "status": "ARRIVAL_COMPLETED",
    "battery_percent": 87,
    "arrived_at": "2026-10-18T13:15:30"
  }
]
```

---

## ⚠️ 5. Maintenance Alerts

### GET `/maintenance-alerts`

**Danh sách alert chưa xử lý:**
```json
[
  {
    "id": 1,
    "drone_code": "DJI-M300-028",
    "title": "Pin BAT-003 hỏng phải thay thế",
    "description": "Pin sức khỏe 15% - phải thay thế ngay",
    "severity": "High",
    "type": "BATTERY_DAMAGED",
    "status": "PENDING",
    "created_at": "2026-10-18T14:30:00"
  }
]
```

**Thêm:**
- `GET /maintenance-alerts/history` — Alert đã xử lý
- `GET /maintenance-alerts/{alert_id}` — Chi tiết
- `PATCH /maintenance-alerts/{alert_id}/acknowledge` — Xác nhận

---

## 🚁 6. Drone Status

| Status | Ý Nghĩa |
|--------|---------|
| **AVAILABLE** | Sẵn sàng, tại hub |
| **IN_FLIGHT** | Đang bay |
| **MAINTENANCE** | Bảo trì/sửa |
| **RETIRED** | Ngừng hoạt động |

### PATCH `/drones/{drone_id}/status`

```json
{
  "status": "MAINTENANCE",
  "reason": "Kiểm tra định kỳ"
}
```

---

## 🔧 Các Endpoint Khác

### Maintenance & Work Orders
- `GET /maintenance-schedules` — Lịch bảo trì
- `POST /work-orders` — Tạo công việc
- `GET /work-orders` — Danh sách công việc

### Hub Inventory
- `GET /hub-inventories` — Tồn kho vật tư
- `PATCH /hub-inventories/{id}/adjust` — Điều chỉnh số lượng

### Relay Hub Flow
- `POST /missions/{id}/confirm-relay-arrival` — Xác nhận tại relay hub
- `POST /missions/{id}/ready` — Sẵn sàng bay tiếp
- `POST /missions/{id}/dispatch` — Dispatch leg tiếp theo

---

## 📌 Quick Workflow

```
1. GET /incoming → Xem drone đến
2. POST /missions/{id}/confirm-arrival → Xác nhận hạ cánh
3. GET /batteries → Chọn pin mới
4. POST /missions/{id}/swap-battery → Thay pin
5. POST /missions/{id}/ready → Sẵn sàng
6. POST /missions/{id}/dispatch → Dispatch
7. Lặp lại bước 1
```

---

## ⚠️ Ghi Chú Quan Trọng

**Những tính năng KHÔNG tự động:**
- ❌ Không tự động set battery cho leg tiếp theo
- ❌ Không tự động tạo MaintenanceAlert khi swap pin
- ❌ Không trả leg info trong `/incoming`

**Xem chi tiết:** `TECHNICIAN_ACTUAL_API.md` để hiểu đầy đủ

---

**Last Updated:** 2026-10-18
