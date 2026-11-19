# 🆕 Technician - Những Tính Năng Mới (Thực Sự)

> **Base URL:** `https://isp492.onrender.com/api/v1/technician`

---

## ✨ Chỉ 2 Tính Năng MỚI Thực Sự Gần Đây

### 1️⃣ Current & Next Leg Info trong `/incoming`

**Khi nào:** Commit `6c61d88` (recent)  
**Gì mới:** Trả `current_leg` & `next_leg` trong response

#### GET `/incoming` 

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
- Hiển thị rõ leg đang bay & leg tiếp theo
- Technician biết drone bay từ đâu đến đâu
- `next_leg.battery_id: null` → Biết cần chọn pin nào

---

### 2️⃣ Battery Charge at Arrival

**Khi nào:** Commit `dcf3986` (recent)  
**Gì mới:** Lưu % pin lúc drone hạ cánh

#### POST `/missions/{mission_id}/confirm-arrival`

```json
{
  "current_hub_id": 2,
  "handling_notes": "Drone hạ cánh"
}
```

**Response:**
```json
{
  "mission_status": "ARRIVAL_COMPLETED",
  "handling_status": null,
  "battery_charge_at_arrival": 87
}
```

#### GET `/history` - Hiển thị `battery_charge_at_arrival`

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
- Lưu pin lúc hạ cánh → Phân tích tiêu hao
- Phát hiện pin bị lỗi (tiêu hao quá nhanh)
- Lập báo cáo pin cho từng chuyến bay

---

## ⚠️ Những API KHÔNG phải mới:

Các endpoint này từ branch `feature/transit-mission-relay-flow` (cũ):
- ❌ `POST /missions/{id}/confirm-relay-arrival` 
- ❌ `POST /missions/{id}/swap-battery`
- ❌ `POST /missions/{id}/ready`
- ❌ `POST /missions/{id}/dispatch`

**Lưu Ý:** Mặc dù những API trên không phải mới, nhưng chúng hoạt động kết hợp với `current_leg` & `next_leg` (MỚI) để hỗ trợ multi-leg missions.

---

## 📊 Tóm Tắt

| Tính Năng | Mới? | Khi Nào |
|-----------|------|---------|
| current_leg / next_leg | ✅ **MỚI** | 6c61d88 |
| battery_charge_at_arrival | ✅ **MỚI** | dcf3986 |
| confirm-relay-arrival | ❌ Cũ | f5fff5a |
| swap-battery | ❌ Cũ | f5fff5a |
| ready | ❌ Cũ | f5fff5a |
| dispatch | ❌ Cũ | f5fff5a |

---

**Last Updated:** 2026-10-18  
**Only 2 new features** 
