# 🆕 Technician - Những Tính Năng Mới

> **Base URL:** `https://isp492.onrender.com/api/v1/technician`

---

## ✨ 1. Current & Next Leg Info trong `/incoming`

### GET `/incoming` - Trả `current_leg` & `next_leg`

**Mới: Technician thấy rõ leg đang bay & leg tiếp theo**

```json
{
  "mission_code": "MSN-20261018-0001",
  "drone_id": 28,
  "handling_status": "AT_HUB",
  "current_leg": {
    "id": 1,
    "sequence_no": 1,
    "from_hub_id": 1,
    "to_hub_id": 2,
    "status": "IN_PROGRESS"
  },
  "next_leg": {
    "id": 2,
    "sequence_no": 2,
    "from_hub_id": 2,
    "to_hub_id": 3,
    "battery_id": null,
    "status": "PENDING"
  }
}
```

**Lợi Ích:**
- Biết drone bay từ đâu đến đâu
- Biết leg tiếp theo cần bay tới hub nào
- `next_leg.battery_id: null` → Technician biết cần chọn pin

---

## 🔋 2. Battery Charge at Arrival - Lưu Pin Lúc Hạ Cánh

### POST `/missions/{mission_id}/confirm-arrival` - Lưu `battery_charge_at_arrival`

**Mới: Lưu % pin khi drone hạ cánh → Phân tích tiêu hao**

```json
{
  "current_hub_id": 2,
  "handling_notes": "Drone hạ cánh an toàn"
}
```

**Response:**
```json
{
  "mission_status": "ARRIVAL_COMPLETED",
  "handling_status": null,
  "battery_charge_at_arrival": 87,
  "arrival_confirmed_at": "2026-10-18T14:23:45"
}
```

**GET `/history` - Hiển thị `battery_charge_at_arrival`:**
```json
[
  {
    "mission_code": "MSN-20261018-0001",
    "battery_percent": 87,
    "battery_charge_at_arrival": 87,
    "arrived_at": "2026-10-18T13:15:30"
  }
]
```

**Lợi Ích:**
- Theo dõi tiêu hao pin qua từng leg
- Phát hiện pin bị lỗi (tiêu hao quá nhanh)
- Lập báo cáo pin cho từng chuyến bay

---

## 🚁 3. Relay Hub Flow - Multi-Leg Missions

### POST `/missions/{mission_id}/confirm-relay-arrival`

**Mới: Xác nhận drone tại relay hub trung gian (không phải final hub)**

```json
{
  "current_hub_id": 2,
  "handling_notes": "Drone tại relay hub"
}
```

**Action:**
- Mark `current_leg → COMPLETED`
- Set `mission.handling_status → AT_HUB`
- Drone chuyển `current_hub_id → relay hub`
- Pin cũ được tháo khỏi drone (chuẩn bị swap)

**Response:**
```json
{
  "mission_id": "MSN-20261018-0001",
  "handling_status": "AT_HUB",
  "current_leg": {
    "sequence_no": 1,
    "status": "COMPLETED"
  },
  "next_leg": {
    "sequence_no": 2,
    "from_hub_id": 2,
    "to_hub_id": 3,
    "battery_id": null
  }
}
```

---

### POST `/missions/{mission_id}/swap-battery` (Relay Hub Version)

**Mới: Thay pin tại relay hub, chuẩn bị cho leg tiếp theo**

```json
{
  "current_battery_id": "BAT-001",
  "new_battery_id": "BAT-002",
  "damaged": false,
  "damage_notes": ""
}
```

**Action:**
- Tháo pin cũ (BAT-001)
- Lắp pin mới (BAT-002)
- Pin cũ → Battery Management tại relay hub
- Pin mới được gán cho drone

**Response:**
```json
{
  "message": "Battery swapped successfully",
  "old_battery_id": "BAT-001",
  "new_battery_id": "BAT-002",
  "mission_id": "MSN-20261018-0001"
}
```

---

### POST `/missions/{mission_id}/ready`

**Mới: Đánh dấu drone sẵn sàng bay leg tiếp theo**

```json
{
  "battery_id": "BAT-002",
  "drone_notes": "Drone sẵn sàng, pin 100%, thời tiết OK"
}
```

**Action:**
- Set `mission.handling_status → READY`
- Drone sẵn sàng dispatch

---

### POST `/missions/{mission_id}/dispatch`

