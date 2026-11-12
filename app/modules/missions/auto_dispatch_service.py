"""
app/modules/missions/auto_dispatch_service.py
─────────────────────────────────────────────
Dịch vụ Tự động Điều phối Nhiệm vụ bay (Auto-Dispatch) thay thế toàn bộ
bước chọn Drone, chọn Pin, chọn Route và tạo Mission thủ công của Operator,
đồng thời giữ lại hệ thống giải thích XAI SHAP minh bạch (tránh black-box).

Quy ước ngôn ngữ:
- Comment & docstring nội bộ: Tiếng Việt.
- Toàn bộ thông điệp trả về FE (HTTPException, SHAP labels/impacts/counterfactuals, summary): Tiếng Anh kỹ thuật tự nhiên.
"""

from typing import Any, Awaitable, Callable, Optional
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai_predictions.service import haversine_distance
from app.modules.fleet.models import (
    Battery,
    Drone,
    DroneStatus,
    batteries_status,
    compute_battery_lifecycle,
)
from app.modules.missions.bundle_service import (
    ACTIVE_MISSION_STATUSES,
    resolve_effective_origin_hub_id,
    validate_bundle_orders,
)
from app.modules.missions.models import (
    DeliveryMode,
    Mission,
    MissionStatus,
    Order,
    OrderStatus,
    PaymentStatus,
)
from app.modules.missions.schemas import (
    AutoDispatchXAIReport,
    DecisionShapExplanation,
    RouteOptionSchema,
    ShapFeatureAttribution,
)
from app.modules.missions.technician.router import get_compatible_battery_models
from app.modules.system.models import Hub


def _compute_additive_shap(
    candidates: list[dict[str, Any]],
    selected_idx: int,
    feature_specs: list[tuple[str, str, float]],
    why_selected: str,
    counterfactual_builder: Callable[[dict[str, Any], float], dict[str, Any]],
    max_counterfactuals: int = 3,
) -> DecisionShapExplanation:
    """
    Tính giá trị Shapley cộng tính chính xác (Additive SHAP):
    - base_value + sum(shap_values) == total_score (đảm bảo tiên đề Efficiency).
    - Nếu có >= 2 ứng viên: dùng Pool Average làm baseline đối chứng.
    - Nếu chỉ có 1 ứng viên: dùng ngưỡng trung bình tham chiếu 0.50 (50.0 điểm) để biểu diễn đóng góp từng tiêu chí.
    """
    selected = candidates[selected_idx]
    scores = [
        round(sum(w * float(c["utilities"].get(k, 0.0)) for k, _, w in feature_specs), 2)
        for c in candidates
    ]
    total_score = scores[selected_idx]

    pool_size = len(candidates)
    if pool_size >= 2:
        expected_u = {
            k: sum(float(c["utilities"].get(k, 0.0)) for c in candidates) / pool_size
            for k, _, _ in feature_specs
        }
        baseline_ref = f"Pool Average ({pool_size} candidates evaluated)"
    else:
        expected_u = {k: 0.5 for k, _, _ in feature_specs}
        baseline_ref = "Neutral Reference Baseline (single feasible candidate, baseline = 50.00 pts)"

    base_value = round(sum(w * expected_u[k] for k, _, w in feature_specs), 2)

    raw_phis = [
        w * (float(selected["utilities"].get(k, 0.0)) - expected_u[k])
        for k, _, w in feature_specs
    ]
    rounded_phis = [round(p, 2) for p in raw_phis]

    # Điều chỉnh sai số làm tròn <= 0.02 ở tiêu chí cuối để giữ bất biến base_value + sum == total_score
    drift = round(total_score - (base_value + sum(rounded_phis)), 2)
    if rounded_phis and drift != 0.0:
        rounded_phis[-1] = round(rounded_phis[-1] + drift, 2)

    shap_values: dict[str, float] = {}
    feature_contributions: list[ShapFeatureAttribution] = []
    for (key, label, _), phi in zip(feature_specs, rounded_phis):
        shap_values[key] = phi
        direction = "positive" if phi > 0.05 else ("negative" if phi < -0.05 else "neutral")
        feature_contributions.append(
            ShapFeatureAttribution(
                feature=key,
                label=label,
                raw_value=str(selected["raw_values"].get(key, "N/A")),
                shap_contribution=phi,
                direction=direction,
                impact=str(selected["impacts"].get(key, "")),
            )
        )

    other_pairs = [
        (c, sc)
        for idx, (c, sc) in enumerate(zip(candidates, scores))
        if idx != selected_idx
    ]
    other_pairs.sort(key=lambda item: -item[1])
    counterfactuals = [
        counterfactual_builder(cand, round(cand_score - total_score, 2))
        for cand, cand_score in other_pairs[:max_counterfactuals]
    ]

    return DecisionShapExplanation(
        selected_id=str(selected["id"]),
        selected_label=str(selected["label"]),
        total_score=total_score,
        base_value=base_value,
        baseline_reference=baseline_ref,
        shap_values=shap_values,
        feature_contributions=feature_contributions,
        why_selected=why_selected,
        counterfactuals=counterfactuals,
    )


