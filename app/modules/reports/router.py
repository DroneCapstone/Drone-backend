"""
[Admin] Reports — Export CSV
Prefix: /admin/reports

Endpoints:
  GET /admin/reports/revenue.csv    — Doanh thu theo khoảng thời gian
  GET /admin/reports/missions.csv   — Danh sách missions
  GET /admin/reports/incidents.csv  — Danh sách incidents
  GET /admin/reports/fleet.csv      — Thống kê drone fleet
"""

import csv
import io
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.db import get_async_db
from app.shared.dependencies import CurrentUser, RoleChecker
from app.shared.roles import UserRole
from app.modules.missions.models import Mission, MissionStatus, Order, Incident
from app.modules.fleet.models import Drone

router = APIRouter(prefix="/admin/reports", tags=["Admin - Reports"])
allow_admin = RoleChecker([UserRole.ADMIN])

_COMPLETED = [
    MissionStatus.COMPLETED, MissionStatus.ARRIVAL_COMPLETED,
    MissionStatus.MISSION_COMPLETED,
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Chuyển list[dict] thành StreamingResponse CSV."""
    if not rows:
        output = io.StringIO()
        output.write("No data available\n")
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse YYYY-MM-DD string to naive datetime."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


# ── Revenue CSV ───────────────────────────────────────────────────────────────

@router.get(
    "/revenue.csv",
    summary="Export báo cáo doanh thu (CSV)",
    response_class=StreamingResponse,
)
async def export_revenue_csv(
    from_date: Optional[str] = Query(None, description="Từ ngày YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="Đến ngày YYYY-MM-DD"),
    user: CurrentUser = Depends(allow_admin),
    db: AsyncSession = Depends(get_async_db),
):
    """
    **[Admin] Export doanh thu CSV.**

    Mỗi dòng = 1 mission đã hoàn thành, có: order_id, mission_id, hub route,
    delivery_mode, payload_kg, delivery_fee, completed_at.

    Filter theo `from_date` / `to_date` (YYYY-MM-DD).
    """
    from app.modules.system.models import Hub

    q = (
        select(
            Mission.id.label("mission_id"),
            Mission.order_id,
            Mission.delivery_fee,
            Mission.origin_hub_id,
            Mission.destination_hub_id,
            Mission.payload_weight,
            Mission.arrival_confirmed_at,
            Mission.end_time,
            Order.delivery_mode,
            Order.package_type,
        )
        .join(Order, Order.id == Mission.order_id, isouter=True)
        .where(Mission.status.in_(_COMPLETED))
    )

    dt_from = _parse_date(from_date)
    dt_to   = _parse_date(to_date)
    ref_col = Mission.arrival_confirmed_at

    if dt_from:
        q = q.where(ref_col >= dt_from)
    if dt_to:
        from datetime import timedelta
        q = q.where(ref_col < dt_to + timedelta(days=1))

    q = q.order_by(ref_col.desc().nullslast())
    missions = (await db.execute(q)).all()

    # Lấy hub names
    hub_ids = {r.origin_hub_id for r in missions} | {r.destination_hub_id for r in missions}
    hub_ids.discard(None)
    hub_map: dict[int, str] = {}
    if hub_ids:
        hubs = (await db.execute(select(Hub).where(Hub.id.in_(hub_ids)))).scalars().all()
        hub_map = {h.id: h.name for h in hubs}

    rows = []
    for r in missions:
        completed_at = r.arrival_confirmed_at or r.end_time
        rows.append({
            "mission_id":        r.mission_id,
            "order_id":          r.order_id or "",
            "origin_hub":        hub_map.get(r.origin_hub_id, str(r.origin_hub_id or "")),
            "destination_hub":   hub_map.get(r.destination_hub_id, str(r.destination_hub_id or "")),
            "delivery_mode":     r.delivery_mode.value if r.delivery_mode else "",
            "package_type":      r.package_type or "",
            "payload_kg":        round(float(r.payload_weight or 0), 2),
            "delivery_fee_vnd":  int(r.delivery_fee or 0),
            "completed_at":      completed_at.strftime("%Y-%m-%d %H:%M") if completed_at else "",
        })

    today = datetime.now().strftime("%Y%m%d")
    return _csv_response(rows, f"revenue_{today}.csv")