**Mới: Dispatch drone tới leg tiếp theo**

```json
{
  "drone_id": 28,
  "battery_id": "BAT-002",
  "weather_conditions": "Clear sky, wind 5 km/h"
}
```

**Action:**
- Set `mission.status → FLYING`
- Drone.status → IN_FLIGHT
- Simulator bắt đầu fly leg tiếp theo

---

## 🏷️ 4. Handling Status - Quản Lý Trạng Thái Drone Tại Hub

**Mới: `handling_status` field hiển thị ở `/incoming` & `/history`**

| Status | Ý Nghĩa |
|--------|---------|
| `INCOMING` | Drone bay tới hub (chưa hạ cánh) |
| `AT_HUB` | Drone hạ cánh, chờ swap pin |
| `READY` | Drone sẵn sàng dispatch |
| `CANNOT_CONTINUE` | Incident - drone không thể tiếp tục |

**Lợi Ích:**
- Rõ ràng trạng thái drone tại hub
- Technician biết cần làm gì kế tiếp
- Filter drone để xử lý ưu tiên

---

## ✅ Workflow Típico - Multi-Leg Mission

```
1. GET /incoming
   ↓ Xem drone bay tới
   
2. POST /missions/{id}/confirm-relay-arrival
   ↓ Xác nhận hạ cánh tại relay hub 2
   
3. GET /batteries (at relay hub 2)
   ↓ Chọn pin mới
   
4. POST /missions/{id}/swap-battery
   ↓ Thay pin BAT-001 → BAT-002
   
5. POST /missions/{id}/ready
   ↓ Đánh dấu sẵn sàng
   
6. POST /missions/{id}/dispatch
   ↓ Dispatch leg 2: hub 2 → hub 3
   
7. Lặp lại từ bước 1 (nếu có leg 3...)
   
8. POST /missions/{id}/confirm-arrival (final hub)
   ↓ Xác nhận hạ cánh cuối cùng
   ↓ `battery_charge_at_arrival` được lưu
```

---

## 🎯 So Sánh Trước vs Sau

| Tính Năng | Trước | Sau |
|-----------|-------|-----|
| **Leg Info** | ❌ Không biết leg nào | ✅ Rõ current_leg & next_leg |
| **Battery at Arrival** | ❌ Không lưu | ✅ Lưu battery_charge_at_arrival |
| **Relay Hub Flow** | ❌ Không có concept | ✅ Đầy đủ relay hub workflow |
| **Status Tracking** | ❌ Chỉ drone status | ✅ Thêm handling_status |
| **Multi-Leg Support** | ❌ Single leg | ✅ Multiple legs hỗ trợ đầy đủ |

---

## 🔍 API Endpoints Mới/Cải Tiến

**Cải Tiến:**
- ✅ `GET /incoming` - Thêm `current_leg` & `next_leg`
- ✅ `GET /history` - Thêm `battery_charge_at_arrival`
- ✅ `POST /confirm-arrival` - Lưu pin lúc hạ cánh

**Mới (Relay Hub):**
- ✅ `POST /missions/{id}/confirm-relay-arrival` - Xác nhận tại relay hub
- ✅ `POST /missions/{id}/swap-battery` - Thay pin tại relay
- ✅ `POST /missions/{id}/ready` - Sẵn sàng dispatch
- ✅ `POST /missions/{id}/dispatch` - Dispatch leg tiếp theo

---

## 💡 Frontend Integration Tips

**1. Show Current vs Next Leg**
```javascript
// Hiển thị leg đang bay & leg tiếp theo
const { current_leg, next_leg } = mission
console.log(`Flying: Hub ${current_leg.from_hub_id} → ${current_leg.to_hub_id}`)
console.log(`Next: Hub ${next_leg.from_hub_id} → ${next_leg.to_hub_id}`)
```

**2. Battery Swap Prompt**
```javascript
// Khi confirm-relay-arrival, hỏi technician thay pin
if (next_leg.battery_id === null) {
  showDialog("Vui lòng chọn pin cho leg tiếp theo")
}
```

**3. Mission History Chart**
```javascript
// Vẽ chart: pin từng leg để phát hiện pin lỗi
const legBatteries = history.map(h => ({
  leg: h.sequence_no,
  start: h.battery_start_pct,
  end: h.battery_charge_at_arrival
}))
```

---

**Last Updated:** 2026-10-18  
**Version:** Latest - Multi-Leg Mission Support