async def _validate_order_and_bundle_for_auto_dispatch(
    db: AsyncSession,
    order_id: str,
    bundled_order_ids: Optional[list[str]],
) -> tuple[Order, list[str], float, float]:
    """Kiểm tra điều kiện hợp lệ của đơn hàng chính và các đơn hàng gộp (nếu có)."""
    clean_order_id = order_id.strip() if order_id else ""
    order = await db.get(Order, clean_order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.payment_status != PaymentStatus.PAID:
        raise HTTPException(status_code=400, detail="Order has not been paid")

    if order.status != OrderStatus.AT_HUB:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Order is currently in '{order.status.value}' status. "
                "Auto-dispatch can only create a mission once a Technician confirms package receipt at the hub (AT_HUB)."
            ),
        )

    existing_active = await db.execute(
        select(Mission.id).where(
            Mission.order_id == order.id,
            Mission.status.in_(ACTIVE_MISSION_STATUSES),
        ).limit(1)
    )
    if existing_active.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="This order already has a scheduled or active mission",
        )

    unconfirmed_inc = await db.execute(
        select(Mission.id).where(
            Mission.order_id == order.id,
            Mission.status.in_([MissionStatus.INCIDENT_RETURN, MissionStatus.FAILED]),
            or_(
                Mission.arrival_confirmed_at.is_(None),
                Mission.handling_status.is_not(None),
            ),
        ).limit(1)
    )
    if unconfirmed_inc.scalars().first():
        raise HTTPException(
            status_code=400,
            detail=(
                "A previous flight for this order encountered an incident or disconnection, and a Technician "
                "has not yet confirmed drone/package recovery at a hub. Please wait for recovery confirmation before auto-dispatching."
            ),
        )

    clean_bundle_ids = list(
        dict.fromkeys(
            str(oid).strip()
            for oid in (bundled_order_ids or [])
            if str(oid).strip() and str(oid).strip() != clean_order_id
        )
    )
    total_payload_kg = float(order.payload_kg or 0.0)
    total_fee = float(order.delivery_fee or 0.0)

    if clean_bundle_ids:
        val_res = await validate_bundle_orders(db, [clean_order_id] + clean_bundle_ids, drone_id=None)
        if not val_res["is_valid"]:
            raise HTTPException(status_code=400, detail=val_res["message"])
        total_payload_kg = float(val_res["total_payload_kg"])
        total_fee = float(val_res["total_delivery_fee"])

    return order, clean_bundle_ids, round(total_payload_kg, 2), round(total_fee, 2)


def _compute_battery_wh_local(battery: Battery, fallback_cap_wh: float = 130.0) -> tuple[float, int, float]:
    """Tính toán (capacity_wh, charge_level_pct, available_energy_wh) thực tế từ DB."""
    cap = float(battery.capacity_wh if battery.capacity_wh is not None else fallback_cap_wh)
    pct = int(battery.charge_level_pct if battery.charge_level_pct is not None else 0)
    return cap, pct, round(cap * (pct / 100.0), 2)


def _score_battery_candidates_and_explain(
    compatible_bats: list[Battery],
    drone: Drone,
    req_leg1_wh: float,
) -> tuple[Battery, DecisionShapExplanation]:
    """Chấm điểm toàn bộ pin tương thích tại Hub xuất phát của Drone và sinh báo cáo SHAP."""
    feature_specs = [
        ("energy_reserve_margin", "Energy Safety Margin (20% Buffer)", 40.0),
        ("state_of_charge", "Current State of Charge (%)", 25.0),
        ("mounting_efficiency", "Airframe Pre-Mount Readiness", 20.0),
        ("battery_lifecycle_health", "Battery Lifecycle Health", 15.0),
    ]

    bat_candidates: list[dict[str, Any]] = []
    for bat in compatible_bats:
        cap_wh, charge_pct, avail_wh = _compute_battery_wh_local(bat)
        safe_usable_wh = avail_wh * 0.80
        meets_reserve = safe_usable_wh >= req_leg1_wh and charge_pct > 0
        is_installed = bat.drone_id == drone.id
        lifecycle_status, days_left = compute_battery_lifecycle(bat)
        is_expired = str(getattr(lifecycle_status, "value", lifecycle_status)) == "Expired"

        if meets_reserve:
            headroom_ratio = (avail_wh - req_leg1_wh) / max(avail_wh, 1.0)
            u_margin = min(1.0, 0.55 + 0.45 * headroom_ratio)
            margin_pct = round(headroom_ratio * 100.0, 1)
            margin_impact = (
                f"Provides {avail_wh:.1f} Wh ({safe_usable_wh:.1f} Wh within 80% safe cap) vs "
                f"{req_leg1_wh:.1f} Wh required ({margin_pct:.1f}% total reserve)"
            )
        else:
            deficit_ratio = avail_wh / max(req_leg1_wh / 0.80, 1.0)
            u_margin = max(0.0, min(0.45, 0.45 * deficit_ratio))
            margin_impact = (
                f"Insufficient 20% safety buffer: 80% safe energy ({safe_usable_wh:.1f} Wh) < {req_leg1_wh:.1f} Wh required"
            )

        u_soc = max(0.0, min(1.0, charge_pct / 100.0))
        soc_impact = (
            "Fully charged (100% SoC) and immediately cleared for takeoff"
            if charge_pct >= 100
            else f"Partially charged ({charge_pct}% SoC)"
        )

        u_mount = 1.0 if is_installed else 0.45
        mount_raw = f"Installed on {drone.id}" if is_installed else f"Rack spare at Hub #{bat.current_hub_id}"
        mount_impact = (
            f"Already mounted on {drone.name} — eliminates manual battery swap before takeoff"
            if is_installed
            else "Available in hub storage rack — requires technician mounting before takeoff"
        )

        u_health = 0.0 if is_expired else round(0.5 + 0.5 * min(1.0, max(0, days_left) / 730.0), 4)
        health_raw = "Expired" if is_expired else f"Active ({days_left} days remaining)"
        health_impact = (
            "Battery exceeded lifecycle warranty threshold"
            if is_expired
            else f"Healthy lifecycle standing with {days_left} operational days remaining"
        )

        # Ưu tiên tuyệt đối cho pin đạt biên dự phòng >=20% và chưa hết hạn
        hard_priority = (
            1 if meets_reserve else 0,
            1 if charge_pct >= 100 else 0,
            1 if not is_expired else 0,
        )
        weighted_score = (
            40.0 * u_margin + 25.0 * u_soc + 20.0 * u_mount + 15.0 * u_health
        )

        bat_candidates.append({
            "battery": bat,
            "id": bat.id,
            "label": f"{bat.id} ({bat.serial_number})",
            "meets_reserve": meets_reserve,
            "hard_priority": hard_priority,
            "weighted_score": round(weighted_score, 2),
            "utilities": {
                "energy_reserve_margin": u_margin,
                "state_of_charge": u_soc,
                "mounting_efficiency": u_mount,
                "battery_lifecycle_health": u_health,
            },
            "raw_values": {
                "energy_reserve_margin": f"{avail_wh:.1f} / {cap_wh:.0f} Wh (need <= {safe_usable_wh:.1f} Wh safe)",
                "state_of_charge": f"{charge_pct}%",
                "mounting_efficiency": mount_raw,
                "battery_lifecycle_health": health_raw,
            },
            "impacts": {
                "energy_reserve_margin": margin_impact,
                "state_of_charge": soc_impact,
                "mounting_efficiency": mount_impact,
                "battery_lifecycle_health": health_impact,
            },
            "avail_wh": avail_wh,
            "charge_pct": charge_pct,
        })

    best_idx = max(
        range(len(bat_candidates)),
        key=lambda i: (bat_candidates[i]["hard_priority"], bat_candidates[i]["weighted_score"], -i),
    )
    chosen = bat_candidates[best_idx]
    chosen_bat: Battery = chosen["battery"]

    why_battery = (
        f"Selected battery {chosen_bat.id} ({chosen_bat.serial_number}) at {chosen['charge_pct']}% SoC "
        f"({chosen['avail_wh']:.1f} Wh usable vs {req_leg1_wh:.1f} Wh Leg-1 demand), "
        + ("already pre-mounted on the assigned drone " if chosen_bat.drone_id == drone.id else "ready in the departure hub rack ")
        + f"with total utility score {chosen['weighted_score']:.2f}/100."
    )

    bat_shap = _compute_additive_shap(
        candidates=bat_candidates,
        selected_idx=best_idx,
        feature_specs=feature_specs,
        why_selected=why_battery,
        counterfactual_builder=lambda c, delta: {
            "candidate_id": c["id"],
            "candidate_label": c["label"],
            "score": c["weighted_score"],
            "score_delta_vs_selected": delta,
            "meets_safety_reserve": c["meets_reserve"],
            "comparison_note": (
                f"Scored {abs(delta):.2f} pts lower "
                f"({c['raw_values']['state_of_charge']} SoC, {c['raw_values']['mounting_efficiency']})."
            ),
        },
    )
    return chosen_bat, bat_shap


def _score_and_explain_routes(
    routes_schemas: list[RouteOptionSchema],
    selected_route: RouteOptionSchema,
    optimization_objective: str,
) -> DecisionShapExplanation:
    """Giải thích XAI SHAP cho quyết định lựa chọn tuyến bay tối ưu so với các phương án còn lại."""
    feature_specs = [
        ("safety_and_weather_margin", "Weather Safety & Reserve Margin", 35.0),
        ("energy_efficiency", "Predicted Energy Efficiency", 25.0),
        ("sla_time_alignment", f"Delivery Mode Fit ({optimization_objective})", 25.0),
        ("operational_simplicity", "Direct vs Relay Simplicity", 15.0),
    ]

    feasible_routes = [r for r in routes_schemas if r.feasible]
    min_energy = min((r.predictedEnergyWh for r in feasible_routes if r.predictedEnergyWh > 0), default=1.0)
    min_duration = min((r.predictedDurationMin for r in feasible_routes if r.predictedDurationMin > 0), default=1)

    route_candidates: list[dict[str, Any]] = []
    selected_idx = 0
    for idx, r in enumerate(routes_schemas):
        if r.routeId == selected_route.routeId:
            selected_idx = idx

        is_bad_w = bool(r.weather and r.weather.is_bad_weather)
        num_legs = len(r.legs_detail) if r.legs_detail else 1
        speed_mps = float(r.flight_speed_mps or 10.0)

        if not r.feasible:
            u_safety = 0.0
            safety_impact = f"Disqualified: {r.infeasible_reason or 'Insufficient energy reserve (<20%)'}"
        elif is_bad_w:
            u_safety = 0.25
            w_warn = (r.weather.warning_message if r.weather else None) or "adverse weather conditions"
            safety_impact = f"Weather penalty applied: {w_warn}"
        else:
            u_safety = min(1.0, 0.65 + 0.35 * max(0.0, float(r.remainingBatteryPct or 0) / 100.0))
            safety_impact = (
                f"Cleared weather thresholds with {r.remainingBatteryPct}% remaining battery reserve"
            )

        u_energy = (
            min(1.0, max(0.15, min_energy / max(float(r.predictedEnergyWh or 1.0), 0.1)))
            if r.feasible else 0.1
        )
        energy_impact = f"Estimated consumption {r.predictedEnergyWh:.1f} Wh over {r.distanceKm:.2f} km"

        if optimization_objective == "FAST":
            u_sla = min(1.0, max(0.2, min_duration / max(int(r.predictedDurationMin or 1), 1)))
            sla_impact = f"Speed {speed_mps:.0f} m/s completes trip in {r.predictedDurationMin} min (FAST objective)"
        else:
            if speed_mps <= 10.5:
                u_sla = 1.0
                sla_impact = f"Cruise speed {speed_mps:.0f} m/s avoids v^2 aerodynamic drag penalty (ECO objective)"
            else:
                u_sla = 0.5
                sla_impact = f"High-speed {speed_mps:.0f} m/s profile incurs aerodynamic drag penalty under ECO mode"

        if num_legs <= 1 and not r.is_cross_hub:
            u_simple = 1.0
            simple_raw = "Direct flight (1 leg)"
            simple_impact = "Non-stop direct corridor avoids intermediate landing and battery swap overhead"
        elif num_legs == 2 or r.is_cross_hub:
            u_simple = 0.65
            simple_raw = f"{num_legs} legs" + (" (Cross-Hub)" if r.is_cross_hub else " (1 Relay Stop)")
            simple_impact = "Includes intermediate positioning or hub battery swap step"
        else:
            u_simple = 0.45
            simple_raw = f"{num_legs} legs (Multi-Relay)"
            simple_impact = "Multi-stop relay trajectory adds turnaround latency"

        w_summary = r.weather_summary or ("Weather alert" if is_bad_w else "Safe weather")
        route_candidates.append({
            "route": r,
            "id": r.routeId,
            "label": f"{r.routeId}: {r.route_string}",
            "utilities": {
                "safety_and_weather_margin": u_safety,
                "energy_efficiency": u_energy,
                "sla_time_alignment": u_sla,
                "operational_simplicity": u_simple,
            },
            "raw_values": {
                "safety_and_weather_margin": f"Feasible={r.feasible}, Reserve={r.remainingBatteryPct}%, {w_summary}",
                "energy_efficiency": f"{r.predictedEnergyWh:.1f} Wh ({r.distanceKm:.2f} km)",
                "sla_time_alignment": f"{r.predictedDurationMin} min @ {speed_mps:.0f} m/s ({int(r.flight_altitude_m or 45)}m)",
                "operational_simplicity": simple_raw,
            },
            "impacts": {
                "safety_and_weather_margin": safety_impact,
                "energy_efficiency": energy_impact,
                "sla_time_alignment": sla_impact,
                "operational_simplicity": simple_impact,
            },
        })

    why_route = selected_route.reason or (
        f"Selected feasible route {selected_route.routeId} ({selected_route.route_string}) "
        f"aligned with {optimization_objective} priority."
    )

    return _compute_additive_shap(
        candidates=route_candidates,
        selected_idx=selected_idx,
        feature_specs=feature_specs,
        why_selected=why_route,
        counterfactual_builder=lambda c, delta: {
            "candidate_id": c["id"],
            "candidate_label": c["label"],
            "score": round(
                35.0 * c["utilities"]["safety_and_weather_margin"]
                + 25.0 * c["utilities"]["energy_efficiency"]
                + 25.0 * c["utilities"]["sla_time_alignment"]
                + 15.0 * c["utilities"]["operational_simplicity"],
                2,
            ),
            "score_delta_vs_selected": delta,
            "feasible": c["route"].feasible,
            "comparison_note": (
                c["route"].infeasible_reason
                if not c["route"].feasible and c["route"].infeasible_reason
                else (
                    f"Estimated {c['route'].predictedEnergyWh:.1f} Wh, {c['route'].predictedDurationMin} min "
                    f"(reserve {c['route'].remainingBatteryPct}%)."
                )
            ),
        },
    )


async def run_auto_dispatch_selection(
    db: AsyncSession,
    order_id: str,
    bundled_order_ids: Optional[list[str]],
    recommend_mode: Optional[str],
    compute_routes_fn: Callable[..., Awaitable[tuple[list[RouteOptionSchema], list[dict]]]],
    get_reserved_batteries_fn: Callable[..., Awaitable[tuple[set[str], dict[int, int]]]],
) -> dict[str, Any]:
    """
    Thực hiện chọn lựa tối ưu hoàn chỉnh (Drone -> Battery -> Route) và tạo báo cáo XAI SHAP.
    Trả về dict gồm toàn bộ dữ liệu để tạo Mission (nếu dry_run=False) và AutoDispatchResponse.
    """
    order, clean_bundle_ids, total_payload_kg, total_fee = await _validate_order_and_bundle_for_auto_dispatch(
        db=db,
        order_id=order_id,
        bundled_order_ids=bundled_order_ids,
    )

    eff_origin_hub_id, is_replan = await resolve_effective_origin_hub_id(db, order)
    if not eff_origin_hub_id or not order.destination_hub_id:
        raise HTTPException(status_code=400, detail="Order is missing Origin Hub or Destination Hub information")

    origin_hub = await db.get(Hub, eff_origin_hub_id)
    dest_hub = await db.get(Hub, order.destination_hub_id)
    if not origin_hub or not dest_hub:
        raise HTTPException(status_code=400, detail="Origin Hub or Destination Hub does not exist in the system")

    req_mode = (recommend_mode or "AUTO").strip().upper()
    order_mode_raw = (
        order.delivery_mode.value
        if hasattr(order.delivery_mode, "value")
        else str(order.delivery_mode or "NORMAL")
    ).strip().upper()
    optimization_objective = (
        req_mode if req_mode in ("ECO", "FAST")
        else ("FAST" if order_mode_raw == DeliveryMode.EXPRESS.value else "ECO")
    )

    # Tải phi đội Drone đang AVAILABLE & không bận, cùng danh sách Pin rảnh
    hubs_res = await db.execute(select(Hub).where(func.lower(Hub.status) == "active"))
    active_hubs = list(hubs_res.scalars().all()) or list((await db.execute(select(Hub))).scalars().all())
    hub_dict = {h.id: h for h in active_hubs}
    hub_dict.setdefault(origin_hub.id, origin_hub)
    hub_dict.setdefault(dest_hub.id, dest_hub)

    busy_drones_res = await db.execute(
        select(Mission.drone_id).where(
            Mission.drone_id.is_not(None),
            Mission.order_id != order.id,
            Mission.status.in_(ACTIVE_MISSION_STATUSES),
        )
    )
    busy_drone_ids = {r[0] for r in busy_drones_res.all() if r[0] is not None}

    drones_res = await db.execute(
        select(Drone).where(
            and_(
                Drone.status == DroneStatus.AVAILABLE,
                Drone.payload_capacity_kg >= total_payload_kg,
            )
        ).order_by(Drone.id)
    )
    candidate_drones = [d for d in drones_res.scalars().all() if d.id not in busy_drone_ids]
    if not candidate_drones:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Auto-dispatch failed: No available idle drone in the fleet can carry the payload of {total_payload_kg:.2f} kg."
            ),
        )

    all_active_bats = list(
        (await db.execute(select(Battery).where(Battery.status == batteries_status.ACTIVE).order_by(Battery.id))).scalars().all()
    )
    reserved_bat_ids, _ = await get_reserved_batteries_fn(
        db=db,
        relay_hub_ids=list(hub_dict.keys()),
        compatible_models=[],
        exclude_order_id=order.id,
    )

    local_drones = [d for d in candidate_drones if d.current_hub_id == eff_origin_hub_id]
    other_drones = [d for d in candidate_drones if d.current_hub_id != eff_origin_hub_id]
    other_drones.sort(
        key=lambda d: haversine_distance(
            (hub_dict.get(d.current_hub_id).latitude if hub_dict.get(d.current_hub_id) else 0.0) or 0.0,
            (hub_dict.get(d.current_hub_id).longitude if hub_dict.get(d.current_hub_id) else 0.0) or 0.0,
            origin_hub.latitude or 0.0,
            origin_hub.longitude or 0.0,
        )
    )

    compat_cache: dict[str, list[str]] = {}

    async def _get_drone_elig_bats(d: Drone) -> list[Battery]:
        d_model_key = d.model or ""
        if d_model_key not in compat_cache:
            compat_cache[d_model_key] = await get_compatible_battery_models(db, d.model)
        compat_models = compat_cache[d_model_key]
        d_hub_id = d.current_hub_id or eff_origin_hub_id
        return [
            b for b in all_active_bats
            if b.id not in reserved_bat_ids
            and (b.charge_level_pct or 0) > 0
            and (b.drone_id == d.id or (b.drone_id is None and b.current_hub_id == d_hub_id))
            and (not compat_models or (b.battery_model and b.battery_model in compat_models))
        ]

    async def _evaluate_drone(d: Drone, preloaded_bats: Optional[list[Battery]] = None) -> Optional[dict[str, Any]]:
        elig_bats = preloaded_bats if preloaded_bats is not None else await _get_drone_elig_bats(d)
        is_local = d.current_hub_id == eff_origin_hub_id
        d_hub = hub_dict.get(d.current_hub_id)
        deadhead_km = (
            0.0 if is_local or not d_hub
            else round(
                haversine_distance(
                    d_hub.latitude or 0.0, d_hub.longitude or 0.0,
                    origin_hub.latitude or 0.0, origin_hub.longitude or 0.0,
                ),
                2,
            )
        )

        # Gọi thuật toán lập tuyến thực tế (bao gồm thời tiết Open-Meteo & Relay/Safety reserve)
        routes_schemas, raw_list = await compute_routes_fn(
            db=db,
            order=order,
            drone_id=d.id,
            battery_id=None,
            strict_battery=False,
            override_payload_kg=total_payload_kg,
            recommend_mode=recommend_mode or "AUTO",
        )
        feasible_pairs = [(s, raw) for s, raw in zip(routes_schemas, raw_list) if s.feasible]
        chosen_pair = (
            next(((s, raw) for s, raw in feasible_pairs if s.recommended), None)
            or (feasible_pairs[0] if feasible_pairs else None)
        )
        is_feasible = chosen_pair is not None and bool(elig_bats)
        rec_route = chosen_pair[0] if chosen_pair else (routes_schemas[0] if routes_schemas else None)
        rec_raw = chosen_pair[1] if chosen_pair else (raw_list[0] if raw_list else {})

        cap_kg = float(d.payload_capacity_kg or 0.0)
        fit_ratio = (total_payload_kg / cap_kg) if (cap_kg > 0 and total_payload_kg > 0) else 1.0
        util_pct = float(d.utilization_pct or 0.0)

        u_locality = 1.0 if is_local else max(0.1, 0.55 - min(deadhead_km, 50.0) / 100.0)
        u_payload = min(1.0, 0.35 + 0.65 * fit_ratio) if total_payload_kg > 0 else 1.0
        u_util = max(0.0, 1.0 - min(1.0, util_pct / 100.0))

        if is_feasible and rec_route:
            has_installed_ready = any(b.drone_id == d.id and (b.charge_level_pct or 0) >= 100 for b in elig_bats)
            reserve_frac = max(0.0, min(1.0, float(rec_route.remainingBatteryPct or 0) / 100.0))
            u_readiness = min(1.0, 0.55 + 0.35 * reserve_frac + (0.10 if has_installed_ready else 0.0))
            readiness_raw = f"Feasible ({rec_route.remainingBatteryPct}% reserve, {len(elig_bats)} ready batteries)"
            readiness_impact = (
                f"Clears route energy & safety checks with {rec_route.remainingBatteryPct}% final reserve"
            )
        else:
            u_readiness = 0.0
            infeas_msg = (rec_route.infeasible_reason if rec_route else None) or "No compatible charged battery available"
            readiness_raw = f"Infeasible ({len(elig_bats)} compatible batteries)"
            readiness_impact = f"Disqualified: {infeas_msg}"

        weighted_score = round(
            35.0 * u_locality + 25.0 * u_payload + 25.0 * u_readiness + 15.0 * u_util,
            2,
        )
        return {
            "drone": d,
            "id": d.id,
            "label": f"{d.id} ({d.name} - {d.model})",
            "is_local": is_local,
            "is_feasible": is_feasible,
            "weighted_score": weighted_score,
            "elig_bats": elig_bats,
            "routes_schemas": routes_schemas,
            "raw_list": raw_list,
            "rec_route": rec_route,
            "rec_raw": rec_raw,
            "infeasible_reason": rec_route.infeasible_reason if rec_route else "No compatible route/battery",
            "utilities": {
                "hub_locality": u_locality,
                "payload_fit": u_payload,
                "energy_readiness": u_readiness,
                "fleet_utilization_balance": u_util,
            },
            "raw_values": {
                "hub_locality": (
                    f"Stationed at Origin Hub #{eff_origin_hub_id} (0.0 km deadhead)"
                    if is_local
                    else f"Cross-Hub #{d.current_hub_id} ({deadhead_km:.2f} km deadhead)"
                ),
                "payload_fit": f"{total_payload_kg:.2f} / {cap_kg:.2f} kg ({fit_ratio * 100:.0f}% utilization)",
                "energy_readiness": readiness_raw,
                "fleet_utilization_balance": f"{util_pct:.1f}% fleet wear utilization",
            },
            "impacts": {
                "hub_locality": (
                    "Zero empty deadhead flight required — stationed directly at effective pickup hub"
                    if is_local
                    else f"Requires {deadhead_km:.2f} km empty positioning flight from Hub #{d.current_hub_id}"
                ),
                "payload_fit": (
                    f"Best-fit airframe sizing for {total_payload_kg:.2f} kg payload ({fit_ratio * 100:.0f}% load ratio)"
                ),
                "energy_readiness": readiness_impact,
                "fleet_utilization_balance": f"Balances fleet wear (current utilization {util_pct:.1f}%)",
            },
        }

    # Sắp xếp trước ứng viên local có pin sẵn sàng & phù hợp tải trọng nhất (giảm roundtrip DB thừa khi hub nhiều drone)
    local_scored: list[tuple[tuple, Drone, list[Battery]]] = []
    for d in local_drones:
        ebats = await _get_drone_elig_bats(d)
        cap_kg = float(d.payload_capacity_kg or 0.0)
        fit_ratio = (total_payload_kg / cap_kg) if (cap_kg > 0 and total_payload_kg > 0) else 1.0
        has_100 = any((b.charge_level_pct or 0) >= 100 for b in ebats)
        has_inst = any(b.drone_id == d.id for b in ebats)
        max_wh = max((_compute_battery_wh_local(b)[2] for b in ebats), default=0.0)
        local_scored.append((
            (1 if ebats else 0, 1 if has_100 else 0, 1 if has_inst else 0, round(fit_ratio, 3), max_wh, -float(d.utilization_pct or 0.0)),
            d,
            ebats,
        ))
    local_scored.sort(key=lambda item: item[0], reverse=True)

    evaluated_drones: list[dict[str, Any]] = []
    ready_local = [(d, bats) for _, d, bats in local_scored if bats]
    for d, bats in ready_local[:5]:
        ev = await _evaluate_drone(d, preloaded_bats=bats)
        if ev:
            evaluated_drones.append(ev)

    feasible_drones = [e for e in evaluated_drones if e["is_feasible"]]
    if not feasible_drones and len(ready_local) > 5:
        for d, bats in ready_local[5:]:
            ev = await _evaluate_drone(d, preloaded_bats=bats)
            if ev:
                evaluated_drones.append(ev)
        feasible_drones = [e for e in evaluated_drones if e["is_feasible"]]

    if not feasible_drones:
        cross_checked = 0
        for od in other_drones:
            obats = await _get_drone_elig_bats(od)
            if not obats:
                continue
            ev = await _evaluate_drone(od, preloaded_bats=obats)
            if ev:
                evaluated_drones.append(ev)
            cross_checked += 1
            if cross_checked >= 3:
                break
        feasible_drones = [e for e in evaluated_drones if e["is_feasible"]]

    if not feasible_drones:
        if not evaluated_drones:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Auto-dispatch failed: Found {len(candidate_drones)} idle drone(s) matching the payload, "
                    "but none currently have an unreserved compatible active battery (>0% charge) at their hub."
                ),
            )
        sample_reason = next(
            (e["infeasible_reason"] for e in evaluated_drones if e.get("infeasible_reason")),
            "No compatible battery or feasible route met the 20% safety reserve / weather limit.",
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Auto-dispatch failed: Evaluated {len(evaluated_drones)} eligible drone(s), "
                f"but none produced a feasible route. Reason: {sample_reason}"
            ),
        )

    best_drone_idx = max(
        range(len(evaluated_drones)),
        key=lambda i: (
            1 if evaluated_drones[i]["is_feasible"] else 0,
            1 if evaluated_drones[i]["is_local"] else 0,
            evaluated_drones[i]["weighted_score"],
            -i,
        ),
    )
    chosen_d_eval = evaluated_drones[best_drone_idx]
    best_drone: Drone = chosen_d_eval["drone"]
    best_route: RouteOptionSchema = chosen_d_eval["rec_route"]
    available_routes: list[RouteOptionSchema] = chosen_d_eval["routes_schemas"]
    raw_list: list[dict] = chosen_d_eval["raw_list"]

    # Chấm điểm SHAP cho Pin tương thích của Drone được chọn và đồng bộ lại tuyến nếu pin tối ưu khác với pin mặc định của bước lọc
    req_leg1_wh = (
        float(best_route.legs_detail[0].energy_wh)
        if best_route.legs_detail
        else float(best_route.predictedEnergyWh or 0.0)
    )
    best_battery, battery_shap = _score_battery_candidates_and_explain(
        compatible_bats=chosen_d_eval["elig_bats"],
        drone=best_drone,
        req_leg1_wh=req_leg1_wh,
    )

    if best_route.assignedBatteryId != best_battery.id:
        recheck_schemas, recheck_raw = await compute_routes_fn(
            db=db,
            order=order,
            drone_id=best_drone.id,
            battery_id=best_battery.id,
            strict_battery=True,
            override_payload_kg=total_payload_kg,
            recommend_mode=recommend_mode or "AUTO",
        )
        recheck_feasible = [s for s in recheck_schemas if s.feasible]
        if recheck_feasible:
            available_routes = recheck_schemas
            raw_list = recheck_raw
            best_route = next((s for s in recheck_feasible if s.recommended), recheck_feasible[0])

    # Xây dựng SHAP giải thích cho lựa chọn Drone
    drone_feature_specs = [
        ("hub_locality", "Station Proximity (Deadhead Avoidance)", 35.0),
        ("payload_fit", "Payload Capacity Fit", 25.0),
        ("energy_readiness", "Energy & Route Feasibility", 25.0),
        ("fleet_utilization_balance", "Fleet Wear Balancing", 15.0),
    ]
    why_drone = (
        f"Selected Drone {best_drone.id} ({best_drone.name}, capacity {best_drone.payload_capacity_kg:.1f} kg) "
        + ("at pickup hub " if chosen_d_eval["is_local"] else f"via Cross-Hub Dispatch from Hub #{best_drone.current_hub_id} ")
        + f"with utility score {chosen_d_eval['weighted_score']:.2f}/100."
    )
    drone_shap = _compute_additive_shap(
        candidates=evaluated_drones,
        selected_idx=best_drone_idx,
        feature_specs=drone_feature_specs,
        why_selected=why_drone,
        counterfactual_builder=lambda c, delta: {
            "candidate_id": c["id"],
            "candidate_label": c["label"],
            "score": c["weighted_score"],
            "score_delta_vs_selected": delta,
            "feasible": c["is_feasible"],
            "comparison_note": (
                f"Infeasible: {c['infeasible_reason']}"
                if not c["is_feasible"]
                else (
                    f"Scored {abs(delta):.2f} pts lower "
                    f"({c['raw_values']['payload_fit']}, {c['raw_values']['hub_locality']})."
                )
            ),
        },
    )

    # Giải thích SHAP cho tuyến đường tối ưu + Physics Energy SHAP
    route_shap = _score_and_explain_routes(
        routes_schemas=available_routes,
        selected_route=best_route,
        optimization_objective=optimization_objective,
    )

    relay_swaps = [
        {
            "hub_id": st.hub_id,
            "hub_code": st.hub_code,
            "hub_name": st.hub_name,
            "battery_id": st.selected_battery_id,
            "battery_serial": st.selected_battery_serial,
            "battery_charge_pct": st.selected_battery_charge_pct,
            "battery_energy_wh": st.selected_battery_energy_wh,
        }
        for st in (best_route.relay_battery_status or [])
        if st.selected_battery_id
    ]

    exec_summary = (
        f"Auto-dispatch assigned Drone {best_drone.id} ({best_drone.name}) with Battery {best_battery.id} "
        f"({best_battery.charge_level_pct or 0}% SoC) on Route {best_route.routeId} ({best_route.route_string}: "
        f"{best_route.distanceKm:.2f} km, {best_route.predictedEnergyWh:.1f} Wh, {best_route.predictedDurationMin} min, "
        f"{best_route.remainingBatteryPct}% reserve) under {optimization_objective} priority."
    )

    xai_report = AutoDispatchXAIReport(
        delivery_mode=order_mode_raw,
        optimization_objective=optimization_objective,
        effective_origin_hub_id=eff_origin_hub_id,
        is_replan_dispatch=is_replan,
        is_cross_hub_dispatch=bool(best_route.is_cross_hub),
        drone_decision=drone_shap,
        battery_decision=battery_shap,
        route_decision=route_shap,
        physics_energy_shap=best_route.shap_values,
        physics_energy_explanation=best_route.ai_explanation,
        relay_swap_batteries=relay_swaps,
        executive_summary=exec_summary,
    )

    return {
        "order": order,
        "bundled_order_ids": clean_bundle_ids,
        "total_payload_kg": total_payload_kg,
        "total_fee": total_fee,
        "drone": best_drone,
        "battery": best_battery,
        "selected_route": best_route,
        "available_routes": available_routes,
        "raw_list": raw_list,
        "xai_report": xai_report,
    }
